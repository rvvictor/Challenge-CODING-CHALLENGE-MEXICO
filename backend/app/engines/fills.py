from __future__ import annotations

from dataclasses import dataclass

from backend.app.core.models import Level


@dataclass
class Fill:
    requested_qty: float
    filled_qty: float
    unfilled_qty: float
    quote: float
    avg_price: float
    level_count: int
    partial: bool


def sort_levels(levels: list[Level], side: str) -> list[Level]:
    clean = [level for level in levels if level.price > 0 and level.qty > 0]
    return sorted(clean, key=lambda level: level.price, reverse=side == "bid")


def depth_qty(levels: list[Level]) -> float:
    return sum(level.qty for level in levels)


def best(levels: list[Level], side: str) -> Level | None:
    ordered = sort_levels(levels, side)
    return ordered[0] if ordered else None


def estimate_fill(levels: list[Level], requested_qty: float, side: str) -> Fill:
    remaining = max(0.0, requested_qty)
    filled = 0.0
    quote = 0.0
    count = 0
    for level in sort_levels(levels, side):
        if remaining <= 0:
            break
        qty = min(remaining, level.qty)
        filled += qty
        quote += qty * level.price
        remaining -= qty
        count += 1
    return Fill(
        requested_qty=requested_qty,
        filled_qty=filled,
        unfilled_qty=max(0.0, remaining),
        quote=quote,
        avg_price=quote / filled if filled else 0.0,
        level_count=count,
        partial=remaining > 1e-9,
    )


def estimate_buy_with_quote(asks: list[Level], quote_budget: float) -> dict[str, float | bool | int]:
    remaining = max(0.0, quote_budget)
    quote_spent = 0.0
    base_received = 0.0
    count = 0
    for level in sort_levels(asks, "ask"):
        if remaining <= 0:
            break
        spend = min(remaining, level.price * level.qty)
        base = spend / level.price
        quote_spent += spend
        base_received += base
        remaining -= spend
        count += 1
    return {
        "quote_spent": quote_spent,
        "base_received": base_received,
        "avg_price": quote_spent / base_received if base_received else 0.0,
        "unspent_quote": remaining,
        "level_count": count,
        "partial": remaining > quote_budget * 0.001,
    }
