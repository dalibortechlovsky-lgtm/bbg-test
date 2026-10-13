import unittest
from decimal import Decimal
from src.keytrader import import_keytrader_csv, parse_keytrader_csv
from src.ledger import TradeLedger

HEADERS = ['Order number','State','Sub State','Order quantity','Open quantity',
           'Quantity type','Side','Instrument','ISIN','Avg. price','Price limit',
           'Currency','Custody acc.','Entered on']


def export(**overrides):
    row = dict(zip(HEADERS, ['TEST-1','Completed','','10','0','Units','Buy',
                            'Example equity','TEST_ISIN','123.45','130','USD',
                            'TEST_ACCOUNT','12.10.2026 09:00:00']))
    row.update(overrides)
    return 'sep=;\n' + '\n'.join(';'.join('=("'+v+'")' for v in values)
                                for values in [HEADERS, [row[h] for h in HEADERS]])


class ImportTests(unittest.TestCase):
    def test_completed_order(self):
        trade = list(parse_keytrader_csv('\ufeff' + export()))[0]
        self.assertEqual(trade.price, Decimal('123.45'))
        self.assertEqual(trade.security, 'TEST_ISIN')
        self.assertEqual(trade.account, 'TEST_ACCOUNT')
        self.assertIsNone(trade.trade_time)
        self.assertEqual(trade.record_kind, 'order_execution_summary')
        self.assertEqual(trade.order_entered_at.hour, 9)

    def test_cancelled_and_working_not_confirmed(self):
        for state in ['Cancelled', 'Working', 'Partially filled']:
            result = import_keytrader_csv(export(State=state))
            self.assertEqual(result.trades, [])
            self.assertEqual(len(result.review), 1)

    def test_invalid_rows_rejected(self):
        for changes in [{'Side':'Unknown'}, {'Avg. price':'NaN'},
                        {'Avg. price':'0'}, {'Open quantity':'2'},
                        {'Quantity type':'Nominal'}, {'Custody acc.':''},
                        {'Order quantity':'-1'}, {'Sub State':'Cancelled'}, {'Currency':'BAD_CODE'}]:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    list(parse_keytrader_csv(export(**changes)))

    def test_repeat_import_and_accounts(self):
        ledger = TradeLedger()
        for content in [export(), export(), export(**{'Custody acc.':'OTHER'})]:
            for trade in parse_keytrader_csv(content):
                ledger.add(trade)
        self.assertEqual(len(ledger.trades), 2)

    def test_duplicate_and_conflicting_orders(self):
        content = export()
        duplicate = content + '\n' + content.splitlines()[-1]
        self.assertEqual(len(list(parse_keytrader_csv(duplicate))), 1)
        conflict = content + '\n' + export(**{'Avg. price':'124'}).splitlines()[-1]
        result = import_keytrader_csv(conflict)
        self.assertTrue(result.errors)
        self.assertEqual(result.trades, [])

    def test_missing_headers(self):
        with self.assertRaises(ValueError):
            import_keytrader_csv('side;quantity\nBUY;10')
        with self.assertRaises(ValueError):
            import_keytrader_csv('\n'.join(export().splitlines()[:2]))
