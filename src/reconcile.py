from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List
from .trades import Trade

@dataclass
class PositionException:
    security: str
    expected: Decimal
    actual: Decimal
    difference: Decimal
    reason: str

def expected_net_trades(trades: Iterable[Trade]) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for t in trades:
        signed = t.quantity if t.side == "BUY" else -t.quantity
        out[t.security] = out.get(t.security, Decimal("0")) + signed
    return out

def reconcile_positions(expected: Dict[str, Decimal],
                        actual: Dict[str, Decimal]) -> List[PositionException]:
    exceptions = []
    for sec in sorted(set(expected) | set(actual)):
        e, a = expected.get(sec, Decimal("0")), actual.get(sec, Decimal("0"))
        if e != a:
            exceptions.append(PositionException(sec, e, a, a-e, "POSITION_MISMATCH"))
    return exceptions
