"""Reconstruct dated UBS holdings and value them with immutable Bloomberg history."""
from collections import defaultdict
import calendar
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import re
import sqlite3

from .accounting import (accounting_view, closing_positions, is_future, matched_reversals,
                         signed_quantity, source_key)
from .baseline import BaselineStore
from .history import HistoryStore
from .historical_terms import HistoricalTermsStore
from .lifecycle import LifecycleStore
from .ubs_activity import ActivityStore
from .report import fx_rate

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
            if 'last_requested_at' not in {r[1] for r in db.execute('PRAGMA table_info(historical_position_requests)')}:
                db.execute('ALTER TABLE historical_position_requests ADD COLUMN last_requested_at TEXT')
            db.execute('INSERT OR IGNORE INTO historical_position_requests (as_of,created_at,source) VALUES (?,?,?)',
                       (as_of,datetime.now(timezone.utc).isoformat(),'Owner requested historical positions and P/L'))
            db.execute('UPDATE historical_position_requests SET last_requested_at=? WHERE as_of=?',
                       (datetime.now(timezone.utc).isoformat(),as_of))
        return view_for_path(self.path,as_of)

    def priority_dates(self):
        if not self.path.exists():return []
        with sqlite3.connect(self.path) as db:
            columns={r[1] for r in db.execute('PRAGMA table_info(historical_position_requests)')}
            if not columns:return []
            order='COALESCE(last_requested_at,created_at)' if 'last_requested_at' in columns else 'created_at'
            return [r[0] for r in db.execute('SELECT as_of FROM historical_position_requests ORDER BY '+order+' DESC,as_of DESC')]

    def dates(self):
        if not self.path.exists():return []
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='historical_position_requests'").fetchone():return []
            return [r[0] for r in db.execute('SELECT as_of FROM historical_position_requests ORDER BY as_of')]

    def history_requests(self):
        merged=defaultdict(set)
        for day in self.dates():
            if day==date.today().isoformat():continue
            for row in reconstruct(self.path,day,False,strict_market=True)['market_requests']:
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
            rate=fx_rate(latest,p['currency'],latest['valuation_currency'])
            result[key]=p|{'_snapshot_as_of':latest['as_of'],'_accrued_native':
                str(D(p.get('accrued_interest') or '0')/rate) if rate else None}|(
                {'verified_unit_scale':str(verified[key])} if verified.get(key) is not None else {})
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


def _linear_value(points,requested):
    """Return a source-point interpolation/proxy and its review description."""
    unique={day:D(value) for day,value in points if value is not None}
    if not unique:return None,None
    if requested in unique:return unique[requested],'Source price on the selected date is not a closing valuation.'
    before=[day for day in unique if day<requested];after=[day for day in unique if day>requested]
    if before and after:
        left=max(before);right=min(after);span=(_date(right)-_date(left)).days
        weight=D((_date(requested)-_date(left)).days)/D(span)
        value=unique[left]+(unique[right]-unique[left])*weight
        return value,f'Linear price estimate between source values dated {left} and {right}.'
    day=max(before) if before else min(after)
    return unique[day],f'Stale price proxy from {day}; no source value brackets the selected date.'


def _price_proxy(key,requested,opening,source,records):
    points=[]
    base=opening.get(key)
    if base and base.get('market_price') is not None:points.append((base.get('as_of') or '',base['market_price']))
    for row in records:
        if source_key(row)==key and row.get('price') is not None and row.get('trade_date'):
            points.append((row['trade_date'],row['price']))
    market=source.get('market_price')
    if isinstance(market,dict):market=market.get('value')
    if market is not None and source.get('_snapshot_as_of'):points.append((source['_snapshot_as_of'],market))
    return _linear_value([(day,value) for day,value in points if day],requested)


def _activity_scale(key,records):
    candidates=[]
    for row in records:
        if source_key(row)!=key or row.get('price') is None or row.get('quantity') is None or row.get('amount') is None:continue
        quantity=abs(D(row['quantity']));price=abs(D(row['price']))
        if not quantity or not price:continue
        ratio=abs(D(row['amount']))/quantity/price;integer=ratio.to_integral_value()
        if integer>=1 and abs(ratio-integer)/integer<=D('0.02'):candidates.append(integer)
    if candidates and len(set(candidates))==1:return candidates[0]
    return None


def _shift_month(day,months):
    month=day.month-1+months;year=day.year+month//12;month=month%12+1
    return date(year,month,min(day.day,calendar.monthrange(year,month)[1]))


def _coupon_accrued(source,requested):
    description=source.get('description','')
    rate=re.search(r'(\d+(?:\.\d+)?)%',description);maturity=re.search(r'(\d{2})\.(\d{2})\.(\d{4})',description)
    if not rate or not maturity:return None
    anchor=date(int(maturity[3]),int(maturity[2]),int(maturity[1]));cutoff=_date(requested)
    candidates=[]
    center=((cutoff.year-anchor.year)*12+cutoff.month-anchor.month)//6
    for offset in range(center-2,center+3):candidates.append(_shift_month(anchor,offset*6))
    previous=max(d for d in candidates if d<=cutoff);following=min(d for d in candidates if d>cutoff)
    return D(rate[1])/D(2)*D((cutoff-previous).days)/D((following-previous).days)


def _accrued_proxy(key,requested,opening,source,quantity):
    if 'ETF' in source.get('description','').upper() or 'ISHARES' in source.get('description','').upper():
        return ZERO,None
    calculated=_coupon_accrued(source,requested)
    if calculated is not None:
        return calculated,'Accrued interest estimated from the stated coupon and semiannual maturity schedule.'
    points=[];base=opening.get(key)
    if base and base.get('accrued_native') is not None and quantity:
        points.append((base.get('as_of') or '',D(base['accrued_native'])/abs(quantity)/D('0.01')))
    latest=source.get('_accrued_native')
    if latest is not None and quantity:points.append((source.get('_snapshot_as_of'),D(latest)/abs(quantity)/D('0.01')))
    value,reason=_linear_value([(day,value) for day,value in points if day],requested)
    return value,(None if reason is None else reason.replace('price','accrued-interest'))


def reconstruct(path,requested,require_market_data=True,strict_market=False,live_quotes=None):
    """Rebuild quantities and balances at a cutoff; never borrow later quantities."""
    cutoff=_date(requested)
    if cutoff>date.today():raise ValueError('Historical date cannot be in the future')
    baseline=BaselineStore(path).latest(requested)
    if not baseline:
        return {'snapshot':None,'positions':[],'issues':['No prior year-end UBS statement supports this date.'],
                'missing_market_data':[],'review_flags':[],'market_requests':[],'source_snapshot_as_of':None,
                'metadata_snapshot_as_of':None}
    from .storage import SnapshotStore
    prior_snapshot=SnapshotStore(path).at_or_before(requested)
    activity=ActivityStore(path).view()
    records=[r for r in (activity or {}).get('ledger',[]) if r['trade_date']<=requested or r['booking_date']<=requested]
    retired=matched_reversals(records);metadata,latest=_metadata(path,baseline)
    opening={p['key']:p|{'as_of':baseline['as_of']} for p in baseline['positions'] if p['kind']=='security'}
    quantities={key:D(p['quantity']) for key,p in opening.items()}
    descriptions={key:p['description'] for key,p in opening.items()};currencies={key:p['currency'] for key,p in opening.items()}
    issues=[]
    if strict_market:
        from .cash_review import CashReviewStore
        confirmed=CashReviewStore(path).all()
        for r in (activity or {}).get('ledger',[]):
            decision=confirmed.get(r['id'],{})
            if (decision.get('classification')=='asset_settlement' and decision.get('opening_as_of')==baseline['as_of']
                    and decision.get('opening_value') and r['booking_date']>requested):
                issues.append('Outside-bank opening asset is not yet settled; a Bloomberg-valued position is required before '+r['booking_date'])
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
    settled={r['key']:r for r in sorted(LifecycleStore(path).all(),key=lambda r:r['settled_by'])
             if r['settled_by']<=requested}
    for key,confirmation in settled.items():
        later=[r for r in trades if source_key(r)==key and r['trade_date']>confirmation['settled_by']]
        if later:issues.append(key+': executions after settlement confirmation require review')
        elif key in quantities:quantities[key]=ZERO
    observations=HistoryStore(path).observations();terms=HistoricalTermsStore(path).all()
    if strict_market:
        observations={k:v for k,v in observations.items() if v.get('source','').startswith('Bloomberg')}
    if live_quotes is not None:
        observations={(s,requested):{'price':D(q['price']),'accrued':None,'currency':q.get('currency')}
                      for s,q in live_quotes.items()}
    market_requests=defaultdict(set);missing=[];reviews=[];positions=[];fx={};active=set()
    for key,quantity in sorted(quantities.items()):
        if not quantity:continue
        source=metadata.get(key,{});term=terms.get(key,{});currency=currencies.get(key) or source.get('currency');active.add(currency)
        isin=source.get('isin') or (key if re.fullmatch('[A-Z0-9]{12}',key) else None);security='/isin/'+isin if isin else None
        if term.get('security'):security=term['security']
        if security:market_requests[security].add(requested)
        observation=observations.get((security,requested)) if security else None;base=opening.get(key)
        scale=D(term['unit_scale']) if term.get('unit_scale') is not None else _scale(source,base)
        if scale is None:
            scale=_activity_scale(key,records)
            if scale is not None:reviews.append(key+f': Review required — contract multiplier {scale} inferred from UBS net settlements.')
        bond=scale==D('0.01');absent=[]
        price=observation['price'] if observation else None
        if observation and observation.get('currency') not in (None,currency):price=None
        if price is None:
            price,reason=(None,None) if strict_market else _price_proxy(key,requested,opening,source,records)
            if price is None:absent.append('Bloomberg PX_LAST')
            else:reviews.append(key+': Review required — '+reason)
        if not security:
            reviews.append(key+': Bloomberg identifier mapping missing.')
        if scale is None:absent.append('verified contract multiplier')
        accrued=observation['accrued'] if observation else None
        if bond and accrued is None:
            accrued,reason=_accrued_proxy(key,requested,opening,source,quantity)
            if accrued is None:absent.append('Bloomberg accrued interest')
            elif reason:reviews.append(key+': Review required — '+reason)
        if term.get('review_required'):
            reviews.append(key+': Review required — '+term['basis'])
        if absent:missing.append(key+': '+', '.join(absent))
        native=quantity*price*scale if price is not None and scale is not None else None
        accrued_native=quantity*accrued*D('0.01') if bond and accrued is not None else ZERO if not bond else None
        positions.append({'key':key,'kind':'security','group':source.get('group','Reconstructed securities'),
            'description':descriptions.get(key,source.get('description',key)),'isin':isin,'valor':source.get('valor'),
            'product':source.get('product',key),'currency':currency,'quantity':str(quantity),
            'price':None if price is None else str(price),'price_convention':'percent' if bond else 'amount',
            'scale':None if scale is None else str(scale),'accrued_native':None if accrued_native is None else str(accrued_native),
            'market_value_native':None if native is None else str(native),'missing':absent,
            'review_required':[r for r in reviews if r.startswith(key+':')]})
    for item in [p for p in baseline['positions'] if p['kind']=='cash_account']:
        account=item['iban'];currency=item['currency']
        balance=D(item['quantity'])+sum((D(r['amount']) for r in records if r['account']==account and baseline['as_of']<r['booking_date']<=requested),ZERO)
        if balance:active.add(currency)
        positions.append({'key':account,'kind':'cash_account','group':item['group'],'description':item['description'],
            'isin':None,'valor':None,'product':account,'currency':currency,'quantity':str(balance),'price':'1',
            'price_convention':'amount','scale':'1','accrued_native':'0','market_value_native':str(balance),'missing':[],'account':account})
    deposits={p['serial']:p for p in baseline['positions'] if p['kind']=='call_deposit'}
    for row in records:
        if row['category']=='deposit_principal' and row.get('deposit_serial') and baseline['as_of']<row['booking_date']<=requested:
            deposits.setdefault(row['deposit_serial'],{'serial':row['deposit_serial'],'currency':row['currency'],
                'quantity':'0','group':'Liquidity - Call deposits','description':row['description']})
    for item in deposits.values():
        serial=item['serial'];currency=item['currency'];balance=D(item['quantity'])
        for row in records:
            if row['category']!='deposit_principal' or row.get('deposit_serial')!=serial or not baseline['as_of']<row['booking_date']<=requested:continue
            if row['quantity'] is None or row['quantity_currency']!=currency or row['price']!='100' or row['price_convention']!='percent':
                issues.append('Unverified deposit movement terms: '+row['transaction_no']);continue
            sign=1 if row['base_type'] in {'New Investment Money Market','Increase Money Market'} else -1
            balance+=D(row['quantity'])*sign*(-1 if row['reversal'] else 1)
        if balance:active.add(currency)
        if balance:positions.append({'key':serial,'kind':'call_deposit','group':item['group'],
            'description':item['description']+' · Serial '+serial,'isin':None,'valor':None,'product':serial,
            'currency':currency,'quantity':str(balance),'price':'1','price_convention':'amount','scale':'1',
            'accrued_native':'0','market_value_native':str(balance),'missing':[]})
    reviews.append('Cash/deposit accrued interest is not estimated between bank snapshots; posted income is included in cash.')
    if strict_market:
        active.update(r['currency'] for r in records if baseline['as_of']<r['booking_date']<=requested and D(r['amount']))
        active.update(p['currency'] for p in baseline['positions'] if D(p['market_value_native']) or D(p['accrued_native']))
    for currency in sorted(active):
        if currency=='CZK':fx['CZK/CZK']='1';continue
        if not strict_market and not any(p['currency']==currency and D(p['quantity']) for p in positions):continue
        security=currency+'CZK Curncy';market_requests[security].add(requested);observation=observations.get((security,requested))
        if observation and observation.get('currency') in (None,'CZK') and observation['price']>0:fx[currency+'/CZK']=str(observation['price'])
        else:
            if strict_market:
                missing.append(security+': Bloomberg PX_LAST');continue
            points=[(day,obs['price']) for (sec,day),obs in observations.items() if sec==security]
            for source in (baseline,prior_snapshot,latest):
                if source:
                    rate=fx_rate(source,currency,'CZK')
                    if rate is not None:points.append((source['as_of'],rate))
            rate,reason=_linear_value(points,requested)
            if rate is None:missing.append(security+': Bloomberg PX_LAST')
            else:
                fx[currency+'/CZK']=str(rate)
                reviews.append(security+': Review required — '+reason)
    complete=not missing and not issues;snapshot=None
    if complete or not require_market_data:
        synthetic=[];market=ZERO;accrued_total=ZERO
        for p in positions:
            rate=D(1) if p['currency']=='CZK' else D(fx[p['currency']+'/CZK']) if p['currency']+'/CZK' in fx else None
            native=D(p['market_value_native']) if p['market_value_native'] is not None else None
            native_accrued=D(p['accrued_native']) if p['accrued_native'] is not None else None
            value=ZERO if native==ZERO else native*rate if native is not None and rate is not None else None
            interest=ZERO if native_accrued==ZERO else native_accrued*rate if native_accrued is not None and rate is not None else None
            synthetic.append({'id':hashlib.sha256((p['key']+'|'+p['currency']).encode()).hexdigest(),
                'portfolio':(latest or {}).get('portfolio',baseline['portfolio']),'product':p['product'],'kind':p['kind'],
                'group':p['group'],'description':p['description'],'isin':p['isin'],'valor':p['valor'],'currency':p['currency'],
                'quantity_or_balance':p['quantity'],'intraday_quantity_or_balance':None,
                'market_value':None if value is None else str(value),'accrued_interest':None if interest is None else str(interest),
                'cost_value':None,'cost_price':None,'market_price':None if p['kind']=='cash_account' else
                    ({'value':p['price'],'convention':p['price_convention']} if p['price'] is not None else None),
                'price_date':requested,'unrealized_pnl_percent':None,'raw':{'IBAN':p.get('account','')} if p['kind']=='cash_account' else {'Serial no.':p['key']} if p['kind']=='call_deposit' else {}})
            if value is not None:market+=value
            if interest is not None:accrued_total+=interest
        snapshot={'id':'reconstructed-'+requested,'source':'reconstructed_ubs_activity_bloomberg_history',
            'portfolio':(latest or {}).get('portfolio',baseline['portfolio']),'as_of':requested,
            'exported_at':datetime.now(timezone.utc).isoformat(),'timestamp_timezone':'UTC','valuation_currency':'CZK',
            'count':len(synthetic),'positions':synthetic,'exchange_rates':fx,
            'summary':{'market_value':str(market),'accrued_interest':str(accrued_total),'cash_balances':{},'groups':{}},
            'warnings':['Reconstructed from the prior year-end UBS statement, dated UBS activity, and Bloomberg historical market data.']}
    return {'snapshot':snapshot,'positions':positions,'issues':sorted(set(issues)),'review_flags':sorted(set(reviews)),
        'missing_market_data':sorted(set(missing)),
        'market_requests':[{'security':s,'dates':sorted(days)} for s,days in sorted(market_requests.items())],
        'source_snapshot_as_of':(prior_snapshot or {}).get('as_of'),
        'metadata_snapshot_as_of':(latest or {}).get('as_of')}


def selection(store,requested=None,strict_market=True):
    dates=store.available_dates()
    if requested is None:
        snapshot=store.latest();actual=snapshot['as_of'] if snapshot else None
        return {'requested_as_of':None,'positions_as_of':actual,'snapshot_as_of':actual,
            'selection':'latest_snapshot' if snapshot else 'no_data','available_dates':dates,
            'message':'Showing the latest stored UBS snapshot dated '+actual+'.' if snapshot else 'No UBS positions snapshot has been imported.',
            'snapshot':snapshot,'source_snapshot_as_of':actual,'missing_market_data':[],'issues':[],'review_flags':[]}
    _date(requested);exact=store.at_or_before(requested)
    if not strict_market and exact and exact['as_of']==requested:
        return {'requested_as_of':requested,'positions_as_of':requested,'snapshot_as_of':requested,
            'selection':'exact_snapshot','available_dates':dates,'message':'Showing the stored UBS snapshot for '+requested+'.',
            'snapshot':exact,'source_snapshot_as_of':requested,'missing_market_data':[],'issues':[],'review_flags':[]}
    current_day=strict_market and requested==date.today().isoformat()
    if current_day:
        from .market_performance import live_performance
        from .quotes import QuoteStore
        rebuilt=live_performance(store.path,QuoteStore(store.path).view())['reconstruction']
    else:
        rebuilt=reconstruct(store.path,requested,strict_market=strict_market)
    available=rebuilt['snapshot'] is not None
    provisional=available and bool(rebuilt['review_flags'])
    return {'requested_as_of':requested,'positions_as_of':requested,'snapshot_as_of':requested if available else None,
        'selection':'reconstructed_provisional' if provisional else 'reconstructed' if available else 'reconstruction_pending','available_dates':dates,
        'message':(('Current-date estimate for '+requested+' uses fresh Bloomberg prices and FX; see flagged accrual estimates.') if current_day and available else
            ('Provisional reconstructed positions and P/L for '+requested+'; flagged estimates require review.') if provisional else
            'Reconstructed positions and valuation for '+requested+' from UBS activity and Bloomberg history.' if available else
            'Positions were reconstructed for '+requested+'; Bloomberg history or verified terms are still missing.'),
        'snapshot':rebuilt['snapshot'],'source_snapshot_as_of':rebuilt['source_snapshot_as_of'],
        'metadata_snapshot_as_of':rebuilt['metadata_snapshot_as_of'],
        'missing_market_data':rebuilt['missing_market_data'],'issues':rebuilt['issues'],'review_flags':rebuilt['review_flags'],
        'reconstructed_positions':rebuilt['positions'],'market_requests':rebuilt['market_requests']}


def historical_accounting(path,snapshot,review_flags):
    accounting=accounting_view(path,snapshot)
    if not accounting:return accounting
    pending=accounting.get('pending_settlements',[])
    bridge=accounting.get('nav_bridge',{})
    if snapshot.get('source')=='reconstructed_ubs_activity_bloomberg_history' and pending and all(
            bridge.get(k) is not None for k in ('opening_nav_czk','net_external_flows_czk')):
        adjustment=ZERO;available=True
        for row in pending:
            if row.get('category') in {'fx_conversion','deposit_principal'}:continue
            rate=fx_rate(snapshot,row['currency'],'CZK')
            if row.get('category') not in {'security_trade','security_redemption'} or rate is None:
                available=False;break
            adjustment+=D(row['amount'])*rate
        if available:
            nav=D(snapshot['summary']['market_value'])+D(snapshot['summary']['accrued_interest'])+adjustment
            pnl=nav-D(bridge['opening_nav_czk'])-D(bridge['net_external_flows_czk'])
            accounting['nav_czk']=str(nav);accounting['ytd_pnl_czk']=str(pnl)
            accounting['settlement_adjustment_czk']=str(adjustment)
            bridge.update(closing_nav_czk=str(nav),pnl_czk=str(pnl),attribution_difference_czk=None)
            review_flags=list(review_flags)+['Estimated settlement receivables/payables included at selected-date FX; cash remains on booking-date basis.']
    if review_flags:
        accounting['status']='Provisional historical valuation · review required'
        accounting['portfolio_pnl_verified']=False
        accounting['provisional']=True
        accounting['issues']=list(accounting.get('issues',[]))+list(review_flags)
    from .trade_calculations import calculate_trades
    from .cash_review import CashReviewStore
    accounting['trade_calculations']=calculate_trades(snapshot,BaselineStore(path).latest(snapshot['as_of']),
        ActivityStore(path).view(snapshot),accounting,CashReviewStore(path).all(),LifecycleStore(path).all())
    return accounting


def view(store,requested=None,strict_market=True):
    result=selection(store,requested,strict_market);snapshot=result.pop('snapshot')
    if not snapshot:return result|{'exported_at':None,'valuation_currency':'CZK','summary':None,
        'positions':result.pop('reconstructed_positions',[]),'accounting':None,
        'valuation_basis':'Quantities and cash are reconstructed; total P/L waits for all required historical market inputs.'}
    positions=[{k:v for k,v in row.items() if k!='raw'} for row in snapshot['positions']]
    accounting=historical_accounting(store.path,snapshot,result.get('review_flags',[]))
    return result|{'exported_at':snapshot['exported_at'],'valuation_currency':snapshot['valuation_currency'],
        'summary':snapshot['summary'],'positions':positions,'accounting':accounting,
        'valuation_basis':('Bloomberg prices and FX only; recorded transactions and balances. Any estimated bond accrual is flagged.' if strict_market and requested else
        'Stored UBS quantities, prices, accrued interest and FX.' if result['selection']=='exact_snapshot' else
        'Prior year-end UBS statement plus dated UBS transactions; available Bloomberg history and explicitly flagged source-data estimates.'
        if result['selection']=='reconstructed_provisional' else
        'Prior year-end UBS statement plus dated UBS transactions; Bloomberg PX_LAST, bond accrued interest and FX at the selected date.')}


def view_for_path(path,requested):
    from .storage import SnapshotStore
    return view(SnapshotStore(path),requested)
