import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from src.history import HistoryStore


class MarketHistoryTests(unittest.TestCase):
    def test_identifier_correction_preserves_units_and_audit_trail(self):
        from src.historical_terms import HistoricalTermsStore
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite3';store=HistoricalTermsStore(path)
            store.record([dict(key='DE000F210WV0',security='/isin/DE000F210WV0',unit_scale='100',basis='Original net-cash terms',review_required=True)])
            with self.assertRaises(ValueError):store.correct_identifier('DE000F210WV0','SAP GR 09/18/26 C175 Equity','wrong','Verified reference data')
            self.assertEqual(store.correct_identifier('DE000F210WV0','SAP GR 09/18/26 C175 Equity','/isin/DE000F210WV0','Verified reference data')['corrected'],1)
            self.assertEqual(store.all()['DE000F210WV0']['unit_scale'],'100')
            self.assertTrue(store.all()['DE000F210WV0']['review_required'])
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM historical_identifier_corrections').fetchone()[0],1)

    def test_exchange_specific_equity_history_requires_requested_identifier(self):
        from src.historical_terms import HistoricalTermsStore, supported_market_security
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite3'
            mapping=dict(key='US5949181045',security='MSFT US Equity',unit_scale='1',
                         basis='Bloomberg reference data confirms USD and matching ISIN',review_required=False)
            self.assertEqual(HistoricalTermsStore(path).record([mapping])['added'],1)
            payload={'quotes':[dict(security='MSFT US Equity',date='2026-06-30',price='100')]}
            with self.assertRaises(ValueError):HistoryStore(path).ingest(payload,[])
            self.assertEqual(HistoryStore(path).ingest(payload,[dict(security='MSFT US Equity',dates=['2026-06-30'])])['accepted'],1)
            self.assertFalse(supported_market_security('MSFT US Equity; arbitrary'))

    def test_bloomberg_valuation_fx_preserves_official_funding_fixing(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE fx_history (security TEXT,date TEXT,price TEXT,source TEXT,PRIMARY KEY(security,date))')
                db.execute('INSERT INTO fx_history VALUES (?,?,?,?)',('USDCZK Curncy','2026-09-30','22','CNB official daily fixing'))
            store=HistoryStore(path)
            requests=[{'security':'USDCZK Curncy','dates':['2026-09-30']}]
            payload={'quotes':[{'security':'USDCZK Curncy','date':'2026-09-30','price':'21.9'}]}
            self.assertEqual(store.ingest(payload,requests)['accepted'],1)
            self.assertEqual(store.ingest(payload,requests)['existing'],1)
            self.assertEqual(store.rates()[('USDCZK Curncy','2026-09-30')],Decimal('22'))
            observation=store.observations()[('USDCZK Curncy','2026-09-30')]
            self.assertEqual(observation['price'],Decimal('21.9'))
            self.assertTrue(observation['source'].startswith('Bloomberg'))
            payload['quotes'][0]['price']='21.8'
            with self.assertRaises(ValueError):store.ingest(payload,requests)
            self.assertEqual(store.observations()[('USDCZK Curncy','2026-09-30')]['price'],Decimal('21.9'))
