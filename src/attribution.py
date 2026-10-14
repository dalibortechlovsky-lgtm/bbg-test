"""Exact asset/FX decomposition of a native-currency portfolio bucket.

Capital movements include external funding and matched internal cross-currency
legs, never same-currency purchases/sales of securities. Opening/closing values
must cover the whole bucket consistently, including cash and relevant liabilities.
This is calculation logic; it does not certify source coverage or broker matches.
"""
from decimal import Decimal, localcontext
import re
from .performance import dec


def attribute_currency(currency,opening_native,closing_native,opening_fx,closing_fx,flows=None,report_currency='CZK'):
    for c in [currency,report_currency]:
        if not isinstance(c,str) or not re.fullmatch('[A-Z]{3}',c): raise ValueError('Three-letter currency codes required')
    b=dec(opening_native,'opening native value');e=dec(closing_native,'closing native value')
    x0=dec(opening_fx,'opening FX');x1=dec(closing_fx,'closing FX')
    if x0<=0 or x1<=0: raise ValueError('FX is positive report currency per native currency unit')
    movements=[]
    if flows is not None and not isinstance(flows,list): raise ValueError('Capital movements must be a list')
    for f in flows or []:
        if not isinstance(f,dict) or set(f)!={'amount','fx'}: raise ValueError('Each movement requires native amount and dated FX')
        amount=dec(f['amount'],'capital movement');rate=dec(f['fx'],'movement FX')
        if rate<=0: raise ValueError('Movement FX must be positive')
        movements.append((amount,rate))
    if currency==report_currency and (x0!=1 or x1!=1 or any(rate!=1 for _,rate in movements)):
        raise ValueError('Report currency must translate to itself at one')
    with localcontext() as context:
        context.prec=110
        flow_native=sum((amount for amount,_ in movements),Decimal(0))
        flow_base=sum((amount*rate for amount,rate in movements),Decimal(0))
        native_profit=e-b-flow_native
        asset=native_profit*x0
        fx=e*(x1-x0)+sum((amount*(x0-rate) for amount,rate in movements),Decimal(0))
        total=e*x1-b*x0-flow_base
        if asset+fx!=total: raise ArithmeticError('Attribution does not reconcile')
        return {'currency':currency,'report_currency':report_currency,'native_asset_pnl':str(native_profit),
                'asset_contribution':str(asset),'currency_contribution':str(fx),'total_pnl':str(total),
                'opening_value':str(b*x0),'closing_value':str(e*x1),'capital_movements':str(flow_base),
                'convention':'Opening FX for asset profit; ending exposure and dated capital movements for FX; interaction assigned to currency',
                'status':'Calculated from supplied values; coverage and broker reconciliation required'}
