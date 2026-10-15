"""Record already owner-confirmed expiries without shell-quoted Python.

Run inside the existing app container. Security references are supplied by the
owner, never embedded in this public utility. No executions or prices are added.
"""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4


def application_path():
    # Console input or a downloaded utility can run outside the source tree.
    for root in (Path('/app'), Path(__file__).resolve().parents[1]):
        if (root / 'src' / 'lifecycle.py').is_file():
            sys.path.insert(0, str(root))
            return
    raise ValueError('Run this utility inside the existing app container')


def normalize_keys(values):
    keys = []
    for value in values:
        key = value.strip().upper()
        if re.fullmatch(r'\d{1,12}', key):
            key = 'VALOR:' + key
        if not re.fullmatch(r'(?:[A-Z0-9]{12}|VALOR:\d{1,12})', key):
            raise ValueError('Invalid security reference; use an ISIN or numeric Valor')
        if key not in keys:
            keys.append(key)
    if not 1 <= len(keys) <= 100:
        raise ValueError('Provide between one and 100 security references')
    return keys


def security_key(row):
    return row.get('isin') or ('VALOR:' + str(row['valor']) if row.get('valor') else None)


def record_expiries(database, values, currency, settled_by):
    application_path()
    from src.backup import backup_database
    from src.lifecycle import LifecycleStore
    from src.storage import SnapshotStore
    from src.ubs_activity import ActivityStore

    database = Path(database)
    if not database.is_file():
        raise ValueError('Existing application database not found; nothing was created')
    keys = normalize_keys(values)
    currency = currency.strip().upper()
    if not re.fullmatch(r'[A-Z]{3}', currency):
        raise ValueError('Provide a three-letter currency code')
    if date.fromisoformat(settled_by).isoformat() != settled_by:
        raise ValueError('Provide the settlement cutoff as YYYY-MM-DD')

    snapshot = SnapshotStore(database).latest()
    if not snapshot or snapshot['as_of'] < settled_by:
        raise ValueError('A UBS holdings snapshot on or after the cutoff is required')
    activity = ActivityStore(database).view(snapshot)
    if not activity:
        raise ValueError('Retained UBS transaction exports are required')
    known = {key: set() for key in keys}
    for row in activity['ledger']:
        key = security_key(row)
        if (key in known and row['category'] in {'security_trade', 'security_redemption'}
                and row['trade_date'] <= settled_by):
            known[key].add(row['currency'])
    for key, currencies in known.items():
        if not currencies:
            raise ValueError('A requested security has no retained UBS execution before the cutoff')
        if currencies != {currency}:
            raise ValueError('A requested security has a different UBS settlement currency')
    for position in snapshot['positions']:
        if security_key(position) in known and Decimal(position['quantity_or_balance']) != 0:
            raise ValueError('A requested security is still held in the latest UBS snapshot')

    store = LifecycleStore(database)
    existing = {(row['key'], row['settled_by']): row for row in store.all()}
    pending = []
    for key in keys:
        previous = existing.get((key, settled_by))
        if previous:
            if previous['currency'] != currency:
                raise ValueError('An existing confirmation has a different currency; audit preserved')
            continue  # Preserve the original source reason on repeated runs.
        pending.append({'key': key, 'currency': currency, 'settled_by': settled_by,
                        'reason': 'Owner confirmed expiry or margin settlement; recorded through owner console.'})
    if not pending:
        return {'confirmed': 0, 'already_recorded': len(keys), 'backup': None}

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = database.parent / 'backups' / f'expiry-confirmations-{stamp}-{uuid4().hex}.sqlite3'
    backup_database(database, backup)
    store.confirm(pending)
    return {'confirmed': len(pending), 'already_recorded': len(keys) - len(pending),
            'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('/data/frontoffice.sqlite3'))
    parser.add_argument('--settled-by', required=True)
    parser.add_argument('--currency', required=True)
    parser.add_argument('securities', nargs='+')
    args = parser.parse_args()
    try:
        result = record_expiries(args.db, args.securities, args.currency, args.settled_by)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'Expiry confirmation stopped: {error}\n')
    print(f"Expiry confirmations recorded: {result['confirmed']}; already recorded: {result['already_recorded']}")
    if result['backup']:
        print(f"Protected database backup: {result['backup']}")
    print('Refresh dashboard reconciliation to see the updated results.')


if __name__ == '__main__':
    main()
