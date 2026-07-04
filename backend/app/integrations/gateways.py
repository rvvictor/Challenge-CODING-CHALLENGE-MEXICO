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
    venue_order_id: str | None = None


class PreTradeGuard:
    """Safety enforced before any order leaves the system: kill switch plus a
    global notional cap tightened by optional per-venue and per-asset caps."""

    def __init__(self, max_order_notional_usd: float = 5000.0, venue_caps: dict | None = None, asset_caps: dict | None = None):
        self.kill_switch = False
        self.max_order_notional_usd = max_order_notional_usd
        self.venue_caps = venue_caps or {}
        self.asset_caps = asset_caps or {}

    def effective_cap(self, venue: str | None, base_asset: str | None) -> float:
        cap = self.max_order_notional_usd
        if venue and venue in self.venue_caps:
            cap = min(cap, self.venue_caps[venue]) if cap else self.venue_caps[venue]
        if base_asset and base_asset in self.asset_caps:
            cap = min(cap, self.asset_caps[base_asset]) if cap else self.asset_caps[base_asset]
        return cap

    def check(self, order: ClientOrder, ref_price: float) -> tuple[bool, str]:
        if self.kill_switch:
            return False, "kill switch active"
        price = order.limit_price or ref_price or 0.0
        notional = order.qty * price
        base_asset = order.symbol.split("/", 1)[0] if order.symbol else None
        cap = self.effective_cap(order.venue, base_asset)
        if cap and notional > cap:
            return False, f"order notional {notional:.0f} exceeds cap {cap:.0f}"
        return True, "ok"

    def snapshot(self) -> dict:
        return {
            "killSwitch": self.kill_switch,
            "maxOrderNotionalUsd": self.max_order_notional_usd,
            "venueCaps": dict(self.venue_caps),
            "assetCaps": dict(self.asset_caps),
        }


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


def _default_testnet_client(ccxt_id: str, key: str, secret: str):  # pragma: no cover - needs real ccxt + keys
    """Build a ccxt client pinned to the venue's SANDBOX (testnet). Raises if the
    venue has no sandbox, so a mainnet URL can never be used by this gateway."""
    import ccxt

    klass = getattr(ccxt, ccxt_id, None)
    if klass is None:
        raise RuntimeError(f"unsupported venue {ccxt_id}")
    client = klass({"apiKey": key, "secret": secret, "enableRateLimit": True, "options": {"defaultType": "spot"}})
    if not getattr(client, "has", {}).get("sandbox", False) and not hasattr(client, "set_sandbox_mode"):
        raise RuntimeError(f"{ccxt_id} has no sandbox/testnet")
    client.set_sandbox_mode(True)  # trading-only testnet; never mainnet
    return client


class TestnetExecutionGateway:
    """Real order lifecycle on exchange TESTNETS (fake money). Enabled only when
    AURELION_ENABLE_LIVE=1 and trading-only testnet keys are present. Places real
    IOC orders per leg, reconciles the trade from the actual fills, and refuses
    withdrawal by construction. This is the honest 'almost functional' path — no
    real capital is ever at risk. `client_factory` is injectable for tests."""

    name = "testnet"

    def __init__(self, guard: PreTradeGuard | None = None, client_factory=None, credentials: dict | None = None):
        self.guard = guard or PreTradeGuard()
        self.enabled = os.getenv("AURELION_ENABLE_LIVE", "") == "1"
        self.credentials = credentials or {}
        self._client_factory = client_factory
        self._clients: dict = {}
        self.last_error = ""
        self.orders_placed = 0

    def capabilities(self) -> dict:
        return {"name": self.name, "marketData": "live", "execution": "testnet-sandbox", "live": True, "readOnly": False, "enabled": self.enabled}

    def supports_withdrawal(self) -> bool:
        return False

    def _client(self, ccxt_id: str):
        if ccxt_id not in self._clients:
            if self._client_factory is not None:
                self._clients[ccxt_id] = self._client_factory(ccxt_id)
            else:  # pragma: no cover - needs real ccxt + keys
                creds = self.credentials.get(ccxt_id) or {}
                self._clients[ccxt_id] = _default_testnet_client(ccxt_id, creds.get("key", ""), creds.get("secret", ""))
        return self._clients[ccxt_id]

    def place_order(self, order: ClientOrder, book) -> GatewayFill:
        ref_price = order.limit_price or 0.0
        allowed, reason = self.guard.check(order, ref_price)
        if not allowed:
            return GatewayFill(order.client_id, order.venue, order.symbol, order.side, order.qty, 0.0, 0.0, "rejected", reason)
        client = self._client(order.venue)
        params = {"timeInForce": "IOC"}
        raw = client.create_order(order.symbol, "limit", order.side, order.qty, order.limit_price, params)
        self.orders_placed += 1
        filled = float(raw.get("filled") or 0.0)
        avg = float(raw.get("average") or raw.get("price") or order.limit_price or 0.0)
        status = "filled" if filled >= order.qty - 1e-12 else "partial" if filled > 0 else "rejected"
        fill = GatewayFill(order.client_id, order.venue, order.symbol, order.side, order.qty, filled, avg, status, "testnet fill")
        fill.venue_order_id = raw.get("id")
        return fill

    def settle_trade(self, trade: dict, opportunity: dict, book_map: dict) -> dict | None:
        if not self.enabled:
            self.last_error = "testnet disabled (set AURELION_ENABLE_LIVE=1 + testnet keys)"
            return None
        # Cross-exchange only for the testnet lifecycle; triangular stays paper.
        if trade.get("strategy") == "triangular":
            self.last_error = "triangular testnet settlement not supported; use cross-exchange"
            return None
        try:
            base = trade.get("baseAsset", "BTC")
            qty = float(trade.get("qtyBtc") or 0.0)
            buy_symbol = f"{base}/USDT"
            sell_symbol = f"{base}/USDT"
            buy_order = ClientOrder(f"{trade['id']}-BUY", trade["buyExchangeId"], buy_symbol, "buy", qty, float(trade.get("buyPrice") or 0.0))
            sell_order = ClientOrder(f"{trade['id']}-SELL", trade["sellExchangeId"], sell_symbol, "sell", qty, float(trade.get("sellPrice") or 0.0))
            buy_fill = self.place_order(buy_order, book_map.get(f"{trade['buyExchangeId']}:{buy_symbol}"))
            sell_fill = self.place_order(sell_order, book_map.get(f"{trade['sellExchangeId']}:{sell_symbol}"))
            filled = min(buy_fill.filled_qty, sell_fill.filled_qty)
            if filled <= 0 or "rejected" in (buy_fill.status, sell_fill.status):
                self.last_error = f"testnet leg rejected (buy={buy_fill.status}, sell={sell_fill.status})"
                return None
            ratio = filled / qty if qty else 0.0
            trade["gateway"] = self.name
            trade["execution"] = "testnet-sandbox"
            trade["qtyBtc"] = round(filled, 8)
            trade["filledRatio"] = round(ratio, 4)
            trade["partial"] = ratio < 0.999
            trade["netProfit"] = round(float(trade.get("netProfit") or 0.0) * ratio, 4)
            trade["status"] = "filled" if ratio >= 0.999 else "partial-fill"
            trade["orders"] = [
                {"clientId": buy_fill.client_id, "venue": buy_fill.venue, "side": "buy", "status": buy_fill.status, "venueOrderId": getattr(buy_fill, "venue_order_id", None), "filled": buy_fill.filled_qty, "avgPrice": buy_fill.avg_price},
                {"clientId": sell_fill.client_id, "venue": sell_fill.venue, "side": "sell", "status": sell_fill.status, "venueOrderId": getattr(sell_fill, "venue_order_id", None), "filled": sell_fill.filled_qty, "avgPrice": sell_fill.avg_price},
            ]
            return trade
        except Exception as exc:  # noqa: BLE001 - a failed testnet order must never break the loop
            self.last_error = f"testnet settlement error: {exc}"
            return None


GATEWAY_MODES = ("paper", "read-only-live", "testnet", "live")


def build_gateway(mode: str, guard: PreTradeGuard) -> ExecutionGateway:
    if mode == "read-only-live":
        return ReadOnlyLiveGateway(guard)
    if mode == "testnet":
        return TestnetExecutionGateway(guard)
    if mode == "live":
        return LiveExecutionGateway(guard)
    return PaperExecutionGateway(guard)
