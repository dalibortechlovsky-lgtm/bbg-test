import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal as D
from src.external_deposits import opening, closing, native_positions, disclose
from src.accounting import calculate_accounting
from src.trade_calculations import calculate_trades


class ExternalDepositTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Path(self.temp.name)/'db'
        # Deliberately synthetic terms, not private portfolio data.
        term=dict(id='external-test',bank='Example bank',principal='1000',annual_rate='0.0365',
                  currency='USD',opening_as_of='2025-12-31',interest_start='2026-01-01',until='2026-12-31')
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE external_deposits(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT INTO external_deposits VALUES (?,?)',(term['id'],json.dumps(term)))
        self.base=dict(as_of='2025-12-31',portfolio='1',valuation_currency='USD',exchange_rates={'USD/CZK':'20'},positions=[])
        self.snap=dict(as_of='2026-06-30',portfolio='1',valuation_currency='CZK',exchange_rates={'USD/CZK':'21'},positions=[],summary={'market_value':'0','accrued_interest':'0'})
    def tearDown(self):self.temp.cleanup()
    def test_interest_boundaries_and_noncompounding(self):
        self.assertEqual(native_positions(self.db,'2025-12-31')[0]['accrued_native'],'0.0000')
        self.assertEqual(D(native_positions(self.db,'2026-01-01')[0]['accrued_native']),D('.1'))
        self.assertEqual(D(native_positions(self.db,'2026-06-30')[0]['accrued_native']),D('18.1'))
        self.assertEqual(D(native_positions(self.db,'2026-12-31')[0]['accrued_native']),D('36.5'))
        self.assertEqual(native_positions(self.db,'2027-01-01'),[])
    def test_nav_funding_income_fx_reconcile_without_mutating_bank(self):
        b=opening(self.db,self.base);s=closing(self.db,self.snap)
        self.assertEqual(b,opening(self.db,b));self.assertEqual(s,closing(self.db,s))
        self.assertEqual(self.base['positions'],[]);self.assertEqual(self.snap['positions'],[])
        a=calculate_accounting(s,b,{'ledger':[],'accounts':[]})
        a=disclose(self.db,s,b,a)
        self.assertEqual(D(a['nav_bridge']['net_external_flows_czk']),0)
        self.assertEqual(D(a['nav_bridge']['opening_nav_czk']),20000)
        self.assertEqual(D(a['nav_bridge']['reported_opening_nav_czk']),0)
        self.assertEqual(D(a['nav_czk']),D('21380.1'))
        t=calculate_trades(s,b,{'ledger':[]},a)
        self.assertEqual(t['issues'],[])
        self.assertEqual(D(t['components']['income_czk']),D('380.1'))
        self.assertEqual(D(t['components']['portfolio_fx_czk']),1000)
        self.assertEqual(D(t['components']['other_balances_czk']),0)
        self.assertEqual(D(t['ytd_pnl_czk']),D('1380.1'))
    def test_missing_fx_does_not_value_external_deposit_as_zero(self):
        self.snap['exchange_rates']={}
        with self.assertRaisesRegex(ValueError,'valuation FX'):closing(self.db,self.snap)

    def test_registration_is_idempotent_and_rejects_conflicts(self):
        from src.record_external_deposit import record
        path=Path(self.temp.name)/'registration'
        term=dict(id='external-new',bank='Example',principal='1000',annual_rate='0.0365',currency='USD',
                  opening_as_of='2025-12-31',interest_start='2026-01-01',until='2026-12-31',source='Synthetic owner confirmation')
        self.assertTrue(record(path,term));self.assertFalse(record(path,term))
        with self.assertRaises(ValueError):record(path,term|{'principal':'2000'})

    def test_live_and_historical_use_same_deposit_and_block_missing_fx(self):
        from test_historical_positions import HistoricalPositionTests, ISIN, ActivityStore, account_csv, HistoryStore, SnapshotStore, view
        from src.market_performance import live_performance
        from src.record_external_deposit import record
        from datetime import date
        fixture=HistoricalPositionTests();fixture.setUp()
        try:
            record(fixture.db,dict(id='external-integration',bank='Example',principal='1000',annual_rate='0.0365',currency='USD',
                opening_as_of='2025-12-31',interest_start='2026-01-01',until='2026-12-31',source='Synthetic owner confirmation'))
            ActivityStore(fixture.db).import_content(account_csv([],opening='100',start='2026-01-01',until='2026-01-05'))
            quotes=[dict(security=s,price=p,currency=c,stale=False,observed_at='2026-01-05T12:00:00Z') for s,p,c in [('/isin/'+ISIN,'90','USD'),('USDCZK Curncy','21','CZK')]]
            HistoryStore(fixture.db).ingest({'quotes':[dict(security=q['security'],price=q['price'],date='2026-01-05') for q in quotes]},[dict(security=q['security'],dates=['2026-01-05']) for q in quotes])
            historical=view(SnapshotStore(fixture.db),'2026-01-05')['accounting']
            live=live_performance(fixture.db,{'quotes':quotes},date(2026,1,5))
            self.assertEqual(historical['trade_calculations']['issues'],[])
            self.assertEqual(live['accounting']['ytd_pnl_czk'],historical['ytd_pnl_czk'])
            self.assertEqual(D(historical['external_deposits'][0]['interest_native']),D('.5'))
            self.assertEqual(D(historical['nav_bridge']['opening_nav_czk']),23600)
            self.assertEqual(D(historical['nav_bridge']['reported_opening_nav_czk']),3600)
            quotes[-1]['stale']=True
            self.assertIsNone(live_performance(fixture.db,{'quotes':quotes},date(2026,1,5))['indicative']['estimated_ytd_pnl_czk'])
        finally:fixture.tearDown()
