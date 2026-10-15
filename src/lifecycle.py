"""Owner-confirmed settlement lifecycle, retained independently from executions."""
from datetime import date
import json
from pathlib import Path
import re
import sqlite3


class LifecycleStore:
    def __init__(self,path):self.path=Path(path)

    def all(self):
        if not self.path.exists():return []
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='settlement_confirmations'").fetchone():return []
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM settlement_confirmations')]

    def confirm(self,rows):
        if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError('Provide one to 100 settlement confirmations')
        clean=[]
        for r in rows:
            if not isinstance(r,dict) or set(r)!={'key','currency','settled_by','reason'}:raise ValueError('Invalid settlement confirmation fields')
            if not isinstance(r['key'],str) or not re.fullmatch(r'(?:[A-Z0-9]{12}|VALOR:\d{1,12})',r['key']):raise ValueError('Invalid security reference')
            if not isinstance(r['currency'],str) or not re.fullmatch('[A-Z]{3}',r['currency']):raise ValueError('Invalid settlement currency')
            if not isinstance(r['settled_by'],str) or date.fromisoformat(r['settled_by']).isoformat()!=r['settled_by']:raise ValueError('Invalid settlement cutoff')
            if not isinstance(r['reason'],str) or not 1<=len(r['reason'])<=512 or any(ord(c)<32 for c in r['reason']):raise ValueError('Confirmation requires a short plain-text source reason')
            clean.append(r)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS settlement_confirmations (security TEXT NOT NULL,date TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(security,date))')
            db.execute('BEGIN IMMEDIATE')
            for r in clean:
                old=db.execute('SELECT payload FROM settlement_confirmations WHERE security=? AND date=?',(r['key'],r['settled_by'])).fetchone()
                if old and json.loads(old[0])!=r:raise ValueError('Existing settlement confirmation differs; preserve audit and review')
                db.execute('INSERT OR IGNORE INTO settlement_confirmations VALUES (?,?,?)',(r['key'],r['settled_by'],json.dumps(r)))
        return {'confirmed':len(clean)}
