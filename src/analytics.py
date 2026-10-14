"""Read-only portfolio estimates. All money calculations use Decimal.

Price ratios assume unchanged units and listing; they are indicative, never
booked valuations. Unsupported instruments remain at broker market value.
"""
from decimal import Decimal


def portfolio_analytics(snapshot, bloomberg):
    if not snapshot:
        return None
    base = snapshot['valuation_currency']
    quotes = {}
    for q in bloomberg.get('quotes', []):
        if q['stale']:
            continue
        key = q['security']
        if key not in quotes or q['observed_at'] > quotes[key]['observed_at']:
            quotes[key] = q
    zero = Decimal(0)
    total = zero
    estimate = zero
    covered_pnl = zero
    covered_value = zero
    price_count = fx_count = cost_count = 0
    allocation, rows = {}, []
    for p in snapshot['positions']:
        value = Decimal(p['market_value'])
        total += value
        ccy = p['currency']
        bucket = allocation.setdefault(ccy, {'market_value': zero, 'gross_value': zero,
                                            'cash_balance': zero, 'deposit_balance': zero})
        bucket['market_value'] += value
        bucket['gross_value'] += abs(value)
        if p['kind'] in {'cash_account', 'call_deposit'}:
            key = 'cash_balance' if p['kind'] == 'cash_account' else 'deposit_balance'
            bucket[key] += Decimal(p['quantity_or_balance'])
        pnl = None
        if p['cost_value'] is not None and p['kind'] == 'security':
            pnl = value - Decimal(p['cost_value'])
            covered_pnl += pnl
            covered_value += value
            cost_count += 1
        estimated = value
        price_factor = Decimal(1)
        fx_factor = Decimal(1)
        notes = []
        used_price = used_fx = None
        # Do not infer bond quote scales or option multipliers from descriptions.
        price_eligible = p['group'] == 'Equities - Equity investments'
        q = quotes.get('/isin/' + p['isin']) if p.get('isin') else None
        old_price = p.get('market_price')
        if price_eligible:
            if (q and q['currency'] == ccy and Decimal(q['price']) > 0
                    and old_price and old_price['convention'] == 'amount'
                    and Decimal(old_price['value']) > 0):
                price_factor = Decimal(q['price']) / Decimal(old_price['value'])
                price_count += 1
                used_price = q['observed_at']
                notes.append('Indicative equity price ratio; listing and units assumed unchanged')
            else:
                notes.append('Equity price unavailable, stale or currency incompatible')
        elif p['kind'] == 'security':
            notes.append('Price retained from UBS; instrument terms require verification')
        if ccy != base:
            fx = quotes.get(ccy + base + ' Curncy')
            old_fx = snapshot['exchange_rates'].get(ccy + '/' + base)
            if not old_fx:
                inverse = snapshot['exchange_rates'].get(base + '/' + ccy)
                if inverse:
                    old_fx = str(Decimal(1) / Decimal(inverse))
            if (fx and old_fx and Decimal(old_fx) > 0 and Decimal(fx['price']) > 0
                    and fx['currency'] == base):
                fx_factor = Decimal(fx['price']) / Decimal(old_fx)
                fx_count += 1
                used_fx = fx['observed_at']
                notes.append('Recent FX applied to dated UBS value')
            else:
                notes.append('FX retained from UBS; recent compatible rate unavailable')
        estimated *= price_factor * fx_factor
        estimate += estimated
        rows.append({'id': p['id'], 'description': p['description'], 'currency': ccy,
                     'broker_value': str(value), 'indicative_value': str(estimated),
                     'valuation_change': str(estimated - value),
                     'snapshot_unrealized_pnl': None if pnl is None else str(pnl),
                     'price_observed_at': used_price, 'fx_observed_at': used_fx,
                     'notes': notes or ['UBS value retained']})
    currencies = []
    for ccy, values in sorted(allocation.items()):
        currencies.append({'currency': ccy, **{k: str(v) for k, v in values.items()},
                           'weight_percent': str(values['market_value'] / total * 100) if total else None})
    return {'as_of': snapshot['as_of'], 'valuation_currency': base,
            'broker_market_value': str(total), 'indicative_market_value': str(estimate),
            'valuation_change': str(estimate - total),
            'snapshot_unrealized_pnl': str(covered_pnl) if cost_count else None,
            'cost_covered_value': str(covered_value), 'cost_covered_count': cost_count,
            'position_count': len(rows), 'recent_price_count': price_count,
            'recent_fx_count': fx_count, 'currencies': currencies, 'positions': rows,
            'daily_pnl': None, 'realized_pnl': None,
            'limitations': [
                'Indicative mixed-date valuation uses dated holdings; later trades are not applied.',
                'Equity price ratios assume the same listing, units and no corporate actions.',
                'Snapshot unrealized P/L is market value minus UBS cost value in valuation currency, excludes accrued interest and covers only rows with cost values.',
                'Valuation change is not daily P/L. Realized and daily P/L need fills, cash flows and opening valuations.',
                'Currency allocation uses denomination, not derivative delta or underlying currency risk.',
                'Accrued interest is excluded from totals and is not refreshed.',
            ]}
