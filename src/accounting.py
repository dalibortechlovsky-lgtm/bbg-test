"""Native UBS accounting. Historical cost and year-start marks are separate."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, localcontext
import re

from .attribution import attribute_currency
from .baseline import BaselineStore,portfolio_identity
from .report import fx_rate
from .ubs_activity import ActivityStore

D = Decimal
ZERO = D(0)


def text(value): return None if value is None else str(value)
def fixed(value): return format(value.quantize(D('0.000000000001')), 'f')
def identity(p): return p.get('isin') or ('VALOR:'+p['valor'] if p.get('valor') else None)


def closing_positions(snapshot):
    result=[]
    for p in snapshot['positions']:
        q=D(p['quantity_or_balance']);ccy=p['currency'];rate=fx_rate(snapshot,ccy,snapshot['valuation_currency'])
        price=p.get('market_price');scale=None
        if p['kind']!='security': native=q;scale=D(1)
        elif price:
            scale=D('0.01') if price['convention']=='percent' else D(1)
            contract=re.search(r'1:(\d+(?:\.\d+)?)',p['description'])
            if contract: scale=D(contract[1])
            native=q*D(price['value'])*scale
            # A stated option ratio is accepted only when independently
            # corroborated by the native price and UBS reported market value.
            if rate is None or abs(native*rate-D(p['market_value']))>max(D(5),abs(D(p['market_value']))*D('0.000005')):
                native=D(p['market_value'])/rate if rate else None;scale=None
        else: native=D(p['market_value'])/rate if rate else None
        accrued=D(p.get('accrued_interest') or '0')
        accrued_native=accrued/rate if accrued and rate else ZERO
        result.append({'key':identity(p),'description':p['description'],'kind':p['kind'],'group':p['group'],
                       'currency':ccy,'quantity':q,'value':native+accrued_native if native is not None else None,
                       'price':D(price['value']) if price else None,'scale':scale,'position':p})
    return result


def matched_reversals(records):
    retired=set()
    for r in records:
        if not r['reversal'] or r['category']!='security_trade': continue
        candidates=[x for x in records if not x['reversal'] and x['id'] not in retired and
                    all(x[k]==r[k] for k in ['account','base_type','trade_date','currency','isin','quantity','price']) and
                    D(x['amount'])==-D(r['amount'])]
        if len(candidates)==1: retired.update([r['id'],candidates[0]['id']])
    return retired


def trade_cash(row):
    cash=D(row['amount'])
    if row['accrued'] is not None:
        if row['accrued_currency'] != row['currency']: raise ValueError('Accrued-interest currency mismatch')
        cash-=D(row['accrued'])
    return cash


def is_future(row): return row['description'].casefold().startswith('future ')


def signed_quantity(row):
    if row['quantity'] is None: raise ValueError('Trade quantity missing')
    positive=row['base_type'].startswith(('Purchase','Buy'))
    sign=1 if positive else -1
    return D(row['quantity'])*sign*(-1 if row['reversal'] else 1)


def source_key(row): return row['isin'] or ('VALOR:'+row['valor'] if row.get('valor') else None)


def coverage_complete(account,start,cutoff):
    cursor=date.fromisoformat(start)
    for first,last in sorted(account.get('coverage_intervals',[(account['from'],account['until'])])):
        first,last=date.fromisoformat(first),date.fromisoformat(last)
        if first>cursor:break
        if last>=cursor:cursor=last+timedelta(days=1)
    return cursor>date.fromisoformat(cutoff)


def internal_fx_adjustments(flows,historical_fx):
    """Keep FX cash exchanges internal even when the daily fixing differs from execution.

    Bucket flows remove each native leg. Their translated net difference is
    assigned once to the receiving currency, within currency contribution.
    """
    pairs=defaultdict(list);adjustments=defaultdict(Decimal)
    for ccy,rows in flows.items():
        for flow in rows:
            if flow['kind']=='internal_fx':pairs[flow['reference']].append((ccy,flow))
    for legs in pairs.values():
        if len(legs)!=2 or len({c for c,_ in legs})!=2 or D(legs[0][1]['amount'])*D(legs[1][1]['amount'])>=0:continue
        rates=[D(1) if c=='CZK' else historical_fx.get((c+'CZK Curncy',f['date'])) for c,f in legs]
        if any(r is None for r in rates):continue
        receiving=next(c for c,f in legs if D(f['amount'])>0)
        adjustments[receiving]+=sum((D(f['amount'])*D(r) for (_,f),r in zip(legs,rates)),ZERO)
    return adjustments


def calculate_accounting(snapshot,baseline,activity,historical_fx=None,lifecycle=None,cash_reviews=None):
    if not snapshot: return None
    if not baseline or baseline['as_of']!=str(int(snapshot['as_of'][:4])-1)+'-12-31':
        return {'status':'Opening statement needed','as_of':snapshot['as_of'],'report_currency':'CZK',
                'assets':[],'currencies':[],'issues':['Import the prior year-end UBS statement for the valuation year.'],
                'portfolio_pnl_verified':False,'ytd_pnl_czk':None,'ytd_asset_contribution_czk':None,
                'realized_totals':{},'history_requests':[],'coverage':[]}
    with localcontext() as context:
        context.prec=70
        return _calculate(snapshot,baseline,activity,historical_fx or {},lifecycle or [],cash_reviews or {})


def _calculate(snapshot,baseline,activity,historical_fx,lifecycle,cash_reviews):
    cutoff=snapshot['as_of'];start=cutoff[:4]+'-01-01';issues=[]
    if baseline['as_of']!=str(int(cutoff[:4])-1)+'-12-31':
        raise ValueError('Opening statement must precede the valuation year')
    if portfolio_identity(baseline['portfolio'])!=portfolio_identity(snapshot['portfolio']):
        raise ValueError('Opening statement and closing snapshot must identify the same UBS portfolio')
    records=(activity or {}).get('ledger',[])
    records=[r for r in records if r['trade_date']<=cutoff or r['booking_date']<=cutoff]
    retired=matched_reversals(records)
    current=closing_positions(snapshot);uncertain=set()
    settled={x['key']:x for x in sorted(lifecycle,key=lambda x:x['settled_by']) if x['settled_by']<=cutoff}
    opening={p['key']:p for p in baseline['positions'] if p['kind']=='security'}
    closed={p['key']:p for p in current if p['kind']=='security'}
    book={}
    for key,p in opening.items():
        q=D(p['quantity']);cost=D(p['cost_native'])/q if q else ZERO
        book[key]={'quantity':q,'average':cost,'currency':p['currency'],'description':p['description'],
                   'basis':'UBS year-end historical cost; original fees may not be included','blocked':False}
    # Use actual net acquisition cash for carried positions when the supplied
    # historical purchases fully reconstruct that opening quantity.
    for key,inv in book.items():
        old=[r for r in records if source_key(r)==key and r['category']=='security_trade' and
             r['trade_date']<start and r['id'] not in retired and not is_future(r)]
        try:
            if (old and all(signed_quantity(r)>0 and r['currency']==inv['currency'] and
                            (not r['price_currency'] or r['price_currency']==inv['currency']) for r in old) and
                    sum((signed_quantity(r) for r in old),ZERO)==inv['quantity']):
                inv['average']=-sum((trade_cash(r) for r in old),ZERO)/inv['quantity']
                inv['basis']='UBS original net acquisition cash; fees included, paid accrued interest excluded'
        except ValueError: pass
    asset_cash=defaultdict(Decimal);income=defaultdict(Decimal);realized=[];futures=defaultdict(Decimal)
    cash_income=defaultdict(Decimal);hedges=defaultdict(Decimal);cash_decisions=[]
    trades=[r for r in records if r['category'] in {'security_trade','security_redemption'} and
            start<=r['trade_date']<=cutoff and r['id'] not in retired]
    mixed=defaultdict(list)
    for r in trades:
        if not is_future(r): mixed[(source_key(r),r['trade_date'])].append(r)
    ambiguous={key for (key,_),rows in mixed.items() if len(rows)>1 and
               len({r['base_type'].startswith(('Purchase','Buy')) for r in rows})>1 and
               any(not r['trade_time'] for r in rows)}
    for r in sorted(trades,key=lambda r:(r['trade_date'],r['trade_time'] or '',r['booking_date'],r['id'])):
        if is_future(r): futures[r['currency']]+=D(r['amount']);continue
        key=source_key(r)
        if not key: issues.append('Trade lacks a security reference: '+r['transaction_no']);continue
        ccy=(closed.get(key) or opening.get(key) or {}).get('currency',r['price_currency'] or r['currency'])
        inv=book.setdefault(key,{'quantity':ZERO,'average':ZERO,'currency':ccy,'description':r['description'],
                                'basis':'UBS net trade cash, including fees','blocked':False})
        try:
            if r['reversal']: raise ValueError('Unmatched trade reversal')
            if key in ambiguous: raise ValueError('Same-day buy/sell order is ambiguous')
            if r['currency']!=ccy:
                uncertain.update([r['currency'],ccy])
                raise ValueError('Cross-currency settlement requires verified execution conversion')
            q=signed_quantity(r);cash=trade_cash(r)
            if not q: raise ValueError('Zero execution quantity')
            if inv['quantity']==0 and q<0 and r['base_type'] not in {'Sell to open'}:
                raise ValueError('Sale has no opening quantity/cost')
            unit=-cash/q;old=inv['quantity']
            closing=min(abs(old),abs(q)) if old*q<0 else ZERO
            if closing:
                profit=closing*(unit-inv['average'])*(1 if old>0 else -1)
                realized.append({'key':key,'date':r['trade_date'],'currency':ccy,'quantity':str(closing),
                                 'amount':str(profit),'description':inv['description'],'basis':inv['basis']})
            remaining=old+(closing if q>0 else -closing)
            residual=abs(q)-closing
            if residual: inv['average']=(abs(remaining)*inv['average']+residual*unit)/(abs(remaining)+residual)
            inv['quantity']=old+q
            if inv['quantity']==0: inv['average']=ZERO
            asset_cash[key]+=D(r['amount'])
        except ValueError as exc:
            inv['blocked']=True;issues.append(key+': '+str(exc))
    for r in records:
        if not start<=r['booking_date']<=cutoff: continue
        if r['category']=='income':
            key=source_key(r)
            if key and r['currency']==(closed.get(key) or opening.get(key) or {}).get('currency',r['currency']):income[key]+=D(r['amount'])
            else:cash_income[r['currency']]+=D(r['amount'])
        if r['category']=='futures_margin': futures[r['currency']]+=D(r['amount'])
        if r['category'] in {'otc_settlement_review','transfer_review'}:
            decision=cash_reviews.get(r['id'],{}).get('classification')
            cash_decisions.append({k:r[k] for k in ['id','category','currency','amount','booking_date','description','transaction_no']}|
                                  {'decision':decision,'reference':r.get('counterparty_reference','')})
            if r['category']=='otc_settlement_review':
                if decision=='fx_hedge_settlement':hedges[r['currency']]+=D(r['amount'])
                elif decision!='asset_settlement':issues.append('OTC settlement needs asset/hedge allocation: '+r['transaction_no'])
    for key,confirmation in settled.items():
        if key not in book:continue
        inv=book[key]
        later=[r for r in trades if source_key(r)==key and r['trade_date']>confirmation['settled_by']]
        if later:
            issues.append(key+': new executions after settlement confirmation require a fresh lifecycle review');continue
        if inv['currency']!=confirmation['currency']:issues.append(key+': settlement currency mismatch');continue
        inv['quantity']=ZERO;inv['average']=ZERO;inv['cash_grouped']=True
        futures[inv['currency']]+=asset_cash.pop(key,ZERO)+income.pop(key,ZERO)
        realized=[r for r in realized if r['key']!=key]
    assets=[];native_asset=defaultdict(Decimal);realized_totals=defaultdict(Decimal)
    for key in sorted(set(book)|set(closed)|set(opening)):
        inv=book.get(key);end=closed.get(key);begin=opening.get(key)
        ccy=(inv or end or begin)['currency'];end_q=end['quantity'] if end else ZERO
        expected=inv['quantity'] if inv else ZERO
        matched=expected==end_q and not (inv or {}).get('blocked',False)
        if not matched:issues.append(key+': calculated quantity '+str(expected)+' differs from UBS '+str(end_q))
        b=(D(begin['market_value_native'])+D(begin['accrued_native'])) if begin else ZERO
        e=end['value'] if end else ZERO
        grouped=(inv or {}).get('cash_grouped',False)
        gain=e-b+asset_cash[key]+income[key] if matched and e is not None and not grouped else None
        life=sum((D(x['amount']) for x in realized if x['key']==key),ZERO) if matched and not grouped else None
        if gain is not None:native_asset[ccy]+=gain
        if life is not None:realized_totals[ccy]+=life
        benchmark=D(begin['market_price'])*D(begin['unit_scale']) if begin else (inv['average'] if inv and inv['quantity'] else None)
        mark=end['price']*end['scale'] if end and end['price'] is not None and end['scale'] is not None else None
        change=(mark/benchmark-1)*100 if mark is not None and benchmark else None
        rate=fx_rate(baseline,ccy,'CZK')
        assets.append({'key':key,'description':(end or inv or begin)['description'],'currency':ccy,
                       'group':(end or begin or {}).get('group','Closed positions'), 'quantity':str(end_q),
                       'quantity_status':'Settled; cash result grouped' if grouped and matched else 'Matched' if matched else 'Review required','expected_quantity':str(expected),
                       'native_price_return_percent':text(change),'price_return_basis':'Year-start market price' if begin else 'Remaining average acquisition cost incl. fees',
                       'ytd_asset_pnl_native':text(gain),'asset_contribution_czk':text(gain*rate) if gain is not None and rate else None,
                       'realized_native':text(life),'income_native':str(income[key]),
                       'average_cost_native':text(inv['average']) if inv and matched and end_q else None,
                       'market_price_native':text(end['price']) if end else None,
                       'market_value_eur':text(e*fx_rate(snapshot,ccy,'EUR')) if e is not None and fx_rate(snapshot,ccy,'EUR') else None,
                       'cost_basis':(inv or {}).get('basis','Missing acquisition record'),
                       'opening_market_value_native':str(b),'closing_market_value_native':text(e)})
    flows=defaultdict(list)
    internal_ids={x['id'] for x in (activity or {}).get('review',[]) if x['classification']=='internal_transfer_candidate'}
    cash_decisions=[r for r in cash_decisions if r['id'] not in internal_ids]
    internal_references={r['transaction_no'] for r in records if r['id'] in internal_ids}
    for ref in internal_references:
        legs=[r for r in records if r['transaction_no']==ref and r['category']=='transfer_review']
        if len(legs)!=2 or len({r['currency'] for r in legs})!=1 or sum((D(r['amount']) for r in legs),ZERO)!=0:
            issues.append('Internal transfer requires both account legs: '+ref)
    for r in records:
        if not start<=r['booking_date']<=cutoff:continue
        if r['category'] in {'fx_conversion','transfer_review'}:
            if r['transaction_no'] in internal_references:continue
            if r['category']=='transfer_review' and cash_reviews.get(r['id'],{}).get('classification')!='external_funding':
                issues.append('Bank payment portfolio boundary requires confirmation: '+r['transaction_no'])
                uncertain.add(r['currency'])
            flows[r['currency']].append({'date':r['booking_date'],'amount':r['amount'],
                'kind':'internal_fx' if r['category']=='fx_conversion' else 'bank_payment',
                'reference':r['transaction_no']})
        elif r['category']=='unclassified':
            issues.append('Unclassified cash posting: '+r['transaction_no']);uncertain.add(r['currency'])
    pending=[{'key':source_key(r),'currency':r['currency'],'amount':r['amount'],
              'trade_date':r['trade_date'],'booking_date':r['booking_date'],'description':r['description']}
             for r in records if r['category'] in {'security_trade','security_redemption','fx_conversion','deposit_principal'}
             and r['trade_date']<=cutoff<r['booking_date'] and r['id'] not in retired]
    if pending:issues.append('Unsettled trades require settlement receivables/payables before whole-portfolio P/L can be certified')
    prior_pending=any(r['category']=='security_trade' and r['trade_date']<start<=r['booking_date']<=cutoff for r in records)
    if prior_pending:
        issues.append('Prior-year executions settled this year require opening settlement receivables/payables')
    cash_checks=[]
    for p in current:
        if p['kind']=='cash_account':
            account=re.sub(r'\s','',p['position'].get('raw',{}).get('IBAN','')).upper()
            prior=[b for b in baseline['positions'] if b['kind']=='cash_account' and b.get('iban')==account]
            if not prior:issues.append(p['currency']+' cash account has no matching opening balance');continue
            expected=D(prior[0]['quantity'])+sum((D(r['amount']) for r in records if r['account']==account and start<=r['booking_date']<=cutoff),ZERO)
        elif p['kind']=='call_deposit':
            serial=re.search(r'Serial (?:no\. )?(\d+)',p['position'].get('raw',{}).get('Description 2',''),re.I)
            if not serial:serial=re.search(r'Serial (?:no\. )?(\d+)',p['description'],re.I)
            if not serial:issues.append('Deposit serial missing: '+p['description']);continue
            prior=[b for b in baseline['positions'] if b['kind']=='call_deposit' and b.get('serial')==serial[1]]
            expected=D(prior[0]['quantity']) if prior else ZERO
            for r in records:
                if r['category']!='deposit_principal' or r.get('deposit_serial')!=serial[1] or not start<=r['booking_date']<=cutoff:continue
                if r['quantity'] is None or r['quantity_currency']!=p['currency'] or r['price']!='100' or r['price_convention']!='percent':
                    issues.append('Unverified deposit movement terms: '+r['transaction_no']);continue
                sign=1 if r['base_type'] in {'New Investment Money Market','Increase Money Market'} else -1
                expected+=D(r['quantity'])*sign*(-1 if r['reversal'] else 1)
        else:continue
        difference=p['quantity']-expected
        cash_checks.append({'description':p['description'],'currency':p['currency'],'kind':p['kind'],
                            'expected':str(expected),'reported':str(p['quantity']),'difference':str(difference)})
        if difference:issues.append(p['currency']+' '+p['kind']+' does not reconcile to opening balances and supplied cash postings')
    opening_values=defaultdict(Decimal);ending_values=defaultdict(Decimal)
    for p in baseline['positions']:opening_values[p['currency']]+=D(p['market_value_native'])+D(p['accrued_native'])
    for p in current:
        if p['value'] is not None:ending_values[p['currency']]+=p['value']
        else:issues.append('Native valuation missing: '+p['description'])
    currencies=[];requests=defaultdict(set);all_rates=True;all_gains=ZERO;total=ZERO
    for ccy in sorted(set(opening_values)|set(ending_values)|set(flows)):
        b=opening_values[ccy];e=ending_values[ccy]
        x0=fx_rate(baseline,ccy,'CZK');x1=fx_rate(snapshot,ccy,'CZK')
        native=e-b-sum((D(f['amount']) for f in flows[ccy]),ZERO)
        native-=hedges[ccy]
        asset=native*x0 if x0 else (ZERO if not b and not e and not flows[ccy] else None)
        missing=[];dated=[]
        for flow in flows[ccy]:
            rate=D(1) if ccy=='CZK' else historical_fx.get((ccy+'CZK Curncy',flow['date']))
            if rate is None:requests[ccy+'CZK Curncy'].add(flow['date']);missing.append(flow['date'])
            else:dated.append({'amount':flow['amount'],'fx':fixed(D(rate))})
        attribution=None
        if x0 and x1 and not missing:
            attribution=attribute_currency(ccy,fixed(b),fixed(e),fixed(x0),fixed(x1),dated)
            attribution['currency_contribution']=str(D(attribution['currency_contribution'])+hedges[ccy]*x0)
            total+=D(attribution['total_pnl'])
        elif b or e or flows[ccy]:all_rates=False
        if asset is not None:all_gains+=asset
        residual=native-native_asset[ccy]-futures[ccy]
        instrument_complete=all(p['quantity_status']!='Review required' for p in assets if p['currency']==ccy)
        currencies.append({'currency':ccy,'opening_native':str(b),'closing_native':str(e),
                           'native_asset_pnl':str(native) if ccy not in uncertain else None,'asset_contribution_czk':text(asset) if ccy not in uncertain else None,
                           'currency_contribution_czk':(attribution['currency_contribution'] if attribution else ('0' if ccy=='CZK' or not b and not e and not flows[ccy] else None)) if ccy not in uncertain else None,
                           'total_pnl_czk':attribution['total_pnl'] if attribution and ccy not in uncertain else None,
                           'opening_fx':text(x0),'closing_fx':text(x1),
                           'currency_move_percent':text((x1/x0-1)*100) if x0 and x1 else None,
                           'missing_flow_fx_dates':sorted(set(missing)), 'futures_settlements_native':str(futures[ccy]),
                           'fx_hedge_settlements_native':str(hedges[ccy]),
                           'cash_deposit_income_costs_native':str(residual) if instrument_complete and ccy not in uncertain else None,
                           'bank_payment_total_native':str(sum((D(f['amount']) for f in flows[ccy] if f['kind']=='bank_payment'),ZERO))})
    adjustments=internal_fx_adjustments(flows,historical_fx)
    for c in currencies:
        adjustment=adjustments[c['currency']]
        c['internal_fx_fixing_difference_czk']=str(adjustment)
        if c['currency_contribution_czk'] is not None:c['currency_contribution_czk']=str(D(c['currency_contribution_czk'])+adjustment)
        if c['total_pnl_czk'] is not None:c['total_pnl_czk']=str(D(c['total_pnl_czk'])+adjustment)
    total+=sum(adjustments.values(),ZERO)
    coverage=[]
    for a in (activity or {}).get('accounts',[]):
        complete=coverage_complete(a,start,cutoff)
        coverage.append({'currency':a['currency'],'from':a['from'],'until':a['until'],'complete':complete})
        if not complete:issues.append(a['currency']+' transaction coverage ends '+a['until']+'; valuation is '+cutoff)
    if not activity:issues.append('UBS account transactions have not been imported')
    available={(a['currency'],a['account']) for a in (activity or {}).get('accounts',[])}
    for p in snapshot['positions']:
        if p['kind']=='cash_account' and (p['currency'],re.sub(r'\s','',p.get('raw',{}).get('IBAN','')).upper()) not in available:
            issues.append(p['currency']+' account has no transaction export; a zero balance does not establish zero activity')
    for pair in (activity or {}).get('fx_pairs',[]):
        if pair['status']!='Matched internal currency legs':issues.append('Unmatched FX transaction: '+pair['transaction_no'])
    if not all_rates:issues.append('Historical FX is required for dated capital movements')
    no_open_margined=not any('future' in p['description'].casefold() and p['quantity'] for p in current)
    if no_open_margined:
        for c,v in futures.items():realized_totals[c]+=v
    # A same-currency bank payment is funding, not performance. Cross-currency
    # FX legs cancel at portfolio level at the selected independent dated fix.
    verified=bool(activity and coverage and all(a['complete'] for a in coverage) and not issues and all_rates)
    usable=not pending and not prior_pending and not uncertain and all(p['value'] is not None for p in current) and all(
        pair['status']=='Matched internal currency legs' for pair in (activity or {}).get('fx_pairs',[]))
    nav_complete=all(p['value'] is not None and (not p['value'] or fx_rate(snapshot,p['currency'],'CZK')) for p in current)
    return {'as_of':cutoff,'opening_as_of':baseline['as_of'],'report_currency':'CZK',
            'status':'Reconciled supplied records' if verified else 'Provisional · coverage or reconciliation pending',
            'portfolio_pnl_verified':verified,'ytd_pnl_czk':text(total) if all_rates and usable else None,
            'ytd_asset_contribution_czk':str(all_gains) if usable and all(c['asset_contribution_czk'] is not None for c in currencies) else None,'assets':assets,'currencies':currencies,
            'realized_totals':{c:str(v) for c,v in realized_totals.items()},'realized':realized,
            'nav_czk':str(sum((v*(fx_rate(snapshot,c,'CZK') or ZERO) for c,v in ending_values.items()),ZERO)) if nav_complete and not pending else None,
            'coverage':coverage,'issues':sorted(set(issues)),'cash_checks':cash_checks,'pending_settlements':pending,'cash_decisions':cash_decisions,
            'history_requests':[{'security':s,'dates':sorted(d)} for s,d in sorted(requests.items())],
            'basis':'Average cost uses UBS native net settlement cash, including recorded fees. Carried costs use the year-end statement unless original UBS acquisition cash fully reconstructs the lot. Realized gains use historical cost and are not added again to YTD gains. YTD uses year-start market values including accrued interest. Asset gains use opening CZK FX; price/FX interaction belongs to currency. Bank payments are external to the selected UBS portfolio; FX conversions and deposits are internal. The difference between internal FX execution amounts and daily fixings is included once in the receiving currency contribution. Owner-confirmed FX hedge cash is moved from asset contribution to currency contribution without changing total P/L. Missing coverage remains provisional.'}


def period_view(previous,snapshot,activity,rates,cash_reviews=None):
    if not previous:return None
    with localcontext() as context:
        context.prec=70
        start=previous['as_of'];end=snapshot['as_of'];begin=defaultdict(Decimal);finish=defaultdict(Decimal)
        for source,target in [(previous,begin),(snapshot,finish)]:
            for p in closing_positions(source):
                if p['value'] is None:return None
                target[p['currency']]+=p['value']
        flows=defaultdict(list);hedges=defaultdict(Decimal);decisions=cash_reviews or {};review_pending=False
        internal_ids={r['id'] for r in (activity or {}).get('review',[]) if r['classification']=='internal_transfer_candidate'}
        for r in (activity or {}).get('ledger',[]):
            if start<r['booking_date']<=end and r['category']=='otc_settlement_review' and decisions.get(r['id'],{}).get('classification')=='fx_hedge_settlement':
                hedges[r['currency']]+=D(r['amount'])
            if start<r['booking_date']<=end and r['category'] in {'fx_conversion','transfer_review'} and r['id'] not in internal_ids:
                if r['category']=='transfer_review' and decisions.get(r['id'],{}).get('classification')!='external_funding':review_pending=True
                flows[r['currency']].append({'date':r['booking_date'],'amount':r['amount'],'reference':r['transaction_no'],
                    'kind':'internal_fx' if r['category']=='fx_conversion' else 'bank_payment'})
        requests=defaultdict(set);rows=[];total=ZERO;complete=True
        adjustments=internal_fx_adjustments(flows,rates)
        for c in sorted(set(begin)|set(finish)|set(flows)):
            x0=fx_rate(previous,c,'CZK');x1=fx_rate(snapshot,c,'CZK');dated=[];missing=False
            for f in flows[c]:
                rate=D(1) if c=='CZK' else rates.get((c+'CZK Curncy',f['date']))
                if rate is None:requests[c+'CZK Curncy'].add(f['date']);missing=True
                else:dated.append({'amount':f['amount'],'fx':fixed(rate)})
            if not begin[c] and not finish[c] and not flows[c]:continue
            if x0 and x1 and not missing:
                a=attribute_currency(c,fixed(begin[c]),fixed(finish[c]),fixed(x0),fixed(x1),dated)
                a['currency_contribution']=str(D(a['currency_contribution'])+adjustments[c])
                a['currency_contribution']=str(D(a['currency_contribution'])+hedges[c]*x0)
                a['asset_contribution']=str(D(a['asset_contribution'])-hedges[c]*x0)
                a['native_asset_pnl']=str(D(a['native_asset_pnl'])-hedges[c])
                a['total_pnl']=str(D(a['total_pnl'])+adjustments[c]);total+=D(a['total_pnl'])
                rows.append(a)
            else:complete=False
        daily=(date.fromisoformat(end)-date.fromisoformat(start)).days==1
        unsettled=any(r['category'] in {'security_trade','security_redemption','fx_conversion','deposit_principal'}
                      and (r['trade_date']<=start<r['booking_date'] or r['trade_date']<=end<r['booking_date'])
                      for r in (activity or {}).get('ledger',[]))
        covered=bool(activity) and all(coverage_complete(a,start,end) for a in (activity or {}).get('accounts',[]))
        return {'from':start,'to':end,'label':'Daily P/L' if daily else 'Period P/L',
                'pnl_czk':str(total) if complete and not unsettled and not review_pending else None,'currencies':rows,
                'status':'Provisional; complete transaction and settlement reconciliation required' if not covered else 'Calculated; see portfolio reconciliation',
                'history_requests':[{'security':s,'dates':sorted(days)} for s,days in requests.items()]}


def accounting_view(path,snapshot=None):
    from .storage import SnapshotStore
    from .history import HistoryStore
    from .lifecycle import LifecycleStore
    from .cash_review import CashReviewStore
    snapshot=snapshot or SnapshotStore(path).latest()
    activity=ActivityStore(path).view(snapshot);rates=HistoryStore(path).rates();decisions=CashReviewStore(path).all()
    result=calculate_accounting(snapshot,BaselineStore(path).latest((snapshot or {}).get('as_of')),
                                activity,rates,LifecycleStore(path).all(),decisions)
    if not result:return result
    import json,sqlite3
    previous=None
    with sqlite3.connect(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots'").fetchone():
            row=db.execute('SELECT payload FROM snapshots WHERE as_of < ? ORDER BY as_of DESC,exported_at DESC LIMIT 1',(snapshot['as_of'],)).fetchone()
            previous=json.loads(row[0]) if row else None
    result['period']=period_view(previous,snapshot,activity,rates,decisions)
    wanted=defaultdict(set)
    for r in result['history_requests']+((result['period'] or {}).get('history_requests',[])):wanted[r['security']].update(r['dates'])
    result['history_requests']=[{'security':s,'dates':sorted(days)} for s,days in sorted(wanted.items())]
    return result
