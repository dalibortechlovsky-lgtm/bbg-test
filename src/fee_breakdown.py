"""Disclosure of posted charges; does not alter P/L or infer embedded commissions."""
from collections import defaultdict
from decimal import Decimal as D
from .report import fx_rate


def fee_breakdown(snapshot, baseline, activity):
    start, end = baseline['as_of'], snapshot['as_of']
    groups = {}; entries = []; native = defaultdict(D); total = D(0); missing = set()
    for r in (activity or {}).get('ledger', []):
        if r['category'] not in {'fees', 'account_interest'} or not start < r['booking_date'] <= end:
            continue
        cost = -D(r['amount'])
        if not cost: continue
        ccy = r['currency']; rate = fx_rate(snapshot, ccy, 'CZK')
        converted = cost * rate if rate is not None else None
        if rate is None: missing.add(ccy)
        else: total += converted
        label = 'Account interest (net)' if r['category'] == 'account_interest' else {
            'Custody Price': 'Custody fees', 'Third-Party Charges': 'Third-party charges',
            'Balance closing of service prices': 'Account / service charges'
        }.get(r.get('base_type'), r.get('base_type') or 'Other posted fees')
        key = (label, ccy)
        group = groups.setdefault(key, dict(category=label, currency=ccy, charges=D(0), credits=D(0), net=D(0), count=0))
        group['charges'] += max(cost, D(0)); group['credits'] += max(-cost, D(0)); group['net'] += cost; group['count'] += 1
        native[ccy] += cost
        entries.append(dict(date=r['booking_date'], reference=r['transaction_no'], category=label,
                            description=r['description'], currency=ccy, cost_native=str(cost),
                            cost_czk=str(converted) if converted is not None else None))
    summary = []
    for (_, ccy), g in sorted(groups.items()):
        rate = fx_rate(snapshot, ccy, 'CZK')
        summary.append({**g, **{k:str(g[k]) for k in ('charges','credits','net')},
                        'net_czk':str(g['net']*rate) if rate is not None else None})
    return dict(start=start, as_of=end, total_czk=str(total) if not missing else None,
                native_totals={k:str(v) for k,v in sorted(native.items())}, groups=summary,
                entries=sorted(entries,key=lambda x:(x['date'],x['reference'])), missing_fx=sorted(missing),
                basis='Posted fees and net account interest only; positive amounts are costs, negative amounts are credits. CZK equivalents use valuation-date FX, not payment-date FX. Already included in P/L; not deducted again. Embedded trading commissions, taxes, fund expenses and FX spreads are not separately quantified here. Coverage is limited to imported transactions.')
