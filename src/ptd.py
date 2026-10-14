"""Immutable PTD audit records, explicit corrections, native P/L and UBS checks."""
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import sqlite3
from .performance import dec, calculate_performance

HEADERS=['Event Date','Portfolio Name','BBID','Security','Trade Date','Event Type','Trade Amount','New Amount','Trade Price','Sec Currency','Port Currency','Fx Rate','Source','User','Broker','Settle Date','Workout Date','Redemption Value','Principal','Commission','Security Fee','Yield','Discount','Accrued','Mtge Factor','Total','Notes']
NUMERIC=['Trade Amount','New Amount','Trade Price','Fx Rate','Redemption Value','Principal','Commission','Security Fee','Yield','Discount','Accrued','Mtge Factor','Total']


def now(): return datetime.now(timezone.utc).isoformat()


def parse_ptd(content):
    reader=csv.DictReader(StringIO(content.lstrip('\ufeff')),strict=True)
    if reader.fieldnames!=HEADERS: raise ValueError('Expected PTD transaction report columns in their original order')
    records=[]; seen=set(); accounts=set()
    try:
        for line,row in enumerate(reader,2):
            if None in row or any(v is None for v in row.values()): raise ValueError(f'PTD column mismatch at row {line}')
            row={k:v.strip() for k,v in row.items()}
            if any(len(v)>2048 or any(ord(c)<32 for c in v) for v in row.values()): raise ValueError('PTD fields must be bounded plain text')
            if not row['Portfolio Name'] or not row['Event Type']: raise ValueError('PTD account/event type missing')
            accounts.add(row['Portfolio Name'])
            event_time=None
            for fmt in ['%m/%d/%Y %H:%M:%S','%m/%d/%Y %H:%M']:
                try:event_time=datetime.strptime(row['Event Date'],fmt).isoformat();break
                except ValueError:pass
            if not event_time: raise ValueError('Invalid PTD Event Date')
            trade_date=datetime.strptime(row['Trade Date'],'%m/%d/%Y').date().isoformat() if row['Trade Date'] else None
            for field in ['Settle Date','Workout Date']:
                if row[field]: datetime.strptime(row[field],'%m/%d/%Y')
            values={f:str(dec(row[f],f)) if row[f] else None for f in NUMERIC}
            is_trade=row['Event Type'] in {'BUY','SELL'}
            if is_trade:
                if not row['BBID'] or not row['Security'] or not trade_date: raise ValueError('Priced PTD trade identity/date missing')
                if not re.fullmatch('[A-Z]{3}',row['Sec Currency']): raise ValueError('PTD native security currency required')
                if any(values[f] is None for f in ['Trade Amount','New Amount','Trade Price']): raise ValueError('PTD trade quantity, post-trade quantity and price required')
                qty=Decimal(values['Trade Amount'])
                if (row['Event Type']=='BUY' and qty<=0) or (row['Event Type']=='SELL' and qty>=0): raise ValueError('PTD side differs from signed Trade Amount')
                if Decimal(values['Trade Price'])<0: raise ValueError('PTD negative trade price unsupported')
                if any(values[f] is not None and Decimal(values[f])<0 for f in ['Commission','Security Fee']): raise ValueError('PTD fees must be nonnegative')
            identifier=hashlib.sha256(json.dumps(row,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if identifier in seen and is_trade:
                raise ValueError('Duplicate identical PTD trade rows; review source before import')
            seen.add(identifier)
            records.append({'id':identifier,'account':row['Portfolio Name'],'bbid':row['BBID'],
                'security':row['Security'],'type':row['Event Type'],'date':trade_date,
                'event_time':event_time,'event_timezone':'unspecified','currency':row['Sec Currency'] or None,
                'quantity':values['Trade Amount'],'post_quantity':values['New Amount'],'price':values['Trade Price'],
                'commission':values['Commission'],'security_fee':values['Security Fee'],'raw':row})
            if len(records)>10000: raise ValueError('PTD report exceeds supported row limit')
    except csv.Error: raise ValueError('Malformed PTD CSV') from None
    if not records or len(accounts)!=1: raise ValueError('Expected one nonempty PTD portfolio per import')
    return records


def schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS ptd_records (id TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ptd_imports (id TEXT PRIMARY KEY,imported_at TEXT NOT NULL,row_count INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS ptd_import_files (id TEXT PRIMARY KEY,source_content TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ptd_corrections (old_id TEXT PRIMARY KEY,new_id TEXT NOT NULL,reason TEXT NOT NULL,recorded_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ptd_settings (account TEXT NOT NULL,bbid TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,bbid));
    ''')


class PtdStore:
    def __init__(self,path): self.path=Path(path)

    def state(self):
        if not self.path.exists(): return [],[],{}
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ptd_records'").fetchone(): return [],[],{}
            db.execute('BEGIN')
            rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM ptd_records')]
            corrections=[{'old_id':r[0],'new_id':r[1],'reason':r[2],'recorded_at':r[3]} for r in db.execute('SELECT * FROM ptd_corrections')]
            settings={(r[0],r[1]):json.loads(r[2]) for r in db.execute('SELECT account,bbid,payload FROM ptd_settings')}
            return rows,corrections,settings

    def import_content(self,content):
        records=parse_ptd(content)
        digest=hashlib.sha256(content.encode()).hexdigest()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            schema(db);db.execute('BEGIN IMMEDIATE')
            added=0
            for record in records:
                added+=db.execute('INSERT OR IGNORE INTO ptd_records VALUES (?,?)',(record['id'],json.dumps(record))).rowcount
            db.execute('INSERT OR IGNORE INTO ptd_imports VALUES (?,?,?)',(digest,now(),len(records)))
            # Repeated identical administrative rows describe one state. Keep
            # every source row in the original file while storing that state once.
            db.execute('INSERT OR IGNORE INTO ptd_import_files VALUES (?,?)',(digest,content))
        return {'added':added,'existing':len(records)-added,'count':len(records)}

    def supersede(self,old_id,new_id,reason):
        if any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in [old_id,new_id]): raise ValueError('Valid PTD record identifiers required')
        if old_id==new_id or not isinstance(reason,str) or not 1<=len(reason.strip())<=256 or any(ord(c)<32 for c in reason): raise ValueError('Distinct correction records and a short reason are required')
        if not self.path.exists(): raise ValueError('Import PTD records first')
        with sqlite3.connect(self.path) as db:
            schema(db);db.execute('BEGIN IMMEDIATE')
            records={r[0]:json.loads(r[1]) for r in db.execute('SELECT id,payload FROM ptd_records WHERE id IN (?,?)',(old_id,new_id))}
            if len(records)!=2: raise ValueError('Both PTD correction records must exist')
            old,new=records[old_id],records[new_id]
            if old['type'] not in {'BUY','SELL'} or any(old[k]!=new[k] for k in ['account','bbid','type','currency','quantity','price','post_quantity']): raise ValueError('Only equivalent booked-trade versions may supersede each other')
            if old['event_time']>=new['event_time']: raise ValueError('Retain the later-entered corrected record')
            previous=db.execute('SELECT new_id FROM ptd_corrections WHERE old_id=?',(old_id,)).fetchone()
            if previous:
                if previous[0]!=new_id: raise ValueError('This record already has a different correction')
                return {'superseded':True,'already_applied':True}
            if db.execute('SELECT 1 FROM ptd_corrections WHERE old_id=?',(new_id,)).fetchone(): raise ValueError('Replacement is itself superseded')
            db.execute('INSERT INTO ptd_corrections VALUES (?,?,?,?)',(old_id,new_id,reason.strip(),now()))
        return {'superseded':True,'already_applied':False}

    def configure(self,payload):
        if not isinstance(payload,dict): raise ValueError('Expected PTD instrument settings')
        if set(payload)-{'account','bbid','multiplier','opening_date','opening_quantity','opening_price','isin','quantity_scale','missing_fees_zero_verified','verified','basis'}: raise ValueError('Unknown PTD settings')
        account,bbid=payload.get('account'),payload.get('bbid')
        records,_,settings=self.state()
        if not any(r['account']==account and r['bbid']==bbid for r in records): raise ValueError('Unknown PTD instrument')
        if payload.get('verified') is not True: raise ValueError('Settings must be verified against source records')
        basis=payload.get('basis')
        if not isinstance(basis,str) or not 1<=len(basis.strip())<=256 or any(ord(c)<32 for c in basis): raise ValueError('Supply the source of verified costs/units/link')
        clean={'verified':True,'basis':basis.strip(),'updated_at':now()}
        if 'missing_fees_zero_verified' in payload:
            if not isinstance(payload['missing_fees_zero_verified'],bool): raise ValueError('Missing fee confirmation must be boolean')
            clean['missing_fees_zero_verified']=payload['missing_fees_zero_verified']
        for k in ['multiplier','quantity_scale']:
            if payload.get(k) not in (None,''):
                v=dec(payload[k],k)
                if not Decimal('0.000000001')<=v<=Decimal('1e9'): raise ValueError('Positive bounded scale required')
                clean[k]=str(v)
        opening_keys=['opening_date','opening_quantity','opening_price']
        if any(payload.get(k) not in (None,'') for k in opening_keys):
            if any(payload.get(k) in (None,'') for k in opening_keys): raise ValueError('Opening date, quantity and native average price are required together')
            d=payload['opening_date']
            if date.fromisoformat(d).isoformat()!=d: raise ValueError('Opening date must be YYYY-MM-DD')
            clean.update(opening_date=d,opening_quantity=str(dec(payload['opening_quantity'],'opening quantity')),opening_price=str(dec(payload['opening_price'],'opening price')))
            if Decimal(clean['opening_price'])<0: raise ValueError('Opening price must be nonnegative, including historical fees')
        if payload.get('isin'):
            if not isinstance(payload['isin'],str) or not re.fullmatch('[A-Z0-9]{12}',payload['isin']): raise ValueError('Supply the exact 12-character UBS ISIN')
            clean['isin']=payload['isin']
        with sqlite3.connect(self.path) as db:
            schema(db)
            db.execute('INSERT OR REPLACE INTO ptd_settings VALUES (?,?,?)',(account,bbid,json.dumps(clean)))
        return {'configured':True}

    def view(self,broker=None,bloomberg=None,as_of=None,cash_events=None):
        records,corrections,settings=self.state()
        if not records: return None
        as_of=as_of or calculate_performance([])['as_of']
        date.fromisoformat(as_of)
        retired={c['old_id'] for c in corrections}
        active=[r for r in records if r['id'] not in retired]
        book={}
        for r in active:
            if r['bbid']: book.setdefault((r['account'],r['bbid'],r['currency']),[]).append(r)
        q_by_id={}
        for q in (bloomberg or {}).get('quotes',[]):
            if q.get('bbid') and (q['bbid'] not in q_by_id or (not q['stale'],q['observed_at'])>(not q_by_id[q['bbid']]['stale'],q_by_id[q['bbid']]['observed_at'])): q_by_id[q['bbid']]=q
        issues=[];positions=[];events=[];candidates=[]
        for (account,bbid,currency),history in sorted(book.items(),key=lambda item:tuple(v or '' for v in item[0])):
            trades=sorted([r for r in history if r['type'] in {'BUY','SELL'} and r['date']<=as_of],key=lambda r:(r['date'],r['event_time'],r['id']))
            config=settings.get((account,bbid),{})
            group_issues=[]
            if not currency: continue  # Cash initialization without currency is not account cash coverage.
            if trades:
                start=Decimal(trades[0]['post_quantity'])-Decimal(trades[0]['quantity'])
                qty=start
                for r in trades:
                    if qty+Decimal(r['quantity'])!=Decimal(r['post_quantity']): group_issues.append('Quantity continuity break / unresolved trade version')
                    qty=Decimal(r['post_quantity'])
                # Administrative quantity changes are not priced executions.
                adjustments=[r for r in history if r['type']=='POSITION_UPDATE' and r['date'] and trades[0]['date']<=r['date']<=as_of and r['quantity'] is not None and Decimal(r['quantity'])!=0]
                if adjustments: group_issues.append('Unpriced position adjustment requires cost/settlement review')
                multiplier=config.get('multiplier') or ('1' if bbid.startswith('EQ') and currency in {'USD','EUR'} else None)
                if not multiplier: group_issues.append('Verified contract / quote multiplier missing')
                if any(r['commission'] is None or r['security_fee'] is None for r in trades) and not config.get('missing_fees_zero_verified'): group_issues.append('Fees missing; blank fees are not zero')
                opening_price=None
                opening_date=trades[0]['date']
                if start==0:
                    opening_price='0'
                elif 'opening_price' in config:
                    if Decimal(config['opening_quantity'])!=start or config['opening_date']>trades[0]['date']: group_issues.append('Opening quantity/date differs from implied pre-trade inventory')
                    else: opening_price=config['opening_price'];opening_date=config['opening_date']
                else: group_issues.append('Carried-position average cost missing')
                if not group_issues:
                    common={'account':account,'security':bbid,'currency':currency,'multiplier':multiplier,'amount':'','description':trades[0]['security']}
                    local=[common|{'event_id':'PTD-opening-'+bbid+'-'+hashlib.sha256(account.encode()).hexdigest()[:10],'date':opening_date,'sequence':0,'type':'OPENING','quantity':str(start),'price':opening_price,'fees':'0'}]
                    local.extend(common|{'event_id':'PTD-'+r['id'],'date':r['date'],'sequence':i,'type':r['type'],'quantity':str(abs(Decimal(r['quantity']))),'price':r['price'],'fees':str(Decimal(r['commission'] or '0')+Decimal(r['security_fee'] or '0'))} for i,r in enumerate(trades,1))
                    events.extend(local)
            else:
                history_before=[r for r in history if r['date'] and r['date']<=as_of and r['post_quantity'] is not None]
                latest=max(history_before,key=lambda r:(r['event_time'],r['id'])) if history_before else None
                qty=Decimal(latest['post_quantity']) if latest else Decimal(0)
                multiplier=config.get('multiplier') or ('1' if bbid.startswith('EQ') and currency in {'USD','EUR'} else None)
                if qty: group_issues.append('Opening-only position: native cost and complete history missing')
            description=history[0]['security']
            positions.append({'account':account,'bbid':bbid,'description':description,'currency':currency,'quantity':str(qty),
                              'multiplier':multiplier,'status':'blocked' if group_issues else 'source-supported',
                              'issues':sorted(set(group_issues)),'trade_count':len(trades),'settings':config,'quote':q_by_id.get(bbid)})
            if group_issues: issues.append({'account':account,'bbid':bbid,'description':description,'reasons':sorted(set(group_issues))})
        # Explicit cash ledger is the sole source of income, closing NAV and external transfers.
        events.extend(e for e in (cash_events or []) if e['type'] in {'INCOME','CASHFLOW','VALUATION'})
        performance=calculate_performance(events,as_of)
        performance['source']='PTD booked trades + supplied cash ledger'
        performance['coverage']='Partial / not broker-reconciled'
        performance['portfolio_pnl_verified']=False
        performance['warnings'].extend(i['description']+': '+', '.join(i['reasons']) for i in issues)
        performance['basis']='Native-currency average cost from eligible PTD records; partial coverage, not total portfolio P/L. Entry timestamps are audit times. Fees must be present. MARS/Port Currency/Fx Rate/Total are not used for conversion or cash movements.'
        for p in performance['periods']: p['status']='Unverified closing-value / external-flow completeness'
        open_costs={(r['account'],r['security'],r['currency']):r for r in performance['open_inventory']}
        for p in positions:
            basis=open_costs.get((p['account'],p['bbid'],p['currency']))
            p['average_cost']=basis['average_cost_per_unit'] if basis else None
            p['native_unrealized']=None
            q=p['quote']
            if basis and q and not q['stale'] and q['currency']==p['currency'] and p['bbid'].startswith('EQ'):
                p['native_unrealized']=str(Decimal(p['quantity'])*Decimal(p['multiplier'])*(Decimal(q['price'])-Decimal(p['average_cost'])))
        # Candidate versions are reviewed explicitly, never deduplicated by economic resemblance.
        clusters={}
        for r in active:
            if r['type'] in {'BUY','SELL'}:
                clusters.setdefault(tuple(r[k] for k in ['account','bbid','type','currency','quantity','price','post_quantity']),[]).append(r)
        for rows in clusters.values():
            if len(rows)>1:
                ordered=sorted(rows,key=lambda r:(r['event_time'],r['id']))
                candidates.append({'description':ordered[0]['security'],'records':[{'id':r['id'],'trade_date':r['date'],'event_time':r['event_time'],'quantity':r['quantity'],'price':r['price']} for r in ordered],
                                   'suggested_old_id':ordered[0]['id'],'suggested_new_id':ordered[-1]['id']})
        audit=[{k:v for k,v in r.items() if k!='raw'}|{'status':'superseded' if r['id'] in retired else 'active'} for r in sorted(records,key=lambda r:(r['event_time'],r['id']),reverse=True)]
        reconciliation=[]
        if broker:
            bank_rows={}
            for b in broker['positions']:
                if b.get('isin'): bank_rows.setdefault(b['isin'],[]).append(b)
            # Re-evaluate PTD positions at the UBS valuation date, rather than comparing different dates.
            dated=self.view(None,bloomberg,broker['as_of']) if broker['as_of']!=as_of else {'positions':positions}
            for p in dated['positions']:
                isin=p['settings'].get('isin') or ((p.get('quote') or {}).get('isin'))
                matches=bank_rows.get(isin,[]) if isin else []
                state='Unmapped';difference=None;bank_qty=None
                if len(matches)==1:
                    bank=matches[0];bank_qty=bank['quantity_or_balance']
                    if bank['currency']!=p['currency']: state='Currency mismatch'
                    elif not (p['bbid'].startswith('EQ') or p['settings'].get('quantity_scale')): state='UBS quantity scale unverified'
                    else:
                        scale=Decimal(p['settings'].get('quantity_scale','1'))
                        difference=Decimal(p['quantity'])*scale-Decimal(bank_qty)
                        state='Quantity matched' if difference==0 else 'Quantity mismatch'
                elif len(matches)>1: state='Ambiguous UBS ISIN'
                reconciliation.append({'account':p['account'],'bbid':p['bbid'],'description':p['description'],'isin':isin,'as_of':broker['as_of'],
                                       'ptd_quantity':p['quantity'],'ubs_quantity':bank_qty,'difference':None if difference is None else str(difference),'status':state})
        return {'source':'PTD','as_of':as_of,'stored_record_count':len(records),'active_record_count':len(active),'superseded_count':len(retired),
                'active_trade_count':sum(r['type'] in {'BUY','SELL'} for r in active),'positions':positions,'issues':issues,
                'performance':performance,'correction_candidates':candidates,'corrections':corrections,'audit':audit,'reconciliation':reconciliation,
                'status':'Partial / not broker-reconciled','daily_pnl_verified':False,'portfolio_pnl_verified':False}
