from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from backend.app.engines.fills import best, estimate_fill

# Execution-gateway seam.
#
# Every market+execution path implements the same interface, so the rest of the
# system depends on the abstraction rather than on the simulator or any one venue.
# Aurelion ships:
#   - PaperExecutionGateway   : deterministic paper fills (default).
#   - ReadOnlyLiveGateway     : real market data, paper fills (honest "live").
#   - LiveExecutionGateway    : real order placement — intentionally a DISABLED
#                               stub. It exists to prove the system is one
#                               connector away from real, not to trade live.
# A PreTradeGuard enforces safety (kill switch, per-order notional cap) and the
# whole layer refuses withdrawal scopes by construction.


@dataclass
class ClientOrder:
    client_id: str
    venue: str
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    limit_price: float | None = None  # IOC / limit-aggressive bound
    tif: str = "IOC"


@dataclass
class GatewayFill:
    client_id: str
    venue: str
    symbol: str
    side: str
    requested_qty: float
    filled_qty: float
    avg_price: float
    status: str  # "filled" | "partial" | "rejected"
    note: str = ""


class PreTradeGuard:
    """Safety enforced before any order leaves the system."""

    def __init__(self, max_order_notional_usd: float = 5000.0):
        self.kill_switch = False
        self.max_order_notional_usd = max_order_notional_usd

    def check(self, order: ClientOrder, ref_price: float) -> tuple[bool, str]:
        if self.kill_switch:
            return False, "kill switch active"
        price = order.limit_price or ref_price or 0.0
        notional = order.qty * price
        if self.max_order_notional_usd and notional > self.max_order_notional_usd:
            return False, f"order notional {notional:.0f} exceeds cap {self.max_order_notional_usd:.0f}"
        return True, "ok"

    def snapshot(self) -> dict:
        return {"killSwitch": self.kill_switch, "maxOrderNotionalUsd": self.max_order_notional_usd}


class ExecutionGateway(Protocol):
    name: str

    def capabilities(self) -> dict: ...

    def supports_withdrawal(self) -> bool: ...

    def place_order(self, order: ClientOrder, book) -> GatewayFill: ...

    def settle_trade(self, trade: dict, opportunity: dict, book_map: dict) -> dict: ...


def _synthetic_orders(trade: dict) -> list[dict]:
    """Client-order records for a paper trade, so the order lifecycle is visible
    (and shaped) the same way a real venue order would be."""
    if trade.get("strategy") == "triangular":
        legs = trade.get("legs") or []
        return [
            {"clientId": f"{trade['id']}-L{i}", "venue": trade.get("exchangeId"), "leg": f"{leg.get('from')}->{leg.get('to')}", "tif": "IOC", "status": trade.get("status")}
            for i, leg in enumerate(legs)
        ]
    return [
        {"clientId": f"{trade['id']}-BUY", "venue": trade.get("buyExchangeId"), "side": "buy", "tif": "IOC", "status": trade.get("status")},
        {"clientId": f"{trade['id']}-SELL", "venue": trade.get("sellExchangeId"), "side": "sell", "tif": "IOC", "status": trade.get("status")},
    ]


class PaperExecutionGateway:
    """Deterministic paper fills computed by walking the provided order book."""

    name = "paper"

    def __init__(self, guard: PreTradeGuard | None = None):
        self.guard = guard or PreTradeGuard()

    def capabilities(self) -> dict:
        return {"name": self.name, "marketData": "simulated", "execution": "paper", "live": False, "readOnly": False}

    def supports_withdrawal(self) -> bool:
        return False

    def place_order(self, order: ClientOrder, book) -> GatewayFill:
        levels = book.asks if order.side == "buy" else book.bids
        side = "ask" if order.side == "buy" else "bid"
        reference = best(levels, side)
        ref_price = reference.price if reference else 0.0

        allowed, reason = self.guard.check(order, ref_price)
        if not allowed:
            return GatewayFill(order.client_id, order.venue, order.symbol, order.side, order.qty, 0.0, 0.0, "rejected", reason)

        fill = estimate_fill(levels, order.qty, side)
        avg = fill.avg_price
        status = "partial" if fill.partial else "filled"
        # Honor the IOC / limit-aggressive bound: a fill worse than the bound is rejected.
        if order.limit_price is not None and avg:
            if order.side == "buy" and avg > order.limit_price:
                status = "rejected"
            elif order.side == "sell" and avg < order.limit_price:
                status = "rejected"
        filled = fill.filled_qty if status != "rejected" else 0.0
        note = "paper fill" if status != "rejected" else "limit not met"
        return GatewayFill(order.client_id, order.venue, order.symbol, order.side, order.qty, filled, avg, status, note)

    def settle_trade(self, trade: dict, opportunity: dict, book_map: dict) -> dict:
        # Paper settlement: the fill was already modeled during opportunity
        # evaluation (book-walk + costs + adverse move), so we trust it and just
        # tag provenance + attach the order lifecycle. This makes the gateway the
        # thing the trade loop routes through while keeping paper P&L identical.
        trade["gateway"] = self.name
        trade["execution"] = "paper"
        trade.setdefault("orders", _synthetic_orders(trade))
        return trade


class ReadOnlyLiveGateway(PaperExecutionGateway):
    """Real market data, paper fills — the honest 'live' path. Inherits the paper
    fill primitive; the difference is that the book it is given comes from real
    venues rather than the simulator."""

    name = "read-only-live"

    def capabilities(self) -> dict:
        return {"name": self.name, "marketData": "live", "execution": "paper", "live": True, "readOnly": True}


class LiveExecutionGateway:
    """Real order placement — intentionally NOT implemented. Present to prove the
    system is one connector away from live; enabling it would require read-only-
    trading credentials and an explicit, security-reviewed step."""

    name = "live"

    def __init__(self, guard: PreTradeGuard | None = None):
        self.guard = guard or PreTradeGuard()
        self.enabled = os.getenv("AURELION_ENABLE_LIVE", "") == "1"

    def capabilities(self) -> dict:
        return {"name": self.name, "marketData": "live", "execution": "live", "live": True, "readOnly": False, "enabled": self.enabled}

    def supports_withdrawal(self) -> bool:
        return False

    def place_order(self, order: ClientOrder, book) -> GatewayFill:
        raise NotImplementedError(
            "Live execution is intentionally not implemented. The gateway exists to demonstrate "
            "readiness; connecting a real venue requires read-only-trading credentials and an "
            "explicit, security-reviewed enablement. Aurelion never holds withdrawal-capable keys."
        )

    def settle_trade(self, trade: dict, opportunity: dict, book_map: dict) -> dict:
        raise NotImplementedError(
            "Mainnet live settlement is intentionally not implemented. Use the testnet gateway; "
            "graduating to real capital is a separate, security-reviewed step (see "
            "docs/SECURITY-live-readiness.md)."
        )


GATEWAY_MODES = ("paper", "read-only-live", "live")


def build_gateway(mode: str, guard: PreTradeGuard) -> ExecutionGateway:
    if mode == "read-only-live":
        return ReadOnlyLiveGateway(guard)
    if mode == "live":
        return LiveExecutionGateway(guard)
    return PaperExecutionGateway(guard)
