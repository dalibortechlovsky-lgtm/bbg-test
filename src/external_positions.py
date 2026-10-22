"""Append-only position snapshots from banks outside the UBS accounting book."""
import csv
from datetime import date,datetime,timezone
from decimal import Decimal,InvalidOperation
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import sqlite3


HEADERS=['institution','portfolio','as_of','position_type','description','currency','quantity',
         'market_value_native','accrued_interest_native','identifier']
TYPES={'cash','deposit','security'}
D=Decimal


def _text(value,label,required=True,limit=160):
    value=value.strip()
    if required and not value:raise ValueError(label+' is required')
    if len(value)>limit or any(ord(c)<32 for c in value):raise ValueError('Invalid '+label.lower())
    return value


def _number(value,label,default=None):
    value=value.strip()
    if not value and default is not None:return default
    try:result=D(value)
    except InvalidOperation:raise ValueError('Invalid '+label.lower()) from None
    if not result.is_finite():raise ValueError('Invalid '+label.lower())
    return str(result)


def parse_external_positions(content):
    reader=csv.DictReader(StringIO(content.lstrip('\ufeff')),strict=True)
    if reader.fieldnames!=HEADERS:raise ValueError('Other-bank positions must use the exact template columns')
    rows=[];scope=None;identities=set()
    try:
        for source in reader:
            if None in source or len(rows)>=1000:raise ValueError('Invalid column count or more than 1,000 positions')
            institution=_text(source['institution'],'Institution',limit=64)
            portfolio=_text(source['portfolio'],'Portfolio',limit=100)
            try:as_of=date.fromisoformat(source['as_of'].strip())
            except ValueError:raise ValueError('As of must be YYYY-MM-DD') from None
            if as_of>date.today():raise ValueError('As of cannot be in the future')
            current=(institution,portfolio,as_of.isoformat())
            if scope is None:scope=current
            elif scope!=current:raise ValueError('One file must contain one institution, portfolio and as-of date')
            kind=source['position_type'].strip().lower()
            if kind not in TYPES:raise ValueError('Position type must be cash, deposit or security')
            description=_text(source['description'],'Description')
            currency=source['currency'].strip()
            if not re.fullmatch('[A-Z]{3}',currency):raise ValueError('Currency must be a three-letter uppercase code')
            identifier=_text(source['identifier'],'Identifier',required=False,limit=100)
            identity=(kind,currency,identifier or description)
            if identity in identities:raise ValueError('Duplicate position identity')
            identities.add(identity)
            rows.append({'position_type':kind,'description':description,'currency':currency,
                'quantity':_number(source['quantity'],'Quantity'),
                'market_value_native':_number(source['market_value_native'],'Market value'),
                'accrued_interest_native':_number(source['accrued_interest_native'],'Accrued interest',default='0'),
                'identifier':identifier or None})
    except csv.Error as exc:raise ValueError('Other-bank positions CSV is malformed: '+str(exc)) from None
    if not rows:raise ValueError('Other-bank positions file is empty')
    return {'source':'other_bank_positions_import','institution':scope[0],'portfolio':scope[1],
        'as_of':scope[2],'count':len(rows),'positions':rows,
        'imported_at':datetime.now(timezone.utc).isoformat()}


def template():
    output=StringIO();writer=csv.writer(output,lineterminator='\n');writer.writerow(HEADERS)
    writer.writerow(['LGT','Portfolio reference',date.today().isoformat(),'deposit','USD deposit','USD','1000000','1000000','0',''])
    return output.getvalue()


class ExternalPositionStore:
    def __init__(self,path):self.path=Path(path)

    def import_content(self,content):
        parsed=parse_external_positions(content);digest=hashlib.sha256(content.encode()).hexdigest();parsed['id']=digest
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS external_position_snapshots '
                       '(id TEXT PRIMARY KEY,institution TEXT NOT NULL,portfolio TEXT NOT NULL,as_of TEXT NOT NULL,imported_at TEXT NOT NULL,payload TEXT NOT NULL)')
            cursor=db.execute('INSERT OR IGNORE INTO external_position_snapshots VALUES (?,?,?,?,?,?)',
                (digest,parsed['institution'],parsed['portfolio'],parsed['as_of'],parsed['imported_at'],json.dumps(parsed)))
            parsed['already_imported']=cursor.rowcount==0
        return parsed

    def latest(self):
        if not self.path.exists():return []
        with sqlite3.connect(self.path) as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_position_snapshots'").fetchone()
            if not exists:return []
            payloads=db.execute('SELECT payload FROM external_position_snapshots ORDER BY as_of DESC,imported_at DESC,rowid DESC').fetchall()
        result=[];seen=set()
        for payload, in payloads:
            item=json.loads(payload);key=(item['institution'],item['portfolio'])
            if key not in seen:seen.add(key);result.append(item)
        return result

    def view(self,ubs_snapshot,bloomberg):
        snapshots=self.latest();quotes={}
        for quote in (bloomberg or {}).get('quotes',[]):
            if quote.get('stale'):continue
            if quote['security'] not in quotes or quote['observed_at']>quotes[quote['security']]['observed_at']:
                quotes[quote['security']]=quote
        rows=[];total=D(0);complete=True;dates=set()
        for snapshot in snapshots:
            dates.add(snapshot['as_of'])
            for position in snapshot['positions']:
                currency=position['currency'];rate=None;source=None;rate_as_of=None
                if currency=='CZK':rate=D(1);source='CZK reporting currency';rate_as_of=snapshot['as_of']
                quote=quotes.get(currency+'CZK Curncy')
                if rate is None and quote and quote.get('currency')=='CZK' and D(quote['price'])>0:
                    rate=D(quote['price']);source='Bloomberg recent FX';rate_as_of=quote['observed_at']
                if rate is None and ubs_snapshot:
                    direct=ubs_snapshot.get('exchange_rates',{}).get(currency+'/CZK')
                    inverse=ubs_snapshot.get('exchange_rates',{}).get('CZK/'+currency)
                    if direct and D(direct)>0:rate=D(direct)
                    elif inverse and D(inverse)>0:rate=D(1)/D(inverse)
                    if rate is not None:source='UBS snapshot FX fallback';rate_as_of=ubs_snapshot['as_of']
                native=D(position['market_value_native'])+D(position['accrued_interest_native'])
                value=None if rate is None else native*rate
                if value is None:complete=False
                else:total+=value
                rows.append(position|{'institution':snapshot['institution'],'portfolio':snapshot['portfolio'],
                    'as_of':snapshot['as_of'],'value_native_including_accrued':str(native),
                    'fx_to_czk':None if rate is None else str(rate),'value_czk':None if value is None else str(value),
                    'fx_source':source,'fx_as_of':rate_as_of})
        return {'snapshots':[{k:v for k,v in s.items() if k!='positions'} for s in snapshots],
            'positions':rows,'aum_czk':str(total) if complete else None,'complete':complete,
            'mixed_position_dates':len(dates)>1,'basis':'Latest imported snapshot per institution and portfolio. Values include imported accrued interest. FX uses a recent Bloomberg quote when available, otherwise the latest UBS snapshot rate. Other-bank AUM is excluded from UBS P/L.'}
