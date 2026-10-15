"""Expiry payoff scenarios from explicit UBS option terms, without model Greeks."""
from datetime import datetime
from decimal import Decimal
import re
from .accounting import closing_positions


def snapshot_risk(snapshot):
    result={'as_of':(snapshot or {}).get('as_of'),'rows':[],'uncovered':[],
            'basis':'Expiry payoff relative to the dated UBS option market value. These are expiry outcomes, not pre-expiry price forecasts or Greeks. Financing and future fees are excluded.'}
    if not snapshot:return result
    for p in closing_positions(snapshot):
        description=p['description']
        if p['kind']!='security':continue
        option=re.match(r'^(Call|Put) ',description)
        if not option:
            if 'Structured products' in p['group']:
                result['uncovered'].append({'description':description,'reason':'Structured payoff requires the issuer term sheet'})
            continue
        strike=re.search(r'Strike price ([A-Z]{3}) ([\d.]+)',description)
        spot=re.search(r'Market price underlying ([A-Z]{3}) ([\d.]+)',description)
        expiry=re.search(r'(\d{2}\.\d{2}\.\d{4})$',description)
        if not strike or not spot or not expiry or p['scale'] is None or p['quantity']<=0 or strike[1]!=p['currency'] or spot[1]!=p['currency']:
            result['uncovered'].append({'description':description,'reason':'Verified native terms, multiplier or long quantity missing'});continue
        k=Decimal(strike[2]);s=Decimal(spot[2]);notional=p['quantity']*p['scale']
        if k<=0 or s<=0:continue
        expiry_date=datetime.strptime(expiry[1],'%d.%m.%Y').date().isoformat()
        if expiry_date<=snapshot['as_of']:
            result['uncovered'].append({'description':description,'reason':'Expired position requires lifecycle reconciliation'});continue
        scenarios={}
        for move in ['-0.2','-0.1','0','0.1','0.2']:
            ending_spot=s*(1+Decimal(move))
            payoff=max(Decimal(0),(ending_spot-k) if option[1]=='Call' else (k-ending_spot))*notional
            scenarios[move]=str(payoff-p['value'])
        result['rows'].append({'key':p['key'],'description':description,'currency':p['currency'],
            'quantity':str(p['quantity']),'multiplier':str(p['scale']),'strike':str(k),'underlying_spot':str(s),
            'expiry':expiry_date,
            'market_value':str(p['value']),'maximum_premium_loss':str(p['value']),
            'expiry_pnl':scenarios,'greeks_status':'Unavailable without verified exercise style, volatility and rates'})
    return result
