from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum


class SideFilter(str, Enum):
    ANY = "ANY"
    BUY = "BUY"
    SELL = "SELL"

    def next_value(self) -> "SideFilter":
        if self is SideFilter.ANY:
            return SideFilter.BUY
        if self is SideFilter.BUY:
            return SideFilter.SELL
        return SideFilter.ANY


class OutcomeFilter(str, Enum):
    ANY = "ANY"
    YES = "YES"
    NO = "NO"

    def next_value(self) -> "OutcomeFilter":
        if self is OutcomeFilter.ANY:
            return OutcomeFilter.YES
        if self is OutcomeFilter.YES:
            return OutcomeFilter.NO
        return OutcomeFilter.ANY


def normalize_wallet(address: str) -> str:
    return address.strip().lower()


def price_to_cents(price: Decimal) -> int:
    return int((price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def short_wallet(address: str) -> str:
    value = normalize_wallet(address)
    if len(value) < 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


@dataclass(slots=True)
class Subscription:
    id: int
    telegram_user_id: int
    chat_id: int
    wallet_address: str
    alias: str | None
    enabled: bool
    alerts_enabled: bool
    paper_enabled: bool
    side_filter: SideFilter
    outcome_filter: OutcomeFilter
    min_price_cents: int | None
    max_price_cents: int | None
    start_timestamp: int


@dataclass(slots=True)
class TradeEvent:
    proxy_wallet: str
    side: str
    asset: str
    condition_id: str
    size: Decimal
    price: Decimal
    timestamp: int
    title: str
    slug: str
    outcome: str
    transaction_hash: str

    @property
    def price_cents(self) -> int:
        return price_to_cents(self.price)


@dataclass(slots=True)
class PaperPosition:
    subscription_id: int
    wallet_address: str
    alias: str | None
    asset: str
    outcome: str
    qty: Decimal
    avg_price: Decimal
    realized_pnl: Decimal
    last_mark_price: Decimal | None


@dataclass(slots=True)
class PortfolioSummary:
    realized: Decimal
    unrealized: Decimal
    total: Decimal
    open_positions: int
