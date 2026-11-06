import unittest
from decimal import Decimal as D
from src.fee_breakdown import fee_breakdown

class FeeTests(unittest.TestCase):
    def test_cutoff_refund_interest_and_no_trade_double_count(self):
        snapshot={'as_of':'2026-06-30','valuation_currency':'USD','exchange_rates':{'USD/CZK':'20'}}
        def row(ref,category,amount,day='2026-06-30'):
            return dict(transaction_no=ref,category=category,amount=amount,booking_date=day,currency='USD',base_type='Custody Price',description='Charge')
        ledger=[row('a','fees','-100'),row('b','fees','20'),row('c','account_interest','-3'),row('d','security_trade','-1000'),row('e','fees','-999','2026-07-01')]
        r=fee_breakdown(snapshot,{'as_of':'2025-12-31'},{'ledger':ledger})
        self.assertEqual(D(r['total_czk']),1660)
        self.assertEqual(len(r['entries']),3)
        g=next(g for g in r['groups'] if g['category']=='Custody fees')
        self.assertEqual((g['charges'],g['credits'],g['net']),('100','20','80'))
    def test_missing_fx_never_returns_partial_total(self):
        r=fee_breakdown({'as_of':'2026-06-30','valuation_currency':'USD','exchange_rates':{}},{'as_of':'2025-12-31'}, {'ledger':[dict(transaction_no='x',category='fees',amount='-1',booking_date='2026-01-01',currency='EUR',description='Fee')]})
        self.assertIsNone(r['total_czk']);self.assertEqual(r['missing_fx'],['EUR'])
