"""Audited instrument identifiers and units used for dated reconstruction."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3


KEY = re.compile(r'(?:[A-Z0-9]{12}|VALOR:\d{1,12})')
OPTION = re.compile(r'[A-Z0-9]{1,8} [A-Z]{2} \d{2}/\d{2}/\d{2} [CP]\d+(?:\.\d+)? (?:Equity|Index)')


def supported_market_security(value):
    return isinstance(value, str) and bool(
        re.fullmatch(r'[A-Z]{3}CZK Curncy', value)
        or re.fullmatch(r'/isin/[A-Z0-9]{12}', value)
        or OPTION.fullmatch(value))


class HistoricalTermsStore:
    """Immutable source-backed mappings; changed terms require an explicit review."""
    def __init__(self, path): self.path = Path(path)

    def all(self):
        if not self.path.exists(): return {}
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='historical_instrument_terms'").fetchone():
                return {}
            return {key: json.loads(payload) for key, payload in db.execute(
                'SELECT key,payload FROM historical_instrument_terms')}

    def record(self, rows):
        if not isinstance(rows, list) or not rows: raise ValueError('Supply historical instrument terms')
        clean = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                    'key', 'security', 'unit_scale', 'basis', 'review_required'}:
                raise ValueError('Historical terms require key, security, unit scale, basis and review status')
            if not isinstance(row['key'], str) or not KEY.fullmatch(row['key']):
                raise ValueError('Invalid UBS instrument key')
            if not supported_market_security(row['security']):
                raise ValueError('Unsupported Bloomberg historical identifier')
            try: scale = Decimal(str(row['unit_scale']))
            except (InvalidOperation, TypeError): raise ValueError('Invalid historical unit scale') from None
            if not scale.is_finite() or not Decimal('0.000000001') <= scale <= Decimal('1000000000'):
                raise ValueError('Historical unit scale outside supported range')
            if (not isinstance(row['basis'], str) or not 1 <= len(row['basis'].strip()) <= 512
                    or any(ord(c) < 32 for c in row['basis'])):
                raise ValueError('Historical terms need a short source basis')
            if not isinstance(row['review_required'], bool): raise ValueError('Review status must be boolean')
            clean.append({'key': row['key'], 'security': row['security'], 'unit_scale': str(scale),
                          'basis': row['basis'].strip(), 'review_required': row['review_required']})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        added = existing = 0
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS historical_instrument_terms '
                       '(key TEXT PRIMARY KEY,payload TEXT NOT NULL,recorded_at TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            for row in clean:
                payload = json.dumps({k: row[k] for k in row if k != 'key'}, sort_keys=True,
                                     separators=(',', ':'))
                old = db.execute('SELECT payload FROM historical_instrument_terms WHERE key=?',
                                 (row['key'],)).fetchone()
                if old:
                    if old[0] != payload:
                        raise ValueError(row['key'] + ': historical terms already exist; review a correction')
                    existing += 1
                else:
                    db.execute('INSERT INTO historical_instrument_terms VALUES (?,?,?)',
                               (row['key'], payload, datetime.now(timezone.utc).isoformat()))
                    added += 1
        return {'added': added, 'existing': existing}
