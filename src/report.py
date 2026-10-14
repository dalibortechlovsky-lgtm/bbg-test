"""Escaped, standalone trader report and spreadsheet-safe CSV exports."""
import csv
from decimal import Decimal, InvalidOperation
from html import escape
from io import StringIO
from datetime import datetime, timezone


def fx_rate(snapshot,source,target):
    if source==target: return Decimal(1)
    graph={}
    for pair,value in snapshot.get('exchange_rates',{}).items():
        a,b=pair.split('/')
        d=Decimal(value)
        if d<=0: continue
        graph.setdefault(a,{})[b]=d
        graph.setdefault(b,{})[a]=1/d
    queue=[(source,Decimal(1))]; seen={source}
    for a,rate in queue:
        for b,d in graph.get(a,{}).items():
            if b==target: return rate*d
            if b not in seen: seen.add(b); queue.append((b,rate*d))
    return None


def convert(snapshot,value,source,target):
    rate=fx_rate(snapshot,source,target)
    return None if value is None or rate is None else Decimal(value)*rate


def fmt(value):
    if value is None: return 'Unavailable'
    d=Decimal(value)
    return '('+format(-d,',.2f')+')' if d<0 else format(d,',.2f')


def category(p):
    if p['group']=='Equities - Equity investments': return 'Equities'
    if p['group'].startswith('Bonds'): return 'Bonds'
    if p['kind'] in {'cash_account','call_deposit'}: return 'Cash and deposits / FX'
    return 'Derivatives, ETFs and structured products'


def report_data(snapshot,analytics,performance,risk,size_currency='EUR',pnl_currency='CZK'):
    if not snapshot: raise ValueError('Import holdings before exporting a report')
    for c in [size_currency,pnl_currency]:
        if len(c)!=3 or not c.isupper() or not c.isalpha(): raise ValueError('Report currencies must be uppercase three-letter codes')
    rows=[]; source=snapshot['valuation_currency']
    for p,a in zip(snapshot['positions'],analytics['positions']):
        price=p['market_price']
        rows.append({'group':category(p),'description':p['description'], 'identifier':p['isin'] or p['valor'] or p['product'],
                     'currency':p['currency'],'quantity':p['quantity_or_balance'],
                     'price':None if not price else price['value'],
                     'price_convention':None if not price else price['convention'],
                     'size':convert(snapshot,p['market_value'],source,size_currency),
                     'unrealized':convert(snapshot,a['snapshot_unrealized_pnl'],source,pnl_currency),
                     'cost_price':p['cost_price'],'return_percent':p['unrealized_pnl_percent']})
    return {'date':performance['as_of'],'generated_at':datetime.now(timezone.utc).isoformat(),
            'holdings_as_of':snapshot['as_of'],'size_currency':size_currency,'pnl_currency':pnl_currency,
            'rows':rows,'performance':performance,'risk':risk,'analytics':analytics,
            'snapshot':snapshot,
            'basis':'UBS holdings/price/cost snapshot as of '+snapshot['as_of']+'. Report size and P/L translations use dated UBS export FX, not historical execution FX. Unrealized P/L is snapshot cost-covered P/L, not YTD. Realized/income totals cover imported records only; neither is total portfolio YTD P/L. Daily valuations require complete closing account values and external flows. Report estimates exclude accrued interest.'}


def spreadsheet(value):
    value='' if value is None else str(value)
    if value.lstrip().startswith(('=','+','-','@')):
        try:
            d=Decimal(value)
            numeric=d.is_finite()
        except InvalidOperation:
            numeric=False
        if not numeric: value="'"+value
    return value


def report_csv(data):
    out=StringIO(); writer=csv.writer(out)
    writer.writerow(['section','date','description','identifier','native_currency','quantity','broker_price','price_convention',
                     'size_'+data['size_currency'],'snapshot_unrealized_'+data['pnl_currency'],'realized_native','income_native','notes'])
    def emit(values): writer.writerow([spreadsheet(v) for v in values])
    emit(['BASIS',data['date'],'','','','','','','','','','',data['basis']])
    for p in data['rows']:
        emit([p['group'],data['holdings_as_of'],p['description'],p['identifier'],p['currency'],p['quantity'],p['price'],p['price_convention'],p['size'],p['unrealized'],'','', 'UBS snapshot'])
    for r in data['performance']['realized']:
        emit(['Realized',r['date'],r['description'] or r['security'],r['security'],r['currency'],r['quantity'],'','','','',r['amount'],'','Average cost; fees included; account '+r['account']])
    for r in data['performance']['income']:
        emit(['Income',r['date'],r['description'],'',r['currency'],'','','','','','',r['amount'],'Account '+r['account']])
    for r in data['performance']['periods']:
        emit([r['kind'].title()+' P/L',r['to'],r['account'],'',r['currency'],'','','','','',r['amount'],'','Closing value change less external flows; from '+r['from']])
    for r in data['risk']['rows']:
        emit(['Risk',data['date'],r['description'],r['underlying'],r['currency'],'','','','','','','',str(r)])
    return out.getvalue().encode('utf-8-sig')


def report_html(data):
    e=lambda v:escape(str(v),quote=True)
    def number(v,color=True):
        cls='missing' if v is None else (('negative' if Decimal(v)<0 else 'positive') if color else '')
        return '<td class="num '+cls+'">'+e(fmt(v))+'</td>'
    parts=['''<!doctype html><html lang="en"><meta charset="utf-8"><title>Trading Overview</title><style>
body{font:12px Arial,sans-serif;color:#161616;background:white;margin:32px auto;max-width:1100px;padding:24px}h1{font-size:22px;color:#777}h2{font-size:14px;margin-top:28px}table{border-collapse:collapse;width:100%;margin:14px 0}td,th{padding:5px 7px;text-align:left;vertical-align:top}.num{text-align:right;white-space:nowrap}.negative{background:#ffc5ca;color:#9c0006}.positive{background:#c6efce;color:#006100}.missing{color:#777}.group td{font-weight:bold;border-bottom:1px solid black;padding-top:20px}.summary{width:65%;margin-left:auto}.notes{font-size:11px;color:#555;line-height:1.5}.toolbar{padding:12px;background:#eee}button,a{margin-right:14px}thead{display:table-header-group}tr{break-inside:avoid}@media print{body{margin:0;max-width:none;padding:0;font-size:10px}td,th{padding:4px}.toolbar{display:none}@page{size:A4 portrait;margin:12mm}}
</style><div class="toolbar"><button onclick="window.print()">Print / Save as PDF</button></div><h1>Trading Overview</h1>''']
    parts.append('<p>Date '+e(data['date'])+' · Holdings '+e(data['holdings_as_of'])+'</p><p class="notes">'+e(data['basis'])+'</p>')
    perf=data['performance']
    parts.append('<table class="summary"><thead><tr><th>Imported performance · average cost</th><th>Currency</th><th>YTD realized</th><th>YTD income</th></tr></thead><tbody>')
    for c,t in perf['totals'].items():
        parts.append('<tr><td>Imported records only</td><td>'+e(c)+'</td>'+number(t['ytd_realized'])+number(t['ytd_income'])+'</tr>')
    if not perf['totals']: parts.append('<tr><td colspan="4">Unavailable · import opening lots and dated fills/income</td></tr>')
    parts.append('</tbody></table><h2>Open positions · snapshot, including FX</h2><table><thead><tr><th>Market price</th><th>Instrument / entry cost</th><th>Size in '+e(data['size_currency'])+'</th><th>Snapshot unrealized P/L in '+e(data['pnl_currency'])+'</th><th>UBS return %</th></tr></thead><tbody>')
    for group in ['Equities','Bonds','Derivatives, ETFs and structured products','Cash and deposits / FX']:
        rows=[r for r in data['rows'] if r['group']==group]
        if not rows: continue
        sizes=[r['size'] for r in rows]
        pnl=[r['unrealized'] for r in rows if r['unrealized'] is not None]
        parts.append('<tr class="group"><td colspan="2">'+e(group)+'</td>'+number(sum(sizes,Decimal(0)) if all(v is not None for v in sizes) else None,False)+number(sum(pnl,Decimal(0)) if pnl else None)+'<td>Cost-covered only</td></tr>')
        for r in rows:
            price='—' if r['price'] is None else fmt(r['price'])+('%' if r['price_convention']=='percent' else '')
            cost=r['cost_price']
            description=r['description']+(' @ '+cost['value']+('%' if cost['convention']=='percent' else '') if cost else '')
            parts.append('<tr><td>'+e(price)+'</td><td>'+e(description)+'<br><small>'+e(r['identifier'])+' · '+e(r['quantity'])+' '+e(r['currency'])+'</small></td>'+number(r['size'],False)+number(r['unrealized'])+'<td class="num">'+e('—' if r['return_percent'] is None else r['return_percent']+'%')+'</td></tr>')
    parts.append('</tbody></table><h2>Trade history · realized P/L (average cost, fees included)</h2><table><thead><tr><th>Date</th><th>Account / instrument</th><th>Closed quantity</th><th>Currency</th><th>Realized P/L</th></tr></thead><tbody>')
    for r in reversed(perf['realized']):
        parts.append('<tr><td>'+e(r['date'])+'</td><td>'+e(r['account']+' / '+(r['description'] or r['security']))+'</td><td>'+e(r['quantity'])+'</td><td>'+e(r['currency'])+'</td>'+number(r['amount'])+'</tr>')
    if not perf['realized']: parts.append('<tr><td colspan="5">No calculable closed trades in imported records.</td></tr>')
    parts.append('</tbody></table><h2>Income</h2><table>')
    for r in reversed(perf['income']): parts.append('<tr><td>'+e(r['date'])+'</td><td>'+e(r['description'])+'</td><td>'+e(r['currency'])+'</td>'+number(r['amount'])+'</tr>')
    parts.append('</table><h2>Daily / period P/L · complete closing valuations less external transfers</h2><table><tr><th>From / to</th><th>Account</th><th>Currency</th><th>Basis</th><th>P/L</th></tr>')
    for r in reversed(perf['periods']):
        parts.append('<tr><td>'+e(r['from']+' / '+r['to'])+'</td><td>'+e(r['account'])+'</td><td>'+e(r['currency'])+'</td><td>'+e(r['kind']+' · '+str(r['days'])+' days')+'</td>'+number(r['amount'])+'</tr>')
    if not perf['periods']: parts.append('<tr><td colspan="5">Unavailable · at least two closing valuations required.</td></tr>')
    parts.append('</table><h2>Derivatives risk</h2><p class="notes">'+e(data['risk'].get('basis','No holdings'))+'</p><table><tr><th>Instrument / underlying</th><th>Currency / status</th><th>Delta cash</th><th>Gamma 1%</th><th>Vega 1pp</th><th>Theta/day</th><th>Spot −10% P/L</th></tr>')
    for r in data['risk']['rows']:
        parts.append('<tr><td>'+e(r['description']+' / '+r['underlying'])+'</td><td>'+e(r['currency']+' / '+r['status'])+'</td>'+''.join(number(r.get(k),False) for k in ['delta_cash','gamma_1pct','vega_1pp','theta_1day'])+number(r.get('scenarios',{}).get('spot_-10pct'))+'</tr>')
    parts.append('</table><p class="notes">Risk inputs are separate dated observations. Uncovered instruments: '+e(len(data['risk']['uncovered']))+'.</p><ul class="notes">')
    for warning in perf['warnings']+ [r['description']+': '+r['reason'] for r in data['risk']['uncovered']]: parts.append('<li>'+e(warning)+'</li>')
    parts.append('</ul><p class="notes">Generated '+e(data['generated_at'])+' · Execution disabled · Missing figures remain unavailable.</p></html>')
    return ''.join(parts).encode()
