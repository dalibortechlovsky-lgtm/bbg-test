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
from src.historical_terms import HistoricalTermsStore
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

    def test_exact_bank_snapshot_does_not_supply_market_prices(self):
        store=SnapshotStore(self.db);store.import_content(sample())
        result=selection(store,'2026-10-08')
        self.assertEqual(result['selection'],'reconstruction_pending')
        self.assertEqual(result['positions_as_of'],'2026-10-08')
        self.assertIsNone(result['snapshot'])
        self.assertTrue(result['missing_market_data'])

    def test_live_uses_fresh_quotes_and_never_historical_price_fallback(self):
        from datetime import date
        from src.market_performance import live_performance
        ActivityStore(self.db).import_content(account_csv([],opening='100',start='2026-01-01',until='2026-01-05'))
        quotes=[dict(security=s,price=p,currency=c,stale=False,observed_at='2026-01-05T12:00:00Z')
                for s,p,c in [('/isin/'+ISIN,'90','USD'),('USDCZK Curncy','21','CZK')]]
        current=live_performance(self.db,{'quotes':quotes},date(2026,1,5))
        self.assertIsNotNone(current['snapshot'])
        self.assertEqual(Decimal(next(p['price'] for p in current['reconstruction']['positions'] if p['key']==ISIN)),90)
        HistoryStore(self.db).ingest({'quotes':[{'security':'/isin/'+ISIN,'date':'2026-01-05','price':'999'}]},[{'security':'/isin/'+ISIN,'dates':['2026-01-05']}])
        quotes[0]['stale']=True
        stale=live_performance(self.db,{'quotes':quotes},date(2026,1,5))
        self.assertIsNone(stale['snapshot'])
        self.assertIsNone(stale['indicative']['estimated_ytd_pnl_czk'])
        self.assertEqual(stale['indicative']['fallback_price_count'],0)

    def test_missing_market_data_blocks_pnl_without_source_estimate(self):
        result=view(SnapshotStore(self.db),'2026-01-02')
        self.assertEqual(result['selection'],'reconstruction_pending')
        self.assertIsNone(result['accounting'])
        self.assertTrue(any('/isin/'+ISIN in r['security'] for r in result['market_requests']))
        self.assertTrue(any('USDCZK Curncy' in item for item in result['missing_market_data']))
        self.assertEqual(next(p for p in result['positions'] if p['isin']==ISIN)['quantity'],'1')

    def test_deposit_opened_after_year_end_is_retained(self):
        row=posting('-50',number='DEP',kind='New Investment Money Market',
            Description1='Fiduciary deposit Serial no. 1234567',
            Description3='Number/Amt. 50 USD; Transaction price: 100 %')
        ActivityStore(self.db).import_content(account_csv([row],opening='100'))
        result=reconstruct(self.db,'2026-01-05')
        deposit=next(p for p in result['positions'] if p['kind']=='call_deposit')
        self.assertEqual(deposit['quantity'],'50')

    def test_unsettled_purchase_estimate_includes_payable_once(self):
        buy=posting('-71',number='BUY',Description3='Number/Amt. 1; Transaction price: 70 USD')
        ActivityStore(self.db).import_content(account_csv([buy],opening='100'))
        requests=[{'security':s,'dates':['2026-01-03','2026-01-05']} for s in ['/isin/'+ISIN,'USDCZK Curncy']]
        HistoryStore(self.db).ingest({'quotes':[{'security':r['security'],'date':day,'price':'70' if r['security'].startswith('/isin/') else '20'} for r in requests for day in r['dates']]},requests)
        result=view(SnapshotStore(self.db),'2026-01-03')
        a=result['accounting']
        self.assertIsNotNone(a['ytd_pnl_czk'])
        self.assertEqual(Decimal(a['settlement_adjustment_czk']),Decimal('-1420'))
        self.assertFalse(a['portfolio_pnl_verified'])
        self.assertTrue(any('receivables/payables included' in issue for issue in a['issues']))
        settled=view(SnapshotStore(self.db),'2026-01-05')['accounting']
        self.assertNotIn('settlement_adjustment_czk',settled)

    def test_distant_maturity_coupon_schedule(self):
        from src.historical_positions import _coupon_accrued
        value=_coupon_accrued({'description':'Treasury 4.5% 15.11.2053'},'2026-09-30')
        self.assertGreater(value,0)
        self.assertLess(value,Decimal('2.25'))

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

    def test_fx_alone_does_not_enable_a_source_price_proxy(self):
        requests=[{'security':'USDCZK Curncy','dates':['2026-01-02']}]
        HistoryStore(self.db).ingest({'quotes':[{'security':'USDCZK Curncy','date':'2026-01-02','price':'21'}]},requests)
        result=view(SnapshotStore(self.db),'2026-01-02')
        self.assertEqual(result['selection'],'reconstruction_pending')
        self.assertIsNone(result['accounting'])
        self.assertTrue(any('Bloomberg PX_LAST' in item for item in result['missing_market_data']))

    def test_audited_option_terms_are_immutable(self):
        store=HistoricalTermsStore(self.db)
        row={'key':'VALOR:123','security':'SPX US 09/18/26 P7400 Index','unit_scale':'100',
             'basis':'Exact UBS/PTD date, side, quantity and price match.','review_required':False}
        self.assertEqual(store.record([row]),{'added':1,'existing':0})
        self.assertEqual(store.record([row]),{'added':0,'existing':1})
        with self.assertRaises(ValueError):store.record([row|{'unit_scale':'10'}])

    def test_request_is_auditable_and_idempotent(self):
        store=HistoricalRequestStore(self.db)
        store.request('2026-01-02');store.request('2026-01-02')
        self.assertEqual(store.dates(),['2026-01-02'])
        self.assertEqual({r['security'] for r in store.history_requests()},
                         {'/isin/'+ISIN,'USDCZK Curncy'})

    def test_multiple_dates_automatically_queue_price_and_fx_without_bank_snapshot(self):
        from src.quotes import QuoteStore
        dates=['2026-01-02','2026-03-31','2026-06-30','2026-09-30','2026-10-31']
        requests=HistoricalRequestStore(self.db)
        for day in dates:requests.request(day)
        pending=QuoteStore(self.db).watchlist()['history_requests']
        self.assertEqual({r['security'] for r in pending},{'/isin/'+ISIN,'USDCZK Curncy'})
        self.assertTrue(all(set(r['dates'])==set(dates) for r in pending))
        # A partial response leaves FX pending; it does not borrow bank FX.
        HistoryStore(self.db).ingest({'quotes':[dict(security='/isin/'+ISIN,date=dates[-1],price='90')]},pending)
        self.assertIsNone(view(SnapshotStore(self.db),dates[-1])['accounting'])
        remaining=QuoteStore(self.db).watchlist()['history_requests']
        self.assertNotIn(dates[-1],next(r['dates'] for r in remaining if r['security']=='/isin/'+ISIN))
        self.assertIn(dates[-1],next(r['dates'] for r in remaining if r['security']=='USDCZK Curncy'))
        HistoryStore(self.db).ingest({'quotes':[dict(security='USDCZK Curncy',date=dates[-1],price='21')]},remaining)
        self.assertNotEqual(view(SnapshotStore(self.db),dates[-1])['selection'],'reconstruction_pending')
        self.assertTrue(all(dates[-1] not in r['dates'] for r in QuoteStore(self.db).watchlist()['history_requests']))

    def test_bounded_queue_prioritizes_reselected_date(self):
        from src.quotes import QuoteStore
        from unittest.mock import patch
        requests=HistoricalRequestStore(self.db)
        requests.request('2026-01-02');requests.request('2026-10-31');requests.request('2026-01-02')
        self.assertEqual(requests.priority_dates()[0],'2026-01-02')
        lots=[dict(security='/isin/'+str(i).zfill(12),dates=['2026-01-02','2026-10-31']) for i in range(100)]
        with patch.object(HistoricalRequestStore,'history_requests',return_value=lots):
            pending=QuoteStore(self.db).watchlist()['history_requests']
        self.assertEqual(sum(len(r['dates']) for r in pending),96)
        self.assertTrue(all(r['dates']==['2026-01-02'] for r in pending))

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
