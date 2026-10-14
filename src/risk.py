"""European vanilla Black–Scholes and linear futures risk; no inferred terms."""
import csv
from datetime import datetime, timezone
from io import StringIO
import json
import math
import sqlite3
from .performance import dec

HEADERS=['position_id','kind','underlying','currency','multiplier','strike','expiry','spot','volatility','rate','dividend_yield','observed_at','verified']


def aware(value):
    t=datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.tzinfo is None: raise ValueError('Risk timestamps require a timezone')
    return t.astimezone(timezone.utc)


class RiskStore:
    def __init__(self,path): self.path=path

    def terms(self):
        if not self.path.exists(): return []
        with sqlite3.connect(self.path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='risk_terms'").fetchone(): return []
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM risk_terms')]

    def import_content(self,content,snapshot):
        if not snapshot: raise ValueError('Import holdings before risk terms')
        positions={p['id']:p for p in snapshot['positions']}
        reader=csv.DictReader(StringIO(content.lstrip('\ufeff')))
        if reader.fieldnames!=HEADERS: raise ValueError('Use the exact risk CSV template columns')
        terms=[]
        seen=set()
        for row in reader:
            if None in row or any(v is None for v in row.values()): raise ValueError('Risk CSV column mismatch')
            row={k:v.strip() for k,v in row.items()}
            p=positions.get(row['position_id'])
            if not p or p['kind']!='security' or row['position_id'] in seen: raise ValueError('Unknown, non-security or duplicate position ID')
            seen.add(row['position_id'])
            if row['kind'] not in {'european_call','european_put','future'} or row['verified']!='yes':
                raise ValueError('Only verified European vanilla options or futures are supported')
            if row['currency']!=p['currency'] or not row['underlying']: raise ValueError('Underlying missing or currency differs from holding')
            if any(len(v)>256 or any(ord(c)<32 for c in v) for v in row.values()): raise ValueError('Invalid risk text field')
            for key in ['multiplier','spot']:
                row[key]=str(dec(row[key],key))
                if dec(row[key],key)<=0: raise ValueError('Multiplier and spot must be positive')
            if not 0.000001 <= float(row['spot']) <= 1e12 or not float(row['multiplier']) <= 1e9:
                raise ValueError('Spot or multiplier outside supported range')
            observed=aware(row['observed_at'])
            if observed > datetime.now(timezone.utc): raise ValueError('Risk observation cannot be in the future')
            row['observed_at']=observed.isoformat()
            expiry=aware(row['expiry'])
            row['expiry']=expiry.isoformat()
            if expiry<=observed: raise ValueError('Risk input observation must precede expiry')
            if (expiry-observed).total_seconds()>50*365*86400:
                raise ValueError('Expiry is beyond the supported 50-year horizon')
            if row['kind']!='future':
                for k in ['strike','volatility','rate','dividend_yield']:
                    row[k]=str(dec(row[k],k))
                if not 0.000001 <= float(row['strike']) <= 1e12 or not 0.0001<=float(row['volatility'])<=5:
                    raise ValueError('Invalid strike or volatility (use decimal volatility, e.g. 0.25)')
                if any(not -1<=float(row[k])<=1 for k in ['rate','dividend_yield']): raise ValueError('Rate/yield must be decimal fractions in [-1,1]')
            terms.append(row)
        if not terms: raise ValueError('No risk terms supplied')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS risk_terms (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            for row in terms:
                old=db.execute('SELECT payload FROM risk_terms WHERE id=?',(row['position_id'],)).fetchone()
                if old and aware(json.loads(old[0])['observed_at'])>aware(row['observed_at']):
                    raise ValueError('Risk inputs older than stored observation; batch rejected')
                db.execute('INSERT OR REPLACE INTO risk_terms VALUES (?,?)',(row['position_id'],json.dumps(row)))
        return {'count':len(terms)}


def bsm(spot,strike,t,vol,rate,yield_,call):
    if t<=0 or vol<=0 or spot<=0 or strike<=0: raise ValueError('Positive time, volatility, spot and strike required')
    sign=1 if call else -1
    root=math.sqrt(t)
    d1=(math.log(spot/strike)+(rate-yield_+vol*vol/2)*t)/(vol*root)
    d2=d1-vol*root
    cdf=lambda x:(1+math.erf(x/math.sqrt(2)))/2
    pdf=math.exp(-d1*d1/2)/math.sqrt(2*math.pi)
    dq=math.exp(-yield_*t); dr=math.exp(-rate*t)
    price=sign*(spot*dq*cdf(sign*d1)-strike*dr*cdf(sign*d2))
    delta=sign*dq*cdf(sign*d1)
    gamma=dq*pdf/(spot*vol*root)
    vega=spot*dq*pdf*root/100  # Per one volatility percentage point.
    theta=(-spot*dq*pdf*vol/(2*root)-sign*rate*strike*dr*cdf(sign*d2)+sign*yield_*spot*dq*cdf(sign*d1))/365
    return {'price':price,'delta':delta,'gamma':gamma,'vega':vega,'theta':theta}


def derivatives_risk(snapshot,terms,now=None):
    now=now or datetime.now(timezone.utc)
    if not snapshot: return {'rows':[],'totals':{},'uncovered':[],'as_of':now.isoformat()}
    positions={p['id']:p for p in snapshot['positions']}
    covered=set(); rows=[]; totals={}
    for term in terms:
        p=positions.get(term['position_id'])
        if not p: continue
        covered.add(p['id'])
        t=(aware(term['expiry'])-now).total_seconds()/(365*86400)
        age=(now-aware(term['observed_at'])).total_seconds()
        row={'position_id':p['id'],'description':p['description'],'underlying':term['underlying'],
             'currency':term['currency'],'observed_at':term['observed_at'],'kind':term['kind'],
             'status':'stale' if age>900 else 'modeled','age_seconds':max(0,round(age))}
        if t<=0:
            rows.append(row|{'status':'expired; settlement required'})
            continue
        scale=float(p['quantity_or_balance'])*float(term['multiplier'])
        if not math.isfinite(scale) or abs(scale)>1e30:
            rows.append(row|{'status':'Unsupported holding quantity / scale; verification required'})
            continue
        spot=float(term['spot'])
        if term['kind']=='future':
            # Spot is the dated futures quote, not the underlying cash spot.
            model={'price':spot,'delta':1,'gamma':0,'vega':0,'theta':0}
            scenario=lambda shock,vol:scale*spot*shock
        else:
            args=(float(term['strike']),t,float(term['volatility']),float(term['rate']),float(term['dividend_yield']),term['kind']=='european_call')
            model=bsm(spot,*args)
            scenario=lambda shock,vol:scale*(bsm(spot*(1+shock),args[0],t,args[2]+vol,args[3],args[4],args[5])['price']-model['price'])
        risk={'model_value':model['price']*scale,'delta_units':model['delta']*scale,
              'delta_cash':model['delta']*scale*spot,'gamma_1pct':0.5*model['gamma']*scale*(spot*0.01)**2,
              'vega_1pp':model['vega']*scale,'theta_1day':model['theta']*scale}
        scenarios={f'spot_{int(shock*100):+d}pct':scenario(shock,0) for shock in [-.1,-.05,.05,.1]}
        scenarios['vol_+5pp']=scenario(0,.05)
        row.update({k:format(v,'.12g') for k,v in risk.items()})
        if term['kind']=='future':
            row['model_value']=None  # Futures price times notional is not an asset MTM.
        row['scenarios']={k:format(v,'.12g') for k,v in scenarios.items()}
        rows.append(row)
        if row['status']=='modeled':
            total=totals.setdefault(term['currency'],{})
            for k,v in (risk|scenarios).items():
                if k not in {'delta_units','model_value'}: total[k]=total.get(k,0)+v
    uncovered=[{'position_id':p['id'],'description':p['description'],'reason':'Verified terms / market inputs missing'}
               for p in positions.values() if p['kind']=='security' and p['id'] not in covered
               and ('Structured' in p['group'] or 'derivative' in p['group'].lower() or not p.get('isin'))]
    return {'as_of':now.isoformat(),'holdings_as_of':snapshot['as_of'],'rows':rows,
            'totals':{c:{k:format(v,'.12g') for k,v in d.items()} for c,d in totals.items()},'uncovered':uncovered,
            'basis':'European vanilla Black–Scholes (ACT/365, continuous dividend yield), or linear futures. Inputs are user-verified, not automatically refreshed by PX_LAST. Greeks use signed dated UBS holdings. Vega per 1 volatility point; theta per calendar day; gamma is the quadratic P/L for a 1% spot move. Scenarios reprice at unchanged expiry/rates; aggregate scenarios assume simultaneous relative shocks across modeled underlyings. Inputs older than 15 minutes are excluded from totals. No American, exotic or structured-product model; no VaR or margin estimate.'}
