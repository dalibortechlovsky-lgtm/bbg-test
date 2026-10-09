import csv
from decimal import Decimal
from io import StringIO
from typing import Iterable
from .trades import Trade, TradeSource, TradeStatus


# Placeholder field aliases. Replace/extend once a real KeyTrader EOD export is supplied.
ALIASES = {
    "id": ("trade_id", "id", "reference", "external_id"),
    "side": ("side", "buy_sell", "b/s"),
    "security": ("security", "ticker", "instrument"),
    "quantity": ("quantity", "qty", "units"),
    "price": ("price", "execution_price", "px"),
}


def _pick(row, names):
    lowered = {k.strip().lower(): v for k, v in row.items()}
    for n in names:
        if n in lowered and lowered[n] not in (None, ""):
            return lowered[n]
    return None


def parse_keytrader_csv(content: str) -> Iterable[Trade]:
    for row in csv.DictReader(StringIO(content)):
        side_raw = (_pick(row, ALIASES["side"]) or "").upper()
        side = "BUY" if side_raw in {"BUY", "B", "BOUGHT"} else "SELL"
        external_id = _pick(row, ALIASES["id"])
        yield Trade(
            trade_id=f"UBS-{external_id}" if external_id else "UBS-PENDING",
            source=TradeSource.UBS_KEYTRADER,
            status=TradeStatus.CONFIRMED,
            side=side,
            security=(_pick(row, ALIASES["security"]) or "").upper(),
            quantity=Decimal((_pick(row, ALIASES["quantity"]) or "0").replace(",", "")),
            price=Decimal((_pick(row, ALIASES["price"]) or "0").replace(",", "")),
            external_id=external_id,
        )
