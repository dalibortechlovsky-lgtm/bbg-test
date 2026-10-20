"""Append-only Bloomberg daily FX fixings; device access is market-data only."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
from .performance import dec


class HistoryStore:
    def __init__(self,path): self.path=Path(path)

    def rates(self):
        if not self.path.exists():return {}
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fx_history'").fetchone():return {}
            return {(r[0],r[1]):Decimal(r[2]) for r in db.execute('SELECT security,date,price FROM fx_history')}

    def sources(self):
        if not self.path.exists():return []
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fx_history'").fetchone():return []
            return [dict(zip(['security','date','price','source'],r)) for r in
                    db.execute('SELECT security,date,price,source FROM fx_history ORDER BY date,security')]

    def ingest(self,payload,requests,device_id=None,now=None):
        if not isinstance(payload,dict) or set(payload)!={'quotes'} or not isinstance(payload['quotes'],list) or len(payload['quotes'])>500:
            raise ValueError('Historical FX payload requires up to 500 quotes')
        allowed={(r['security'],d) for r in requests for d in r['dates']}
        previous=self.rates();clean={}
        for row in payload['quotes']:
            if not isinstance(row,dict) or set(row)!={'security','date','price'}:raise ValueError('Invalid historical quote fields')
            sec,day=row['security'],row['date']
            if not isinstance(sec,str) or not re.fullmatch('[A-Z]{3}CZK Curncy',sec):raise ValueError('Only requested CZK FX fixings are supported')
            if not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day:raise ValueError('Historical date must be YYYY-MM-DD')
            price=dec(row['price'],'historical FX')
            if not Decimal('0.00000001')<=price<=Decimal('100000000'):raise ValueError('Historical FX outside supported range')
            key=sec,day
            if key not in allowed and key not in previous:raise ValueError('Unrequested historical FX date or security')
            if key in previous and previous[key]!=price:raise ValueError('Historical fixing conflict; preserve original and review correction')
            if key in clean and clean[key]!=price:raise ValueError('Conflicting historical rows')
            clean[key]=price
        self.path.parent.mkdir(parents=True,exist_ok=True)
        added=0
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS fx_history (security TEXT NOT NULL,date TEXT NOT NULL,price TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(security,date))')
            db.execute('BEGIN IMMEDIATE')
            if device_id is not None:
                from .quotes import timestamp
                from datetime import datetime,timezone
                token=db.execute('SELECT expires_at,revoked FROM bridge_tokens WHERE id=?',(device_id,)).fetchone()
                if not token or token[1] or timestamp(token[0])<=(now or datetime.now(timezone.utc)):
                    raise PermissionError('Bridge token is no longer valid')
            for (sec,day),price in clean.items():
                old=db.execute('SELECT price FROM fx_history WHERE security=? AND date=?',(sec,day)).fetchone()
                if old and Decimal(old[0])!=price:raise ValueError('Concurrent historical fixing conflict')
                added+=db.execute('INSERT OR IGNORE INTO fx_history VALUES (?,?,?,?)',
                                  (sec,day,str(price),'Bloomberg PX_LAST daily; previous close on non-trading days')).rowcount
        return {'accepted':added,'existing':len(clean)-added}
