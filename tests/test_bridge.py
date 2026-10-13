import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

spec = importlib.util.spec_from_file_location('bloomberg_bridge', Path(__file__).resolve().parents[1]/'bridge'/'bloomberg_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def test_partial_success_keeps_only_failed_quotes_old(self):
        prior = {'quotes': [{'security':'A','fieldData':{'PX_LAST':'1'},'observed_at':'old'},
                            {'security':'B','fieldData':{'PX_LAST':'2'},'observed_at':'old'}], 'received_at':'old'}
        response = [{'security':'A','fieldData':{'PX_LAST':'3'},'fieldExceptions':[{'fieldId':'ID_ISIN'}]},
                    {'security':'B','securityError':{'message':'not entitled'}}]
        state = bridge.cycle(prior, ['A','B'], lambda s: response, lambda:'new')
        self.assertEqual(state['status'],'degraded')
        quotes = {row['security']:row for row in state['quotes']}
        self.assertEqual(quotes['A']['observed_at'],'new')
        self.assertEqual(quotes['B']['observed_at'],'old')
        self.assertEqual(quotes['B']['fieldData']['PX_LAST'],'2')

    def test_config_rejects_plain_http_and_cross_host(self):
        config = {'endpoint':'https://desk.example/api/bridge/quotes',
                  'watchlist_url':'https://desk.example/api/bridge/watchlist',
                  'token':'bbg_'+'x'*40}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            path.write_text(json.dumps(config))
            self.assertEqual(bridge.load_config(path),config)
            for key, value in [('endpoint','http://desk.example/api/bridge/quotes'),
                               ('watchlist_url','https://other.example/watchlist'),
                               ('endpoint','https://user:pass@desk.example/quotes')]:
                path.write_text(json.dumps(config | {key:value}))
                with self.assertRaises(ValueError):
                    bridge.load_config(path)

    def test_redirects_cannot_forward_device_credentials(self):
        handler=bridge.NoRedirect()
        request=urllib.request.Request('https://desk.example/api/bridge/quotes',data=b'{}',
                                       headers={'Authorization':'Bearer synthetic-test'})
        self.assertIsNone(handler.redirect_request(request,None,302,'redirect',{},'https://other.example'))

    def test_success_failure_and_recovery_timestamps(self):
        good = lambda securities: [{'security': securities[0], 'fieldData': {'PX_LAST': 123}, 'fieldExceptions': []}]
        state = bridge.cycle({'quotes': [], 'received_at': None}, ['TEST Equity'], good, lambda: 'first')
        self.assertEqual(state['status'], 'connected')
        def fail(securities):
            raise ConnectionError('offline')
        failed = bridge.cycle(state, ['TEST Equity'], fail, lambda: 'second')
        self.assertEqual(failed['status'], 'disconnected')
        self.assertEqual(failed['received_at'], 'first')
        self.assertEqual(failed['quotes'], state['quotes'])
        recovered = bridge.cycle(failed, ['TEST Equity'], good, lambda: 'third')
        self.assertEqual(recovered['received_at'], 'third')
        self.assertIsNone(recovered['error'])

    def test_entitlement_and_missing_security_never_mark_connected(self):
        for response in [[], [{'security':'TEST Equity','securityError':{'message':'not entitled'}}],
                         [{'security':'TEST Equity','fieldData':{}}],
                         [{'security':'OTHER Equity','fieldData':{'PX_LAST':123}}]]:
            state = bridge.cycle({'quotes':[], 'received_at':None}, ['TEST Equity'], lambda s: response)
            self.assertEqual(state['status'], 'degraded')
            self.assertIsNone(state['received_at'])

    def test_atomic_output_preserves_decimal_safe_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'data'/'quotes.json'
            bridge.save(path, {'status':'disconnected','received_at':'old'})
            bridge.save(path, {'status':'connected','received_at':'new'})
            self.assertEqual(json.loads(path.read_text())['received_at'], 'new')
            self.assertFalse(path.with_name('quotes.json.tmp').exists())
