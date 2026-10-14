import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from src.server import create_server
from src.positions import parse_positions
from src.analytics import portfolio_analytics
from src.performance import calculate_performance
from src.risk import derivatives_risk
from src.report import report_data,report_html,report_csv,fx_rate,spreadsheet
from tests.test_positions import sample
from tests.test_performance import event,csv_events
from tests.test_risk import term,content as risk_content
from datetime import datetime,timezone


class ReportTests(unittest.TestCase):
    def test_fx_cross_missing_rates_escaping_and_formula_protection(self):
        s=parse_positions(sample()); s['exchange_rates']['USD/CZK']='22'
        s['positions'][-1]['description']='=HYPERLINK("bad")<script>alert(1)</script>'
        d=report_data(s,portfolio_analytics(s,{'quotes':[]}),calculate_performance([]),derivatives_risk(s,[]))
        self.assertAlmostEqual(float(fx_rate(s,'EUR','CZK')),24.64,places=4)
        html=report_html(d).decode(); csv=report_csv(d).decode('utf-8-sig')
        self.assertNotIn('<script>alert(1)</script>',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertIn("'=HYPERLINK",csv)
        self.assertIn('snapshot',html.lower())
        self.assertIn('Print / Save as PDF',html)
        self.assertEqual(spreadsheet('-123'),'-123')
        self.assertTrue(spreadsheet('  =SUM(1,2)').startswith("'"))
        self.assertIsNone(fx_rate(s,'USD','JPY'))
        with self.assertRaises(ValueError): report_data(None,None,{}, {})

    def test_http_imports_reports_restart_and_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'db'; server=create_server(0,db)
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            root=f'http://127.0.0.1:{server.server_port}'
            def post(kind,text,override=None):
                prefix='ubs-' if kind=='positions' else 'desk-'
                headers={'Content-Type':'text/csv','X-Import-Format':prefix+kind}|(override or {})
                with urlopen(Request(root+'/api/import/'+kind,data=text.encode(),headers=headers)) as r: return json.load(r)
            try:
                with self.assertRaises(HTTPError) as err: urlopen(root+'/api/reports/trader.html')
                self.assertEqual(err.exception.code,400)
                post('positions',sample())
                ledger=csv_events([event('opening','OPENING','10','100','0'),event('sale','SELL','5','120','2',sequence=1),
                                  event('v1','VALUATION',amount='1000'),event('v2','VALUATION',amount='1100',day='2026-10-13')])
                self.assertEqual(post('performance',ledger)['added'],4)
                self.assertEqual(post('performance',ledger)['existing'],4)
                for kind,text in [('performance',ledger),('risk',risk_content([term(parse_positions(sample()),datetime.now(timezone.utc))]))]:
                    with self.assertRaises(HTTPError) as err: post(kind,text,{'Origin':'https://evil.example'})
                    self.assertEqual(err.exception.code,403)
                self.assertEqual(post('risk',risk_content([term(parse_positions(sample()),datetime.now(timezone.utc))]))['count'],1)
                with urlopen(root+'/api/status') as r: state=json.load(r)
                self.assertEqual(state['performance']['totals']['USD']['realized'],'98')
                self.assertEqual(state['performance']['periods'][0]['amount'],'100')
                self.assertEqual(len(state['risk']['rows']),1)
                for path in ['/reports/trader','/api/reports/trader.html','/api/reports/trader.csv','/api/templates/performance.csv','/api/templates/risk.csv']:
                    with urlopen(root+path) as r:
                        body=r.read()
                        self.assertEqual(r.status,200)
                        self.assertGreater(len(body),50)
                        if path.endswith('.csv') or path.endswith('.html'): self.assertIn('attachment',r.headers['Content-Disposition'])
                with self.assertRaises(HTTPError) as err: urlopen(root+'/reports/trader?size_currency=INVALID')
                self.assertEqual(err.exception.code,400)
            finally:
                server.shutdown();server.server_close();thread.join()
            from src.performance import PerformanceStore
            from src.risk import RiskStore
            self.assertEqual(len(PerformanceStore(db).events()),4)
            self.assertEqual(len(RiskStore(db).terms()),1)
