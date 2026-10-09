from dataclasses import dataclass
from .trades import Trade, TradeStatus

@dataclass
class BookingResult:
    accepted: bool
    message: str
    external_id: str | None = None

class BloombergMarsAdapter:
    """Boundary for the permitted Bloomberg MARS/PTT integration.

    No live write is attempted until the workstation/API mechanism and required
    fields are verified. This prevents accidental or duplicate bookings.
    """
    def validate(self, trade: Trade) -> None:
        if trade.status != TradeStatus.CONFIRMED:
            raise ValueError("Only confirmed executions may be sent to MARS/PTT")
        if not trade.account:
            raise ValueError("Account is required before MARS/PTT booking")

    def book(self, trade: Trade) -> BookingResult:
        self.validate(trade)
        return BookingResult(False, "DRY_RUN: Bloomberg write adapter not configured")
