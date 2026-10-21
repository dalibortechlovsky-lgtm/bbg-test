"""Reconstruct dated UBS holdings and value them with immutable Bloomberg history."""
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import re
import sqlite3

from .accounting import (accounting_view, closing_positions, is_future, matched_reversals,
                         signed_quantity, source_key)
from .baseline import BaselineStore
from .history import HistoryStore
from .lifecycle import LifecycleStore
from .ubs_activity import ActivityStore

D=Decimal
ZERO=D(0)


def _date(value):
    try:parsed=date.fromisoformat(value)
    except (TypeError,ValueError):raise ValueError('Historical date must be YYYY-MM-DD') from None
    if parsed.isoformat()!=value:raise ValueError('Historical date must be YYYY-MM-DD')
    return parsed


class HistoricalRequestStore:
    """Append-only owner requests consumed by the read-only Bloomberg worker."""
    def __init__(self,path):self.path=path

    def request(self,as_of):
        requested=_date(as_of)
        if requested>date.today():raise ValueError('Historical date cannot be in the future')
        if not BaselineStore(self.path).latest(as_of):
            raise ValueError('No prior year-end UBS statement supports this date')
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS historical_position_requests '
                       '(as_of TEXT PRIMARY KEY,created_at TEXT NOT NULL,source TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO historical_position_requests VALUES (?,?,?)',
                       (as_of,datetime.now(timezone.utc).isoformat(),'Owner requested historical positions and P/L'))
        return view_for_path(self.path,as_of)

    def dates(self):
        if not self.path.exists():return []
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='historical_position_requests'").fetchone():return []
            return [r[0] for r in db.execute('SELECT as_of FROM historical_position_requests ORDER BY as_of')]

    def history_requests(self):
        merged=defaultdict(set)
        for day in self.dates():
            for row in reconstruct(self.path,day,False)['market_requests']:
                merged[row['security']].update(row['dates'])
        return [{'security':security,'dates':sorted(days)} for security,days in sorted(merged.items())]


def _key(position):
    return position.get('isin') or ('VALOR:'+position['valor'] if position.get('valor') else None)


def _metadata(path,baseline):
    from .storage import SnapshotStore
    latest=SnapshotStore(path).latest();result={}
    verified={}
    for row in closing_positions(latest) if latest else []:
        scale=row['scale']
        # If UBS reports quantity, clean market value and unit price that
        # reconcile to an integer contract scale, the broker snapshot itself
        # verifies that scale even when the description omits a 1:n ratio.
        if (scale is None and row['kind']=='security' and row['price'] and row['quantity'] and
                row['value'] is not None and not row['position'].get('accrued_interest')):
            candidate=row['value']/row['quantity']/row['price'];integer=candidate.to_integral_value()
            if integer>0 and abs(candidate-integer)<=D('0.000001'):scale=integer
        if scale is not None:verified[row['key']]=scale
    for p in (latest or {}).get('positions',[]):
        key=_key(p)
        if key:
            result[key]=p|({'verified_unit_scale':str(verified[key])}
                           if verified.get(key) is not None else {})
    for p in baseline['positions']:
        if p['kind']=='security' and p['key'] not in result:
            result[p['key']]={'isin':p.get('isin'),'valor':p.get('valor'),'product':p['key'],
                'kind':'security','group':p['group'],'description':p['description'],'currency':p['currency']}
    return result,latest


def _scale(position,baseline_position=None):
    if baseline_position:return D(baseline_position['unit_scale'])
    if position.get('verified_unit_scale') is not None:return D(position['verified_unit_scale'])
    if position.get('group','').startswith('Bonds'):return D('0.01')
    match=re.search(r'1:(\d+(?:\.\d+)?)',position.get('description',''))
    if match:return D(match[1])
    if position.get('group')=='Equities - Equity investments':return D(1)
    return None


def reconstruct(path,requested,require_market_data=True):
    """Rebuild quantities and balances at a cutoff; never borrow later quantities."""
    cutoff=_date(requested)
    if cutoff>date.today():raise ValueError('Historical date cannot be in the future')
    baseline=BaselineStore(path).latest(requested)
    if not baseline:
        return {'snapshot':None,'positions':[],'issues':['No prior year-end UBS statement supports this date.'],
                'missing_market_data':[],'market_requests':[],'source_snapshot_as_of':None,
                'metadata_snapshot_as_of':None}
    from .storage import SnapshotStore
    prior_snapshot=SnapshotStore(path).at_or_before(requested)
    activity=ActivityStore(path).view()
    records=[r for r in (activity or {}).get('ledger',[]) if r['trade_date']<=requested or r['booking_date']<=requested]
    retired=matched_reversals(records);metadata,latest=_metadata(path,baseline)
    opening={p['key']:p for p in baseline['positions'] if p['kind']=='security'}
    quantities={key:D(p['quantity']) for key,p in opening.items()}
    descriptions={key:p['description'] for key,p in opening.items()};currencies={key:p['currency'] for key,p in opening.items()}
    issues=[]
    trades=[r for r in records if r['category'] in {'security_trade','security_redemption'} and
            baseline['as_of']<r['trade_date']<=requested and r['id'] not in retired and not is_future(r)]
    for row in sorted(trades,key=lambda r:(r['trade_date'],r['trade_time'] or '',r['booking_date'],r['id'])):
        key=source_key(row)
        if not key:issues.append('Trade lacks a security reference: '+row['transaction_no']);continue
        try:q=signed_quantity(row)
        except ValueError as exc:issues.append(key+': '+str(exc));continue
        quantities[key]=quantities.get(key,ZERO)+q;descriptions.setdefault(key,row['description'])
        currencies.setdefault(key,row['price_currency'] or row['currency'])
        metadata.setdefault(key,{'isin':row.get('isin'),'valor':row.get('valor'),'product':key,
            'kind':'security','group':'Reconstructed securities','description':row['description'],'currency':currencies[key]})
    prior_quantities={_key(p):D(p['quantity_or_balance']) for p in (prior_snapshot or {}).get('positions',[])
                      if p['kind']=='security' and _key(p)}
    settled={r['key']:r for r in sorted(LifecycleStore(path).all(),key=lambda r:r['settled_by'])
             if r['settled_by']<=requested or prior_snapshot and r['key'] not in prior_quantities}
    for key,confirmation in settled.items():
        later=[r for r in trades if source_key(r)==key and r['trade_date']>confirmation['settled_by']]
        if later:issues.append(key+': executions after settlement confirmation require review')
        elif key in quantities:quantities[key]=ZERO
    observations=HistoryStore(path).observations();market_requests=defaultdict(set);missing=[];positions=[];fx={};active=set()
    for key,quantity in sorted(quantities.items()):
        if not quantity:continue
        source=metadata.get(key,{});currency=currencies.get(key) or source.get('currency');active.add(currency)
        isin=source.get('isin') or (key if re.fullmatch('[A-Z0-9]{12}',key) else None);security='/isin/'+isin if isin else None
        if security:market_requests[security].add(requested)
        observation=observations.get((security,requested)) if security else None;base=opening.get(key);scale=_scale(source,base)
        bond=source.get('group','').startswith('Bonds');absent=[]
        if not security:absent.append('Bloomberg identifier mapping')
        if not observation:absent.append('Bloomberg PX_LAST')
        if scale is None:absent.append('verified contract multiplier')
        if bond and (not observation or observation['accrued'] is None):absent.append('Bloomberg YAS_BOND_ACCRUED')
        if absent:missing.append(key+': '+', '.join(absent))
        price=observation['price'] if observation else None;accrued=observation['accrued'] if observation else None
        native=quantity*price*scale if price is not None and scale is not None else None
        accrued_native=quantity*accrued*D('0.01') if bond and accrued is not None else ZERO if not bond else None
        positions.append({'key':key,'kind':'security','group':source.get('group','Reconstructed securities'),
            'description':descriptions.get(key,source.get('description',key)),'isin':isin,'valor':source.get('valor'),
            'product':source.get('product',key),'currency':currency,'quantity':str(quantity),
            'price':None if price is None else str(price),'price_convention':'percent' if bond else 'amount',
            'scale':None if scale is None else str(scale),'accrued_native':None if accrued_native is None else str(accrued_native),
            'market_value_native':None if native is None else str(native),'missing':absent})
    for item in [p for p in baseline['positions'] if p['kind']=='cash_account']:
        account=item['iban'];currency=item['currency'];active.add(currency)
        balance=D(item['quantity'])+sum((D(r['amount']) for r in records if r['account']==account and baseline['as_of']<r['booking_date']<=requested),ZERO)
        positions.append({'key':account,'kind':'cash_account','group':item['group'],'description':item['description'],
            'isin':None,'valor':None,'product':account,'currency':currency,'quantity':str(balance),'price':'1',
            'price_convention':'amount','scale':'1','accrued_native':'0','market_value_native':str(balance),'missing':[],'account':account})
    for item in [p for p in baseline['positions'] if p['kind']=='call_deposit']:
        serial=item['serial'];currency=item['currency'];active.add(currency);balance=D(item['quantity'])
        for row in records:
            if row['category']!='deposit_principal' or row.get('deposit_serial')!=serial or not baseline['as_of']<row['booking_date']<=requested:continue
            if row['quantity'] is None or row['quantity_currency']!=currency or row['price']!='100' or row['price_convention']!='percent':
                issues.append('Unverified deposit movement terms: '+row['transaction_no']);continue
            sign=1 if row['base_type'] in {'New Investment Money Market','Increase Money Market'} else -1
            balance+=D(row['quantity'])*sign*(-1 if row['reversal'] else 1)
        if balance:positions.append({'key':serial,'kind':'call_deposit','group':item['group'],
            'description':item['description']+' · Serial '+serial,'isin':None,'valor':None,'product':serial,
            'currency':currency,'quantity':str(balance),'price':'1','price_convention':'amount','scale':'1',
            'accrued_native':'0','market_value_native':str(balance),'missing':[]})
    for currency in sorted(active):
        if currency=='CZK':fx['CZK/CZK']='1';continue
        security=currency+'CZK Curncy';market_requests[security].add(requested);observation=observations.get((security,requested))
        if observation:fx[currency+'/CZK']=str(observation['price'])
        else:missing.append(security+': Bloomberg PX_LAST')
    complete=not missing and not issues;snapshot=None
    if complete or not require_market_data:
        synthetic=[];market=ZERO;accrued_total=ZERO
        for p in positions:
            rate=D(1) if p['currency']=='CZK' else D(fx[p['currency']+'/CZK']) if p['currency']+'/CZK' in fx else None
            native=D(p['market_value_native']) if p['market_value_native'] is not None else None
            native_accrued=D(p['accrued_native']) if p['accrued_native'] is not None else None
            value=native*rate if native is not None and rate is not None else None
            interest=native_accrued*rate if native_accrued is not None and rate is not None else None
            synthetic.append({'id':hashlib.sha256((p['key']+'|'+p['currency']).encode()).hexdigest(),
                'portfolio':(latest or {}).get('portfolio',baseline['portfolio']),'product':p['product'],'kind':p['kind'],
                'group':p['group'],'description':p['description'],'isin':p['isin'],'valor':p['valor'],'currency':p['currency'],
                'quantity_or_balance':p['quantity'],'intraday_quantity_or_balance':None,
                'market_value':None if value is None else str(value),'accrued_interest':None if interest is None else str(interest),
                'cost_value':None,'cost_price':None,'market_price':None if p['kind']=='cash_account' else
                    ({'value':p['price'],'convention':p['price_convention']} if p['price'] is not None else None),
                'price_date':requested,'unrealized_pnl_percent':None,'raw':{'IBAN':p.get('account','')} if p['kind']=='cash_account' else {}})
            if value is not None:market+=value
            if interest is not None:accrued_total+=interest
        snapshot={'id':'reconstructed-'+requested,'source':'reconstructed_ubs_activity_bloomberg_history',
            'portfolio':(latest or {}).get('portfolio',baseline['portfolio']),'as_of':requested,
            'exported_at':datetime.now(timezone.utc).isoformat(),'timestamp_timezone':'UTC','valuation_currency':'CZK',
            'count':len(synthetic),'positions':synthetic,'exchange_rates':fx,
            'summary':{'market_value':str(market),'accrued_interest':str(accrued_total),'cash_balances':{},'groups':{}},
            'warnings':['Reconstructed from the prior year-end UBS statement, dated UBS activity, and Bloomberg historical market data.']}
    return {'snapshot':snapshot,'positions':positions,'issues':sorted(set(issues)),
        'missing_market_data':sorted(set(missing)),
        'market_requests':[{'security':s,'dates':sorted(days)} for s,days in sorted(market_requests.items())],
        'source_snapshot_as_of':(prior_snapshot or {}).get('as_of'),
        'metadata_snapshot_as_of':(latest or {}).get('as_of')}


def selection(store,requested=None):
    dates=store.available_dates()
    if requested is None:
        snapshot=store.latest();actual=snapshot['as_of'] if snapshot else None
        return {'requested_as_of':None,'positions_as_of':actual,'snapshot_as_of':actual,
            'selection':'latest_snapshot' if snapshot else 'no_data','available_dates':dates,
            'message':'Showing the latest stored UBS snapshot dated '+actual+'.' if snapshot else 'No UBS positions snapshot has been imported.',
            'snapshot':snapshot,'source_snapshot_as_of':actual,'missing_market_data':[],'issues':[]}
    _date(requested);exact=store.at_or_before(requested)
    if exact and exact['as_of']==requested:
        return {'requested_as_of':requested,'positions_as_of':requested,'snapshot_as_of':requested,
            'selection':'exact_snapshot','available_dates':dates,'message':'Showing the stored UBS snapshot for '+requested+'.',
            'snapshot':exact,'source_snapshot_as_of':requested,'missing_market_data':[],'issues':[]}
    rebuilt=reconstruct(store.path,requested);available=rebuilt['snapshot'] is not None
    return {'requested_as_of':requested,'positions_as_of':requested,'snapshot_as_of':requested if available else None,
        'selection':'reconstructed' if available else 'reconstruction_pending','available_dates':dates,
        'message':('Reconstructed positions and valuation for '+requested+' from UBS activity and Bloomberg history.' if available else
            'Positions were reconstructed for '+requested+'; Bloomberg history or verified terms are still missing.'),
        'snapshot':rebuilt['snapshot'],'source_snapshot_as_of':rebuilt['source_snapshot_as_of'],
        'metadata_snapshot_as_of':rebuilt['metadata_snapshot_as_of'],
        'missing_market_data':rebuilt['missing_market_data'],'issues':rebuilt['issues'],
        'reconstructed_positions':rebuilt['positions'],'market_requests':rebuilt['market_requests']}


def view(store,requested=None):
    result=selection(store,requested);snapshot=result.pop('snapshot')
    if not snapshot:return result|{'exported_at':None,'valuation_currency':'CZK','summary':None,
        'positions':result.pop('reconstructed_positions',[]),'accounting':None,
        'valuation_basis':'Quantities and cash are reconstructed; total P/L waits for all required historical market inputs.'}
    positions=[{k:v for k,v in row.items() if k!='raw'} for row in snapshot['positions']]
    return result|{'exported_at':snapshot['exported_at'],'valuation_currency':snapshot['valuation_currency'],
        'summary':snapshot['summary'],'positions':positions,'accounting':accounting_view(store.path,snapshot),
        'valuation_basis':('Stored UBS quantities, prices, accrued interest and FX.' if result['selection']=='exact_snapshot' else
        'Prior year-end UBS statement plus dated UBS transactions; Bloomberg PX_LAST, bond accrued interest and FX at the selected date.')}


def view_for_path(path,requested):
    from .storage import SnapshotStore
    return view(SnapshotStore(path),requested)
