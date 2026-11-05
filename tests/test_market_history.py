import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from src.history import HistoryStore


class MarketHistoryTests(unittest.TestCase):
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
