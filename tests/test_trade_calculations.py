import copy
import unittest
from decimal import Decimal as D
from src.trade_calculations import calculate_trades


class TradeCalculationTests(unittest.TestCase):
    def setUp(self):
        self.base={'as_of':'2025-12-31','positions':[
            dict(key='X',kind='security',currency='USD',description='Stock',quantity='10',market_value_native='1000',accrued_native='0'),
            dict(key='cash',kind='cash_account',currency='USD',quantity='1000',market_value_native='1000',accrued_native='0')]}
        self.snapshot={'as_of':'2026-11-04','valuation_currency':'USD','exchange_rates':{'USD/CZK':'2'},'positions':[
            dict(id='X',isin='X',kind='security',group='Equities - Equity investments',currency='USD',description='Stock',quantity_or_balance='5',market_value='600',market_price={'value':'120','convention':'amount'},accrued_interest='0'),
            dict(id='cash',kind='cash_account',group='Cash',currency='USD',description='Cash',quantity_or_balance='1650',market_value='1650',accrued_interest='0')]}
        self.rows=[self.trade('sell','2026-02-01','Sale Spot','10','1200'),self.trade('buy','2026-03-01','Purchase Spot','5','-550')]
        self.accounting={'ytd_pnl_czk':'500','nav_bridge':{'opening_nav_czk':'4000','net_external_flows_czk':'0','settlement_bases':[]}}

    def trade(self,ref,day,kind,q,amount):
        return dict(id=ref,transaction_no=ref,category='security_trade',isin='X',valor=None,currency='USD',description='Stock',
                    quantity=q,amount=amount,trade_date=day,booking_date=day,trade_time=None,base_type=kind,reversal=False)

    def run_calc(self):return calculate_trades(self.snapshot,self.base,{'ledger':self.rows},self.accounting)

    def test_closed_and_reopened_positions_do_not_share_old_basis(self):
        r=self.run_calc();self.assertEqual(r['issues'],[])
        self.assertEqual(D(r['open_subtotal_czk']),100)
        self.assertEqual(D(r['closed_subtotal_czk']),400)
        self.assertEqual(D(r['reconciliation_difference_czk']),0)

    def test_partial_closes_allocate_average_net_cost(self):
        self.base['positions'][0].update(quantity='0',market_value_native='0')
        self.rows=[self.trade('b1','2026-01-01','Buy to open','10','-1010'),self.trade('b2','2026-01-01','Buy to open','10','-990'),
                   self.trade('s1','2026-02-01','Sell to close','10','1100'),self.trade('s2','2026-03-01','Sell to close','10','1200')]
        self.snapshot['positions']=self.snapshot['positions'][1:];self.snapshot['positions'][0]['quantity_or_balance']='1300'
        self.accounting['nav_bridge']['opening_nav_czk']='2000';self.accounting['ytd_pnl_czk']='600'
        r=self.run_calc();self.assertEqual(r['issues'],[])
        self.assertEqual([D(p['pnl_native']) for p in r['positions']],[D(100),D(200)])

    def test_income_fees_and_margin_are_separate_and_counted_once(self):
        for ref,cat,amount in [('i','income','20'),('f','fees','-3'),('m','futures_margin','7')]:
            self.rows.append(self.trade(ref,'2026-03-02','Cash','0',amount)|{'category':cat})
        self.snapshot['positions'][1]['quantity_or_balance']='1674';self.accounting['ytd_pnl_czk']='548'
        r=self.run_calc();self.assertEqual(r['issues'],[])
        self.assertEqual(D(r['components']['income_czk']),40)
        self.assertEqual(D(r['components']['fees_czk']),-6)
        self.assertEqual(D(r['components']['pooled_margin_czk']),14)

    def test_future_transactions_never_enter_selected_date(self):
        before=self.run_calc()
        self.rows.append(self.trade('future','2026-11-05','Purchase Spot','2','-240'))
        self.assertEqual(before,self.run_calc())

    def test_unsettled_trade_uses_trade_date_basis_and_pending_nav(self):
        self.rows[1]['booking_date']='2026-11-05'
        self.snapshot['positions'][1]['quantity_or_balance']='2200'
        r=self.run_calc();self.assertEqual(r['issues'],[])
        self.assertEqual(D(r['ytd_pnl_czk']),500)

    def test_valuation_fx_matches_excel_not_opening_fx(self):
        self.snapshot['exchange_rates']['USD/CZK']='3';self.accounting['ytd_pnl_czk']='2750'
        r=self.run_calc();self.assertEqual(r['issues'],[])
        self.assertEqual(D(next(p['pnl_czk'] for p in r['positions'] if p['status']=='Open')),150)
        self.assertEqual(D(r['components']['portfolio_fx_czk']),2000)

    def test_missing_fx_blocks_total(self):
        self.snapshot['exchange_rates']={};r=self.run_calc()
        self.assertIsNone(r['ytd_pnl_czk']);self.assertTrue(r['issues'])

    def test_zero_cash_balance_does_not_require_unused_fx(self):
        self.base['positions'].append(dict(key='CHF',kind='cash_account',currency='CHF',quantity='0',market_value_native='0',accrued_native='0'))
        self.assertEqual(self.run_calc()['issues'],[])

    def test_inputs_are_not_mutated(self):
        original=copy.deepcopy((self.snapshot,self.base,self.rows,self.accounting));self.run_calc()
        self.assertEqual(original,(self.snapshot,self.base,self.rows,self.accounting))

