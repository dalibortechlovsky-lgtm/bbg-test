import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from src.positions import COMMON, parse_positions
from src.storage import SnapshotStore
from src.server import create_server


def sample(as_of='08.10.2026', exported='09.10.2026 11:10'):
    out = io.StringIO()
    writer = csv.writer(out, delimiter=';')
    common = ['Banking relationship'] + sorted(COMMON - {'Banking relationship'})
    extra = ['Description 2', 'Date', 'Cost value', 'Cost price', 'Market price', 'Unrealized P/L']
    def section(rows, headers):
        writer.writerow(headers + ['', ''])
        for values in rows:
            base = {'Banking relationship':'TEST_BANK', 'Portfolio':'TEST_PORTFOLIO',
                    'Product':'TEST_CUSTODY', 'Ccy.':'USD', 'Number/Amt.':'10',
                    'Group of products':'Equities - Equity investments',
                    'Description':'Synthetic security', 'ISIN':'TEST_ISIN',
                    'Market value':'200', 'Date':as_of}
            base.update(values)
            writer.writerow([base.get(h, '') for h in headers] + ['', ''])
    section([{'Group of products':'Liquidity - Accounts', 'Product':'TEST_CASH',
              'ISIN':'', 'Number/Amt.':'12.34', 'Intraday number/amount':'15.50',
              'Market value':'12', 'IBAN':'TEST_IBAN'}], common + ['IBAN'])
    section([{'Group of products':'Liquidity - Call deposits', 'Description 2':'Serial A',
              'ISIN':'', 'Number/Amt.':'1000', 'Market value':'1000'},
             {'Group of products':'Liquidity - Call deposits', 'Description 2':'Serial B',
              'ISIN':'', 'Number/Amt.':'2000', 'Market value':'2000'}], common + extra)
    section([{'Group of products':'Bonds - Bond investments', 'Number/Amt.':'10000',
              'Market price':'91.25%', 'Cost price':'98%', 'Market value':'9125',
              'Accrued interest':'40', 'Unrealized P/L':'-6.89%'},
             {'ISIN':'', 'Valor':'TEST_OPTION', 'Description':'Call synthetic 1:100',
              'Number/Amt.':'2', 'Market price':'3.5', 'Market value':'700'}], common + extra)
    writer.writerow(['Exchange rates'])
    writer.writerow(['EUR/USD', '1.12', 'USD/EUR', '0.892857'])
    for value in ['Portfolio number: TEST_PORTFOLIO', f'Valued as of: {as_of}',
                  'Valued in: USD', f'Export created on: {exported}', 'Number of positions: 5']:
        writer.writerow([value])
    return out.getvalue()


class PositionsTests(unittest.TestCase):
    def test_section_headers_and_reported_conventions(self):
        result = parse_positions('\ufeff' + sample())
        self.assertEqual(result['count'], 5)
        self.assertEqual(result['as_of'], '2026-10-08')
        self.assertEqual(result['summary']['market_value'], '12837')
        self.assertEqual(result['summary']['accrued_interest'], '40')
        self.assertEqual(result['summary']['cash_balances'], {'USD':'12.34'})
        cash, deposit1, deposit2, bond, option = result['positions']
        self.assertEqual(cash['intraday_quantity_or_balance'], '15.50')
        self.assertNotEqual(deposit1['id'], deposit2['id'])
        self.assertEqual(bond['market_price'], {'value':'91.25', 'convention':'percent'})
        self.assertEqual(bond['unrealized_pnl_percent'], '-6.89')
        self.assertIsNone(option['isin'])
        self.assertEqual(option['valor'], 'TEST_OPTION')
        self.assertEqual(option['market_value'], '700')  # Not 2 * 3.5.

    def test_rejects_incomplete_or_ambiguous_exports(self):
        for content in [sample().replace('Number of positions: 5', 'Number of positions: 6'),
                        sample().replace('Valued in: USD', 'Valued in: '),
                        sample().replace('Serial B', 'Serial A'),
                        sample().replace('9125', 'NaN'),
                        sample().replace('Valued as of: 08.10.2026', 'Valued as of: 99.10.2026'),
                        sample().replace('TEST_OPTION', ''),
                        sample().split('Exchange rates')[0]]:
            with self.subTest(content=content[-100:]):
                with self.assertRaises(ValueError):
                    parse_positions(content)

    def test_persistence_repeat_and_date_order(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory)/'db.sqlite3'
            store = SnapshotStore(db)
            self.assertIsNone(store.latest())
            first = store.import_content(sample())
            self.assertFalse(first['already_imported'])
            self.assertTrue(store.import_content(sample())['already_imported'])
            store.import_content(sample('07.10.2026', '08.10.2026 11:10'))
            self.assertEqual(SnapshotStore(db).latest()['as_of'], '2026-10-08')
            with self.assertRaises(ValueError):
                store.import_content(sample().replace('9125', 'NaN'))
            self.assertEqual(store.latest()['id'], first['id'])

    def test_http_import_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory)/'db.sqlite3'
            server = create_server(0, db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            root = f'http://127.0.0.1:{server.server_port}'
            try:
                headers = {'Content-Type':'text/csv', 'X-Import-Format':'ubs-positions'}
                request = Request(root+'/api/import/positions', data=sample().encode(), headers=headers)
                with urlopen(request) as response:
                    self.assertEqual(json.load(response)['count'], 5)
                with urlopen(root+'/api/status') as response:
                    data = json.load(response)
                self.assertFalse(data['execution_enabled'])
                self.assertEqual(len(data['positions']), 5)
                self.assertEqual(data['snapshot']['as_of'], '2026-10-08')
                self.assertEqual(data['analytics']['broker_market_value'], '12837')
                self.assertEqual(data['analytics']['indicative_market_value'], '12837')
                self.assertIsNone(data['analytics']['daily_pnl'])
                self.assertNotIn('raw', data['positions'][0])
                for override, code in [({'Origin':'https://untrusted.example'},403),
                                       ({'X-Import-Format':'wrong'},403),
                                       ({'Content-Type':'text/plain'},415)]:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(Request(root+'/api/import/positions', data=sample().encode(),
                                        headers=headers | override))
                    self.assertEqual(error.exception.code, code)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(root+'/api/import/positions', data=b'invalid', headers=headers))
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(SnapshotStore(db).latest()['count'], 5)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            restarted = create_server(0, db)
            thread = threading.Thread(target=restarted.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f'http://127.0.0.1:{restarted.server_port}/api/status') as response:
                    self.assertEqual(json.load(response)['snapshot']['count'], 5)
            finally:
                restarted.shutdown()
                restarted.server_close()
                thread.join()
