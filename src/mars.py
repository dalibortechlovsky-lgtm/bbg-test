"""Bounded static XLSX reader for MARS snapshots; values and units remain reported."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO
import json
import posixpath
import re
import sqlite3
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
HEADERS=['Ticker','Position','Portfolio','Notional','DeltaNotional','Strike','Trade Date','Expiry Date','Delivery Date',
         'P&L DealVl Port Ccy','P&L Port Ccy','MktPx','CostVal Port Ccy','MktVal Port Ccy','MktVal','P&L','CostPx',
         'Delta','Theta','Gamma','Vega','Rho','Underlying','UndFwdPx']
BASE_HEADERS=['Ticker','Position','Portfolio','Notional','Strike','Trade Date','Expiry Date','Delivery Date','MktPx','CostPx','Underlying']
NUMBERS=set(HEADERS)-{'Ticker','Position','Portfolio','Trade Date','Expiry Date','Delivery Date','Underlying'}


def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Unsupported XML declarations')
    return ET.fromstring(data)


def column(ref):
    if not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]*',ref or ''): raise ValueError('Invalid worksheet cell reference')
    result=0
    for c in re.match('[A-Z]+',ref)[0]: result=result*26+ord(c)-64
    if result>100 or int(re.search('[0-9]+',ref)[0])>5000: raise ValueError('Worksheet exceeds supported dimensions')
    return result


def worksheet_rows(content):
    if not isinstance(content,bytes) or not 0<len(content)<=2*1024*1024: raise ValueError('Workbook must be between 1 byte and 2 MiB')
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos=archive.infolist()
            if len(infos)>200 or sum(i.file_size for i in infos)>20*1024*1024 or any(i.file_size>5*1024*1024 or i.flag_bits&1 for i in infos):
                raise ValueError('Workbook exceeds supported archive limits or is encrypted')
            names=[i.filename for i in infos]
            if len(set(names))!=len(names): raise ValueError('Duplicate archive entries')
            if any('vbaproject' in n.lower() or 'externallinks/' in n.lower() for n in names):
                raise ValueError('Export a static XLSX without macros or external links')
            strings=[]
            if 'xl/sharedStrings.xml' in names:
                strings=[''.join(t.text or '' for t in si.findall('.//s:t',NS)) for si in xml(archive.read('xl/sharedStrings.xml'))]
            workbook=xml(archive.read('xl/workbook.xml'))
            if workbook.find('s:workbookPr',NS) is not None and workbook.find('s:workbookPr',NS).get('date1904') in {'1','true'}:
                raise ValueError('1904 workbook dates are unsupported; use a standard static export')
            rels=xml(archive.read('xl/_rels/workbook.xml.rels'))
            targets={r.get('Id'):r.get('Target') for r in rels if r.get('TargetMode')!='External'}
            found=[]
            for sheet in workbook.findall('s:sheets/s:sheet',NS):
                target=targets.get(sheet.get('{'+NS['r']+'}id'))
                if not target: continue
                path=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
                if not path.startswith('xl/worksheets/') or path not in names: raise ValueError('Invalid worksheet location')
                root=xml(archive.read(path)); rows=[]; cells=0; references=set()
                for row in root.findall('s:sheetData/s:row',NS):
                    values={}
                    for cell in row.findall('s:c',NS):
                        cells+=1
                        if cells>20000: raise ValueError('Too many cells')
                        col=column(cell.get('r'))
                        if cell.get('r') in references: raise ValueError('Duplicate cell reference')
                        references.add(cell.get('r'))
                        if col in values: raise ValueError('Duplicate worksheet cell')
                        if cell.find('s:f',NS) is not None: raise ValueError('Formula cells are unsupported; export static values')
                        typ=cell.get('t'); value=cell.find('s:v',NS)
                        text=value.text or '' if value is not None else ''
                        if typ=='s':
                            if not text.isdigit() or int(text)>=len(strings): raise ValueError('Invalid shared string')
                            text=strings[int(text)]
                        elif typ=='inlineStr': text=''.join(t.text or '' for t in cell.findall('.//s:t',NS))
                        elif typ in {'e','b'}:
                            header=HEADERS[col-2] if 2<=col<=25 else None
                            if header not in BASE_HEADERS and header is not None:
                                text=''  # Errors in excluded MARS calculations are irrelevant to base data.
                            else:
                                raise ValueError('A base-data cell contains an unsupported error or boolean value')
                        if len(text)>1024 or any(ord(c)<32 and c not in '\t\n\r' for c in text): raise ValueError('Invalid cell text')
                        values[col]=(text.strip(),typ)
                    if any(v[0] for v in values.values()): rows.append(values)
                for i,row in enumerate(rows):
                    labels=[row.get(c,('',None))[0] for c in range(2,26)]
                    if labels==HEADERS:
                        found.append((sheet.get('name',''),rows[i+1:]))
                        break
            if len(found)!=1: raise ValueError('Expected exactly one worksheet with the MARS export columns')
            return found[0]
    except (BadZipFile,ET.ParseError,KeyError,IndexError,RuntimeError,OverflowError) as exc:
        raise ValueError('Invalid or unsupported static XLSX workbook') from exc


def numeric(value):
    if not value: return None
    try: number=Decimal(value)
    except InvalidOperation: raise ValueError('MARS numeric cell contains text') from None
    if not number.is_finite() or number.copy_abs()>Decimal('1e30') or number.as_tuple().exponent < -18:
        raise ValueError('MARS number exceeds supported precision/range')
    return str(number)


def source_date(value,typ):
    if not value: return None
    try:
        if typ in {None,'n'}:
            serial=Decimal(value)
            if serial!=serial.to_integral_value() or not 36526<=serial<=73415: raise ValueError('Unsupported Excel date')
            return (date(1899,12,30)+timedelta(days=int(serial))).isoformat()
        for fmt in ['%m/%d/%y','%m/%d/%Y','%Y-%m-%d']:
            try: return datetime.strptime(value,fmt).date().isoformat()
            except ValueError: pass
    except (InvalidOperation,OverflowError): pass
    raise ValueError('Unsupported MARS date cell')


def parse_mars(content,metadata=None):
    metadata={} if metadata is None else metadata
    if not isinstance(metadata,dict) or set(metadata)-{'as_of','portfolio_currency','pnl_period'}: raise ValueError('Invalid MARS metadata fields')
    as_of=metadata.get('as_of') or None
    if as_of and (not isinstance(as_of,str) or date.fromisoformat(as_of).isoformat()!=as_of): raise ValueError('Valuation date must be YYYY-MM-DD')
    currency=metadata.get('portfolio_currency') or None
    if currency and (not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency)): raise ValueError('Invalid portfolio currency')
    period=metadata.get('pnl_period') or None
    if period and (not isinstance(period,str) or len(period)>80 or any(ord(c)<32 for c in period)): raise ValueError('Invalid P/L period')
    sheet,source=worksheet_rows(content)
    instruments=[]; groups=[]; totals=None; current=None; identities=set(); portfolios=set()
    for row in source:
        if any(v[0] for c,v in row.items() if c>25): raise ValueError('Unexpected extra MARS columns')
        label=row.get(1,('',None))[0]
        fields={h:row.get(c,('',None))[0] or None for c,h in enumerate(HEADERS,2)}
        for c,h in enumerate(HEADERS,2):
            if h not in BASE_HEADERS: fields[h]=None
            elif h in NUMBERS: fields[h]=numeric(fields[h])
            elif h in {'Trade Date','Expiry Date','Delivery Date'}: fields[h]=source_date(fields[h],row.get(c,('',None))[1])
        if not fields['Ticker']:
            if not label: raise ValueError('Unlabeled summary row')
            if any(fields[h] for h in ['Position','Portfolio','Underlying','Trade Date','Expiry Date','Delivery Date']): raise ValueError('Ambiguous non-instrument row')
            summary={'label':label,'fields':fields}
            if label=='Portfolio Totals':
                if totals is not None: raise ValueError('Duplicate portfolio totals')
                totals=summary
            else:
                if any(g['label']==label for g in groups): raise ValueError('Duplicate MARS group heading')
                groups.append(summary); current=label
            continue
        portfolio=fields['Portfolio']
        if not portfolio or not fields['Position']: raise ValueError('Instrument portfolio and position required')
        portfolios.add(portfolio)
        ticker=' '.join(fields['Ticker'].split())
        if ticker in identities: raise ValueError('Duplicate instrument ticker')
        identities.add(ticker)
        group=current if label.startswith('--') else 'Ungrouped instruments'
        instruments.append({'ticker':ticker,'label':label or ticker,'group':group or 'Ungrouped instruments','fields':fields})
    if not instruments or totals is None or len(portfolios)!=1: raise ValueError('Expected instruments, one portfolio, and portfolio totals')
    # Calculated currency values and Greeks are deliberately excluded by user request.
    for instrument in instruments:
        instrument['fields']={h:instrument['fields'][h] for h in BASE_HEADERS}
    return {'source':'bloomberg_mars_xlsx','sheet':sheet,'portfolio':next(iter(portfolios)),
            'as_of':as_of,'portfolio_currency':currency,'pnl_period':period,
            'metadata_basis':'User supplied; workbook does not identify valuation date/currency/P&L period',
            'calculation_policy':'base_data_only','count':len(instruments),'instruments':instruments,
            'groups':[{'label':g['label']} for g in groups],
            'excluded_columns':[h for h in HEADERS if h not in BASE_HEADERS],
            'warnings':['MARS currency-dependent values, P/L, converted costs and Greeks are excluded from this import.',
                        'Portfolio snapshot, not an execution history or cash-flow ledger; daily/realized P/L cannot be derived from this file alone.',
                        'Position display, notional and quoted prices retain source units. Native currency, price scale and contract multipliers require verification.',
                        'MARS records are separate from UBS holdings and do not update average-cost inventory or modeled risk inputs.',
                        'Workbook modified/created timestamps are not used as valuation timestamps.']}



class MarsStore:
    def __init__(self,path): self.path=path

    def import_content(self,content,metadata=None):
        snapshot=parse_mars(content,metadata)
        digest=hashlib.sha256(content).hexdigest()
        snapshot['id']=hashlib.sha256((digest+'base_data_only_v1'+json.dumps({k:snapshot[k] for k in ['as_of','portfolio_currency','pnl_period']},sort_keys=True)).encode()).hexdigest()
        snapshot['file_digest']=digest
        snapshot['imported_at']=datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS mars_snapshots (id TEXT PRIMARY KEY,as_of TEXT,imported_at TEXT NOT NULL,payload TEXT NOT NULL)')
            cursor=db.execute('INSERT OR IGNORE INTO mars_snapshots VALUES (?,?,?,?)',(snapshot['id'],snapshot['as_of'],snapshot['imported_at'],json.dumps(snapshot)))
            snapshot['already_imported']=cursor.rowcount==0
        return snapshot

    def count(self):
        if not self.path.exists(): return 0
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='mars_snapshots'").fetchone(): return 0
            return db.execute('SELECT count(*) FROM mars_snapshots').fetchone()[0]

    def latest(self):
        if not self.path.exists(): return None
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='mars_snapshots'").fetchone(): return None
            row=db.execute('SELECT payload FROM mars_snapshots ORDER BY coalesce(as_of,\'\') DESC,imported_at DESC,rowid DESC LIMIT 1').fetchone()
            return json.loads(row[0]) if row else None


def enrich_base_data(snapshot,bloomberg):
    """Exact ticker lookup only. Independent quote metadata never adopts MARS FX."""
    if snapshot is None: return None
    quotes={}
    for q in bloomberg.get('quotes',[]):
        ticker=' '.join(q['security'].split())
        previous=quotes.get(ticker)
        if not previous or (not q['stale'],q['observed_at'])>(not previous['stale'],previous['observed_at']):
            quotes[ticker]=q
    return snapshot|{'instruments':[p|{'quote':quotes.get(p['ticker'])} for p in snapshot['instruments']]}
