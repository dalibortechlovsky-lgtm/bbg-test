"""Owner-confirmed deposits held outside the imported bank portfolio.

Terms live in the private database, never in source code. Interest is an
ACT/365 simple estimate, including the selected day; no assumed compounding.
"""
import copy
import json
import sqlite3
from datetime import date
from decimal import Decimal as D
from .report import fx_rate


def terms(path):
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='external_deposits'").fetchone(): return []
        return [json.loads(r[0]) for r in db.execute('SELECT payload FROM external_deposits ORDER BY id')]


def applicable(path, day):
    return [r for r in terms(path) if r['opening_as_of'] <= day <= r['until']]


def native_positions(path, day):
    rows=[]
    for r in applicable(path,day):
        days=max(0,(date.fromisoformat(day)-date.fromisoformat(r['interest_start'])).days+1)
        interest=D(r['principal'])*D(r['annual_rate'])*D(days)/D(365)
        rows.append(dict(key=r['id'],kind='external_deposit',group='External bank deposits',
            description=r['bank']+' deposit Â· estimated interest',isin=None,valor=None,product=r['id'],
            currency=r['currency'],quantity=r['principal'],price='1',price_convention='amount',scale='1',
            accrued_native=str(interest),market_value_native=r['principal'],missing=[],
            review_required=[r['bank']+': interest estimated using simple ACT/365, including selected day; retained in the same account. Bank credit dates, compounding and tax are not verified.']))
    return rows


def opening(path, baseline):
    if not baseline:return baseline
    result=copy.deepcopy(baseline)
    for r in applicable(path,baseline['as_of']):
        if r['opening_as_of'] != baseline['as_of']:continue
        if any(p['key']==r['id'] for p in result['positions']):continue
        result['positions'].append(dict(key=r['id'],kind='external_deposit',group='External bank deposits',
            description=r['bank']+' deposit',currency=r['currency'],quantity=r['principal'],
            market_value_native=r['principal'],accrued_native='0',external=True))
    return result


def closing(path, snapshot):
    if not snapshot:return snapshot
    result=copy.deepcopy(snapshot)
    for p in native_positions(path,snapshot['as_of']):
        if any(x.get('product')==p['key'] for x in result['positions']):continue
        rate=fx_rate(snapshot,p['currency'],snapshot['valuation_currency'])
        if rate is None:raise ValueError('External deposit requires valuation FX for '+p['currency'])
        market=D(p['market_value_native'])*rate;interest=D(p['accrued_native'])*rate
        result['positions'].append(dict(id=p['key'],product=p['key'],kind=p['kind'],group=p['group'],
            description=p['description'],isin=None,valor=None,currency=p['currency'],quantity_or_balance=p['quantity'],
            market_value=str(market),accrued_interest=str(interest),market_price={'value':'1','convention':'amount'},
            price_date=snapshot['as_of'],raw={}))
        for k,v in [('market_value',market),('accrued_interest',interest)]:
            result['summary'][k]=str(D(result['summary'][k])+v)
    return result


def disclose(path, snapshot, baseline, accounting):
    if not accounting or not baseline:return accounting
    rows=[];opening_czk=D(0)
    for p in native_positions(path,snapshot['as_of']):
        r=next(r for r in applicable(path,snapshot['as_of']) if r['id']==p['key'])
        if r['opening_as_of']!=baseline['as_of']:continue
        x0=fx_rate(baseline,p['currency'],'CZK');x1=fx_rate(snapshot,p['currency'],'CZK')
        if x0 is None or x1 is None:continue
        principal=D(p['quantity']);interest=D(p['accrued_native']);opening_czk+=principal*x0
        rows.append(dict(bank=r['bank'],currency=p['currency'],principal=str(principal),
            interest_native=str(interest),interest_czk=str(interest*x1),opening_czk=str(principal*x0),
            closing_czk=str((principal+interest)*x1),fx_pnl_czk=str(principal*(x1-x0)),
            ytd_pnl_czk=str((principal+interest)*x1-principal*x0),basis=p['review_required'][0]))
    if rows:
        accounting['external_deposits']=rows
        b=accounting.get('nav_bridge',{})
        if b.get('reported_opening_nav_czk') is not None:
            b['reported_opening_nav_czk']=str(D(b['reported_opening_nav_czk'])-opening_czk)
        b['external_deposit_opening_czk']=str(opening_czk)
        accounting['issues']+= [r['basis'] for r in rows]
        accounting['portfolio_pnl_verified']=False;accounting['provisional']=True
        accounting['basis']+=' Includes owner-confirmed external deposits; estimated retained interest is included once.'
    return accounting
