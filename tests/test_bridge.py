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

class HistoricalBridgeTests(unittest.TestCase):
    def test_historical_request_collects_partial_responses_and_preserves_filling_convention(self):
        from types import SimpleNamespace
        from datetime import date
        class Element:
            def __init__(self,value=None):self.value=value;self.items=[]
            def appendValue(self,value):self.items.append(value)
            def toPy(self):return self.value
        class Request:
            def __init__(self):self.elements={};self.values={}
            def getElement(self,key):return self.elements.setdefault(key,Element())
            def set(self,key,value):self.values[key]=value
        request=Request()
        class Message:
            def __init__(self,row):self.row=row
            def hasElement(self,key):return key=='securityData'
            def messageType(self):return 'HistoricalDataResponse'
            def getElement(self,key):return Element(self.row)
        class Event(list):
            RESPONSE=5
            def __init__(self,kind,rows):super().__init__([Message(r) for r in rows]);self.kind=kind
            def eventType(self):return self.kind
        events=[Event(4,[{'security':'USDCZK Curncy','fieldData':[{'date':date(2026,1,2),'PX_LAST':21.5},
                    {'date':date(2026,1,3),'PX_LAST':21.5}]}]),Event(5,[{'security':'EURCZK Curncy',
                    'fieldData':[{'date':date(2026,1,2),'PX_LAST':24.1}]}])]
        class Session:
            def __init__(self,*args):self.stopped=False
            def start(self):return True
            def openService(self,name):return name=='//blp/refdata'
            def getService(self,name):return self
            def createRequest(self,name):self.request_type=name;return request
            def sendRequest(self,value):self.sent=value
            def nextEvent(self,timeout):return events.pop(0)
            def stop(self):self.stopped=True
        session=Session()
        options=SimpleNamespace(setServerHost=lambda value:None,setServerPort=lambda value:None)
        sdk=SimpleNamespace(SessionOptions=lambda:options,Session=lambda value:session,Event=Event,Name=lambda value:value)
        with patch.dict('sys.modules',{'blpapi':sdk}):
            result=bridge.request_history([{'security':'USDCZK Curncy','dates':['2026-01-02']},
                                           {'security':'EURCZK Curncy','dates':['2026-01-02']}])
        self.assertTrue(session.stopped)
        self.assertEqual(session.request_type,'HistoricalDataRequest')
        self.assertEqual(request.values['nonTradingDayFillMethod'],'PREVIOUS_VALUE')
        self.assertEqual(request.values['nonTradingDayFillOption'],'ALL_CALENDAR_DAYS')
        self.assertEqual(request.values['startDate'],'20260102')
        self.assertEqual(request.elements['fields'].items,['PX_LAST'])
        self.assertEqual(len(result),2)
        self.assertEqual(result[0],{'security':'USDCZK Curncy','date':'2026-01-02','price':'21.5'})
