"""Bloomberg-only current valuation using the same engine as dated reports."""
from datetime import date

from .historical_positions import reconstruct, historical_accounting
from .storage import SnapshotStore


def live_performance(path, bloomberg, today=None):
    today = today or date.today()
    fresh = {}
    for q in bloomberg.get('quotes', []):
        if q.get('stale', True): continue
        if q['security'] not in fresh or q['observed_at'] > fresh[q['security']]['observed_at']:
            fresh[q['security']] = q
    result = reconstruct(path, today.isoformat(), strict_market=True, live_quotes=fresh)
    snapshot = result['snapshot']
    accounting = historical_accounting(path,snapshot,result['review_flags']) if snapshot else None
    trades = (accounting or {}).get('trade_calculations')
    blockers = result['missing_market_data']+result['issues']+(trades or {}).get('issues', [])
    if not fresh: blockers.append('No fresh Bloomberg quotes are available.')
    available = bool(trades and trades['ytd_pnl_czk'] is not None and not blockers)
    if snapshot and not available and not blockers: blockers.append('Opening balances, funding or transaction reconciliation is incomplete.')
    anchor = SnapshotStore(path).latest()
    times = [q['observed_at'] for q in fresh.values()]
    security_rows = [p for p in result['positions'] if p['kind']=='security']
    estimate = dict(status='Bloomberg estimate · corrected trade calculations' if available else 'Waiting for Bloomberg / reconciled inputs',
        as_of=today.isoformat(),anchor_as_of=(anchor or {}).get('as_of'),
        estimated_ytd_pnl_czk=trades['ytd_pnl_czk'] if available else None,
        estimated_nav_czk=accounting['nav_czk'] if available else None,
        market_change_czk=None,quote_time_from=min(times) if times else None,quote_time_to=max(times) if times else None,
        fresh_price_count=sum(p['price'] is not None for p in security_rows),security_count=len(security_rows),
        fallback_price_count=0,fresh_fx_position_count=0,positions=[],
        blockers=sorted(set(blockers)),warnings=result['review_flags']+['Only recorded activity is included. Missing Bloomberg prices or FX block the total; bank prices are never substituted.'],
        trade_calculations=trades,
        basis='Same weighted-average trade and NAV calculation as the corrected Excel. Bloomberg current prices and FX; recorded holdings, transactions, opening bases and explicitly flagged accrued interest.')
    for p in security_rows:
        estimate['positions'].append(dict(description=p['description'],currency=p['currency'],change_czk=None,
            price_observed_at=None,notes=p.get('missing',[])+p.get('review_required',[])))
    return dict(indicative=estimate,snapshot=snapshot,accounting=accounting,reconstruction=result)
