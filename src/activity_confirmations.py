"""Audited owner statements about account activity; never synthetic bank exports."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from .ubs_activity import iban


def conflicts(row,records):
    return any(r['account']==row['account'] and
               (row['from']<=r['trade_date']<=row['until'] or
                row['kind']=='no_activity' and row['from']<=r['booking_date']<=row['until'])
               for r in records)


def applicable(confirmations,records,start,cutoff):
    valid=[];issues=[]
    for row in confirmations:
        if row['until']<start or row['from']>cutoff:continue
        if conflicts(row,records):
            issues.append('Owner activity confirmation conflicts with UBS transactions: '+
                          row['currency']+' '+row['from']+' to '+row['until'])
        else:valid.append(row)
    return valid,issues


def with_confirmed_intervals(account,confirmations):
    intervals=account.get('coverage_intervals',[(account['from'],account['until'])])
    owner=[(r['from'],r['until']) for r in confirmations
           if r['account']==account['account'] and r['currency']==account['currency']]
    return account|{'coverage_intervals':list(intervals)+owner}


class ActivityConfirmationStore:
    def __init__(self,path):self.path=Path(path)

    def all(self):
        if not self.path.exists():return []
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='activity_confirmations'").fetchone():return []
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM activity_confirmations ORDER BY rowid')]

    def confirm(self,rows):
        from .storage import SnapshotStore
        from .ubs_activity import ActivityStore
        if not self.path.is_file():raise ValueError('Existing application database required')
        if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError('Provide one to 100 owner confirmations')
        snapshot=SnapshotStore(self.path).latest()
        if not snapshot:raise ValueError('UBS account snapshot required')
        known={(iban(p.get('raw',{}).get('IBAN','')),p['currency']) for p in snapshot['positions'] if p['kind']=='cash_account'}
        records=(ActivityStore(self.path).view(snapshot) or {}).get('ledger',[])
        clean=[]
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'account','currency','from','until','kind','reason'}:
                raise ValueError('Invalid owner activity confirmation fields')
            if not all(isinstance(v,str) for v in row.values()):raise ValueError('Confirmation fields must be text')
            row=row|{'account':iban(row['account']),'currency':row['currency'].upper()}
            if not re.fullmatch(r'[A-Z]{2}\d{2}[A-Z0-9]{11,30}',row['account']) or (row['account'],row['currency']) not in known:
                raise ValueError('Confirmed account/currency not present in UBS snapshot')
            if row['kind'] not in {'no_activity','no_further_transactions'}:raise ValueError('Unsupported confirmation kind')
            for key in ['from','until']:
                if date.fromisoformat(row[key]).isoformat()!=row[key]:raise ValueError('Invalid confirmation date')
            if row['from']>row['until'] or date.fromisoformat(row['until'])>date.today():raise ValueError('Invalid confirmation period')
            if not 1<=len(row['reason'])<=512 or any(ord(c)<32 for c in row['reason']):raise ValueError('Short plain-text owner source reason required')
            if conflicts(row,records):raise ValueError('Owner confirmation conflicts with retained UBS transactions')
            clean.append(row)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS activity_confirmations (account TEXT NOT NULL,kind TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,kind,start,end))')
            db.execute('BEGIN IMMEDIATE')
            added=0
            for row in clean:
                key=tuple(row[k] for k in ['account','kind','from','until'])
                old=db.execute('SELECT payload FROM activity_confirmations WHERE account=? AND kind=? AND start=? AND end=?',key).fetchone()
                if old and json.loads(old[0])!=row:raise ValueError('Existing owner statement differs; preserve audit')
                added+=db.execute('INSERT OR IGNORE INTO activity_confirmations VALUES (?,?,?,?,?,?)',
                                  (*key,datetime.now(timezone.utc).isoformat(),json.dumps(row))).rowcount
        return {'confirmed':added,'existing':len(clean)-added}
