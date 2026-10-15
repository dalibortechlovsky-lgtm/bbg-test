"""One-time UBS year-end PDF import. Document text is financial data only."""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

NUMBER = r'-?\d+(?: \d{3})*(?:\.\d+)?'
GROUPS = {'Liquidity - Accounts', 'Liquidity - Call deposits', 'Liquidity - Money market investments',
          'Bonds - Bond investments', 'Equities - Equity investments'}


def portfolio_identity(value):
    parts=re.findall(r'\d+',value)
    return tuple(str(int(x)) for x in parts) if parts else (value.strip().upper(),)


def validate_baseline(db,data):
    for row in db.execute('SELECT payload FROM opening_baselines WHERE as_of=?',(data['as_of'],)):
        prior=json.loads(row[0])
        if {k:v for k,v in prior.items() if k!='id'}!={k:v for k,v in data.items() if k!='id'}:
            raise ValueError('Opening statement conflicts with retained year-end data; review the correction')


def n(value):
    d = Decimal(value.replace(' ', ''))
    if not d.is_finite() or abs(d) > Decimal('1e30'): raise ValueError('Invalid statement number')
    return str(d)


def parse_statement_text(text):
    match = re.search(r'Statement of assets\s+As of (\d{1,2} \w+ \d{4})', text)
    if not match: raise ValueError('Expected UBS statement of assets with a valuation date')
    as_of = datetime.strptime(match[1], '%d %B %Y').date().isoformat()
    if not as_of.endswith('-12-31'): raise ValueError('Opening statement must be as of 31 December')
    portfolio = re.search(r'Portfolio (\d+-\d+-\d+), valued in .*?\(([A-Z]{3})\)', text)
    if not portfolio: raise ValueError('Statement portfolio or valuation currency missing')
    base = portfolio[2]
    declared = re.findall(r'^Net assets\s+(' + NUMBER + r')\s+100\.00\s*$', text, re.M)
    if not declared: raise ValueError('Statement net assets total missing')
    fx = {a+'/'+b: n(v) for a,b,v in re.findall(r'([A-Z]{3})\s+1=\s+([A-Z]{3})\s+([\d.]+)', text)}
    def rate(ccy):
        if ccy == base: return Decimal(1)
        if ccy+'/'+base not in fx: raise ValueError('Missing statement FX for '+ccy)
        return Decimal(fx[ccy+'/'+base])
    positions, group, row, detailed = [], None, None, False
    def finish():
        nonlocal row
        if not row: return
        if row['kind'] == 'security' and not row.get('isin'): raise ValueError('Statement security lacks an ISIN')
        if row['kind'] == 'call_deposit' and not row.get('serial'): raise ValueError('Deposit serial missing')
        key = row.get('isin') or row.get('serial') or row.get('iban')
        if not key: raise ValueError('Statement position identity missing')
        row['key'] = key
        row['accrued_base'] = row.get('accrued_base', '0')
        row['accrued_native'] = str(Decimal(row['accrued_base']) / rate(row['currency'])) if Decimal(row['accrued_base']) else '0'
        positions.append(row)
        row = None
    for line in text.splitlines():
        if 'Detailed positions' in line and 'Statement' not in line: detailed = True
        if line.strip() in GROUPS:
            finish(); group = line.strip(); continue
        if not detailed or group is None: continue
        if line.startswith('Overview of cash flow') or line.startswith('Abbreviations and explanations'):
            finish(); group = None; continue
        primary = re.match(r'^\s*(?:(USD|EUR|CHF|CZK)\s+)?(' + NUMBER + r')\s+(\S.*)', line)
        parts = re.split(r'\s{2,}', line.strip())
        if primary and primary.start(2)<30 and len(parts) >= 4 and re.fullmatch(r'\d+\.\d+', parts[-1]):
            finish()
            cash = group == 'Liquidity - Accounts'
            deposit = group == 'Liquidity - Call deposits'
            value = n(parts[-2])
            if cash:
                ccy = primary[1]
                if not ccy: raise ValueError('Cash currency missing')
                price, cost, scale = '1', None, '1'
            else:
                price_cell, cost_cell = (parts[-3], parts[-4]) if deposit else (parts[-4], parts[-5])
                cost_match = re.fullmatch(r'(?:(USD|EUR|CZK|CHF)\s+)?('+NUMBER+r')(%?)(?:\s+[A-Za-z]+)?', cost_cell)
                price_match = re.fullmatch(r'('+NUMBER+r')(%?)(?:\s+[A-Za-z]+)?', price_cell)
                if not cost_match or not price_match: raise ValueError('Unrecognized statement cost/market columns')
                ccy = cost_match[1] or primary[1]
                if not ccy: raise ValueError('Security currency missing')
                cost, price = n(cost_match[2]), n(price_match[1])
                scale = '0.01' if cost_match[3] == '%' else '1'
            qty = n(primary[2])
            row = {'kind': 'cash_account' if cash else 'call_deposit' if deposit else 'security',
                   'group': group, 'currency': ccy, 'quantity': qty,
                   'description': primary[3].split('  ')[0].strip(), 'market_value_base': value,
                   'market_price': price, 'cost_price': cost, 'unit_scale': scale,
                   'market_value_native': str(Decimal(qty)*Decimal(price)*Decimal(scale)),
                   'cost_native': str(Decimal(qty)*Decimal(cost)*Decimal(scale)) if cost is not None else None}
            if cash or deposit: row['market_value_native'] = qty
            continue
        if not row: continue
        if re.match(r'^(?:Subtotal|Total|Net assets)\b',line.strip()): continue
        ident = re.search(r'Valor (\d+) - ISIN ([A-Z0-9]{12})', line)
        if ident:
            if row.get('isin') and row['isin'] != ident[2]: raise ValueError('Ambiguous statement security layout')
            row.update(valor=ident[1], isin=ident[2])
        serial = re.search(r'Serial no\.\s*(\d+)', line)
        if serial: row['serial'] = serial[1]
        account = re.search(r'CH\d{2}(?: [A-Z0-9]+)+', line)
        if account and row['kind'] == 'cash_account': row['iban'] = account[0].replace(' ', '')
        accrued = re.search(r'('+NUMBER+r')\s{2,}\d+\.\d+%?\s*$', line)
        if accrued and accrued.start(1) > 130: row['accrued_base'] = n(accrued[1])
    finish()
    keys = [(p['key'],p['currency']) for p in positions]
    if len(keys) != len(set(keys)) or not positions: raise ValueError('Duplicate or empty statement positions')
    market = sum((Decimal(p['market_value_base']) for p in positions), Decimal(0))
    accrued = sum((Decimal(p['accrued_base']) for p in positions), Decimal(0))
    delta = market + accrued - Decimal(n(declared[-1]))
    if abs(delta) > len(positions): raise ValueError('Extracted statement positions do not reconcile to net assets')
    return {'source':'UBS year-end statement', 'portfolio':portfolio[1], 'as_of':as_of,
            'valuation_currency':base, 'exchange_rates':fx, 'positions':positions,
            'net_assets_base':n(declared[-1]), 'extracted_market_value_base':str(market),
            'accrued_interest_base':str(accrued), 'rounding_difference_base':str(delta),
            'cost_basis':'UBS stated historical cost; matched UBS acquisition cash is preferred when available'}


def parse_statement(raw):
    if not isinstance(raw,bytes) or not raw.startswith(b'%PDF-') or len(raw)>4*1024*1024:
        raise ValueError('Select a UBS PDF statement under 4 MiB')
    with tempfile.NamedTemporaryFile(suffix='.pdf') as file:
        file.write(raw); file.flush()
        try:
            result = subprocess.run(['pdftotext','-f','1','-l','60','-layout','-enc','UTF-8',file.name,'-'],
                                    capture_output=True, timeout=20)
        except (FileNotFoundError,subprocess.TimeoutExpired):
            raise ValueError('PDF extraction unavailable or timed out') from None
    if result.returncode or len(result.stdout)>3*1024*1024: raise ValueError('PDF text could not be read safely')
    data = parse_statement_text(result.stdout.decode('utf-8'))
    data['id'] = hashlib.sha256(raw).hexdigest()
    return data


class BaselineStore:
    def __init__(self,path): self.path=Path(path)

    def import_pdf(self,raw):
        data=parse_statement(raw)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS opening_baselines (id TEXT PRIMARY KEY,as_of TEXT NOT NULL,payload TEXT NOT NULL,source_pdf BLOB NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            validate_baseline(db,data)
            cursor=db.execute('INSERT OR IGNORE INTO opening_baselines VALUES (?,?,?,?)',
                              (data['id'],data['as_of'],json.dumps(data),raw))
        return {'as_of':data['as_of'],'count':len(data['positions']),'already_imported':cursor.rowcount==0}

    def latest(self,as_of=None):
        if not self.path.exists(): return None
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='opening_baselines'").fetchone(): return None
            row=db.execute('SELECT payload FROM opening_baselines WHERE as_of < ? ORDER BY as_of DESC,rowid DESC LIMIT 1',
                           (as_of or '9999-12-31',)).fetchone()
        return json.loads(row[0]) if row else None
