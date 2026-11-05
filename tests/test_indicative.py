import copy
from datetime import date
from decimal import Decimal as D
import unittest

from src.indicative import indicative_ytd


def quote(security, price, currency='USD', stale=False):
    return dict(security=security, price=price, currency=currency, stale=stale,
                observed_at='2026-11-05T09:00:00+00:00')


class IndicativeTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'as_of':'2026-10-14', 'valuation_currency':'USD',
                         'exchange_rates':{'USD/CZK':'20'}, 'positions':[
            dict(id='a', isin='US0000000001', currency='USD', kind='security',
                 group='Equities - Equity investments', description='Stock',
                 quantity_or_balance='10', market_value='1000', accrued_interest='0',
                 market_price={'value':'100','convention':'amount'})]}
        self.accounting = dict(nav_czk='20000', ytd_pnl_czk='2000', portfolio_pnl_verified=True)

    def calc(self, quotes, **kwargs):
        return indicative_ytd(self.snapshot, self.accounting, {'quotes':quotes}, today=date(2026,11,5), **kwargs)

    def test_price_fx_interaction_and_no_double_realized(self):
        before=copy.deepcopy(self.snapshot)
        r=self.calc([quote('/isin/US0000000001','110'),quote('USDCZK Curncy','22','CZK')])
        self.assertEqual(D(r['estimated_nav_czk']), D(24200))
        self.assertEqual(D(r['estimated_ytd_pnl_czk']),D(6200))
        self.assertEqual(self.snapshot,before)

    def test_bond_percent_and_accrued_not_price_revalued(self):
        self.snapshot['positions'][0].update(group='Bonds', quantity_or_balance='1000',
            market_value='950',accrued_interest='10',market_price={'value':'95','convention':'percent'})
        r=self.calc([quote('/isin/US0000000001','100')])
        self.assertEqual(D(r['market_change_czk']), D(1000))

    def test_option_multiplier(self):
        self.snapshot['positions'][0].update(description='Call 1:100',quantity_or_balance='2',
            market_value='20000')
        r=self.calc([quote('/isin/US0000000001','110')])
        self.assertEqual(D(r['market_change_czk']),D(40000))

    def test_audited_valor_mapping(self):
        self.snapshot['positions'][0].update(isin=None,valor='123',description='Option',
            quantity_or_balance='2',market_value='20000')
        r=self.calc([quote('ABC US 12/18/26 C100 Equity','110')],
                    terms={'VALOR:123':{'security':'ABC US 12/18/26 C100 Equity','unit_scale':'100'}})
        self.assertEqual(r['fresh_price_count'],1)

    def test_missing_price_retains_value_and_uses_fx(self):
        r=self.calc([quote('USDCZK Curncy','22','CZK')])
        self.assertEqual(D(r['estimated_ytd_pnl_czk']),D(4000))
        self.assertEqual(r['fallback_price_count'],1)

    def test_stale_quotes_do_not_look_like_zero_pnl(self):
        r=self.calc([quote('/isin/US0000000001','110',stale=True)])
        self.assertIsNone(r['estimated_ytd_pnl_czk'])

    def test_wrong_currency_and_unknown_scale_retain_price(self):
        for change in ({}, {'market_value':'5000'}):
            self.snapshot['positions'][0].update(change)
            r=self.calc([quote('/isin/US0000000001','110','EUR'),quote('USDCZK Curncy','22','CZK')])
            self.assertEqual(r['fresh_price_count'],0)

    def test_cash_and_accrued_fx(self):
        self.snapshot['positions'][0].update(kind='cash_account',quantity_or_balance='1000',accrued_interest='10')
        r=self.calc([quote('USDCZK Curncy','22','CZK')])
        self.assertEqual(D(r['market_change_czk']),D(2020))

    def test_inverse_and_cross_fx(self):
        r=self.calc([quote('CZKUSD Curncy','0.05'),quote('/isin/US0000000001','110')])
        self.assertEqual(D(r['market_change_czk']),D(2000))

    def test_new_activity_blocks_stale_holdings_estimate(self):
        r=self.calc([quote('/isin/US0000000001','110')],activity={'ledger':[{'booking_date':'2026-10-15'}]})
        self.assertIsNone(r['estimated_ytd_pnl_czk'])
        self.assertIn('recorded transactions',r['blockers'][0])

    def test_no_anchor_and_year_boundary(self):
        self.accounting['ytd_pnl_czk']=None
        self.assertIsNone(self.calc([quote('/isin/US0000000001','110')])['estimated_ytd_pnl_czk'])
        self.accounting['ytd_pnl_czk']='2000';self.snapshot['as_of']='2025-12-31'
        self.assertIsNone(self.calc([quote('/isin/US0000000001','110')])['estimated_ytd_pnl_czk'])

    def test_no_snapshot(self):
        self.assertIsNone(indicative_ytd(None,None,{}))
