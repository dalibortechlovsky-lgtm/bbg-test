from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from src.accounting import calculate_accounting,period_view,internal_fx_adjustments
from src.baseline import parse_statement_text,parse_statement,BaselineStore
from src.desk_report import desk_html,desk_csv
from src.history import HistoryStore
from src.lifecycle import LifecycleStore
from src.positions import parse_positions
from src.quotes import QuoteStore
from src.report import fx_rate
from src.server import create_server
from src.snapshot_risk import snapshot_risk
from src.ubs_activity import ActivityStore,parse_activity
from test_ubs_activity import posting,account_csv

ISIN='US0000000001'
IBAN='CH0000000000000000001'
STATEMENT='''Statement of assets
As of 31 December 2025
Portfolio 123-456-01, valued in US Dollar (USD)
USD 1= CZK 20
CZK 1= USD 0.05
EUR 1= USD 1.2
USD 1= EUR 0.833333
Detailed positions
Liquidity - Accounts
USD 100  UBS Current Account  100  55.56
CH00 00000000000000001
Equities - Equity investments
1  Synthetic equity  USD 50  80  60.00%  80  44.44
Valor 123 - ISIN US0000000001
Net assets  180  100.00
'''


def pdf_document(text):
    # Original synthetic financial fixture; no private portfolio content.
    commands=['BT /F1 9 Tf 11 TL 20 800 Td']
    for line in text.splitlines():
        line=line.replace('  ','      ')
        escaped=line.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
        commands.extend(['('+escaped+') Tj','T*'])
    stream=('\n'.join(commands)+'\nET').encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1200 850] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>',
             b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    out=bytearray(b'%PDF-1.4\n');offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(out));out.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    start=len(out);out.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:out.extend(f'{offset:010d} 00000 n \n'.encode())
    out.extend(f'trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF'.encode())
    return bytes(out)


def snapshot(quantity='1',price='90',cash='100',as_of='2026-10-14',fx='21'):
    return {'portfolio':'0123-000456-0001','as_of':as_of,'valuation_currency':'USD',
      'exchange_rates':{'USD/CZK':fx,'CZK/USD':str(1/Decimal(fx)),'USD/EUR':'0.9','EUR/USD':'1.111111'},
      'positions':[{'id':'cash','kind':'cash_account','group':'Liquidity - Accounts','currency':'USD',
                    'quantity_or_balance':cash,'market_value':cash,'description':'Cash','market_price':None,
                    'raw':{'IBAN':IBAN}},
                   {'id':'stock','isin':ISIN,'kind':'security','group':'Equities - Equity investments','currency':'USD',
                    'quantity_or_balance':quantity,'market_value':str(Decimal(quantity)*Decimal(price)),
                    'description':'Synthetic equity','market_price':{'value':price,'convention':'amount'}}]}


def activity(rows,until='2026-10-14'):
    parsed=parse_activity(account_csv(rows,opening='100',start=min(['2026-01-01']+[r['Trade date'] for r in rows]),until=until))
    return {'ledger':parsed['rows'],'accounts':[parsed], 'review':[],'fx_pairs':[]}


class NativeAccountingTests(unittest.TestCase):
    def setUp(self):self.baseline=parse_statement_text(STATEMENT)

    def test_new_reporting_year_requests_its_own_opening_statement(self):
        a=calculate_accounting(snapshot(as_of='2027-01-05'),self.baseline,activity([]))
        self.assertFalse(a['portfolio_pnl_verified'])
        self.assertEqual(a['status'],'Opening statement needed')
        self.assertIsNone(a['ytd_pnl_czk'])

    def test_pdf_reconciles_and_rejects_incomplete_or_foreign_layout(self):
        result=parse_statement(pdf_document(STATEMENT))
        self.assertEqual(result['rounding_difference_base'],'0')
        self.assertEqual(result['positions'][1]['cost_native'],'50')
        self.assertEqual(result['positions'][1]['market_value_native'],'80')
        for bad in [STATEMENT.replace('180  100.00','200  100.00'),STATEMENT.replace('31 December','30 December'),
                    STATEMENT.replace('Valor 123 - ISIN US0000000001','Unknown identity')]:
            with self.assertRaises(ValueError):parse_statement_text(bad)
        with self.assertRaises(ValueError):parse_statement(b'not a PDF')

    def test_ytd_mark_is_separate_from_fee_inclusive_lifetime_realized(self):
        row=posting('89','Sale Spot',Description3='Number/Amt. 1; Transaction price: 90 USD')
        a=calculate_accounting(snapshot('0','90','189'),self.baseline,activity([row]))
        p=a['assets'][0]
        self.assertEqual(Decimal(p['realized_native']),39)  # 90 less 1 fee less 50 cost
        self.assertEqual(Decimal(p['ytd_asset_pnl_native']),9)  # 89 less year-start 80
        self.assertEqual(Decimal(a['ytd_pnl_czk']),369)  # 189*21 - 180*20
        self.assertEqual(Decimal(a['ytd_asset_contribution_czk']),180)
        self.assertEqual(Decimal(a['currencies'][0]['currency_contribution_czk']),189)
        self.assertTrue(a['portfolio_pnl_verified'])

    def test_original_acquisition_cash_overrides_statement_cost_without_reset_trade(self):
        original=posting('-52',number='ORIGINAL',**{'Trade date':'2025-12-18','Booking date':'2025-12-22',
            'Value date':'2025-12-22','Description3':'Number/Amt. 1; Transaction price: 50 USD'})
        sell=posting('89','Sale Spot',number='SALE',Description3='Number/Amt. 1; Transaction price: 90 USD')
        a=calculate_accounting(snapshot('0','90','189'),self.baseline,activity([original,sell]))
        self.assertEqual(Decimal(a['assets'][0]['realized_native']),37)
        self.assertEqual(Decimal(a['assets'][0]['ytd_asset_pnl_native']),9)
        self.assertIn('original net acquisition cash',a['assets'][0]['cost_basis'])

    def test_average_cost_partial_sale_and_later_purchase(self):
        buy=posting('-71',number='BUY',Description3='Number/Amt. 1; Transaction price: 70 USD')
        sale=posting('79','Sale Spot',number='SALE',Description3='Number/Amt. 1; Transaction price: 80 USD',
                     **{'Trade date':'2026-01-06','Booking date':'2026-01-08','Value date':'2026-01-08'})
        a=calculate_accounting(snapshot('1','90','108'),self.baseline,activity([buy,sale]))
        p=a['assets'][0]
        self.assertEqual(Decimal(p['realized_native']),Decimal('18.5'))
        self.assertEqual(Decimal(p['average_cost_native']),Decimal('60.5'))
        self.assertEqual(Decimal(p['ytd_asset_pnl_native']),18)

    def test_funding_is_not_profit_and_missing_fixing_withholds_czk_total(self):
        flow=posting('50','Special payment order','FUND',Description1='External funding')
        a=calculate_accounting(snapshot(cash='150'),self.baseline,activity([flow]))
        self.assertIsNone(a['ytd_pnl_czk'])
        self.assertEqual(a['history_requests'],[{'security':'USDCZK Curncy','dates':['2026-01-05']}])
        view=activity([flow]);identifier=view['ledger'][0]['id']
        a=calculate_accounting(snapshot(cash='150'),self.baseline,view,{('USDCZK Curncy','2026-01-05'):Decimal(21)},
                               cash_reviews={identifier:{'classification':'external_funding'}})
        self.assertEqual(Decimal(a['ytd_pnl_czk']),390)
        self.assertEqual(Decimal(a['ytd_asset_contribution_czk']),200)

    def test_outside_bank_trade_settlement_is_performance_and_flags_missing_cost(self):
        flow=posting('50','Special payment order','EXPIRY',Description1='Derivative expiry from other bank')
        view=activity([flow]);identifier=view['ledger'][0]['id']
        decisions={identifier:{'classification':'asset_settlement'}}
        result=calculate_accounting(snapshot(cash='150'),self.baseline,view,cash_reviews=decisions)
        self.assertEqual(Decimal(result['ytd_pnl_czk']),1440)
        self.assertEqual(Decimal(result['nav_bridge']['net_external_flows_czk']),0)
        self.assertEqual(result['nav_bridge']['external_flows'],[])
        self.assertEqual(result['cash_decisions'][0]['decision'],'asset_settlement')
        self.assertTrue(any('opening premium/cost is not in UBS records: EXPIRY' in issue for issue in result['issues']))

        previous=snapshot('0','0','100',as_of='2026-01-01',fx='20')
        current=snapshot('0','0','150',as_of='2026-10-14',fx='21')
        period=period_view(previous,current,view,{},cash_reviews=decisions)
        self.assertEqual(Decimal(period['pnl_czk']),1150)

    def test_internal_fx_is_never_removed_as_external_funding(self):
        flows={'USD':[{'kind':'internal_fx','reference':'FX','date':'2026-01-05','amount':'-100'}],
               'EUR':[{'kind':'internal_fx','reference':'FX','date':'2026-01-05','amount':'89'}]}
        rates={('USDCZK Curncy','2026-01-05'):Decimal(20),('EURCZK Curncy','2026-01-05'):Decimal(22)}
        self.assertEqual(internal_fx_adjustments(flows,rates),{'EUR':Decimal(-42)})
        begin=snapshot('0','0','100',as_of='2026-01-01',fx='20')
        end=snapshot('0','0','0',as_of='2026-01-02',fx='20')
        end['exchange_rates']['EUR/CZK']='22'
        end['positions'].append(dict(end['positions'][0],currency='EUR',quantity_or_balance='89',market_value='97.9'))
        records=[{'id':str(i),'category':'fx_conversion','booking_date':'2026-01-02','transaction_no':'FX',
                  'currency':c,'amount':v,'trade_date':'2026-01-02'} for i,(c,v) in enumerate([('USD','-100'),('EUR','89')])]
        rates={(c+'CZK Curncy','2026-01-02'):v for c,v in [('USD',Decimal(20)),('EUR',Decimal(22))]}
        p=period_view(begin,end,{'ledger':records,'accounts':[]},rates)
        self.assertEqual(Decimal(p['pnl_czk']),-42)
        self.assertEqual(p['label'],'Daily P/L')

    def test_ytd_nav_bridge_does_not_need_internal_fx_attribution_fixings(self):
        view=activity([])
        view['ledger']=[{'id':str(i),'category':'fx_conversion','booking_date':'2026-01-05',
                       'transaction_no':'FX','currency':c,'amount':v,'trade_date':'2026-01-05',
                       'reversal':False,'isin':None,'account':IBAN if c=='USD' else 'CH0000000000000000002',
                       'description':'Synthetic internal FX'} for i,(c,v) in enumerate([('USD','-100'),('EUR','89')])]
        view['fx_pairs']=[{'transaction_no':'FX','status':'Matched internal currency legs'}]
        close=snapshot(cash='0',fx='20')
        close['exchange_rates']['EUR/CZK']='22'
        close['positions'].append(dict(close['positions'][0],currency='EUR',quantity_or_balance='89',
                                       raw={'IBAN':'CH0000000000000000002'}))
        result=calculate_accounting(close,self.baseline,view)
        self.assertEqual(Decimal(result['ytd_pnl_czk']),158)  # 90*20 + 89*22 - 180*20
        self.assertEqual(result['nav_bridge']['net_external_flows_czk'],'0')
        self.assertEqual(result['nav_bridge']['external_flows'],[])
        self.assertFalse(result['portfolio_pnl_verified'])
        self.assertTrue(result['history_requests'])
        self.assertTrue(any(c['currency_contribution_czk'] is None for c in result['currencies']))

    def test_ytd_bridge_uses_dated_external_funding_and_matches_attribution(self):
        rows=[posting('50','Special payment order','DEPOSIT',Description1='External deposit'),
              posting('-10','Special payment order','WITHDRAW',Description1='External withdrawal',
                      **{'Trade date':'2026-01-06','Booking date':'2026-01-07','Value date':'2026-01-07'})]
        view=activity(rows)
        decisions={r['id']:{'classification':'external_funding'} for r in view['ledger']}
        rates={('USDCZK Curncy','2026-01-05'):Decimal(21),('USDCZK Curncy','2026-01-07'):Decimal(22)}
        result=calculate_accounting(snapshot(cash='140'),self.baseline,view,rates,cash_reviews=decisions)
        bridge=result['nav_bridge']
        self.assertEqual(Decimal(bridge['opening_nav_czk']),3600)
        self.assertEqual(Decimal(bridge['closing_nav_czk']),4830)
        self.assertEqual(Decimal(bridge['net_external_flows_czk']),830)
        self.assertEqual(Decimal(result['ytd_pnl_czk']),400)
        self.assertEqual(Decimal(bridge['attribution_difference_czk']),0)
        self.assertTrue(result['portfolio_pnl_verified'])
        blocked=calculate_accounting(snapshot(cash='140'),self.baseline,view,{},cash_reviews=decisions)
        self.assertIsNone(blocked['ytd_pnl_czk'])
        self.assertEqual(len(blocked['nav_bridge']['missing_external_fx']),2)

    def test_csv_exports_main_ytd_and_nav_bridge_with_provisional_status(self):
        result=calculate_accounting(snapshot(cash='100'),self.baseline,activity([],until='2026-09-30'))
        csv=desk_csv({'accounting':result})
        self.assertIn(b'YTD TOTAL',csv)
        self.assertIn(b'OPENING NAV',csv)
        self.assertIn(b'NET EXTERNAL FUNDING',csv)
        self.assertIn(b'Provisional',csv)
        self.assertIn(b'YTD = closing NAV',desk_html({'accounting':result}))

    def test_ambiguous_same_day_fills_and_missing_inventory_are_not_invented(self):
        buy=posting('-70','Purchase Spot','BUY',Description3='Number/Amt. 1; Transaction price: 70 USD')
        sell=posting('90','Sale Spot','SELL',Description3='Number/Amt. 1; Transaction price: 90 USD')
        a=calculate_accounting(snapshot(cash='120'),self.baseline,activity([buy,sell]))
        self.assertIsNone(a['assets'][0]['realized_native'])
        self.assertFalse(a['portfolio_pnl_verified'])
        self.assertTrue(any('ambiguous' in x for x in a['issues']))

    def test_pending_settlement_and_future_dated_execution_cutoff(self):
        row=posting('-70','Purchase Spot','PENDING',Description3='Number/Amt. 1; Transaction price: 70 USD',
                    **{'Trade date':'2026-10-14','Booking date':'2026-10-19','Value date':'2026-10-19'})
        a=calculate_accounting(snapshot('2','90','100'),self.baseline,activity([row]))
        self.assertEqual(len(a['pending_settlements']),1)
        self.assertFalse(a['portfolio_pnl_verified'])
        self.assertIsNone(a['ytd_pnl_czk'])
        self.assertIsNone(a['nav_czk'])
        row['Trade date']='2026-10-15'
        a=calculate_accounting(snapshot(),self.baseline,activity([row],until='2026-10-15'))
        self.assertEqual(a['assets'][0]['expected_quantity'],'1')
        self.assertEqual(a['pending_settlements'],[])

    def test_owner_confirmed_settlement_clears_quantity_and_groups_cash_once(self):
        row=posting('-20','Buy to open','OPTION',Description1='Synthetic option; US0000000002',
                    Description3='Number/Amt. 1; Transaction price: 20 USD')
        margin=posting('30','Variation margin','MARGIN',Description1='Margin settlement')
        view=activity([row,margin]);next(r for r in view['ledger'] if r['transaction_no']=='MARGIN')['category']='futures_margin'
        a=calculate_accounting(snapshot(cash='110'),self.baseline,view,lifecycle=[{'key':'US0000000002',
            'currency':'USD','settled_by':'2026-10-14','reason':'Owner confirmed expiry or margin settlement'}])
        p=next(p for p in a['assets'] if p['key']=='US0000000002')
        self.assertEqual(p['quantity_status'],'Settled; cash result grouped')
        self.assertIsNone(p['realized_native'])
        self.assertEqual(Decimal(a['currencies'][0]['futures_settlements_native']),10)
        self.assertEqual(Decimal(a['realized_totals']['USD']),10)

    def test_owner_hedge_classification_changes_contribution_without_changing_total(self):
        row=posting('20','OTC FI Rate Refix/Payment','OTC',Description1='Synthetic OTC payment')
        view=activity([row]);view['ledger'][0]['category']='otc_settlement_review'
        identifier=view['ledger'][0]['id'];close=snapshot(cash='120')
        unresolved=calculate_accounting(close,self.baseline,view)
        self.assertFalse(unresolved['portfolio_pnl_verified'])
        hedge=calculate_accounting(close,self.baseline,view,cash_reviews={identifier:{'classification':'fx_hedge_settlement'}})
        asset=calculate_accounting(close,self.baseline,view,cash_reviews={identifier:{'classification':'asset_settlement'}})
        self.assertEqual(hedge['ytd_pnl_czk'],asset['ytd_pnl_czk'])
        h=hedge['currencies'][0];a=asset['currencies'][0]
        self.assertEqual(Decimal(a['asset_contribution_czk'])-Decimal(h['asset_contribution_czk']),400)
        self.assertEqual(Decimal(h['currency_contribution_czk'])-Decimal(a['currency_contribution_czk']),400)
        self.assertEqual(Decimal(h['asset_contribution_czk'])+Decimal(h['currency_contribution_czk']),Decimal(h['total_pnl_czk']))
        self.assertTrue(hedge['portfolio_pnl_verified'])

    def test_cash_chain_and_coverage_gaps_prevent_certification(self):
        a=calculate_accounting(snapshot(cash='105'),self.baseline,activity([],until='2026-09-30'))
        self.assertFalse(a['portfolio_pnl_verified'])
        self.assertTrue(any('does not reconcile' in x for x in a['issues']))
        self.assertTrue(any('coverage ends' in x for x in a['issues']))
        wrong=deepcopy(self.baseline);wrong['portfolio']='987-654-01'
        with self.assertRaises(ValueError):calculate_accounting(snapshot(),wrong,activity([]))

    def test_published_fx_direction_wins_over_rounded_reciprocal(self):
        self.assertEqual(fx_rate({'exchange_rates':{'USD/CZK':'21.914346','CZK/USD':'0.045632'}},'USD','CZK'),Decimal('21.914346'))

    def test_period_is_not_mislabeled_daily_and_reports_preserve_basis(self):
        previous=snapshot(as_of='2026-10-08');current=snapshot(price='95')
        p=period_view(previous,current,activity([]),{})
        self.assertEqual(p['label'],'Period P/L')
        self.assertEqual(Decimal(p['pnl_czk']),105)
        a=calculate_accounting(current,self.baseline,activity([]));a['period']=p
        data={'accounting':a,'snapshot_risk':{'rows':[]}}
        html=desk_html(data);csv=desk_csv(data)
        self.assertIn(b'Size EUR',html)
        self.assertIn(b'Period P/L',html)
        self.assertIn(b'Historical average cost',csv)
        self.assertIn(b'currency_contribution_czk',csv)

    def test_expiry_risk_uses_verified_multiplier_and_excludes_expired_terms(self):
        s=snapshot('2','5','100');option=s['positions'][1]
        option.update(description='Call Synthetic 1:100 Strike price USD 100 Market price underlying USD 105 18.12.2026',market_value='1000')
        r=snapshot_risk(s)['rows'][0]
        self.assertEqual(r['maximum_premium_loss'],'1000')
        self.assertEqual(Decimal(r['expiry_pnl']['0']),0)
        self.assertEqual(Decimal(r['expiry_pnl']['-0.1']),-1000)
        self.assertEqual(Decimal(r['expiry_pnl']['0.1']),2100)
        option['description']=option['description'].replace('18.12.2026','18.09.2026')
        self.assertFalse(snapshot_risk(s)['rows'])
        option['description']=option['description'].replace('18.09.2026','18.12.2026')
        option['market_value']='10000'
        self.assertFalse(snapshot_risk(s)['rows'])


class AccountingStorageTests(unittest.TestCase):
    def test_history_idempotency_conflict_atomicity_and_revoked_device(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'db.sqlite3';store=HistoryStore(db)
            requested=[{'security':'USDCZK Curncy','dates':['2026-01-02','2026-01-03']}]
            row={'security':'USDCZK Curncy','date':'2026-01-02','price':'21'}
            self.assertEqual(store.ingest({'quotes':[row]},requested)['accepted'],1)
            self.assertEqual(store.ingest({'quotes':[row]},[])['existing'],1)
            bond={'security':'/isin/'+ISIN,'date':'2026-01-02','price':'91'}
            bond_request=[{'security':'/isin/'+ISIN,'dates':['2026-01-02']}]
            self.assertEqual(store.ingest({'quotes':[bond]},bond_request)['accepted'],1)
            self.assertEqual(store.ingest({'quotes':[bond|{'accrued':'1.25'}]},bond_request)['accepted'],1)
            self.assertEqual(store.observations()[('/isin/'+ISIN,'2026-01-02')]['accrued'],Decimal('1.25'))
            with self.assertRaises(ValueError):
                store.ingest({'quotes':[bond|{'accrued':'1.5'}]},bond_request)
            with self.assertRaises(ValueError):store.ingest({'quotes':[row|{'date':'2026-01-03'},row|{'price':'22'}]},requested)
            self.assertEqual(len(store.rates()),2)
            with self.assertRaises(ValueError):store.ingest({'quotes':[row|{'security':'IBM US Equity'}]},requested)
            quotes=QuoteStore(db);device=quotes.create_token('Test');quotes.revoke(device['id'])
            with self.assertRaises(PermissionError):store.ingest({'quotes':[]},[],device['id'])

    def test_native_pdf_and_csv_batch_repeat_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'db.sqlite3';store=ActivityStore(db)
            pdf={'pdf_base64':base64.b64encode(pdf_document(STATEMENT)).decode()}
            batch=[pdf,account_csv([],opening='100')]
            self.assertEqual(store.import_batch(batch)['files'][0]['format'],'baseline')
            self.assertTrue(store.import_batch(batch)['files'][0]['already_imported'])
            changed={'pdf_base64':base64.b64encode(pdf_document(STATEMENT.replace('USD 50','USD 51'))).decode()}
            with self.assertRaises(ValueError):store.import_batch([changed,'bad csv'])
            with self.assertRaises(ValueError):store.import_batch([changed])
            with sqlite3.connect(db) as connection:self.assertEqual(connection.execute('SELECT COUNT(*) FROM opening_baselines').fetchone()[0],1)
            self.assertEqual(BaselineStore(db).latest()['positions'][1]['cost_native'],'50')

    def test_lifecycle_confirmation_is_append_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store=LifecycleStore(Path(directory)/'db.sqlite3')
            row={'key':ISIN,'currency':'USD','settled_by':'2026-10-14','reason':'Owner: expiry or margin settlement'}
            store.confirm([row]);store.confirm([row]);self.assertEqual(store.all(),[row])
            with self.assertRaises(ValueError):store.confirm([row|{'currency':'EUR'}])
            with self.assertRaises(ValueError):store.confirm([row|{'reason':'line\nbreak'}])

    def test_cash_decisions_retain_audit_and_reject_wrong_source_category(self):
        from src.cash_review import CashReviewStore
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'db.sqlite3';store=CashReviewStore(db)
            rows=activity([posting('10','Special payment order','PAY')])['ledger']
            decision={'id':rows[0]['id'],'classification':'external_funding','reason':'Owner confirmed portfolio boundary'}
            store.record([decision],rows);store.record([decision],rows)
            self.assertEqual(store.all()[decision['id']],decision)
            with self.assertRaises(ValueError):store.record([decision|{'classification':'fx_hedge_settlement'}],rows)
            replacement=decision|{'classification':'asset_settlement','reason':'Owner corrected: outside-bank trade expiry'}
            store.record([replacement],rows)
            self.assertEqual(store.all()[decision['id']],replacement)
            with sqlite3.connect(db) as connection:self.assertEqual(connection.execute('SELECT COUNT(*) FROM cash_reviews').fetchone()[0],2)

    def test_owner_import_and_market_device_routes_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            server=create_server(0,Path(directory)/'db.sqlite3');threading.Thread(target=server.serve_forever,daemon=True).start()
            root=f'http://127.0.0.1:{server.server_port}'
            def post(path,body,headers):
                return urlopen(Request(root+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}|headers))
            try:
                with post('/api/bridge/token',{'name':'Test'},{'X-Bridge-Action':'manage'}) as response:device=json.load(response)
                headers={'Authorization':'Bearer '+device['config']['token']}
                with post('/api/bridge/history',{'quotes':[]},headers) as response:self.assertEqual(json.load(response)['accepted'],0)
                for path,body,extra,code in [('/api/bridge/history',{'quotes':[]},{'Origin':'https://other.example'},403),
                    ('/api/accounting/settlements',[],{},403),('/api/accounting/cash-review',[],{},403),('/api/import/ubs',{'files':[]},{},403)]:
                    with self.assertRaises(HTTPError) as error:post(path,body,headers|extra)
                    self.assertEqual(error.exception.code,code)
                with post('/api/bridge/revoke',{'id':device['id']},{'X-Bridge-Action':'manage'}):pass
                with self.assertRaises(HTTPError) as error:post('/api/bridge/history',{'quotes':[]},headers)
                self.assertEqual(error.exception.code,401)
            finally:server.shutdown();server.server_close()
