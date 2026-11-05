"""Trade-level YTD at valuation-date FX, shared by dated and live views.

Opening marks are YTD bases, not acquisition costs. Income, portfolio FX and
pooled futures margin are separate. No balancing amount is assigned to a trade.
"""
from collections import defaultdict
from decimal import Decimal as D

from .accounting import closing_positions, matched_reversals, signed_quantity, source_key, is_future
from .report import fx_rate

ZERO = D(0)


def calculate_trades(snapshot, baseline, activity, accounting, decisions=None, lifecycle=None):
    if not snapshot or not baseline:
        return None
    decisions = decisions or {}
    cutoff = snapshot['as_of']; start = baseline['as_of']
    all_rows = (activity or {}).get('ledger', [])
    retired = matched_reversals([r for r in all_rows if r['trade_date'] <= cutoff or r['booking_date'] <= cutoff])
    booked = [r for r in all_rows if start < r['booking_date'] <= cutoff]
    trades = [r for r in all_rows if r['category'] in {'security_trade', 'security_redemption'}
              and start < r['trade_date'] <= cutoff and r['id'] not in retired]
    current = {p['key']: p for p in closing_positions(snapshot) if p['kind'] == 'security'}
    grouped = {r['key'] for r in lifecycle or [] if r['settled_by'] <= cutoff}
    grouped.update(source_key(r) for r in trades if is_future(r))
    book = {}; rows = []; issues = []; opening_securities = ZERO
    def rate(ccy):
        value = fx_rate(snapshot, ccy, 'CZK')
        if value is None: issues.append('Missing valuation-date Bloomberg FX: ' + ccy)
        return value
    def emit(key, description, ccy, quantity, pnl, status, day=None, basis=None):
        fx = rate(ccy)
        row = dict(key=key, description=description, currency=ccy, quantity=str(quantity),
                   status=status, closed_on=day, pnl_native=str(pnl),
                   pnl_czk=str(pnl*fx) if fx is not None else None,
                   basis_native=None if basis is None else str(basis))
        rows.append(row)
    for p in baseline['positions']:
        if p['kind'] != 'security': continue
        basis = D(p['market_value_native']) + D(p['accrued_native'])
        fx = rate(p['currency'])
        if fx is not None: opening_securities += basis * fx
        book[p['key']] = dict(q=D(p['quantity']), basis=basis, currency=p['currency'], description=p['description'])
    # Buys on the same day precede sales only when no opposing order ambiguity
    # exists; an ambiguous day is flagged rather than certified as exact.
    by_day = defaultdict(list)
    for r in trades: by_day[(source_key(r), r['trade_date'])].append(r)
    for (key, day), records in by_day.items():
        if len({r['base_type'].startswith(('Purchase','Buy')) for r in records}) > 1 and any(not r.get('trade_time') for r in records):
            issues.append(key + ': same-day execution order needs review on ' + day)
    for r in sorted(trades, key=lambda x:(x['trade_date'], x.get('trade_time') or '', x['booking_date'], x['id'])):
        key = source_key(r); cash = D(r['amount']); ccy = r['currency']
        if not key:
            issues.append('Trade identifier missing: ' + r['transaction_no']); continue
        if key in grouped:
            emit(key, r['description'], ccy, ZERO, cash, 'Commission / settlement cash only', r['trade_date'])
            continue
        inv = book.setdefault(key, dict(q=ZERO, basis=ZERO, currency=ccy, description=r['description']))
        if inv['currency'] != ccy or r.get('reversal'):
            issues.append(key + ': unmatched reversal or cross-currency trade'); continue
        try: q = signed_quantity(r)
        except ValueError as exc:
            issues.append(key + ': ' + str(exc)); continue
        if not q:
            issues.append(key + ': zero execution quantity'); continue
        old = inv['q']
        if old*q < 0:
            closed = min(abs(old), abs(q)); fraction = closed/abs(q)
            removed = inv['basis']*closed/abs(old)
            emit(key, inv['description'], ccy, closed, cash*fraction-removed,
                 'Closed', r['trade_date'], removed)
            inv['basis'] -= removed
            residual_cash = cash*(1-fraction)
            inv['basis'] -= residual_cash
        else:
            inv['basis'] -= cash
        inv['q'] += q
        if not inv['q']: inv['basis'] = ZERO
    for key in sorted(set(book)|set(current)):
        if key in grouped:
            if key in current and current[key]['quantity']:
                issues.append(key + ': open margined contract needs a verified valuation')
            continue
        inv = book.get(key); end = current.get(key)
        q = end['quantity'] if end else ZERO
        if (inv['q'] if inv else ZERO) != q:
            issues.append(key + ': trade quantity does not match valued holdings'); continue
        if not q: continue
        value = end['value']; basis = inv['basis']
        if value is None:
            issues.append(key + ': missing closing valuation'); continue
        emit(key, end['description'], end['currency'], q, value-basis, 'Open', basis=basis)
        rows[-1].update(price=str(end['price']) if end['price'] is not None else None,
                        value_native=str(value), pnl_percent=str((value-basis)/basis*100) if basis else None)
    # Combine executions in the same closing row, but retain reopened positions
    # separately. This matches Excel's weighted-average split of partial closes.
    combined = {}
    for r in rows:
        k = (r['key'], r['closed_on'], r['status'], r['currency'])
        if k not in combined: combined[k] = r.copy(); continue
        for field in ('quantity','pnl_native','pnl_czk','basis_native'):
            if r.get(field) is not None and combined[k].get(field) is not None:
                combined[k][field] = str(D(combined[k][field])+D(r[field]))
    rows = list(combined.values())
    totals = defaultdict(D); cash_movement = ZERO
    for r in booked:
        fx = rate(r['currency'])
        if fx is None: continue
        cash = D(r['amount'])*fx; cash_movement += cash; category = r['category']
        if category == 'income': totals['income_czk'] += cash
        elif category in {'fees','account_interest'}: totals['fees_czk'] += cash
        elif category == 'unclassified': issues.append('Unclassified cash: '+r['transaction_no'])
        elif category == 'futures_margin': totals['pooled_margin_czk'] += cash
        elif category == 'fx_conversion': totals['fx_cash_czk'] += cash
        elif category == 'deposit_principal': totals['principal_czk'] += cash
        elif category in {'transfer_review','otc_settlement_review'}:
            choice = decisions.get(r['id'], {}).get('classification')
            if category == 'otc_settlement_review' and choice in {'asset_settlement','fx_hedge_settlement'} or choice == 'trading_profit':
                emit(r['transaction_no'], r['description'], r['currency'], ZERO, D(r['amount']), 'Closed cash settlement', r['booking_date'])
            elif choice == 'external_funding': totals['external_at_close_czk'] += cash
            elif choice == 'asset_settlement': totals['transferred_czk'] += cash
            elif choice is None: issues.append('Cash classification missing: '+r['transaction_no'])
    # emit appends to rows after combining above.
    totals['pooled_margin_czk'] += D((accounting or {}).get('pending_margin_czk') or '0')
    bridge = (accounting or {}).get('nav_bridge') or {}
    opening_at_close = ZERO
    for p in baseline['positions']:
        amount = D(p['market_value_native'])+D(p['accrued_native'])
        if not amount: continue
        fx = rate(p['currency'])
        if fx is not None: opening_at_close += amount*fx
    for b in bridge.get('settlement_bases', []):
        fx = rate(b['currency'])
        if fx is not None: opening_at_close += D(b['amount_native'])*fx
    nonsecurity = ZERO
    for p in closing_positions(snapshot):
        if p['kind'] == 'security': continue
        if p['value'] == ZERO: continue
        fx = rate(p['currency'])
        if p['value'] is None: issues.append('Missing non-security balance: '+p['description'])
        elif fx is not None: nonsecurity += p['value']*fx
    totals['other_balances_czk'] = nonsecurity-(opening_at_close-opening_securities)+totals['principal_czk']+totals['transferred_czk']-cash_movement
    required = ('opening_nav_czk','net_external_flows_czk')
    if all(bridge.get(k) is not None for k in required):
        totals['portfolio_fx_czk'] = opening_at_close-D(bridge['opening_nav_czk'])+totals['fx_cash_czk']+totals['external_at_close_czk']-D(bridge['net_external_flows_czk'])
    else: issues.append('Opening NAV or dated external funding unavailable')
    open_pnl = sum((D(r['pnl_czk']) for r in rows if r['status']=='Open' and r['pnl_czk'] is not None), ZERO)
    closed_pnl = sum((D(r['pnl_czk']) for r in rows if r['status']!='Open' and r['pnl_czk'] is not None), ZERO)
    open_total = open_pnl+totals['portfolio_fx_czk']+totals['other_balances_czk']+totals['fees_czk']
    closed_total = closed_pnl+totals['income_czk']+totals['pooled_margin_czk']
    total = open_total+closed_total
    target = (accounting or {}).get('ytd_pnl_czk')
    difference = total-D(target) if target is not None else None
    if difference is not None and abs(difference)>D('0.01'): issues.append('Trade totals do not reconcile to NAV')
    available = not issues and target is not None
    return dict(as_of=cutoff, positions=rows, components={k:str(v) for k,v in totals.items()},
                open_security_pnl_czk=str(open_pnl), closed_security_pnl_czk=str(closed_pnl),
                open_subtotal_czk=str(open_total) if available else None,
                closed_subtotal_czk=str(closed_total) if available else None,
                ytd_pnl_czk=str(total) if available else None,
                reconciliation_difference_czk=str(difference) if difference is not None else None,
                issues=sorted(set(issues)),
                basis='Opening marks / net weighted-average trade cash; separate closed and reopened positions. Security P/L uses valuation-date FX. Income, fees, portfolio FX and pooled margin are counted once; pooled margin is not allocated to individual contracts.')
