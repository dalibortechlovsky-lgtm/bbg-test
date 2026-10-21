from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.baseline import BaselineStore
from src.history import HistoryStore
from src.historical_positions import HistoricalRequestStore, _metadata, _scale, reconstruct, selection, view
from src.storage import SnapshotStore
from src.server import create_server
from src.ubs_activity import ActivityStore
from test_accounting import ISIN, STATEMENT, pdf_document
from test_positions import sample
from test_ubs_activity import account_csv, posting


class HistoricalPositionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Path(self.temp.name)/'frontoffice.sqlite3'
        BaselineStore(self.db).import_pdf(pdf_document(STATEMENT))

    def tearDown(self):self.temp.cleanup()

    def test_exact_snapshot_is_never_relabelled_as_reconstructed(self):
        store=SnapshotStore(self.db);store.import_content(sample())
        result=selection(store,'2026-10-08')
        self.assertEqual(result['selection'],'exact_snapshot')
        self.assertEqual(result['positions_as_of'],'2026-10-08')
        self.assertEqual(result['snapshot']['as_of'],'2026-10-08')

    def test_missing_market_data_keeps_valuation_and_pnl_unavailable(self):
        result=view(SnapshotStore(self.db),'2026-01-02')
        self.assertEqual(result['selection'],'reconstruction_pending')
        self.assertIsNone(result['accounting'])
        self.assertTrue(any('/isin/'+ISIN in r['security'] for r in result['market_requests']))
        self.assertTrue(any('USDCZK Curncy' in item for item in result['missing_market_data']))
        self.assertEqual(next(p for p in result['positions'] if p['key']==ISIN)['quantity'],'1')

    def test_activity_reconstructs_quantity_cash_and_bloomberg_valuation(self):
        buy=posting('-71',number='BUY',Description3='Number/Amt. 1; Transaction price: 70 USD')
        ActivityStore(self.db).import_batch([account_csv([buy],opening='100',start='2026-01-01',until='2026-01-05')])
        requests=[{'security':'/isin/'+ISIN,'dates':['2026-01-05']},
                  {'security':'USDCZK Curncy','dates':['2026-01-05']}]
        HistoryStore(self.db).ingest({'quotes':[
            {'security':'/isin/'+ISIN,'date':'2026-01-05','price':'90'},
            {'security':'USDCZK Curncy','date':'2026-01-05','price':'21'}]},requests)
        result=reconstruct(self.db,'2026-01-05')
        security=next(p for p in result['positions'] if p['key']==ISIN)
        cash=next(p for p in result['positions'] if p['kind']=='cash_account')
        self.assertEqual(Decimal(security['quantity']),2)
        self.assertEqual(Decimal(cash['quantity']),29)
        self.assertEqual(Decimal(result['snapshot']['summary']['market_value']),Decimal(209)*21)

    def test_request_is_auditable_and_idempotent(self):
        store=HistoricalRequestStore(self.db)
        store.request('2026-01-02');store.request('2026-01-02')
        self.assertEqual(store.dates(),['2026-01-02'])
        self.assertEqual({r['security'] for r in store.history_requests()},
                         {'/isin/'+ISIN,'USDCZK Curncy'})

    def test_later_ubs_snapshot_can_verify_contract_scale(self):
        content=(sample().replace('Call synthetic 1:100','Structured certificate')
                 .replace('Equities - Equity investments','Equities - Structured products with equity character'))
        SnapshotStore(self.db).import_content(content)
        metadata,_=_metadata(self.db,BaselineStore(self.db).latest('2026-10-08'))
        self.assertEqual(_scale(metadata['VALOR:TEST_OPTION']),Decimal(100))

    def test_future_and_prebaseline_dates_are_rejected_or_explicit(self):
        with self.assertRaises(ValueError):view(SnapshotStore(self.db),'2999-01-01')
        result=view(SnapshotStore(self.db),'2025-12-31')
        self.assertEqual(result['selection'],'reconstruction_pending')
        self.assertTrue(result['issues'])

    def test_owner_route_queues_request_and_get_is_read_only(self):
        server=create_server(0,self.db);threading.Thread(target=server.serve_forever,daemon=True).start()
        root='http://127.0.0.1:'+str(server.server_port);body=json.dumps({'as_of':'2026-01-02'}).encode()
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(root+'/api/positions/history',data=body,headers={'Content-Type':'application/json'}))
            self.assertEqual(error.exception.code,403)
            with urlopen(Request(root+'/api/positions/history',data=body,headers={
                    'Content-Type':'application/json','X-Historical-Action':'request'})) as response:
                self.assertEqual(json.load(response)['selection'],'reconstruction_pending')
            with urlopen(root+'/api/positions/history?as_of=2026-01-02') as response:
                self.assertEqual(json.load(response)['positions_as_of'],'2026-01-02')
            self.assertEqual(HistoricalRequestStore(self.db).dates(),['2026-01-02'])
        finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
