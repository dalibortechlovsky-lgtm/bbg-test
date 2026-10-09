from decimal import Decimal
from src.trades import parse_bloomberg_chat, Trade, TradeSource, TradeStatus
from src.ledger import TradeLedger
from src.reconcile import reconcile_positions

def test_chat_to_keytrader_confirmation():
    ledger = TradeLedger()
    p = ledger.add(parse_bloomberg_chat("bought Apple US at 500 in 10,000 units"))
    execution = Trade("UBS-1", TradeSource.UBS_KEYTRADER, TradeStatus.CONFIRMED,
                      "BUY", "APPLE US", Decimal("10000"), Decimal("500"),
                      external_id="1", account="TEST")
    matched = ledger.add(execution)
    assert matched.trade_id == p.trade_id
    assert matched.status == TradeStatus.CONFIRMED
    assert len(ledger.trades) == 1

def test_reconciliation_exception():
    x = reconcile_positions({"AAPL US": Decimal("10000")},
                            {"AAPL US": Decimal("9900")})
    assert len(x) == 1
    assert x[0].difference == Decimal("-100")
