from __future__ import annotations

from .types import OutcomeFilter, SideFilter, Subscription, TradeEvent


def _normalized_outcome(value: str) -> str:
    return value.strip().lower()


def trade_matches_subscription(trade: TradeEvent, sub: Subscription) -> bool:
    if sub.side_filter is not SideFilter.ANY and trade.side != sub.side_filter.value:
        return False

    if sub.outcome_filter is OutcomeFilter.YES and _normalized_outcome(trade.outcome) != "yes":
        return False
    if sub.outcome_filter is OutcomeFilter.NO and _normalized_outcome(trade.outcome) != "no":
        return False

    cents = trade.price_cents
    if sub.min_price_cents is not None and cents < sub.min_price_cents:
        return False
    if sub.max_price_cents is not None and cents > sub.max_price_cents:
        return False

    return True
