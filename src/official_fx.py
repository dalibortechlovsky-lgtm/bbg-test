"""Owner-run CNB FX backfill with retained source documents and no overwrites."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

from .backup import backup_database
from .history import HistoryStore


def cnb_url(year):
    if not isinstance(year,int) or not 2000 <= year <= 2100:
        raise ValueError('Unsupported CNB source year')
    return ('https://www.cnb.cz/en/financial_markets/foreign_exchange_market/'
            'exchange_rate_fixing/year.txt?year='+str(year))


def parse_documents(documents,requests):
    if not isinstance(documents,list) or not 1 <= len(documents) <= 4:
        raise ValueError('One to four original CNB yearly documents required')
    rates={};sources={}
    for document in documents:
        if set(document)!={'year','url','text'} or document['url']!=cnb_url(document['year']):
            raise ValueError('Unexpected CNB source URL or fields')
        content=document['text']
        if not isinstance(content,str) or not 1 <= len(content.encode()) <= 1024*1024:
            raise ValueError('Invalid CNB source size')
        digest=hashlib.sha256(content.encode()).hexdigest()
        lines=content.strip().splitlines();header=lines[0].lstrip('\ufeff').split('|')
        if header[0]!='Date' or len(header)<2:
            raise ValueError('Unrecognized CNB yearly fixing layout')
        columns=[]
        for name in header[1:]:
            match=re.fullmatch(r'([1-9]\d*) ([A-Z]{3})',name)
            if not match:raise ValueError('Invalid CNB currency unit heading')
            columns.append((match[2],Decimal(match[1])))
        if len({c for c,_ in columns})!=len(columns):
            raise ValueError('Duplicate CNB currency column')
        for line in lines[1:]:
            fields=line.split('|')
            if len(fields)!=len(header):raise ValueError('Incomplete CNB fixing row')
            day=datetime.strptime(fields[0],'%d.%m.%Y').date()
            if day.year!=document['year']:raise ValueError('CNB fixing year differs from requested source')
            for (currency,unit),value in zip(columns,fields[1:]):
                price=Decimal(value)/unit
                if not price.is_finite() or not Decimal('0.00000001')<=price<=Decimal('100000000'):
                    raise ValueError('Invalid official FX fixing')
                key=currency,day
                if key in rates and rates[key]!=price:raise ValueError('Conflicting CNB source fixings')
                rates[key]=price;sources[key]=(digest,document['url'])
    rows=[]
    for request in requests:
        security=request['security']
        if not re.fullmatch('[A-Z]{3}CZK Curncy',security):raise ValueError('Only CZK FX requests supported')
        currency=security[:3]
        for value in request['dates']:
            day=date.fromisoformat(value)
            if day.isoformat()!=value:raise ValueError('Invalid requested FX date')
            candidates=[d for c,d in rates if c==currency and day-timedelta(days=7)<=d<=day]
            if not candidates:raise ValueError('Official fixing unavailable: '+security+' on '+value)
            fixing=max(candidates);digest,url=sources[currency,fixing]
            rows.append({'security':security,'date':value,'price':str(rates[currency,fixing]),
                         'source':f'CNB official daily fixing {fixing.isoformat()}; source {url}; SHA256 {digest}'})
    return rows


def import_documents(path,documents,requests):
    path=Path(path)
    if not path.is_file():raise ValueError('Existing application database required')
    rows=parse_documents(documents,requests)
    previous=HistoryStore(path).rates();clean={}
    for row in rows:
        key=row['security'],row['date']
        if key in previous:
            if previous[key]!=Decimal(row['price']):
                raise ValueError('Stored fixing differs; preserve original and review source')
            continue
        if key in clean and clean[key]!=row:raise ValueError('Conflicting official fixings')
        clean[key]=row
    if not clean:return {'accepted':0,'existing':len(rows),'backup':None}
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=path.parent/'backups'/('official-fx-'+stamp+'.sqlite3')
    backup_database(path,backup)
    added=0
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS fx_history (security TEXT NOT NULL,date TEXT NOT NULL,price TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(security,date))')
        db.execute('CREATE TABLE IF NOT EXISTS fx_source_documents (sha256 TEXT PRIMARY KEY,url TEXT NOT NULL,retrieved_at TEXT NOT NULL,content TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        for row in clean.values():
            old=db.execute('SELECT price FROM fx_history WHERE security=? AND date=?',(row['security'],row['date'])).fetchone()
            if old and Decimal(old[0])!=Decimal(row['price']):raise ValueError('Concurrent fixing conflict; original preserved')
            added+=db.execute('INSERT OR IGNORE INTO fx_history VALUES (?,?,?,?)',tuple(row[k] for k in ['security','date','price','source'])).rowcount
        for document in documents:
            digest=hashlib.sha256(document['text'].encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO fx_source_documents VALUES (?,?,?,?)',
                       (digest,document['url'],datetime.now(timezone.utc).isoformat(),document['text']))
    return {'accepted':added,'existing':len(rows)-added,'backup':str(backup)}


def main():
    from .accounting import accounting_view
    path=Path('/data/frontoffice.sqlite3')
    payload=json.load(sys.stdin)
    if not isinstance(payload,dict) or set(payload)!={'documents'}:raise ValueError('Original source documents required')
    requests=accounting_view(path)['history_requests']
    if not requests:
        print('No historical FX backfill needed.')
        return
    result=import_documents(path,payload['documents'],requests)
    print('Official CNB fixings added:',result['accepted'],'already stored:',result['existing'])
    print('Protected backup:',result['backup'])


if __name__=='__main__':main()
