from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import re
from typing import Optional


class TradeSource(str, Enum):
    BLOOMBERG_CHAT = "bloomberg_chat"
    UBS_KEYTRADER = "ubs_keytrader"


class TradeStatus(str, Enum):
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    BOOKED_MARS = "booked_mars"
    RECONCILED = "reconciled"
    EXCEPTION = "exception"


@dataclass
class Trade:
    trade_id: str
    source: TradeSource
    status: TradeStatus
    side: str
    security: str
    quantity: Decimal
    price: Decimal
    trade_time: Optional[datetime] = None
    currency: Optional[str] = None
    account: Optional[str] = None
    external_id: Optional[str] = None

    def fingerprint(self) -> str:
        # Stable economic identity; later enriched with account/currency when available.
        raw = f"{self.side}|{self.security}|{self.quantity}|{self.price}"
        return sha256(raw.encode()).hexdigest()[:20]

    def to_dict(self):
        return asdict(self)


_CHAT = re.compile(
    r"^\s*(?P<side>bought|buy|sold|sell)\s+"
    r"(?P<security>.+?)\s+at\s+"
    r"(?P<price>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?:in\s+)?(?P<qty>[0-9][0-9,]*)\s+(?:units?|shares?)?\s*$",
    re.IGNORECASE,
)


def parse_bloomberg_chat(text: str, trade_time: Optional[datetime] = None) -> Trade:
    m = _CHAT.match(text)
    if not m:
        raise ValueError(f"Unrecognized trade instruction: {text!r}")

    side = "BUY" if m.group("side").lower() in {"bought", "buy"} else "SELL"
    security = " ".join(m.group("security").split()).upper()
    qty = Decimal(m.group("qty").replace(",", ""))
    price = Decimal(m.group("price"))
    seed = f"{side}|{security}|{qty}|{price}|{trade_time or ''}"
    trade_id = "CHAT-" + sha256(seed.encode()).hexdigest()[:12].upper()

    return Trade(
        trade_id=trade_id,
        source=TradeSource.BLOOMBERG_CHAT,
        status=TradeStatus.PROVISIONAL,
        side=side,
        security=security,
        quantity=qty,
        price=price,
        trade_time=trade_time,
    )


def merge_confirmation(provisional: Trade, confirmed: Trade) -> Trade:
    if provisional.fingerprint() != confirmed.fingerprint():
        raise ValueError("Trades do not economically match")
    provisional.status = TradeStatus.CONFIRMED
    provisional.external_id = confirmed.external_id or provisional.external_id
    provisional.trade_time = confirmed.trade_time or provisional.trade_time
    provisional.currency = confirmed.currency or provisional.currency
    provisional.account = confirmed.account or provisional.account
    return provisional
