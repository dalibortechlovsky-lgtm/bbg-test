import copy
from decimal import Decimal
import unittest
from src.analytics import portfolio_analytics
from src.positions import parse_positions
from tests.test_positions import sample


def quote(security, price, currency='USD', stale=False, observed='2026-10-14T10:00:00+00:00'):
    return {'security': security, 'price': price, 'currency': currency,
            'stale': stale, 'observed_at': observed}


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = parse_positions(sample())

    def test_broker_reconciliation_and_no_mutation(self):
        before = copy.deepcopy(self.snapshot)
        result = portfolio_analytics(self.snapshot, {'quotes': []})
        self.assertEqual(result['broker_market_value'], '12837')
        self.assertEqual(result['indicative_market_value'], '12837')
        self.assertEqual(result['currencies'][0]['cash_balance'], '12.34')
        self.assertEqual(result['currencies'][0]['deposit_balance'], '3000')
        self.assertEqual(result['valuation_change'], '0')
        self.assertIsNone(result['snapshot_unrealized_pnl'])
        self.assertIsNone(result['daily_pnl'])
        self.assertIsNone(result['realized_pnl'])
        self.assertEqual(self.snapshot, before)

    def test_equity_and_fx_ratios_cost_coverage(self):
        equity = self.snapshot['positions'][-1]
        equity.update(group='Equities - Equity investments', isin='TEST', currency='EUR',
                      market_price={'value': '3.5', 'convention': 'amount'}, cost_value='600')
        quotes = [quote('/isin/TEST', '7', 'EUR'), quote('EURUSD Curncy', '1.232')]
        result = portfolio_analytics(self.snapshot, {'quotes': quotes})
        row = result['positions'][-1]
        self.assertEqual(Decimal(row['indicative_value']), Decimal('1540'))
        self.assertEqual(row['snapshot_unrealized_pnl'], '100')
        self.assertEqual(result['recent_price_count'], 1)
        self.assertEqual(result['recent_fx_count'], 1)
        self.assertEqual(result['cost_covered_count'], 1)
        self.assertEqual(result['snapshot_unrealized_pnl'], '100')

    def test_stale_wrong_currency_nonpositive_and_unsupported(self):
        equity = self.snapshot['positions'][-1]
        equity.update(group='Equities - Equity investments', isin='TEST')
        for q in [quote('/isin/TEST','7',stale=True), quote('/isin/TEST','7','EUR'),
                  quote('/isin/TEST','0'), quote('/isin/TEST','-1')]:
            with self.subTest(q=q):
                result = portfolio_analytics(self.snapshot, {'quotes':[q]})
                self.assertEqual(result['valuation_change'], '0')
                self.assertEqual(result['recent_price_count'], 0)
        equity['group'] = 'Equities - Structured products with equity character'
        result = portfolio_analytics(self.snapshot, {'quotes':[quote('/isin/TEST','7'),
                    quote('/isin/TEST_ISIN','100')]})
        self.assertEqual(result['valuation_change'], '0')  # No option/bond scale guesses.

    def test_inverse_fx_signed_allocation_and_zero_total(self):
        s = self.snapshot
        s['exchange_rates'] = {'USD/EUR': '0.5'}
        s['positions'] = [copy.deepcopy(s['positions'][0]), copy.deepcopy(s['positions'][0])]
        s['positions'][0].update(currency='EUR', market_value='100')
        s['positions'][1].update(market_value='-100')
        r = portfolio_analytics(s, {'quotes':[quote('EURUSD Curncy','3')]})
        self.assertEqual(Decimal(r['indicative_market_value']), Decimal('50'))
        self.assertTrue(all(c['weight_percent'] is None for c in r['currencies']))
        self.assertEqual(r['currencies'][1]['market_value'], '-100')

    def test_newest_fresh_device_quote_and_empty(self):
        self.assertIsNone(portfolio_analytics(None, {}))
        p = self.snapshot['positions'][-1]
        p.update(group='Equities - Equity investments', isin='TEST')
        q = [quote('/isin/TEST','14',observed='2026-10-14T09:00:00+00:00'),
             quote('/isin/TEST','7'), quote('/isin/TEST','21',stale=True)]
        r = portfolio_analytics(self.snapshot, {'quotes':q})
        self.assertEqual(Decimal(r['positions'][-1]['indicative_value']), Decimal('1400'))
