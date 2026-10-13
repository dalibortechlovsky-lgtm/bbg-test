from pathlib import Path
import tempfile
import unittest
from src.backup import backup_database
from src.storage import SnapshotStore
from test_keytrader import export


class BackupTests(unittest.TestCase):
    def test_restore_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.sqlite3'
            destination = Path(directory) / 'backup.sqlite3'
            store = SnapshotStore(source)
            store.import_orders_content(export())
            backup_database(source, destination)
            restored = SnapshotStore(destination)
            self.assertEqual(len(restored.orders()), 1)
            self.assertEqual(len(restored.order_imports()), 1)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                backup_database(source, destination)
            self.assertEqual(len(restored.orders()), 1)

    def test_missing_source_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'missing.sqlite3'
            destination = Path(directory) / 'backup.sqlite3'
            with self.assertRaises(ValueError):
                backup_database(source, destination)
            self.assertFalse(source.exists())
            self.assertFalse(destination.exists())
