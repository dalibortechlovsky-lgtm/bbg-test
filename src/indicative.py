"""Non-booked Bloomberg estimate anchored to a dated accounting result."""
from datetime import date
from decimal import Decimal
import re

from .accounting import closing_positions, identity, signed_quantity
from .report import fx_rate

D = Decimal


def indicative_ytd(snapshot, accounting, bloomberg, terms=None, activity=None, today=None):
    if not snapshot:
        return None
    today = today or date.today()
    terms = terms or {}
    accounting = accounting or {}
    fresh = {}
    for quote in bloomberg.get('quotes', []):
        if quote.get('stale', True):
            continue
        key = quote['security']
        if key not in fresh or quote['observed_at'] > fresh[key]['observed_at']:
            fresh[key] = quote
    rates = {}
    for security, quote in fresh.items():
        if re.fullmatch('[A-Z]{6} Curncy', security) and quote['currency'] == security[3:6] and D(quote['price']) > 0:
            rates[security[:3] + '/' + security[3:6]] = quote['price']
    market_fx = {'exchange_rates': rates}
    rows, warnings, blockers, used_times = [], [], [], []
    change = D(0)
    price_count = fx_count = security_count = 0
    fallback_count = 0
    marks = {}
    for item in closing_positions(snapshot):
        p = item['position']; currency = item['currency']
        native = item['value']
        old_fx = fx_rate(snapshot, currency, 'CZK')
        new_fx = fx_rate(market_fx, currency, 'CZK')
        notes = []
        if new_fx is None:
            new_fx = old_fx
            notes.append('FX retained from ' + snapshot['as_of'])
        elif currency != 'CZK':
            fx_count += 1
        if native is None or old_fx is None or new_fx is None:
            blockers.append(p['description'] + ': missing anchor value or CZK FX')
            rows.append({'description': p['description'], 'currency': currency,
                         'change_czk': None, 'price_observed_at': None, 'notes': notes + ['Value unavailable']})
            continue
        estimated_native = native
        used_price = None
        if p['kind'] == 'security':
            security_count += 1
            term = terms.get(identity(p), {})
            security = term.get('security') or ('/isin/' + p['isin'] if p.get('isin') else None)
            quote = fresh.get(security)
            # Use the broker-corroborated unit scale, not a guessed multiplier.
            scale = item['scale']
            if term.get('unit_scale') is not None and item['price'] is not None:
                proposed = D(term['unit_scale'])
                clean = native - D(p.get('accrued_interest') or '0') / fx_rate(snapshot, currency, snapshot['valuation_currency'])
                if abs(item['quantity'] * item['price'] * proposed - clean) <= max(D('0.01'), abs(clean) * D('0.00001')):
                    scale = proposed
            if (quote and quote.get('currency') == currency and D(quote['price']) >= 0
                    and item['price'] is not None and item['price'] > 0 and scale is not None
                    and not p['description'].casefold().startswith('future ')):
                estimated_native += item['quantity'] * scale * (D(quote['price']) - item['price'])
                used_price = quote['observed_at']; used_times.append(used_price); price_count += 1
                marks[identity(p)] = (currency, D(quote['price']), scale, new_fx)
                notes.append('Bloomberg price; unchanged listing, units and quantity assumed')
                if term.get('review_required'):
                    notes.append('Instrument mapping requires review: ' + term.get('basis', ''))
            else:
                fallback_count += 1
                notes.append('Price retained from ' + snapshot['as_of'] + '; fresh compatible quote or verified units missing')
        delta = estimated_native * new_fx - native * old_fx
        change += delta
        rows.append({'description': p['description'], 'currency': currency,
                     'change_czk': str(delta), 'price_observed_at': used_price, 'notes': notes})
    if fx_count:
        used_times.extend(q['observed_at'] for s, q in fresh.items()
                          if re.fullmatch('[A-Z]{6} Curncy', s) and s[:3] + '/' + s[3:6] in rates)
    anchor_day = date.fromisoformat(snapshot['as_of'])
    if anchor_day.year != today.year or anchor_day > today:
        blockers.append('A reconciled snapshot in the current calendar year is required for this YTD estimate.')
    later = [r for r in (activity or {}).get('ledger', [])
             if any(snapshot['as_of'] < (r.get(k) or '') <= today.isoformat() for k in ('trade_date', 'booking_date'))]
    applied = []
    reduced_principal = {}
    # Apply settled ledger changes separately from anchor holdings. Net trade
    # cash already contains costs: adding it once avoids double-counting fees.
    for record in later:
        reason = None
        delta = D(0)
        category = record.get('category')
        trade_day, booked = record.get('trade_date'), record.get('booking_date')
        currency = record.get('currency')
        if not trade_day or not booked or not snapshot['as_of'] < trade_day <= booked <= today.isoformat():
            reason = 'settlement crosses the anchor or is incomplete'
        elif record.get('reversal'):
            reason = 'reversal needs reconciliation against its original entry'
        elif category == 'security_trade':
            key = record.get('isin') or ('VALOR:' + record['valor'] if record.get('valor') else None)
            mark = marks.get(key)
            if (not mark or mark[0] != currency or record.get('price_currency') != currency
                    or not record.get('base_type', '').startswith(('Purchase', 'Buy', 'Sale', 'Sell'))
                    or record.get('quantity') is None or record.get('amount') is None
                    or record.get('accrued') not in (None, '', '0')):
                reason = 'trade requires a fresh compatible mark, verified units and cash basis'
            else:
                quantity = signed_quantity(record)
                cash = D(record['amount'])
                if quantity * cash >= 0:
                    reason = 'trade quantity and cash signs are inconsistent'
                else:
                    delta = (quantity * mark[1] * mark[2] + cash) * mark[3]
        elif category == 'deposit_principal':
            # A pure principal transfer from an existing same-currency deposit
            # into cash changes neither NAV nor P/L. Never treat it as income.
            deposits = [p for p in snapshot['positions'] if p['kind'] == 'call_deposit' and p['currency'] == currency]
            amount = D(record.get('amount') or '0')
            quantity = D(record.get('quantity') or '0')
            cumulative = reduced_principal.get(currency, D(0)) + quantity
            if (len(deposits) != 1 or not record.get('base_type', '').startswith('Reduction')
                    or record.get('quantity_currency') != currency
                    or amount <= 0 or amount != quantity or cumulative > D(deposits[0]['quantity_or_balance'])):
                reason = 'deposit transfer cannot be matched to anchor principal'
            else:
                reduced_principal[currency] = cumulative
        else:
            reason = 'transaction category requires reconciliation'
        if reason:
            blockers.append('Post-anchor recorded transactions: ' + str(record.get('transaction_no') or record.get('id') or category) + ': ' + reason + '.')
        else:
            change += delta
            applied.append({'reference': record.get('transaction_no') or record.get('id'),
                            'category': category, 'change_czk': str(delta)})
    if not used_times:
        blockers.append('No fresh Bloomberg prices or FX are available.')
    if accounting.get('nav_czk') is None or accounting.get('ytd_pnl_czk') is None:
        blockers.append('The anchor has no calculable NAV and YTD P/L; opening balances and funding records are required.')
    warnings.extend([
        'Includes supported recorded transactions after ' + snapshot['as_of'] + '; assumes no unrecorded trades, external cash flows or corporate actions.',
        'Accrued interest is carried from the anchor; later income, fees, tax and financing are not estimated.',
        'Bloomberg receipt times are not exchange trade timestamps. Missing prices and FX retain their anchor values.',
        'This estimate covers the UBS portfolio only. Other banks are excluded.',
    ])
    if not accounting.get('portfolio_pnl_verified'):
        warnings.append('The starting accounting result is provisional; its reconciliation issues also apply here.')
    available = not blockers
    return {'status': 'Indicative · known holdings and recorded activity' if available else 'Estimate unavailable',
            'applied_transactions': applied,
            'anchor_as_of': snapshot['as_of'], 'as_of': today.isoformat(),
            'quote_time_from': min(used_times) if used_times else None,
            'quote_time_to': max(used_times) if used_times else None,
            'anchor_ytd_pnl_czk': accounting.get('ytd_pnl_czk'),
            'estimated_ytd_pnl_czk': str(D(accounting['ytd_pnl_czk']) + change) if available else None,
            'estimated_nav_czk': str(D(accounting['nav_czk']) + change) if available else None,
            'market_change_czk': str(change) if available else None,
            'fresh_price_count': price_count, 'security_count': security_count,
            'fresh_fx_position_count': fx_count, 'fallback_price_count': fallback_count,
            'positions': rows, 'warnings': warnings, 'blockers': blockers,
            'basis': 'Anchor YTD P/L + price and FX change on anchor holdings + marked value and net cash of supported recorded transactions. Realized P/L is not added again.'}
