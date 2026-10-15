"""Owner cash classifications with an append-only decision history."""
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3


class CashReviewStore:
    def __init__(self,path):self.path=Path(path)

    def all(self):
        if not self.path.exists():return {}
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cash_reviews'").fetchone():return {}
            return {r['id']:r for payload, in db.execute('SELECT payload FROM cash_reviews ORDER BY rowid') for r in [json.loads(payload)]}

    def record(self,rows,ledger):
        if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError('Provide one to 100 cash decisions')
        sources={r['id']:r for r in ledger};clean=[]
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'id','classification','reason'}:raise ValueError('Invalid cash decision fields')
            if not isinstance(row['id'],str) or row['id'] not in sources:raise ValueError('Cash posting not found')
            source=sources[row['id']]
            allowed={'external_funding'} if source['category']=='transfer_review' else {'asset_settlement','fx_hedge_settlement'} if source['category']=='otc_settlement_review' else set()
            if not isinstance(row['classification'],str) or row['classification'] not in allowed:raise ValueError('Unsupported classification for this cash posting')
            if not isinstance(row['reason'],str) or not 1<=len(row['reason'])<=512 or any(ord(c)<32 for c in row['reason']):raise ValueError('A short plain-text decision reason is required')
            clean.append(row)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cash_reviews (id TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            for row in clean:
                previous=db.execute('SELECT payload FROM cash_reviews WHERE id=? ORDER BY rowid DESC LIMIT 1',(row['id'],)).fetchone()
                if previous and json.loads(previous[0])==row:continue
                db.execute('INSERT INTO cash_reviews VALUES (?,?,?)',(row['id'],datetime.now(timezone.utc).isoformat(),json.dumps(row)))
        return {'reviewed':len(clean)}
