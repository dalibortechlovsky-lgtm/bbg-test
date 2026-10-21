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

    prices = rates

    def observations(self):
        """Return immutable price observations and optional bond accrued interest."""
        if not self.path.exists():return {}
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fx_history'").fetchone():return {}
            columns={r[1] for r in db.execute('PRAGMA table_info(fx_history)')}
            accrued='accrued' if 'accrued' in columns else 'NULL'
            return {(r[0],r[1]):{'price':Decimal(r[2]),
                    'accrued':Decimal(r[3]) if r[3] is not None else None,'source':r[4]}
                    for r in db.execute(f'SELECT security,date,price,{accrued},source FROM fx_history')}

    def sources(self):
        if not self.path.exists():return []
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fx_history'").fetchone():return []
            return [dict(zip(['security','date','price','source'],r)) for r in
                    db.execute('SELECT security,date,price,source FROM fx_history ORDER BY date,security')]

    def ingest(self,payload,requests,device_id=None,now=None):
        if not isinstance(payload,dict) or set(payload)!={'quotes'} or not isinstance(payload['quotes'],list) or len(payload['quotes'])>500:
            raise ValueError('Historical market-data payload requires up to 500 quotes')
        allowed={(r['security'],d) for r in requests for d in r['dates']}
        previous=self.observations();clean={}
        for row in payload['quotes']:
            if (not isinstance(row,dict) or set(row) not in
                    ({'security','date','price'},{'security','date','price','accrued'})):
                raise ValueError('Invalid historical quote fields')
            sec,day=row['security'],row['date']
            supported=(isinstance(sec,str) and
                       (re.fullmatch('[A-Z]{3}CZK Curncy',sec) or re.fullmatch('/isin/[A-Z0-9]{12}',sec)))
            if not supported:raise ValueError('Only requested CZK FX or ISIN price history is supported')
            if not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day:raise ValueError('Historical date must be YYYY-MM-DD')
            price=dec(row['price'],'historical price')
            if not Decimal('0.00000001')<=price<=Decimal('100000000'):raise ValueError('Historical price outside supported range')
            accrued=None
            if row.get('accrued') is not None:
                accrued=dec(row['accrued'],'historical accrued interest')
                if abs(accrued)>Decimal('100000000'):raise ValueError('Historical accrued interest outside supported range')
            key=sec,day
            if key not in allowed and key not in previous:raise ValueError('Unrequested historical date or security')
            if key in previous and (previous[key]['price']!=price or
                    previous[key]['accrued'] is not None and accrued is not None and previous[key]['accrued']!=accrued):
                raise ValueError('Historical observation conflict; preserve original and review correction')
            value={'price':price,'accrued':accrued}
            if key in clean and clean[key]!=value:raise ValueError('Conflicting historical rows')
            clean[key]=value
        self.path.parent.mkdir(parents=True,exist_ok=True)
        added=0
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS fx_history (security TEXT NOT NULL,date TEXT NOT NULL,price TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(security,date))')
            if 'accrued' not in {r[1] for r in db.execute('PRAGMA table_info(fx_history)')}:
                db.execute('ALTER TABLE fx_history ADD COLUMN accrued TEXT')
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            if device_id is not None:
                from .quotes import timestamp
                from datetime import datetime,timezone
                token=db.execute('SELECT expires_at,revoked FROM bridge_tokens WHERE id=?',(device_id,)).fetchone()
                if not token or token[1] or timestamp(token[0])<=(now or datetime.now(timezone.utc)):
                    raise PermissionError('Bridge token is no longer valid')
            for (sec,day),value in clean.items():
                old=db.execute('SELECT price,accrued FROM fx_history WHERE security=? AND date=?',(sec,day)).fetchone()
                if old and (Decimal(old[0])!=value['price'] or old[1] is not None and value['accrued'] is not None and Decimal(old[1])!=value['accrued']):
                    raise ValueError('Concurrent historical observation conflict')
                added+=db.execute('INSERT OR IGNORE INTO fx_history (security,date,price,source,accrued) VALUES (?,?,?,?,?)',
                                  (sec,day,str(value['price']),'Bloomberg PX_LAST daily; previous close on non-trading days',
                                   None if value['accrued'] is None else str(value['accrued']))).rowcount
        return {'accepted':added,'existing':len(clean)-added}
