import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from src.ptd import HEADERS, PtdStore
from src.server import create_server


def record(**changes):
    row = {h: '' for h in HEADERS}
    row.update({'Event Date': '01/22/2026 12:00:00', 'Portfolio Name': 'SYNTHETIC',
                'BBID': 'EQ_SYNTHETIC', 'Security': 'Synthetic security', 'Trade Date': '01/02/2026',
                'Event Type': 'BUY', 'Trade Amount': '10', 'New Amount': '10', 'Trade Price': '100',
                'Sec Currency': 'USD', 'Commission': '0', 'Security Fee': '0'})
    row.update(changes)
    return row


def export(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


class PtdTests(unittest.TestCase):
    def test_repeated_admin_state_is_stored_once_and_original_file_is_retained(self):
        admin = record(**{'Event Type': 'POSITION_NEW', 'Trade Amount': '', 'New Amount': '0', 'Trade Price': ''})
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'db.sqlite3'
            store = PtdStore(db)
            content = export([admin, admin])
            result = store.import_content(content)
            self.assertEqual(result['added'], 1)
            self.assertEqual(len(store.state()[0]), 1)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute('SELECT source_content FROM ptd_import_files').fetchone()[0], content)
                self.assertEqual(connection.execute('SELECT row_count FROM ptd_imports').fetchone()[0], 2)
            with self.assertRaisesRegex(ValueError, 'Duplicate identical PTD trade'):
                store.import_content(export([record(), record()]))
            self.assertEqual(len(store.state()[0]), 1)

    def test_correction_preserves_audit_and_actual_trade_date(self):
        old = record(**{'Trade Date': '01/20/2026', 'Event Date': '01/21/2026 12:00:00'})
        corrected = record()
        with tempfile.TemporaryDirectory() as directory:
            store = PtdStore(Path(directory) / 'db.sqlite3')
            store.import_content(export([old, corrected]))
            rows = sorted(store.state()[0], key=lambda r: r['event_time'])
            before = store.view(as_of='2026-02-01')
            self.assertEqual(before['superseded_count'], 0)
            self.assertEqual(len(before['correction_candidates']), 1)
            store.supersede(rows[0]['id'], rows[1]['id'], 'Owner confirmed correction')
            after = store.view(as_of='2026-02-01')
            self.assertEqual(after['stored_record_count'], 2)
            self.assertEqual(after['superseded_count'], 1)
            active = next(r for r in after['audit'] if r['status'] == 'active')
            self.assertEqual(active['date'], '2026-01-02')
            self.assertEqual(active['event_time'], '2026-01-22T12:00:00')
            self.assertTrue(store.supersede(rows[0]['id'], rows[1]['id'], 'Same correction')['already_applied'])
            self.assertFalse(after['portfolio_pnl_verified'])

    def test_carried_quantity_without_opening_cost_does_not_produce_realized_profit(self):
        sale = record(**{'Event Type': 'SELL', 'Trade Amount': '-6', 'New Amount': '4',
                         'Trade Price': '90', 'Commission': '2'})
        with tempfile.TemporaryDirectory() as directory:
            store = PtdStore(Path(directory) / 'db.sqlite3')
            store.import_content(export([sale]))
            before = store.view(as_of='2026-02-01')
            self.assertEqual(before['performance']['realized'], [])
            self.assertTrue(before['issues'])
            store.configure({'account': 'SYNTHETIC', 'bbid': 'EQ_SYNTHETIC', 'verified': True,
                             'basis': 'Synthetic dated broker opening statement', 'opening_date': '2025-12-31',
                             'opening_quantity': '10', 'opening_price': '50'})
            after = store.view(as_of='2026-02-01')
            self.assertEqual(after['performance']['realized'][0]['amount'], '238')
            self.assertFalse(after['portfolio_pnl_verified'])

    def test_ptd_is_a_cross_check_and_is_never_automatically_posted_to_main_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(0, Path(directory) / 'db.sqlite3')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            root = 'http://127.0.0.1:' + str(server.server_port)
            try:
                # A PTD price may be a year-start reset. Its role cannot be
                # established solely by the exported BUY label.
                request = Request(root + '/api/import/ptd', export([record()]).encode(),
                                  {'Content-Type': 'text/csv', 'X-Import-Format': 'bloomberg-ptd'})
                with urlopen(request) as response: self.assertEqual(response.status, 200)
                with urlopen(root + '/api/status') as response: state = json.load(response)
                self.assertEqual(state['performance']['event_count'], 0)
                self.assertEqual(state['performance']['totals'], {})
                self.assertIn('Independent audit source', state['ptd']['accounting_status'])
            finally:
                server.shutdown()
                server.server_close()
