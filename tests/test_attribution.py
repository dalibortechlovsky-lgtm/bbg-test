from decimal import Decimal
import unittest
from src.attribution import attribute_currency


class AttributionTests(unittest.TestCase):
    def test_local_gain_fx_loss_and_interaction_reconcile(self):
        r=attribute_currency('USD','100','110','25','24')
        self.assertEqual(Decimal(r['native_asset_pnl']),10)
        self.assertEqual(Decimal(r['asset_contribution']),250)
        self.assertEqual(Decimal(r['currency_contribution']),-110)
        self.assertEqual(Decimal(r['total_pnl']),140)
        self.assertEqual(Decimal(r['asset_contribution'])+Decimal(r['currency_contribution']),Decimal(r['closing_value'])-Decimal(r['opening_value']))

    def test_mid_period_funding_is_not_profit(self):
        r=attribute_currency('USD','0','110','25','23',[{'amount':'100','fx':'24'}])
        # End value 2,530 less actual deposit value 2,400 = total profit 130.
        self.assertEqual(Decimal(r['total_pnl']),130)
        self.assertEqual(Decimal(r['asset_contribution']),250)
        self.assertEqual(Decimal(r['currency_contribution']),-120)
        self.assertEqual(Decimal(r['native_asset_pnl']),10)

    def test_withdrawal_and_report_currency_have_no_fake_fx(self):
        r=attribute_currency('USD','100','80','25','24',[{'amount':'-30','fx':'25'}])
        self.assertEqual(Decimal(r['total_pnl']),170)
        self.assertEqual(Decimal(r['asset_contribution']),250)
        self.assertEqual(Decimal(r['currency_contribution']),-80)
        r=attribute_currency('CZK','100','160','1','1',[{'amount':'50','fx':'1'}])
        self.assertEqual(Decimal(r['total_pnl']),10)
        self.assertEqual(Decimal(r['currency_contribution']),0)

    def test_rejects_unknown_flow_rates_and_nonfinite_values(self):
        for flow in [[{'amount':'100'}],[{'amount':'100','fx':'0'}]]:
            with self.assertRaises(ValueError): attribute_currency('USD','0','100','25','24',flow)
        for value in ['NaN','Infinity']:
            with self.assertRaises(ValueError): attribute_currency('USD',value,'100','25','24')
        with self.assertRaises(ValueError): attribute_currency('CZK','100','110','25','24')
