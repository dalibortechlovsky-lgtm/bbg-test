"""Consistent SQLite backup; existing files are never overwritten."""
import argparse
import os
from pathlib import Path
import sqlite3


def backup_database(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise ValueError('Source database does not exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as origin:
            with sqlite3.connect(destination) as target:
                origin.backup(target)
                if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Backup integrity check failed')
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    backup_database(args.source, args.destination)
    print('Consistent database backup created and integrity checked')


if __name__ == '__main__':
    main()
