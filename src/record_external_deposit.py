"""Register owner-confirmed terms from a private JSON file, without rewriting bank data."""
import argparse
from datetime import date,datetime,timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3


def record(path, terms):
    required={'id','bank','currency','principal','annual_rate','opening_as_of','interest_start','until','source'}
    if set(terms)!=required:raise ValueError('Deposit terms fields do not match required schema')
    for key in ('opening_as_of','interest_start','until'):date.fromisoformat(terms[key])
    if not terms['opening_as_of'].endswith('-12-31') or terms['interest_start']!=str(int(terms['opening_as_of'][:4])+1)+'-01-01':
        raise ValueError('This deposit estimate requires a prior-year opening and January 1 interest start')
    if terms['until']!=terms['interest_start'][:4]+'-12-31':raise ValueError('Confirm terms separately for each valuation year')
    if len(terms['currency'])!=3 or not terms['currency'].isupper():raise ValueError('Invalid currency')
    for key in ('principal','annual_rate'):
        d=Decimal(terms[key])
        if not d.is_finite() or d<0:raise ValueError('Invalid amount or rate')
    if Decimal(terms['annual_rate'])>1 or Decimal(terms['principal'])<=0:raise ValueError('Invalid deposit terms')
    if not terms['source'].strip() or not terms['id'].startswith('external-'):raise ValueError('Owner evidence and external identifier required')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS external_deposits(id TEXT PRIMARY KEY,payload TEXT NOT NULL,recorded_at TEXT NOT NULL)')
        prior=db.execute('SELECT payload FROM external_deposits WHERE id=?',(terms['id'],)).fetchone()
        if prior:
            if json.loads(prior[0])!=terms:raise ValueError('Existing terms conflict; review correction instead of overwriting')
            return False
        db.execute('INSERT INTO external_deposits VALUES (?,?,?)',(terms['id'],json.dumps(terms),datetime.now(timezone.utc).isoformat()))
    return True


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('database');parser.add_argument('terms_file');args=parser.parse_args()
    print('Deposit recorded' if record(args.database,json.loads(Path(args.terms_file).read_text())) else 'Identical deposit already recorded')
