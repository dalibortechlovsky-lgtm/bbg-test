import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.lifecycle import LifecycleStore
from src.storage import SnapshotStore
from src.ubs_activity import ActivityStore
from test_positions import sample
from test_ubs_activity import account_csv, posting


spec = importlib.util.spec_from_file_location(
    'record_expiries', Path(__file__).resolve().parents[1] / 'deploy' / 'record-expiries.py')
utility = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utility)


class RecordExpiriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'frontoffice.sqlite3'
        SnapshotStore(self.database).import_content(sample().replace('TEST_ISIN', 'US0000000002'))
        ActivityStore(self.database).import_content(account_csv([
            posting('-100'), posting('-200', 'Buy to open', 'OPTION1',
                                     Description1='Call Synthetic Index; 12345678')]))

    def source_state(self, database=None):
        with sqlite3.connect(database or self.database) as connection:
            tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if r[0] != 'settlement_confirmations']
            return {name: connection.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall()
                    for name in tables}

    def run_repair(self, keys=None, currency='usd', cutoff='2026-10-08'):
        return utility.record_expiries(self.database, keys or ['us0000000001', '12345678'], currency, cutoff)

    def test_backed_up_idempotent_annotations_preserve_all_imported_data(self):
        before = self.source_state()
        result = self.run_repair()
        self.assertEqual(result['confirmed'], 2)
        backup = Path(result['backup'])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.source_state(backup), before)
        self.assertEqual(LifecycleStore(backup).all(), [])
        self.assertEqual(self.source_state(), before)
        records = LifecycleStore(self.database).all()
        self.assertEqual({r['key'] for r in records}, {'US0000000001', 'VALOR:12345678'})
        self.assertEqual({r['currency'] for r in records}, {'USD'})
        self.assertEqual(self.run_repair(), {'confirmed': 0, 'already_recorded': 2, 'backup': None})
        self.assertEqual(len(list(backup.parent.iterdir())), 1)

    def test_prior_owner_reason_is_preserved(self):
        prior = {'key': 'US0000000001', 'currency': 'USD', 'settled_by': '2026-10-08',
                 'reason': 'Earlier owner confirmation, retained verbatim.'}
        LifecycleStore(self.database).confirm([prior])
        self.assertEqual(self.run_repair()['already_recorded'], 1)
        self.assertIn(prior, LifecycleStore(self.database).all())

    def test_invalid_batch_does_not_write_or_backup(self):
        for keys, currency, cutoff in [
                (['US0000000001', 'US0000000009'], 'USD', '2026-10-08'),
                (['US0000000001', 'US0000000002'], 'USD', '2026-10-08'),
                (['US0000000001'], 'EUR', '2026-10-08'),
                (['US0000000001'], 'USD', '2026-10-09'),
                (['US0000000001'], 'USD', '2025-12-31'),
                (['US0000000001', 'bad-reference'], 'USD', '2026-10-08')]:
            with self.subTest(keys=keys, currency=currency, cutoff=cutoff):
                before = self.database.read_bytes()
                with self.assertRaises(ValueError):
                    self.run_repair(keys, currency, cutoff)
                self.assertEqual(self.database.read_bytes(), before)
                self.assertFalse((self.database.parent / 'backups').exists())

    def test_open_position_rejected_even_with_known_execution(self):
        ActivityStore(self.database).import_content(account_csv([
            posting('-100', number='HELD1', Description1='Held synthetic; US0000000002')]))
        with self.assertRaisesRegex(ValueError, 'still held'):
            self.run_repair(['US0000000002'])
        self.assertEqual(LifecycleStore(self.database).all(), [])

    def test_conflicting_confirmation_preserves_audit_and_whole_batch(self):
        prior = {'key': 'VALOR:12345678', 'currency': 'EUR', 'settled_by': '2026-10-08',
                 'reason': 'Conflicting earlier record for manual review.'}
        LifecycleStore(self.database).confirm([prior])
        with self.assertRaisesRegex(ValueError, 'audit preserved'):
            self.run_repair()
        self.assertEqual(LifecycleStore(self.database).all(), [prior])
        self.assertFalse((self.database.parent / 'backups').exists())

    def test_backup_failure_prevents_annotations(self):
        with patch('src.backup.backup_database', side_effect=OSError('Synthetic backup failure')):
            with self.assertRaises(OSError):
                self.run_repair()
        self.assertEqual(LifecycleStore(self.database).all(), [])

    def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / 'missing.sqlite3'
        with self.assertRaises(ValueError):
            utility.record_expiries(missing, ['US0000000001'], 'USD', '2026-10-08')
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
