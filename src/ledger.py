from dataclasses import replace
from decimal import Decimal
from typing import Iterable, List, Optional, Tuple
from .trades import Trade, TradeSource, TradeStatus

PRICE_TOLERANCE_BPS = Decimal("25")

def _norm_security(s: str) -> str:
    return " ".join(s.upper().replace(" EQUITY", "").split())

def match_score(a: Trade, b: Trade) -> int:
    """Conservative score for matching a provisional instruction to an execution."""
    if a.side != b.side or a.quantity != b.quantity:
        return -1
    if _norm_security(a.security) != _norm_security(b.security):
        return -1
    score = 80
    if a.price and b.price:
        mid = (a.price + b.price) / 2
        bps = abs(a.price - b.price) / mid * Decimal("10000")
        if bps <= PRICE_TOLERANCE_BPS:
            score += 20
        else:
            return -1
    return score

class TradeLedger:
    def __init__(self):
        self.trades: List[Trade] = []

    def add(self, trade: Trade) -> Trade:
        if trade.external_id and any(t.external_id == trade.external_id for t in self.trades):
            return next(t for t in self.trades if t.external_id == trade.external_id)
        if trade.source == TradeSource.UBS_KEYTRADER:
            candidates = [(match_score(t, trade), t) for t in self.trades
                          if t.status == TradeStatus.PROVISIONAL]
            candidates = [(s,t) for s,t in candidates if s >= 0]
            if candidates:
                score, provisional = max(candidates, key=lambda x:x[0])
                provisional.status = TradeStatus.CONFIRMED
                provisional.external_id = trade.external_id
                provisional.trade_time = trade.trade_time or provisional.trade_time
                provisional.currency = trade.currency or provisional.currency
                provisional.account = trade.account or provisional.account
                return provisional
        self.trades.append(trade)
        return trade

    def exceptions(self) -> List[Trade]:
        return [t for t in self.trades if t.status in {TradeStatus.PROVISIONAL, TradeStatus.EXCEPTION}]
