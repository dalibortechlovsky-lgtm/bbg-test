"""Atomic, append-only local snapshot storage. Raw exports stay outside Git."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from .positions import parse_positions

DEFAULT_DB = Path(__file__).resolve().parents[1] / 'data' / 'frontoffice.sqlite3'


def has_table(db, name):
    return db.execute('SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?',
                      ('table', name)).fetchone() is not None


class SnapshotStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)

    def import_content(self, content):
        snapshot = parse_positions(content)  # Reject completely before opening a write transaction.
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        snapshot['id'] = digest
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshots '
                       '(id TEXT PRIMARY KEY, as_of TEXT NOT NULL, exported_at TEXT NOT NULL, payload TEXT NOT NULL)')
            cursor = db.execute('INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?)',
                                (digest, snapshot['as_of'], snapshot['exported_at'], json.dumps(snapshot)))
            snapshot['already_imported'] = cursor.rowcount == 0
        return snapshot

    def latest(self):
        if not self.path.exists():
            return None
        with sqlite3.connect(self.path) as db:
            if not has_table(db, 'snapshots'):
                return None
            result = db.execute('SELECT payload FROM snapshots '
                                'ORDER BY as_of DESC, exported_at DESC, rowid DESC LIMIT 1').fetchone()
        return json.loads(result[0]) if result else None

    def import_orders_content(self, content):
        from .keytrader import import_keytrader_csv
        parsed = import_keytrader_csv(content)
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        candidates = [json.loads(json.dumps(trade.to_dict(), default=str)) for trade in parsed.trades]
        result = {'id': digest, 'rows': parsed.rows, 'added': 0, 'existing': 0,
                  'review': parsed.review, 'errors': list(parsed.errors),
                  'accepted': not parsed.errors, 'already_imported': False,
                  'imported_at': datetime.now(timezone.utc).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS order_imports
                (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS executions
                (source TEXT NOT NULL, account TEXT NOT NULL, external_id TEXT NOT NULL,
                 payload TEXT NOT NULL, import_id TEXT NOT NULL,
                 PRIMARY KEY (source, account, external_id));
            ''')
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT payload FROM order_imports WHERE id = ?', (digest,)).fetchone()
            if previous:
                prior = json.loads(previous[0])
                prior['already_imported'] = True
                prior['existing'] += prior['added']
                prior['added'] = 0
                return prior
            new = []
            for item in parsed.review:
                if item['reason'] == 'NON_COMPLETED_ORDER' and db.execute(
                    'SELECT 1 FROM executions WHERE source = ? AND account = ? AND external_id = ?',
                    ('ubs_keytrader', item['account'], item['order']),
                ).fetchone():
                    result['errors'].append({'order': item['order'], 'account': item['account'],
                                             'reason': 'NON_COMPLETED_STATE_FOR_STORED_ORDER'})
            for trade in candidates:
                key = (trade['source'], trade['account'], trade['external_id'])
                existing = db.execute('SELECT payload FROM executions WHERE source = ? '
                                      'AND account = ? AND external_id = ?', key).fetchone()
                if existing:
                    old = json.loads(existing[0])
                    # Decimal scale changes do not constitute an economic correction.
                    equivalent = all(Decimal(old[k]) == Decimal(trade[k]) for k in ['quantity', 'price'])
                    equivalent = equivalent and all(old[k] == trade[k] for k in trade if k not in {'quantity', 'price'})
                    if equivalent:
                        result['existing'] += 1
                    else:
                        result['errors'].append({'order': trade['external_id'], 'account': trade['account'],
                                                 'reason': 'CONFLICT_WITH_STORED_ORDER'})
                else:
                    new.append((key, trade))
            result['accepted'] = not result['errors']
            if result['accepted']:
                for key, trade in new:
                    db.execute('INSERT INTO executions VALUES (?, ?, ?, ?, ?)',
                               (*key, json.dumps(trade), digest))
                result['added'] = len(new)
            db.execute('INSERT INTO order_imports VALUES (?, ?)', (digest, json.dumps(result)))
        return result

    def orders(self):
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as db:
            if not has_table(db, 'executions'):
                return []
            trades = [json.loads(row[0]) for row in db.execute('SELECT payload FROM executions')]
        return sorted(trades, key=lambda trade: (trade['order_entered_at'] or '', trade['external_id']), reverse=True)

    def order_imports(self):
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as db:
            if not has_table(db, 'order_imports'):
                return []
            return [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM order_imports ORDER BY rowid DESC LIMIT 20')]
