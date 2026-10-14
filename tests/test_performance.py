import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from src.performance import HEADERS, PerformanceStore, calculate_performance


def event(id,kind,quantity='',price='',fees='',amount='',day='2026-10-12',sequence=0,currency='USD'):
    return dict(zip(HEADERS,[id,day,str(sequence),kind,'TEST_ACCOUNT','TEST' if kind in {'OPENING','BUY','SELL'} else '',currency,
                            quantity,price,'1' if kind in {'OPENING','BUY','SELL'} else '',fees,amount,'Synthetic']))


def csv_events(events):
    out=StringIO(); w=csv.DictWriter(out,HEADERS); w.writeheader(); w.writerows(events); return out.getvalue()


class PerformanceTests(unittest.TestCase):
    def run_events(self,events):
        with tempfile.TemporaryDirectory() as d:
            store=PerformanceStore(Path(d)/'db')
            store.import_content(csv_events(events))
            return calculate_performance(store.events(),'2026-12-31')

    def test_average_cost_fees_short_and_crossing(self):
        r=self.run_events([event('o','OPENING','10','100','0'),event('b','BUY','10','120','20',sequence=1),
             event('s','SELL','5','130','5',sequence=2),event('ss','SELL','20','100','20',sequence=3),
             event('c','BUY','10','90','10',sequence=4),event('bb','BUY','5','100','5',sequence=5)])
        self.assertEqual([Decimal(x['amount']) for x in r['realized']],[Decimal('90'),Decimal('-180'),Decimal('40')])
        self.assertEqual(Decimal(r['totals']['USD']['realized']),Decimal('-50'))
        self.assertEqual(r['open_inventory'][0]['quantity'],'10')
        self.assertEqual(Decimal(r['open_inventory'][0]['average_cost_per_unit']),Decimal('96'))

    def test_missing_baseline_future_records_and_native_currencies(self):
        r=self.run_events([event('bad','SELL','3','100','0'),event('income','INCOME',amount='5',currency='EUR'),
                           event('future','INCOME',amount='100',day='2027-01-01')])
        self.assertFalse(r['inventory_complete'])
        self.assertFalse(r['realized'])
        self.assertEqual(r['totals'],{'EUR':{'realized':'0','income':'5','ytd_realized':'0','ytd_income':'5'}})
        self.assertIn('Missing opening',r['warnings'][0])

    def test_complete_close_flow_adjusted_daily_and_gap(self):
        r=self.run_events([event('v1','VALUATION',amount='1000',day='2026-10-09'),
                           event('f','CASHFLOW',amount='200',day='2026-10-10'),
                           event('i','INCOME',amount='30',day='2026-10-10'),
                           event('v2','VALUATION',amount='1300',day='2026-10-10'),
                           event('v3','VALUATION',amount='1200',day='2026-10-13')])
        self.assertEqual(r['periods'][0]['amount'],'100')
        self.assertEqual(r['periods'][0]['kind'],'daily')
        self.assertEqual(r['periods'][1]['kind'],'period')
        self.assertEqual(r['periods'][1]['days'],3)
        self.assertEqual(r['periods'][1]['amount'],'-100')

    def test_atomic_idempotent_conflict_and_ambiguous_order(self):
        with tempfile.TemporaryDirectory() as d:
            store=PerformanceStore(Path(d)/'db')
            original=[event('o','OPENING','0','0','0')]
            self.assertEqual(store.import_content(csv_events(original))['added'],1)
            self.assertEqual(store.import_content(csv_events(original))['existing'],1)
            for batch in [[event('new','BUY','1','10','0',sequence=1),event('o','OPENING','2','10','0')],
                          [event('new','BUY','1','10','0')],
                          [event('o2','OPENING','0','0','0',sequence=1)],
                          [event('nan','BUY','1','NaN','0',sequence=1)]]:
                with self.assertRaises(ValueError): store.import_content(csv_events(batch))
                self.assertEqual(len(store.events()),1)

    def test_contract_multiplier_and_missing_trade_cost(self):
        events=[event('o','OPENING','-2','5','0'),event('b','BUY','1','3','2',sequence=1)]
        for e in events: e['multiplier']='100'
        r=self.run_events(events)
        self.assertEqual(r['realized'][0]['amount'],'198')
        self.assertEqual(r['open_inventory'][0]['quantity'],'-1')

    def test_fractional_average_cost_round_trip_preserves_pnl(self):
        events=[event('o','OPENING','0','0','0'),
                event('b1','BUY','10','3.21','0',sequence=1),
                event('b2','BUY','3','4.56','0',sequence=2),
                event('b3','BUY','4','5.67','0',sequence=3),
                event('s','SELL','17','8.9','0',sequence=4)]
        result=self.run_events(events)
        # Expected proceeds less summed acquisition cost, independent of
        # the engine's rounded weighted-average representation.
        expected=Decimal('17')*Decimal('8.9')-(Decimal('10')*Decimal('3.21')+Decimal('3')*Decimal('4.56')+Decimal('4')*Decimal('5.67'))
        self.assertEqual(Decimal(result['totals']['USD']['realized']).quantize(Decimal('.01')),expected)
        self.assertFalse(result['open_inventory'])
