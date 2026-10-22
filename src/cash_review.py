"""Owner cash classifications with an append-only decision history."""
from datetime import date,datetime,timezone
from decimal import Decimal,InvalidOperation
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
            required={'id','classification','reason'};basis={'opening_value','opening_currency','opening_as_of'}
            if not isinstance(row,dict) or not required.issubset(row) or set(row)-required-basis:raise ValueError('Invalid cash decision fields')
            if set(row)&basis and not basis.issubset(row):raise ValueError('Opening settlement basis requires value, currency and date')
            if not isinstance(row['id'],str) or row['id'] not in sources:raise ValueError('Cash posting not found')
            source=sources[row['id']]
            allowed={'external_funding','asset_settlement'} if source['category']=='transfer_review' else {'asset_settlement','fx_hedge_settlement'} if source['category']=='otc_settlement_review' else set()
            if not isinstance(row['classification'],str) or row['classification'] not in allowed:raise ValueError('Unsupported classification for this cash posting')
            if not isinstance(row['reason'],str) or not 1<=len(row['reason'])<=512 or any(ord(c)<32 for c in row['reason']):raise ValueError('A short plain-text decision reason is required')
            if basis.issubset(row):
                if source['category']!='transfer_review' or row['classification']!='asset_settlement':raise ValueError('Opening basis is only supported for an outside-bank trade settlement')
                try:value=Decimal(row['opening_value']);opening=date.fromisoformat(row['opening_as_of'])
                except (InvalidOperation,ValueError,TypeError):raise ValueError('Invalid opening settlement basis') from None
                if not value.is_finite() or value<=0:raise ValueError('Opening settlement value must be positive')
                if not isinstance(row['opening_currency'],str) or len(row['opening_currency'])!=3 or not row['opening_currency'].isalpha() or not row['opening_currency'].isupper():raise ValueError('Opening settlement currency must be a three-letter uppercase code')
                if opening>date.fromisoformat(source['booking_date']):raise ValueError('Opening settlement date must precede settlement')
                row=row|{'opening_value':str(value),'opening_as_of':opening.isoformat()}
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
