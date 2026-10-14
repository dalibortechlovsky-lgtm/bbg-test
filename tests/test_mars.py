from datetime import date
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from xml.sax.saxutils import escape
from zipfile import ZipFile
from src.mars import HEADERS,MarsStore,parse_mars
from src.report import mars_report_html,mars_report_csv
from src.server import create_server
from src.storage import SnapshotStore
from tests.test_positions import sample


def workbook(overrides=None,formula=False,inline=True):
    headers=['Group']+HEADERS
    rows=[headers,
          ['Portfolio Totals']+['']*24,
          ['Equity Option']+['']*24,
          ['--TEST Call','TEST US 12/18/26 C100 Equity','2','TEST_PORTFOLIO','200','10000','100','10/01/26','12/18/26','',
           '25','40','5','800','1000','10','2','4','100','-1','0.5','2','3','TEST US Equity','105'],
          ['--SECOND','SECOND US Equity','5k','TEST_PORTFOLIO','5000','50000','','','','','10','20','10','480','500','50000','5','9','5000','0','0','0','0','SECOND US Equity','']]
    for label,row in [('totals',rows[1]),('group',rows[2])]:
        for h,v in {'P&L Port Ccy':'60','P&L DealVl Port Ccy':'35','MktVal Port Ccy':'1500','CostVal Port Ccy':'1280','Theta':'-20','Vega':'40','Rho':'60'}.items():row[headers.index(h)]=v
    if overrides:
        for row,col,value in overrides: rows[row][headers.index(col)]=value
    parts=[];strings=[]
    for n,row in enumerate(rows,1):
        cells=[]
        for col,v in enumerate(row,1):
            if v=='':continue
            letters='';j=col
            while j:j,r=divmod(j-1,26);letters=chr(65+r)+letters
            ref=letters+str(n)
            try:Decimal(str(v));numeric=n!=1
            except Exception:numeric=False
            f='<f>1+1</f>' if formula and n==4 and col==13 else ''
            if numeric:cells.append(f'<c r="{ref}">{f}<v>{escape(str(v))}</v></c>')
            elif inline:cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>')
            else:
                strings.append(str(v));cells.append(f'<c r="{ref}" t="s"><v>{len(strings)-1}</v></c>')
        parts.append(f'<row r="{n}">'+''.join(cells)+'</row>')
    main='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    out=BytesIO()
    with ZipFile(out,'w') as z:
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{main}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Snapshot" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{main}"><sheetData>'+''.join(parts)+'</sheetData></worksheet>')
        if not inline:z.writestr('xl/sharedStrings.xml',f'<sst xmlns="{main}">'+''.join('<si><t>'+escape(v)+'</t></si>' for v in strings)+'</sst>')
    return out.getvalue()


class MarsTests(unittest.TestCase):
    def test_base_fields_dates_and_calculations_excluded(self):
        for inline in [True,False]:
            s=parse_mars(workbook(inline=inline),{'as_of':'2026-10-14','pnl_period':'YTD or trade date'})
            self.assertEqual(s['count'],2)
            self.assertEqual(s['instruments'][0]['fields']['Expiry Date'],'2026-12-18')
            self.assertEqual(s['instruments'][1]['fields']['Position'],'5k')
            self.assertIsNone(s['portfolio_currency'])
            self.assertEqual(s['calculation_policy'],'base_data_only')
            self.assertNotIn('totals',s)
            self.assertNotIn('Vega',s['instruments'][0]['fields'])
            self.assertNotIn('P&L Port Ccy',s['instruments'][0]['fields'])
            self.assertIn('P&L Port Ccy',s['excluded_columns'])
            self.assertEqual(s['instruments'][0]['fields']['Strike'],'100')

    def test_excel_serial_date_and_missing_values(self):
        serial=(date(2026,12,18)-date(1899,12,30)).days
        s=parse_mars(workbook([(3,'Expiry Date',str(serial)),(3,'CostPx','')]))
        self.assertEqual(s['instruments'][0]['fields']['Expiry Date'],'2026-12-18')
        self.assertIsNone(s['instruments'][0]['fields']['CostPx'])

    def test_rejects_formulas_invalid_numbers_mixed_portfolios_and_duplicates(self):
        for raw in [b'not a workbook',workbook(formula=True),workbook([(3,'Strike','NaN')]),
                    workbook([(4,'Portfolio','OTHER')]),workbook([(4,'Ticker','TEST US 12/18/26 C100 Equity')]),
                    workbook([(3,'Expiry Date','not a date')])]:
            with self.subTest(length=len(raw)):
                with self.assertRaises(ValueError):parse_mars(raw)
        for metadata in [{'as_of':'20261014'},{'portfolio_currency':'usd'},{'unexpected':'field'},[]]:
            with self.assertRaises(ValueError):parse_mars(workbook(),metadata)

    def test_archive_bounds_and_xml_declarations(self):
        raw=BytesIO()
        with ZipFile(raw,'w') as z:z.writestr('oversized.xml',b'x'*(5*1024*1024+1))
        with self.assertRaises(ValueError):parse_mars(raw.getvalue())
        from src.mars import xml
        with self.assertRaises(ValueError):xml(b'<!DOCTYPE x [<!ENTITY a "test">]><x/>')

    def test_persistence_idempotency_dates_and_bank_data_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db';bank=SnapshotStore(path).import_content(sample());store=MarsStore(path)
            first=store.import_content(workbook(),{'as_of':'2026-10-14'})
            self.assertFalse(first['already_imported'])
            self.assertTrue(store.import_content(workbook(),{'as_of':'2026-10-14'})['already_imported'])
            store.import_content(workbook(),{'as_of':'2026-10-13'})
            self.assertEqual(store.latest()['as_of'],'2026-10-14')
            self.assertEqual(store.count(),2)
            with self.assertRaises(ValueError):store.import_content(workbook(formula=True))
            self.assertEqual(store.count(),2)
            self.assertEqual(SnapshotStore(path).latest()['id'],bank['id'])

    def test_report_escaping_and_source_labels(self):
        with tempfile.TemporaryDirectory() as d:
            s=MarsStore(Path(d)/'db').import_content(workbook([(3,'Underlying','<script>bad</script>'),(4,'Ticker','=CMD()')]),{'as_of':'2026-10-14'})
            html=mars_report_html(s).decode();csv=mars_report_csv(s).decode('utf-8-sig')
            self.assertNotIn('<script>bad</script>',html)
            self.assertIn('&lt;script&gt;',html)
            self.assertIn("'=CMD()",csv)
            self.assertIn('MARS calculated P/L',html)
            self.assertIn('Unconfirmed',html)
            self.assertIn('unverified units',html)

    def test_http_upload_report_and_origin_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            server=create_server(0,Path(d)/'db');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            root=f'http://127.0.0.1:{server.server_port}'
            headers={'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','X-Import-Format':'bloomberg-mars','X-Mars-Metadata':'{"as_of":"2026-10-14"}'}
            try:
                with urlopen(Request(root+'/api/import/mars',data=workbook(),headers=headers)) as r:self.assertEqual(json.load(r)['count'],2)
                with urlopen(root+'/api/status') as r:state=json.load(r)
                self.assertEqual(state['mars']['count'],2)
                self.assertEqual(state['mars_snapshot_count'],1)
                self.assertEqual(state['performance']['stored_event_count'],0)
                self.assertEqual(state['risk_inputs_count'],0)
                self.assertIsNone(state['snapshot'])
                for route in ['/reports/mars','/api/reports/mars.html','/api/reports/mars.csv']:
                    with urlopen(root+route) as r:self.assertGreater(len(r.read()),50)
                for override,code in [({'Origin':'https://evil.example'},403),({'Content-Type':'text/csv'},415),({'X-Mars-Metadata':'not json'},400)]:
                    with self.assertRaises(HTTPError) as error:urlopen(Request(root+'/api/import/mars',data=workbook(),headers=headers|override))
                    self.assertEqual(error.exception.code,code)
            finally:server.shutdown();server.server_close();thread.join()

    def test_exact_ticker_quote_enrichment_and_fx_watchlist(self):
        from datetime import datetime,timezone
        from src.mars import enrich_base_data
        from src.quotes import QuoteStore
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'db'
            m=MarsStore(path).import_content(workbook(),{'as_of':'2026-10-14','portfolio_currency':'CZK'})
            quotes=QuoteStore(path)
            device=quotes.create_token('Synthetic')
            self.assertIn('SECOND US Equity',quotes.watchlist()['securities'])
            self.assertNotIn('IBM US Equity',quotes.watchlist()['securities'])
            quotes.ingest(device['id'],{'schema_version':1,'status':'connected','quotes':[
                {'security':'SECOND US Equity','fieldData':{'PX_LAST':'12','CRNCY':'EUR','ID_ISIN':'TEST_ISIN'},
                 'observed_at':datetime.now(timezone.utc).isoformat()}]})
            self.assertIn('EURCZK Curncy',quotes.watchlist()['securities'])
            enriched=enrich_base_data(m,quotes.view())
            self.assertIsNone(enriched['instruments'][0]['quote'])
            q=enriched['instruments'][1]['quote']
            self.assertEqual(q['currency'],'EUR')
            self.assertEqual(q['isin'],'TEST_ISIN')
            self.assertEqual(enriched['instruments'][1]['fields']['MktPx'],'10')
            self.assertNotIn('Vega',enriched['instruments'][1]['fields'])

    def test_invalid_excluded_calculations_do_not_corrupt_base_data(self):
        s=parse_mars(workbook([(3,'Gamma','NaN'),(3,'P&L Port Ccy','#VALUE!')]))
        self.assertEqual(s['instruments'][0]['fields']['Strike'],'100')
        self.assertNotIn('Gamma',s['instruments'][0]['fields'])
        self.assertNotIn('P&L Port Ccy',s['instruments'][0]['fields'])
