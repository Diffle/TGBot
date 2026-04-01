from __future__ import annotations

from decimal import Decimal


ZERO = Decimal("0")


def apply_fill(
    qty: Decimal,
    avg_price: Decimal,
    realized_pnl: Decimal,
    *,
    side: str,
    size: Decimal,
    price: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    if size <= ZERO:
        return qty, avg_price, realized_pnl

    side_upper = side.upper()
    signed_fill = size if side_upper == "BUY" else -size

    if qty == ZERO or (qty > ZERO and signed_fill > ZERO) or (qty < ZERO and signed_fill < ZERO):
        new_qty = qty + signed_fill
        if new_qty == ZERO:
            return ZERO, ZERO, realized_pnl

        if qty == ZERO:
            return new_qty, price, realized_pnl

        new_avg = ((abs(qty) * avg_price) + (abs(signed_fill) * price)) / abs(new_qty)
        return new_qty, new_avg, realized_pnl

    close_qty = min(abs(qty), abs(signed_fill))
    if qty > ZERO and signed_fill < ZERO:
        realized_pnl += (price - avg_price) * close_qty
    elif qty < ZERO and signed_fill > ZERO:
        realized_pnl += (avg_price - price) * close_qty

    new_qty = qty + signed_fill
    if new_qty == ZERO:
        return ZERO, ZERO, realized_pnl

    if qty > ZERO and new_qty > ZERO:
        return new_qty, avg_price, realized_pnl
    if qty < ZERO and new_qty < ZERO:
        return new_qty, avg_price, realized_pnl

    return new_qty, price, realized_pnl


def unrealized_pnl(qty: Decimal, avg_price: Decimal, mark_price: Decimal) -> Decimal:
    if qty > ZERO:
        return (mark_price - avg_price) * qty
    if qty < ZERO:
        return (avg_price - mark_price) * abs(qty)
    return ZERO
