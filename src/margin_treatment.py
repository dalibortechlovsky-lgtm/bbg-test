"""Audited futures-style option treatment, independent of bank descriptions."""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class MarginTreatmentStore:
    def __init__(self, path): self.path = Path(path)

    def all(self):
        if not self.path.exists(): return {}
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='margin_treatments'").fetchone(): return {}
            return {k: json.loads(v) for k,v in db.execute('SELECT key,payload FROM margin_treatments')}

    def record(self, key, basis):
        if not re.fullmatch(r'(?:[A-Z0-9]{12}|VALOR:\d{1,12})', key): raise ValueError('Invalid instrument key')
        if not isinstance(basis,str) or not 1 <= len(basis) <= 2000: raise ValueError('Source evidence required')
        payload=json.dumps(dict(style='futures_margin',basis=basis),sort_keys=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS margin_treatments (key TEXT PRIMARY KEY,payload TEXT NOT NULL,recorded_at TEXT NOT NULL)')
            old=db.execute('SELECT payload FROM margin_treatments WHERE key=?',(key,)).fetchone()
            if old and old[0]!=payload: raise ValueError('Existing treatment differs; review correction')
            db.execute('INSERT OR IGNORE INTO margin_treatments VALUES (?,?,?)',(key,payload,datetime.now(timezone.utc).isoformat()))
        return {'recorded':int(not old),'existing':int(bool(old))}
