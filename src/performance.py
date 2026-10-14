"""Explicit average-cost inventory and flow-adjusted closing-valuation P&L."""
import csv
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from io import StringIO
import json
import sqlite3

HEADERS = ['event_id','date','sequence','type','account','security','currency','quantity','price','multiplier','fees','amount','description']
KINDS = {'OPENING','BUY','SELL','INCOME','CASHFLOW','VALUATION'}


def dec(value, field):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'Invalid {field}') from None
    if not d.is_finite():
        raise ValueError(f'Non-finite {field}')
    if abs(d) > Decimal('1e30') or d.as_tuple().exponent < -18:
        raise ValueError(f'{field} exceeds supported precision or range')
    return d


def parse_events(content):
    reader = csv.DictReader(StringIO(content.lstrip('\ufeff')))
    if reader.fieldnames != HEADERS:
        raise ValueError('Use the performance CSV template with its exact columns')
    events, seen = [], set()
    for n, row in enumerate(reader, 2):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'Column count mismatch at row {n}')
        row = {k:v.strip() for k,v in row.items()}
        if not row['event_id'] or len(row['event_id']) > 128 or row['event_id'] in seen:
            raise ValueError(f'Missing or duplicate event_id at row {n}')
        seen.add(row['event_id'])
        if date.fromisoformat(row['date']).isoformat()!=row['date']:
            raise ValueError('Dates must use YYYY-MM-DD')
        if not row['sequence'].isdigit():
            raise ValueError('Sequence must be a nonnegative integer; order same-day fills explicitly')
        row['sequence'] = int(row['sequence'])
        if row['type'] not in KINDS or not row['account']:
            raise ValueError('Invalid type or missing account')
        if len(row['currency']) != 3 or not row['currency'].isalpha() or not row['currency'].isupper():
            raise ValueError('Currency must be a three-letter uppercase code')
        if any(len(str(v)) > 512 or any(ord(c)<32 for c in str(v)) for v in row.values()):
            raise ValueError('Fields must be short plain text')
        if row['type'] in {'OPENING','BUY','SELL'}:
            if not row['security']:
                raise ValueError('Inventory events require security')
            for f in ['quantity','price','multiplier','fees']:
                row[f] = str(dec(row[f] or ('0' if f=='fees' else ''), f))
            if dec(row['price'],'price') < 0 or dec(row['multiplier'],'multiplier') <= 0 or dec(row['fees'],'fees') < 0:
                raise ValueError('Price/fees must be nonnegative and multiplier positive')
            if row['type'] != 'OPENING' and dec(row['quantity'],'quantity') <= 0:
                raise ValueError('Trade quantity must be positive')
            if row['type']=='OPENING' and dec(row['fees'],'fees') != 0:
                raise ValueError('Opening average price must already include historical fees; opening fees must be zero')
            if row['amount']:
                raise ValueError('Inventory events must leave amount blank')
        else:
            row['amount'] = str(dec(row['amount'],'amount'))
            if any(row[f] for f in ['quantity','price','multiplier','fees','security']):
                raise ValueError('Cash and valuation events must leave security and inventory fields blank')
        events.append(row)
    if not events:
        raise ValueError('No events supplied')
    return events


class PerformanceStore:
    def __init__(self, path):
        self.path = path

    def events(self):
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='performance_events'").fetchone():
                return []
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM performance_events')]

    def import_content(self, content):
        incoming = parse_events(content)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS performance_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            old = {r[0]:json.loads(r[1]) for r in db.execute('SELECT id,payload FROM performance_events')}
            added = 0
            for row in incoming:
                if row['event_id'] in old and row != old[row['event_id']]:
                    raise ValueError('Conflicting event_id; batch rejected without changes')
                if row['event_id'] not in old:
                    old[row['event_id']] = row
                    added += 1
            # Reject ambiguous inventory chronology and duplicate closing valuations atomically.
            ordering, openings, valuations = set(), set(), set()
            for row in old.values():
                inventory = (row['account'],row['security'],row['currency'])
                if row['type'] in {'OPENING','BUY','SELL'}:
                    order = (*inventory,row['date'],row['sequence'])
                    if order in ordering:
                        raise ValueError('Duplicate inventory date/sequence; use explicit unique sequence numbers')
                    ordering.add(order)
                    if row['type']=='OPENING':
                        if inventory in openings:
                            raise ValueError('Only one opening baseline per account/security/currency is allowed')
                        openings.add(inventory)
                if row['type']=='VALUATION':
                    key=(row['account'],row['currency'],row['date'])
                    if key in valuations:
                        raise ValueError('Only one closing valuation per account/currency/date is allowed')
                    valuations.add(key)
            for row in incoming:
                db.execute('INSERT OR IGNORE INTO performance_events VALUES (?,?)',(row['event_id'],json.dumps(row)))
        return {'added':added,'existing':len(incoming)-added,'count':len(old)}


def calculate_performance(events, as_of=None):
    as_of = as_of or datetime.now(ZoneInfo('Europe/Prague')).date().isoformat()
    date.fromisoformat(as_of)
    ordered = sorted((e for e in events if e['date'] <= as_of), key=lambda e:(e['date'],e['sequence'],e['event_id']))
    inventory, blocked, realized, income, warnings = {}, set(), [], [], []
    for e in ordered:
        kind=e['type']
        if kind=='INCOME':
            income.append({'date':e['date'],'currency':e['currency'],'account':e['account'],
                           'amount':e['amount'],'description':e['description'],'event_id':e['event_id']})
        if kind not in {'OPENING','BUY','SELL'}:
            continue
        key=(e['account'],e['security'],e['currency'])
        if key in blocked:
            continue
        mult=dec(e['multiplier'],'multiplier')
        price=dec(e['price'],'price')*mult
        quantity=dec(e['quantity'],'quantity')
        if kind=='OPENING':
            if key in inventory:
                blocked.add(key)
                warnings.append('Opening baseline occurs after a trade: '+e['security'])
                continue
            inventory[key]={'quantity':quantity,'average':price,'multiplier':mult}
            continue
        if key not in inventory:
            blocked.add(key)
            warnings.append('Missing opening baseline (use zero quantity for a new position): '+e['security'])
            continue
        inv=inventory[key]
        if inv['multiplier'] != mult:
            blocked.add(key)
            warnings.append('Multiplier changed without a corporate-action adjustment: '+e['security'])
            continue
        signed=quantity if kind=='BUY' else -quantity
        fees=dec(e['fees'],'fees')
        previous=inv['quantity']
        closing=min(abs(previous),quantity) if previous*signed<0 else Decimal(0)
        if closing:
            sign=Decimal(1) if previous>0 else Decimal(-1)
            closing_fee=fees*closing/quantity
            pnl=closing*(price-inv['average'])*sign-closing_fee
            realized.append({'event_id':e['event_id'],'date':e['date'],'account':e['account'],
                             'security':e['security'],'currency':e['currency'],'quantity':str(closing),
                             'amount':str(pnl),'fees':str(closing_fee),'description':e['description']})
        residual=quantity-closing
        remaining=previous+(signed/quantity)*closing
        if residual:
            unit=price+fees/quantity if signed>0 else price-fees/quantity
            inv['average']=(abs(remaining)*inv['average']+residual*unit)/(abs(remaining)+residual)
        inv['quantity']=previous+signed
        if not inv['quantity']:
            inv['average']=Decimal(0)
    # Incomplete inventory invalidates even earlier realized rows for that instrument.
    realized=[r for r in realized if (r['account'],r['security'],r['currency']) not in blocked]
    open_rows=[{'account':a,'security':s,'currency':c,'quantity':str(v['quantity']),
                'average_cost_per_unit':str(v['average']/v['multiplier']), 'multiplier':str(v['multiplier'])}
               for (a,s,c),v in inventory.items() if (a,s,c) not in blocked and v['quantity']]
    totals={}
    for r in realized:
        c=r['currency']
        t=totals.setdefault(c,{'realized':Decimal(0),'income':Decimal(0),'ytd_realized':Decimal(0),'ytd_income':Decimal(0)})
        # Calculated average costs may have more fractional digits than imported
        # money inputs. Apply input precision limits only while parsing inputs.
        calculated=Decimal(r['amount'])
        t['realized']+=calculated
        if r['date'][:4]==as_of[:4]: t['ytd_realized']+=calculated
    for r in income:
        t=totals.setdefault(r['currency'],{'realized':Decimal(0),'income':Decimal(0),'ytd_realized':Decimal(0),'ytd_income':Decimal(0)})
        t['income']+=dec(r['amount'],'amount')
        if r['date'][:4]==as_of[:4]: t['ytd_income']+=dec(r['amount'],'amount')
    periods=[]
    keys=sorted({(e['account'],e['currency']) for e in ordered if e['type']=='VALUATION'})
    for account,currency in keys:
        marks=[e for e in ordered if e['type']=='VALUATION' and e['account']==account and e['currency']==currency]
        for before,after in zip(marks,marks[1:]):
            flows=sum((dec(e['amount'],'amount') for e in ordered if e['type']=='CASHFLOW'
                       and e['account']==account and e['currency']==currency and before['date']<e['date']<=after['date']),Decimal(0))
            change=dec(after['amount'],'amount')-dec(before['amount'],'amount')-flows
            days=(date.fromisoformat(after['date'])-date.fromisoformat(before['date'])).days
            periods.append({'account':account,'currency':currency,'from':before['date'],'to':after['date'],
                            'days':days,'kind':'daily' if days==1 else 'period','amount':str(change),'external_flows':str(flows)})
    return {'method':'average_cost','as_of':as_of,'event_count':len(ordered),'stored_event_count':len(events),'realized':realized,
            'income':income,'open_inventory':open_rows,'periods':periods,
            'totals':{c:{k:str(v) for k,v in t.items()} for c,t in sorted(totals.items())},
            'warnings':warnings,'inventory_complete':bool(inventory) and not blocked,
            'basis':'Imported records only; opening average costs include historical fees. Amounts remain in native currencies. Closing valuations must include all assets and cash; CASHFLOW records are external transfers only. Income and trading flows must not be entered as external flows.'}
