import csv
import io
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.server import create_server
from src.storage import SnapshotStore
from src.ubs_activity import ActivityStore, HEADERS, activity_csv, parse_activity
from test_positions import sample


def posting(amount, kind='Purchase Spot', number='TRADE1', **changes):
    row = {'Trade date': '2026-01-02', 'Trade time': '', 'Booking date': '2026-01-05',
           'Value date': '2026-01-05', 'Currency': 'USD',
           'Debit': amount if Decimal(amount) <= 0 else '',
           'Credit': amount if Decimal(amount) > 0 else '', 'Individual amount': '',
           'Transaction no.': number, 'Description1': 'Synthetic equity; US0000000001',
           'Description2': kind, 'Description3': 'Number/Amt. 10; Transaction price: 9.8 USD',
           'Footnotes': ''}
    row.update(changes)
    return row


def account_csv(rows, currency='USD', opening='1000', start='2026-01-01', until='2026-01-31', reverse=True):
    rows = [dict(row, Currency=currency) for row in rows]
    running = Decimal(opening)
    for row in rows:
        running += Decimal(row['Debit'] or 0) + Decimal(row['Credit'] or 0)
        row['Balance'] = str(running)
    account = {'USD': 'CH0000000000000000001', 'EUR': 'CH0000000000000000002', 'CZK': 'CH0000000000000000003'}[currency]
    out = io.StringIO()
    writer = csv.writer(out, delimiter=';')
    metadata = {'Account number': 'SYNTHETIC-' + currency, 'IBAN': account,
                'From': start, 'Until': until, 'Opening balance': opening,
                'Closing balance': str(running), 'Valued in': currency,
                'Numbers of transactions in this period': str(len(rows))}
    for k, v in metadata.items(): writer.writerow([k + ':', v, ''])
    writer.writerow([])
    writer.writerow(HEADERS + [''])
    for row in reversed(rows) if reverse else rows:
        writer.writerow([row.get(k, '') for k in HEADERS] + [''])
    return out.getvalue()


class ActivityTests(unittest.TestCase):
    def test_signed_reversals_and_shared_fee_reference_are_preserved(self):
        rows = [posting('-100'),
                posting('-50', 'Special payment order', 'PAY1'),
                posting('-5', 'Third-Party Charges', 'PAY1'),
                posting('100', 'Purchase Spot;Reversal', 'REV1', Debit='100', Credit=''),
                posting('-4', 'Interest;Reversal', 'REV2', Debit='', Credit='-4')]
        parsed = parse_activity('\ufeff' + account_csv(rows))
        self.assertEqual(parsed['closing_balance'], '941')
        self.assertEqual(parsed['count'], 5)
        self.assertEqual(parsed['balance_chain'], 'reconciled')
        self.assertEqual(len({r['id'] for r in parsed['rows']}), 5)
        reversal = next(r for r in parsed['rows'] if r['transaction_no'] == 'REV1')
        self.assertTrue(reversal['reversal'])
        self.assertEqual(reversal['amount'], '100')
        self.assertEqual(reversal['isin'], 'US0000000001')
        self.assertEqual(reversal['quantity'], '10')
        self.assertEqual(reversal['price_currency'], 'USD')
        self.assertEqual(parse_activity(account_csv(rows, reverse=False))['export_order'], 'oldest_first')

    def test_rejects_missing_or_invalid_cash_proofs(self):
        content = account_csv([posting('-100')])
        bad = [content.replace('Closing balance:;900;', 'Closing balance:;901;'),
               content.replace(';900;TRADE1;', ';899;TRADE1;'),
               content.replace('Numbers of transactions in this period:;1;', 'Numbers of transactions in this period:;2;'),
               content.replace(';USD;-100;', ';EUR;-100;'),
               account_csv([posting('-100', **{'Individual amount': '100'})]),
               account_csv([posting('-100', Credit='5')]),
               account_csv([posting('-100'), posting('-100')]),
               content.replace('From:;2026-01-01;', 'From:;2026-02-01;')]
        for invalid in bad:
            with self.subTest(content=invalid[-100:]):
                with self.assertRaises(ValueError): parse_activity(invalid)

    def test_unparsed_trade_terms_are_retained_for_review_without_inventing_a_price(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / 'db.sqlite3')
            store.import_content(account_csv([posting('-100', Description3='Number/Amt. 10; Transaction price: NaN USD')]))
            view = store.view()
            self.assertIsNone(view['ledger'][0]['price'])
            self.assertEqual(view['ledger'][0]['amount'], '-100')
            self.assertIn('price/quantity conventions', view['review'][0]['reason'])

    def test_broker_valor_and_explicit_contract_terms_are_preserved_as_references(self):
        row = posting('-100', 'Buy to open', Description1=
                      'Call Synthetic Index; 12345678; Strike price: USD 100.00, 1:100; Multiplier: 100; 2026-06-18')
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / 'db.sqlite3')
            store.import_content(account_csv([row]))
            view = store.view()
            trade = view['ledger'][0]
            self.assertEqual(trade['valor'], '12345678')
            self.assertIsNone(trade['isin'])
            self.assertEqual(trade['source_multiplier'], '100')
            self.assertEqual(trade['source_expiry'], '2026-06-18')
            self.assertEqual(trade['source_strike'], '100')
            self.assertEqual(view['review'], [])
            self.assertFalse(view['portfolio_pnl_verified'])
            # A displayed 1:100 ratio alone is not a verified multiplier.
            parsed = parse_activity(account_csv([row | {'Description1': 'Call Synthetic Index; 12345678; 1:100'}]))
            self.assertIsNone(parsed['rows'][0]['source_multiplier'])

    def test_overlap_is_idempotent_and_economic_conflict_rolls_back_whole_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'db.sqlite3'
            store = ActivityStore(db)
            first = account_csv([posting('-100')])
            self.assertEqual(store.import_content(first)['added'], 1)
            self.assertTrue(store.import_content(first)['already_imported'])
            overlap = account_csv([posting('-100'), posting('10', 'Dividend', 'DIV1')])
            self.assertEqual(store.import_content(overlap)['added'], 1)
            self.assertEqual(store.view()['record_count'], 2)
            self.assertEqual(store.view()['export_count'], 2)
            with self.assertRaisesRegex(ValueError, 'Changed UBS transaction'):
                store.import_batch([sample(), account_csv([posting('-101')])])
            self.assertIsNone(SnapshotStore(db).latest())
            self.assertEqual(store.view()['record_count'], 2)
            self.assertEqual(store.view()['export_count'], 2)

    def test_future_booking_is_excluded_from_dated_cash_comparison(self):
        rows = [posting('-100'), posting('5', 'Dividend', 'DIV1', **{'Booking date': '2026-02-02', 'Value date': '2026-02-02'})]
        snapshot = {'as_of': '2026-01-31', 'positions': [{
            'kind': 'cash_account', 'currency': 'USD', 'quantity_or_balance': '900',
            'intraday_quantity_or_balance': '905', 'raw': {'IBAN': 'CH0000000000000000001'}}]}
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / 'db.sqlite3')
            store.import_content(account_csv(rows))
            account = store.view(snapshot)['accounts'][0]
            self.assertEqual(account['booked_beyond_filter_count'], 1)
            self.assertEqual(account['snapshot_match']['status'], 'Matches intraday balance only')
            self.assertEqual(account['snapshot_match']['excluded_later_booking_amount'], '5')
            self.assertTrue(account['snapshot_match']['matches_booked_after_excluding_later_bookings'])
            self.assertFalse(store.view(snapshot)['portfolio_pnl_verified'])

    def test_overlapping_fx_export_can_omit_optional_terms_without_erasing_prior_source(self):
        first=posting('-100','Purchase FX Spot','FX1',Description1='Synthetic FX',
                      Description3='Number/Amt. 5; Transaction price: 20 CZK')
        later=first|{'Description3':''}
        with tempfile.TemporaryDirectory() as directory:
            store=ActivityStore(Path(directory)/'db.sqlite3')
            store.import_content(account_csv([first]))
            result=store.import_content(account_csv([later]))
            self.assertEqual(result['added'],0)
            self.assertEqual(result['prior_fx_details_retained'],1)
            records,exports=store.state()
            self.assertEqual(records[0]['quantity'],'5')
            self.assertEqual(records[0]['price'],'20')
            self.assertEqual(len(exports),2)
            self.assertEqual(exports[-1]['rows'][0]['raw']['Description3'],'')
            self.assertTrue(store.import_content(account_csv([later]))['already_imported'])
            for changed in [first|{'Description3':'Number/Amt. 5; Transaction price: 21 CZK'},first|{'Debit':'-101'}]:
                with self.assertRaisesRegex(ValueError,'Changed UBS transaction'):
                    store.import_content(account_csv([changed]))
            self.assertEqual(len(store.state()[0]),1)

    def test_fx_legs_margin_and_deposit_principal_are_not_external_funding(self):
        usd = account_csv([
            posting('-25', 'Purchase FX Spot', 'FX1'),
            posting('-100', 'New Investment Money Market', 'DEP1',
                    Description1='Synthetic deposit; Serial no. A1',
                    Description3='Number/Amt. 100 USD; Transaction price: 100 %'),
            posting('8', 'variation margin futures', 'VM1', Description1='variation margin futures', Description3=''),
            posting('-2', 'Period: 2026-01-01 - 2026-01-31', 'INT1', Description1='Interest calculation balance')])
        eur = account_csv([posting('20', 'Purchase FX Spot', 'FX1')], currency='EUR')
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / 'db.sqlite3')
            store.import_batch([usd, eur])
            view = store.view()
            self.assertEqual(view['fx_pairs'][0]['status'], 'Matched internal currency legs')
            self.assertNotIn('transfer_review', view['categories'])
            self.assertEqual(view['cash_posting_totals']['USD']['futures_margin'], '8')
            self.assertEqual(view['cash_posting_totals']['USD']['account_interest'], '-2')
            self.assertEqual(view['deposit_principal_movements'][0]['net_invested'], '100')
            self.assertIn('opening principal not verified', view['deposit_principal_movements'][0]['status'])
            self.assertFalse(view['portfolio_pnl_verified'])

    def test_review_csv_neutralizes_formulas_and_retains_date_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ActivityStore(Path(directory) / 'db.sqlite3')
            store.import_content(account_csv([posting('1', 'credit', 'PAY1', Description1='=HYPERLINK("unsafe")')]))
            rows = list(csv.reader(io.StringIO(activity_csv(store.view()).decode('utf-8-sig'))))
            self.assertEqual(rows[1][0], 'COVERAGE')
            self.assertEqual(rows[1][3:5], ['2026-01-01', '2026-01-31'])
            # Event type precedes description, so it cannot start a formula.
            self.assertEqual(rows[-1][-1], 'credit; =HYPERLINK("unsafe")')

    def test_http_batch_reconciliation_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'db.sqlite3'
            server = create_server(0, db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            root = 'http://127.0.0.1:' + str(server.server_port)
            try:
                body = json.dumps({'files': [sample(), account_csv([posting('-100')])]}).encode()
                request = Request(root + '/api/import/ubs', body,
                                  {'Content-Type': 'application/json', 'X-Import-Format': 'ubs-batch'})
                with urlopen(request) as response:
                    self.assertEqual(len(json.load(response)['files']), 2)
                with urlopen(root + '/api/status') as response:
                    state = json.load(response)
                self.assertEqual(state['activity']['record_count'], 1)
                self.assertFalse(state['performance']['portfolio_pnl_verified'])
                self.assertEqual(state['performance']['totals'], {})
                with urlopen(root + '/api/reports/ubs-activity.csv') as response:
                    self.assertIn(b'POSTING', response.read())
                for headers in [{'Content-Type': 'application/json'},
                                {'Content-Type': 'application/json', 'X-Import-Format': 'ubs-batch', 'Origin': 'https://unrelated.test'}]:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(Request(root + '/api/import/ubs', body, headers))
                    self.assertEqual(error.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
            self.assertEqual(ActivityStore(db).view()['record_count'], 1)
            self.assertEqual(SnapshotStore(db).latest()['count'], 5)


if __name__ == '__main__': unittest.main()
