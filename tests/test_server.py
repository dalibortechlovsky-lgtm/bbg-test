import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from src.server import Handler


class DashboardTests(unittest.TestCase):
    def test_read_only_dashboard(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            root = f'http://127.0.0.1:{server.server_port}'
            with urlopen(root + '/api/status') as response:
                status = json.load(response)
            self.assertFalse(status['execution_enabled'])
            self.assertEqual(len(status['asset_classes']), 4)
            self.assertEqual(status['positions'], [])
            with urlopen(root) as response:
                self.assertIn(b'Trade blotter', response.read())
            with urlopen(root + '/health') as response:
                self.assertEqual(json.load(response), {'status': 'ok', 'execution_enabled': False})
        finally:
            server.shutdown()
            server.server_close()
