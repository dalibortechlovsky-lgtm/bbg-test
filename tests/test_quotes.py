from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from src.quotes import QuoteStore
from src.server import create_server
from src.storage import SnapshotStore
from test_positions import sample


NOW = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)


def payload(at=NOW, price='123.45', status='connected'):
    return {'schema_version': 1, 'status': status, 'quotes': [
        {'security': 'TEST Equity', 'fieldData': {'PX_LAST': price, 'CRNCY': 'USD'}, 'observed_at': at.isoformat()}]}


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'db.sqlite3'
        self.now = NOW
        self.store = QuoteStore(self.path, lambda: self.now)
        self.device = self.store.create_token('Windows test')
        self.auth = 'Bearer ' + self.device['token']

    def test_tokens_hashed_expired_revoked_and_not_disclosed(self):
        self.assertEqual(self.store.authenticate(self.auth), self.device['id'])
        self.assertIsNone(self.store.authenticate('Basic wrong'))
        self.assertIsNone(self.store.authenticate('Bearer wrong-value'))
        with sqlite3.connect(self.path) as db:
            value = db.execute('SELECT digest FROM bridge_tokens').fetchone()[0]
        self.assertEqual(value, hashlib.sha256(self.device['token'].encode()).hexdigest())
        self.assertNotIn(self.device['token'], json.dumps(self.store.devices()))
        self.now += timedelta(days=91)
        self.assertIsNone(self.store.authenticate(self.auth))
        self.now = NOW
        self.assertTrue(self.store.revoke(self.device['id']))
        self.assertIsNone(self.store.authenticate(self.auth))
        with self.assertRaises(PermissionError):
            self.store.ingest(self.device['id'], payload())

    def test_repeat_out_of_order_and_silence_do_not_refresh_prices(self):
        result = self.store.ingest(self.device['id'], payload())
        self.assertEqual(result['accepted'], 1)
        self.now += timedelta(seconds=60)
        self.assertEqual(self.store.ingest(self.device['id'], payload())['ignored'], 1)
        view = self.store.view()
        self.assertEqual(view['quotes'][0]['age_seconds'], 60)
        self.assertFalse(view['quotes'][0]['stale'])
        self.store.ingest(self.device['id'], payload(NOW-timedelta(seconds=1), '1'))
        self.assertEqual(self.store.view()['quotes'][0]['price'], '123.45')
        self.now += timedelta(seconds=31)
        self.assertTrue(self.store.view()['quotes'][0]['stale'])
        self.now += timedelta(seconds=61)
        self.assertEqual(self.store.view()['devices'][0]['status'], 'offline')

    def test_disconnect_keeps_old_quotes_without_freshness_claim(self):
        self.store.ingest(self.device['id'], payload())
        self.now += timedelta(seconds=5)
        self.store.ingest(self.device['id'], {'schema_version':1,'status':'disconnected','quotes':[]})
        quote = self.store.view()['quotes'][0]
        self.assertEqual(quote['price'], '123.45')
        self.assertEqual(quote['observed_at'], NOW.isoformat())
        self.assertTrue(quote['stale'])
        self.store.ingest(self.device['id'], payload(self.now,'125'))
        self.assertFalse(self.store.view()['quotes'][0]['stale'])

    def test_validation_atomicity_and_no_ubs_repricing(self):
        holdings = SnapshotStore(self.path).import_content(sample())
        original = SnapshotStore(self.path).latest()
        good = payload()
        for broken in [payload(price='NaN'),payload(price='Infinity'),payload(at=NOW+timedelta(minutes=3)),
                       {'schema_version':1,'status':'connected','quotes':[]},
                       {'schema_version':1,'status':'unknown','quotes':[]}]:
            with self.assertRaises(ValueError):
                self.store.ingest(self.device['id'], broken)
        mixed = payload()
        mixed['quotes'].append({'security':'BAD','fieldData':{'PX_LAST':'NaN'},'observed_at':NOW.isoformat()})
        with self.assertRaises(ValueError):
            self.store.ingest(self.device['id'], mixed)
        self.assertEqual(self.store.view()['quotes'], [])
        self.store.ingest(self.device['id'], good)
        self.assertEqual(SnapshotStore(self.path).latest(), original)
        self.assertEqual(holdings['summary']['market_value'], original['summary']['market_value'])

    def test_isin_watchlist_and_missing_options(self):
        SnapshotStore(self.path).import_content(sample())
        result = self.store.watchlist()
        self.assertEqual(result['unmapped_count'], 1)
        self.assertIn('/isin/TEST_ISIN', result['securities'])
        data = payload()
        data['quotes'][0]['requested_security']='/isin/TEST_ISIN'
        data['quotes'][0]['fieldData']['ID_ISIN']='DIFFERENT'
        with self.assertRaises(ValueError):
            self.store.ingest(self.device['id'], data)

    def test_http_auth_create_upload_download_and_revoke(self):
        server = create_server(0, self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        def post(path, data, headers):
            return urlopen(Request(root+path,data=json.dumps(data).encode(),headers=headers))
        try:
            headers={'Content-Type':'application/json','X-Bridge-Action':'manage'}
            with post('/api/bridge/token',{'name':'HTTP test'},headers) as r:
                config=json.load(r)
            auth={'Authorization':'Bearer '+config['config']['token'],'Content-Type':'application/json'}
            with urlopen(Request(root+'/api/bridge/watchlist',headers=auth)) as r:
                self.assertIn('securities',json.load(r))
            with post('/api/bridge/quotes',payload(datetime.now(timezone.utc)),auth) as r:
                self.assertEqual(json.load(r)['accepted'],1)
            with self.assertRaises(HTTPError) as error:
                post('/api/bridge/quotes',payload(),{'Content-Type':'application/json'})
            self.assertEqual(error.exception.code,401)
            with self.assertRaises(HTTPError) as error:
                post('/api/bridge/quotes',payload(),auth|{'Origin':'https://foreign.test'})
            self.assertEqual(error.exception.code,403)
            with urlopen(root+'/api/status') as r:
                result=json.load(r)
            self.assertEqual(len(result['bloomberg_data']['quotes']),1)
            self.assertNotIn(config['config']['token'],json.dumps(result))
            with urlopen(root+'/bridge/bloomberg_bridge.py') as r:
                self.assertIn(b'def cloud_request',r.read())
            with post('/api/bridge/revoke',{'id':config['id']},headers) as r:
                self.assertTrue(json.load(r)['revoked'])
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(root+'/api/bridge/watchlist',headers=auth))
            self.assertEqual(error.exception.code,401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
