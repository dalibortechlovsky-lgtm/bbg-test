"""Scoped device tokens and latest Bloomberg quotes, separate from UBS valuations."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import secrets
import sqlite3
import uuid
from .storage import SnapshotStore, has_table
from .mars import MarsStore


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Quote timestamp is required')
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('Quote timestamp must include its timezone')
    return date.astimezone(timezone.utc)


def schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS bridge_tokens
        (id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
         created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS bridge_heartbeats
        (device_id TEXT PRIMARY KEY, status TEXT NOT NULL, uploaded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bloomberg_quotes
        (device_id TEXT NOT NULL, security TEXT NOT NULL, payload TEXT NOT NULL,
         PRIMARY KEY (device_id, security));
    ''')


class QuoteStore:
    def __init__(self, path, now=utcnow):
        self.path = path
        self.now = now

    def create_token(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            raise ValueError('Device name must contain 1–64 characters')
        token = 'bbg_' + secrets.token_urlsafe(32)
        current = self.now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        device = {'id': uuid.uuid4().hex, 'name': name.strip(), 'created_at': current.isoformat(),
                  'expires_at': (current + timedelta(days=90)).isoformat()}
        with sqlite3.connect(self.path) as db:
            schema(db)
            db.execute('INSERT INTO bridge_tokens (id,name,digest,created_at,expires_at) VALUES (?,?,?,?,?)',
                       (device['id'], device['name'], hashlib.sha256(token.encode()).hexdigest(),
                        device['created_at'], device['expires_at']))
        return device | {'token': token}

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith('Bearer '):
            return None
        token = authorization[7:]
        if not 20 <= len(token) <= 256 or not self.path.exists():
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with sqlite3.connect(self.path) as db:
            if not has_table(db, 'bridge_tokens'):
                return None
            row = db.execute('SELECT id,expires_at,revoked FROM bridge_tokens WHERE digest=?', (digest,)).fetchone()
        return row[0] if row and not row[2] and timestamp(row[1]) > self.now() else None

    def devices(self):
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as db:
            if not has_table(db, 'bridge_tokens'):
                return []
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                'SELECT id,name,created_at,expires_at,revoked FROM bridge_tokens ORDER BY created_at DESC')]

    def revoke(self, device_id):
        if not self.path.exists():
            return False
        with sqlite3.connect(self.path) as db:
            schema(db)
            return db.execute('UPDATE bridge_tokens SET revoked=1 WHERE id=?', (device_id,)).rowcount == 1

    def watchlist(self):
        snapshot = SnapshotStore(self.path).latest()
        mars = MarsStore(self.path).latest()
        mars_tickers = {p['ticker'] for p in mars['instruments']} if mars else set()
        if not snapshot and not mars:
            return {'securities': ['IBM US Equity'], 'unmapped_count': 0, 'snapshot_as_of': None}
        positions = snapshot['positions'] if snapshot else []
        ids = sorted({p['isin'] for p in positions if p.get('isin')})
        base = snapshot['valuation_currency'] if snapshot else None
        currencies = sorted({p['currency'] for p in positions if p['currency'] != base})
        securities = {'/isin/' + value for value in ids} | mars_tickers | {ccy + base + ' Curncy' for ccy in currencies}
        if mars and mars.get('portfolio_currency'):
            report_ccy=mars['portfolio_currency']
            quote_currencies={q['currency'] for q in self.view()['quotes'] if q['security'] in mars_tickers and q.get('currency')}
            securities |= {ccy + report_ccy + ' Curncy' for ccy in quote_currencies if ccy != report_ccy}
        from .accounting import accounting_view
        accounting=accounting_view(self.path,snapshot)
        from .historical_positions import HistoricalRequestStore
        requested=HistoricalRequestStore(self.path).history_requests()
        history=defaultdict(set)
        for row in (accounting.get('history_requests',[]) if accounting else [])+requested:
            history[row['security']].update(row['dates'])
        return {'securities': sorted(securities),
                'history_requests':[{'security':s,'dates':sorted(days)} for s,days in sorted(history.items())],
                'unmapped_count': sum(p['kind'] == 'security' and not p.get('isin') for p in positions),
                'snapshot_as_of': snapshot['as_of'] if snapshot else None,
                'mars_as_of': mars['as_of'] if mars else None}

    def ingest(self, device_id, state):
        if not isinstance(state, dict) or state.get('schema_version') != 1:
            raise ValueError('Unsupported quote payload')
        status = state.get('status')
        if status not in {'connected', 'degraded', 'disconnected', 'starting', 'stopped'}:
            raise ValueError('Invalid bridge status')
        rows = state.get('quotes')
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError('At most 500 quotes may be uploaded')
        now = self.now()
        clean, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid quote')
            security = row.get('requested_security') or row.get('security')
            if (not isinstance(security, str) or not 1 <= len(security) <= 256
                    or any(ord(char) < 32 for char in security) or security in seen):
                raise ValueError('Invalid or duplicate security')
            seen.add(security)
            fields = row.get('fieldData')
            if not isinstance(fields, dict) or row.get('securityError'):
                raise ValueError('Quote has no valid field data')
            try:
                price = Decimal(str(fields['PX_LAST']))
            except (InvalidOperation, KeyError):
                raise ValueError('Quote price is missing or invalid') from None
            if not price.is_finite():
                raise ValueError('Quote price must be finite')
            observed = timestamp(row.get('observed_at'))
            if observed > now + timedelta(seconds=120):
                raise ValueError('Windows clock is more than two minutes ahead')
            isin = fields.get('ID_ISIN') or None
            if security.startswith('/isin/'):
                expected = security[6:]
                if isin and isin != expected:
                    raise ValueError('Bloomberg ISIN does not match requested security')
                isin = expected
            if isin is not None and (not isinstance(isin, str) or len(isin) > 32):
                raise ValueError('Invalid ISIN')
            currency = fields.get('CRNCY') or None
            if currency is not None and (not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha()):
                raise ValueError('Invalid quote currency')
            bbid=fields.get('ID_BB_UNIQUE') or None
            if bbid is not None and (not isinstance(bbid,str) or len(bbid)>64 or any(ord(c)<32 for c in bbid)):
                raise ValueError('Invalid Bloomberg unique identifier')
            clean.append({'security': security, 'isin': isin, 'bbid':bbid, 'price': str(price), 'currency': currency,
                          'observed_at': observed.isoformat(), 'uploaded_at': now.isoformat(), 'source': 'Bloomberg PX_LAST'})
        if status == 'connected' and not clean:
            raise ValueError('Connected bridge must supply at least one quote')
        accepted, ignored = 0, 0
        with sqlite3.connect(self.path) as db:
            schema(db)
            # Recheck revocation inside the write transaction.
            db.execute('BEGIN IMMEDIATE')
            token = db.execute('SELECT expires_at,revoked FROM bridge_tokens WHERE id=?', (device_id,)).fetchone()
            if not token or token[1] or timestamp(token[0]) <= now:
                raise PermissionError('Bridge token is no longer valid')
            for row in clean:
                previous = db.execute('SELECT payload FROM bloomberg_quotes WHERE device_id=? AND security=?',
                                      (device_id, row['security'])).fetchone()
                if previous and timestamp(json.loads(previous[0])['observed_at']) >= timestamp(row['observed_at']):
                    ignored += 1
                    continue
                db.execute('INSERT OR REPLACE INTO bloomberg_quotes VALUES (?,?,?)',
                           (device_id, row['security'], json.dumps(row)))
                accepted += 1
            db.execute('INSERT OR REPLACE INTO bridge_heartbeats VALUES (?,?,?)',
                       (device_id, status, now.isoformat()))
        return {'accepted': accepted, 'ignored': ignored}

    def view(self):
        now, quotes, devices = self.now(), [], []
        if not self.path.exists():
            return {'status': 'Not connected', 'quotes': [], 'devices': []}
        with sqlite3.connect(self.path) as db:
            if not has_table(db, 'bridge_tokens'):
                return {'status': 'Not connected', 'quotes': [], 'devices': []}
            rows = db.execute('SELECT t.id,t.name,h.status,h.uploaded_at,t.expires_at,t.revoked FROM bridge_tokens t '
                              'LEFT JOIN bridge_heartbeats h ON h.device_id=t.id').fetchall()
            for device_id, name, status, uploaded, expiry, revoked in rows:
                if revoked or timestamp(expiry) <= now:
                    continue
                age = (now - timestamp(uploaded)).total_seconds() if uploaded else None
                effective = 'offline' if age is None or age > 90 else status
                devices.append({'id': device_id, 'name': name, 'status': effective, 'uploaded_at': uploaded})
                for (payload,) in db.execute('SELECT payload FROM bloomberg_quotes WHERE device_id=?', (device_id,)):
                    quote = json.loads(payload)
                    quote_age = max((now - timestamp(quote['observed_at'])).total_seconds(),
                                    (now - timestamp(quote['uploaded_at'])).total_seconds(), 0)
                    quote.update(device=name, age_seconds=round(quote_age), stale=quote_age > 90 or effective not in {'connected', 'degraded'})
                    quotes.append(quote)
        if not devices:
            label = 'Not connected'
        elif any(d['status'] == 'connected' for d in devices) and any(not q['stale'] for q in quotes):
            label = 'Receiving Bloomberg snapshots'
        elif any(not q['stale'] for q in quotes):
            label = 'Receiving partial snapshots · review missing instruments'
        else:
            label = 'Offline or degraded · prices may be stale'
        return {'status': label, 'quotes': sorted(quotes, key=lambda q: q['security']), 'devices': devices}
