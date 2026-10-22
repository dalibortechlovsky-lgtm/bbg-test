"""Trader overview built from native UBS accounting and explicit FX attribution."""
import csv
from decimal import Decimal
from html import escape
from io import StringIO
from .report import fmt,spreadsheet


def desk_csv(data):
    a=data['accounting'];out=StringIO();writer=csv.writer(out)
    columns=['section','as_of','description','currency','quantity','native_price_return_percent',
             'market_price_native','average_cost_native','market_value_eur','asset_pnl_native','asset_contribution_czk','currency_contribution_czk','total_pnl_czk','realized_native','income_native','status']
    writer.writerow(columns)
    def emit(row):writer.writerow([spreadsheet(row.get(c)) for c in columns])
    selection=data.get('snapshot_selection') or {}
    if selection.get('requested_as_of'):
        emit({'section':'REQUESTED DATE','as_of':selection['requested_as_of'],
              'description':selection['message'],'status':selection['selection']})
    reconstructed=selection.get('selection','').startswith('reconstructed')
    source_label='PROVISIONAL RECONSTRUCTED VALUATION' if selection.get('selection')=='reconstructed_provisional' else 'RECONSTRUCTED VALUATION' if reconstructed else 'ACTUAL UBS SNAPSHOT'
    emit({'section':source_label,'as_of':a['as_of'],
          'description':('Positions and P/L are reconstructed from UBS activity and Bloomberg history.'
                         if reconstructed else 'Positions, prices and P/L use this stored UBS valuation date.'),
          'status':selection.get('selection','latest_snapshot')})
    emit({'section':'BASIS','as_of':a['as_of'],'description':a['basis'],'status':a['status']})
    for notice in selection.get('review_flags',[]):
        emit({'section':'REVIEW REQUIRED','as_of':a['as_of'],'description':notice,'status':'Provisional input'})
    for confirmation in a.get('activity_confirmations',[]):
        emit({'section':'OWNER ACTIVITY CONFIRMATION','as_of':confirmation['until'],'currency':confirmation['currency'],
              'description':confirmation['from']+' to '+confirmation['until']+'; '+confirmation['kind']+'; '+confirmation['reason'],
              'status':'Owner statement; original UBS export preserved'})
    bridge=a.get('nav_bridge',{})
    for title,key in [('REPORTED UBS OPENING NAV','reported_opening_nav_czk'),('TRANSFERRED OPENING BASIS','transferred_opening_basis_czk'),
                      ('ADJUSTED OPENING VALUE','opening_nav_czk'),('CLOSING NAV','closing_nav_czk'),
                      ('NET EXTERNAL FUNDING','net_external_flows_czk')]:
        if bridge:emit({'section':title,'as_of':a['opening_as_of'] if key=='opening_nav_czk' else a['as_of'],
                        'currency':'CZK','description':bridge['basis'],'total_pnl_czk':bridge[key],'status':a['status']})
    emit({'section':'YTD TOTAL','as_of':a['as_of'],'currency':'CZK','total_pnl_czk':a['ytd_pnl_czk'],'status':a['status']})
    for flow in bridge.get('external_flows',[]):
        emit({'section':'EXTERNAL FUNDING','as_of':flow['date'],'currency':flow['currency'],
              'description':flow['reference']+'; native '+flow['amount_native']+'; CZK FX '+str(flow['fx_to_czk']),
              'total_pnl_czk':flow['amount_czk'],'status':'Capital movement excluded from P/L'})
    for basis in bridge.get('settlement_bases',[]):
        emit({'section':'TRANSFERRED OPENING BASIS','as_of':basis['as_of'],'currency':basis['currency'],
              'description':basis['reference']+'; native '+basis['amount_native']+'; opening CZK FX '+basis['fx_to_czk'],
              'total_pnl_czk':basis['amount_czk'],'status':'Deducted from gross settlement proceeds when calculating P/L'})
    for fixing in a.get('fx_sources',[]):
        emit({'section':'HISTORICAL FX','as_of':fixing['date'],'currency':fixing['security'][:3],
              'description':fixing['source'],'market_price_native':fixing['price'],'status':'CZK per native currency unit'})
    for issue in a['issues']:emit({'section':'REVIEW','as_of':a['as_of'],'description':issue})
    for p in a['assets']:
        emit(p|{'section':'OPEN' if Decimal(p['quantity']) else 'CLOSED','as_of':a['as_of'],
                'asset_pnl_native':p['ytd_asset_pnl_native'],'status':p['quantity_status']+'; '+p['cost_basis']})
    for c in a['currencies']:
        emit(c|{'section':'CURRENCY','as_of':a['as_of'],'description':c['currency']+' whole native bucket: holdings, cash and deposits',
                'asset_pnl_native':c['native_asset_pnl'],'status':a['status']})
        emit({'section':'SETTLED DERIVATIVES','as_of':a['as_of'],'currency':c['currency'],
              'description':'Grouped expiry/margin cash result','asset_pnl_native':c['futures_settlements_native']})
        emit({'section':'CASH AND DEPOSITS','as_of':a['as_of'],'currency':c['currency'],
              'description':'Income, costs and unallocated cash items','asset_pnl_native':c['cash_deposit_income_costs_native']})
        emit({'section':'FX HEDGE CASH','as_of':a['as_of'],'currency':c['currency'],
              'description':'Owner-confirmed cash assigned to currency contribution','income_native':c['fx_hedge_settlements_native']})
    for c,v in a['realized_totals'].items():emit({'section':'REALIZED TOTAL','currency':c,'realized_native':v,'status':'Historical average cost; not added again to YTD'})
    if a.get('period'):emit({'section':a['period']['label'].upper(),'as_of':a['as_of'],'description':a['period']['from']+' to '+a['period']['to'],
                             'total_pnl_czk':a['period']['pnl_czk'],'status':a['period']['status']})
    for p in data.get('snapshot_risk',{}).get('rows',[]):
        emit({'section':'EXPIRY RISK','as_of':a['as_of'],'description':p['description'],'currency':p['currency'],
              'status':'Max premium loss '+p['maximum_premium_loss']+'; expiry P/L '+str(p['expiry_pnl'])})
    return out.getvalue().encode('utf-8-sig')


def desk_html(data):
    a=data['accounting'];e=lambda v:escape(str(v),quote=True)
    def cell(v,percent=False,highlight=True):
        cls='missing' if v is None else ('negative' if Decimal(v)<0 else 'positive' if Decimal(v)>0 else '') if highlight else ''
        return '<td class="num '+cls+'">'+e(fmt(v))+('%' if percent and v is not None else '')+'</td>'
    parts=['''<!doctype html><html lang="en"><meta charset="utf-8"><title>Trader overview</title><style>
body{font:12px Arial;color:#202020;margin:24px auto;padding:20px;max-width:1100px}h1{color:#777;font-size:24px}h2{font-size:15px;margin-top:25px}table{border-collapse:collapse;width:100%;margin:12px 0}th,td{padding:5px 7px;text-align:left;vertical-align:top;border-bottom:1px solid #ddd}.num{text-align:right;white-space:nowrap}.negative{background:#ffc5ca;color:#9c0006}.positive{background:#c6efce;color:#006100}.missing,.notes{color:#666}.notes{font-size:10px;line-height:1.5}.summary{font-size:18px}.group{background:#eee;font-weight:bold}.toolbar{background:#eee;padding:12px}tr{break-inside:avoid}thead{display:table-header-group}@media print{body{padding:0;margin:0;font-size:9px}.toolbar{display:none}td,th{padding:4px}.notes{font-size:8px}@page{size:A4 portrait;margin:10mm}}
</style><div class="toolbar"><button onclick="window.print()">Print / Save PDF</button></div><h1>Trading Overview</h1>''']
    selection=data.get('snapshot_selection') or {}
    if selection.get('requested_as_of'):
        label=('Provisional reconstructed positions and P/L date' if selection.get('selection')=='reconstructed_provisional' else
               'Reconstructed positions and P/L date' if selection.get('selection')=='reconstructed' else 'Actual UBS snapshot and P/L date')
        parts.append('<p><strong>Requested date: '+e(selection['requested_as_of'])+'</strong><br>'+label+': '+e(a['as_of'])+'</p>')
        parts.append('<p class="notes">'+e(selection['message'])+'</p>')
    parts.append('<p>UBS portfolio · CZK report · '+e(a['opening_as_of'])+' → '+e(a['as_of'])+'</p><p><strong>'+e(a['status'])+'</strong></p>')
    if selection.get('review_flags'):
        parts.append('<p class="notes"><strong>Review required:</strong></p><ul class="notes">')
        for notice in selection['review_flags']:parts.append('<li>'+e(notice)+'</li>')
        parts.append('</ul>')
    for confirmation in a.get('activity_confirmations',[]):
        kind='no activity' if confirmation['kind']=='no_activity' else 'no further transactions beyond supplied exports'
        parts.append('<p class="notes">Owner-confirmed coverage: '+e(confirmation['currency']+' · '+confirmation['from']+' to '+confirmation['until']+' · '+kind)+'. '+e(confirmation['reason'])+'</p>')
    parts.append('<table class="summary"><tr><th>Net asset value CZK · incl. accrued interest</th>'+cell(a.get('nav_czk'),highlight=False)+'</tr><tr><th>YTD P/L CZK</th>'+cell(a['ytd_pnl_czk'])+'</tr></table>')
    bridge=a.get('nav_bridge')
    if bridge:
        parts.append('<p class="notes">YTD = closing NAV − adjusted opening value − net external funding. Positive funding means deposits into the portfolio; withdrawals are negative. '+e(bridge['basis'])+'</p>')
        parts.append('<table><tr><th>Reported UBS opening NAV CZK</th><th>Transferred opening basis CZK</th><th>Adjusted opening value CZK</th><th>Closing NAV CZK</th><th>Net external funding CZK</th></tr><tr>'+cell(bridge['reported_opening_nav_czk'],highlight=False)+cell(bridge['transferred_opening_basis_czk'],highlight=False)+cell(bridge['opening_nav_czk'],highlight=False)+cell(bridge['closing_nav_czk'],highlight=False)+cell(bridge['net_external_flows_czk'],highlight=False)+'</tr></table>')
        if bridge['missing_external_fx']:
            parts.append('<p class="notes">YTD needs verified funding-date FX: '+e('; '.join(bridge['missing_external_fx']))+'.</p>')
    sources=sorted({'CNB official daily fixings' if f['source'].startswith('CNB ') else f['source'] for f in a.get('fx_sources',[])})
    if sources:parts.append('<p class="notes">Historical FX sources: '+e('; '.join(sources))+'. Publication dates and source evidence are retained; fixing details are included in the CSV export.</p>')
    parts.append('<h2>Asset contribution and currency contribution</h2><table><thead><tr><th>Currency</th><th>Native asset P/L</th><th>Asset contribution CZK</th><th>Currency contribution CZK</th><th>Total CZK</th></tr></thead><tbody>')
    for c in a['currencies']:
        if Decimal(c['opening_native'])==0 and Decimal(c['closing_native'])==0:continue
        parts.append('<tr><td>'+e(c['currency'])+'</td>'+cell(c['native_asset_pnl'])+cell(c['asset_contribution_czk'])+cell(c['currency_contribution_czk'])+cell(c['total_pnl_czk'])+'</tr>')
    parts.append('</tbody></table>')
    parts.append('<h2>Cash, deposits and settled derivatives</h2><table><tr><th>Currency</th><th>Grouped expiry / margin cash result</th><th>Cash / deposits and unallocated items</th><th>FX hedge cash · currency contribution</th><th>Realized total · historical average cost</th></tr>')
    for c in a['currencies']:parts.append('<tr><td>'+e(c['currency'])+'</td>'+cell(c['futures_settlements_native'])+cell(c['cash_deposit_income_costs_native'])+cell(c['fx_hedge_settlements_native'])+cell(a['realized_totals'].get(c['currency']))+'</tr>')
    parts.append('</table>')
    if a.get('period'):
        p=a['period'];parts.append('<p>'+e(p['label']+' CZK · '+p['from']+' → '+p['to'])+': <strong>'+e(fmt(p['pnl_czk']))+'</strong> · '+e(p['status'])+'</p>')
    for title,open_only in [('Open positions',True),('Closed positions and settlement exceptions',False)]:
        parts.append('<h2>'+title+'</h2><table><thead><tr><th>Instrument / native currency</th><th>Quantity</th><th>Size EUR</th><th>Native price return</th><th>YTD asset P/L native</th><th>Asset contribution CZK</th><th>Realized native · average cost</th></tr></thead><tbody>')
        rows=[p for p in a['assets'] if bool(Decimal(p['quantity']))==open_only]
        group=None
        for p in sorted(rows,key=lambda p:(p['group'],p['description'])):
            if p['group']!=group:group=p['group'];parts.append('<tr class="group"><td colspan="7">'+e(group)+'</td></tr>')
            parts.append('<tr><td>'+e(p['description'])+'<br><span class="notes">'+e(p['currency']+' · '+p['quantity_status'])+'</span></td>'+cell(p['quantity'],highlight=False)+cell(p['market_value_eur'],highlight=False)+cell(p['native_price_return_percent'],True)+cell(p['ytd_asset_pnl_native'])+cell(p['asset_contribution_czk'])+cell(p['realized_native'])+'</tr>')
        parts.append('</tbody></table>')
    risk=data.get('snapshot_risk',{})
    parts.append('<h2>Derivatives · expiry outcomes</h2><p class="notes">'+e(risk.get('basis',''))+'</p><table><tr><th>Option / expiry</th><th>Currency</th><th>Max premium loss</th><th>Underlying −10%</th><th>Unchanged</th><th>Underlying +10%</th></tr>')
    for p in risk.get('rows',[]):parts.append('<tr><td>'+e(p['description'])+'<br>'+e(p['expiry'])+'</td><td>'+e(p['currency'])+'</td>'+cell('-'+p['maximum_premium_loss'])+cell(p['expiry_pnl']['-0.1'])+cell(p['expiry_pnl']['0'])+cell(p['expiry_pnl']['0.1'])+'</tr>')
    parts.append('</table><h2>Basis and reconciliation</h2><p class="notes">'+e(a['basis'])+'</p><ul class="notes">')
    for issue in a['issues']+[p['description']+': '+p['reason'] for p in risk.get('uncovered',[])]:parts.append('<li>'+e(issue)+'</li>')
    parts.append('</ul></html>')
    return ''.join(parts).encode()
