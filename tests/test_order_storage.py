import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from src.server import create_server
from src.storage import SnapshotStore
from test_keytrader import export


class OrderStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'orders.sqlite3'
        self.store = SnapshotStore(self.path)

    def test_persistence_deduplication_and_account_scope(self):
        result = self.store.import_orders_content(export())
        self.assertEqual(result['added'], 1)
        self.assertTrue(result['accepted'])
        self.assertIsNone(self.store.latest())  # Order-only database is valid.
        result = self.store.import_orders_content(export())
        self.assertTrue(result['already_imported'])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['existing'], 1)
        result = self.store.import_orders_content(export(**{'Avg. price':'123.4500'}))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['existing'], 1)
        self.assertEqual(result['added'], 0)
        result = self.store.import_orders_content(export(**{'Custody acc.':'OTHER_ACCOUNT'}))
        self.assertEqual(result['added'], 1)
        reopened = SnapshotStore(self.path)
        self.assertEqual(len(reopened.orders()), 2)
        self.assertEqual(reopened.orders()[0]['record_kind'], 'order_execution_summary')
        self.assertIsNone(reopened.orders()[0]['trade_time'])
        self.assertEqual(len(reopened.order_imports()), 3)

    def test_conflict_rejects_whole_batch_and_preserves_audit(self):
        self.store.import_orders_content(export())
        content = export(**{'Avg. price':'99'}) + '\n' + export(**{'Order number':'NEW'}).splitlines()[-1]
        result = self.store.import_orders_content(content)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['errors'][0]['reason'], 'CONFLICT_WITH_STORED_ORDER')
        reopened = SnapshotStore(self.path)
        self.assertEqual(len(reopened.orders()), 1)
        self.assertEqual(reopened.orders()[0]['price'], '123.45')
        self.assertFalse(reopened.order_imports()[0]['accepted'])
        repeated = reopened.import_orders_content(content)
        self.assertTrue(repeated['already_imported'])
        self.assertFalse(repeated['accepted'])
        self.assertEqual(len(reopened.order_imports()), 2)

    def test_non_completed_review_is_persistent_without_execution(self):
        result = self.store.import_orders_content(export(State='Cancelled'))
        self.assertTrue(result['accepted'])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['review'][0]['order'], 'TEST-1')
        self.assertEqual(result['review'][0]['state'], 'Cancelled')
        self.assertEqual(self.store.orders(), [])
        self.assertEqual(SnapshotStore(self.path).order_imports()[0]['review'], result['review'])

    def test_cancelled_state_cannot_overwrite_completed_order(self):
        self.store.import_orders_content(export())
        result = self.store.import_orders_content(export(State='Cancelled'))
        self.assertFalse(result['accepted'])
        self.assertEqual(result['errors'][0]['reason'], 'NON_COMPLETED_STATE_FOR_STORED_ORDER')
        self.assertEqual(len(self.store.orders()), 1)

    def test_invalid_batch_and_malformed_csv(self):
        content = export() + '\n' + export(**{'Order number':'BAD', 'Side':'Unknown'}).splitlines()[-1]
        result = self.store.import_orders_content(content)
        self.assertFalse(result['accepted'])
        self.assertEqual(self.store.orders(), [])
        self.assertEqual(len(self.store.order_imports()), 1)
        with self.assertRaises(ValueError):
            self.store.import_orders_content('"unterminated')
        self.assertEqual(len(self.store.order_imports()), 1)

    def test_http_orders_and_restart(self):
        server = create_server(0, self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        headers = {'Content-Type':'text/csv', 'X-Import-Format':'ubs-orders'}
        try:
            request = Request(root + '/api/import/orders', data=export().encode(), headers=headers)
            with urlopen(request) as response:
                self.assertEqual(json.load(response)['added'], 1)
            with urlopen(root + '/api/status') as response:
                data = json.load(response)
            self.assertEqual(len(data['trades']), 1)
            self.assertFalse(data['execution_enabled'])
            self.assertIsNone(data['snapshot'])
            self.assertEqual(len(data['order_imports']), 1)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(root + '/api/import/orders', data=export(**{'Avg. price':'99'}).encode(), headers=headers))
            self.assertEqual(error.exception.code, 409)
            self.assertFalse(json.load(error.exception)['accepted'])
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(root + '/api/import/orders', data=export().encode(),
                                headers=headers | {'X-Import-Format':'ubs-positions'}))
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        server = create_server(0, self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/status') as response:
                data = json.load(response)
            self.assertEqual(len(data['trades']), 1)
            self.assertEqual(len(data['order_imports']), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
