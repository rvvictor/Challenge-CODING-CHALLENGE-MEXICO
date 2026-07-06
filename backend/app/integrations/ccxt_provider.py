from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from backend.app.core.config import ExchangeConfig, Settings, live_symbols
from backend.app.core.models import Level, OrderBook
from backend.app.engines.fills import sort_levels


BookCallback = Callable[[OrderBook], None]
EventCallback = Callable[[dict], Awaitable[None]]


VALID_ORDER_BOOK_LIMITS = {
    "bitfinex": (25, 100),
    "bybit": (1, 50, 200, 1000),
    "kraken": (10, 25, 100, 500, 1000),
    "kucoin": (5, 20, 50, 100),
}


class CcxtStreamProvider:
    def __init__(self, settings: Settings, on_book: BookCallback, on_event: EventCallback):
        self.settings = settings
        self.on_book = on_book
        self.on_event = on_event
        self.active = False
        self.clients: dict[str, object] = {}
        self.states: dict[str, dict] = {}
        self.ccxt = None
        self.available = False
        self.unavailable_reason = ""

    async def start(self) -> None:
        if self.active:
            return
        self.active = True
        await self.load_ccxt()
        if not self.available:
            await self.emit("provider-unavailable", {"severity": "warning", "reason": self.unavailable_reason})
            return
        # Watch the primary + triangular legs, and (when alt trading is on) the
        # direct XRP/LTC/SOL/AVAX pairs where real edges were found.
        for exchange in self.settings.exchanges:
            symbols = live_symbols(exchange) if self.settings.live_alt_enabled else dict.fromkeys((exchange.primary_symbol, *exchange.triangular_symbols))
            for symbol in symbols:
                state = self.state(exchange, symbol)
                asyncio.create_task(self.watch_loop(state))

    async def stop(self) -> None:
        self.active = False
        for client in self.clients.values():
            close = getattr(client, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        self.clients.clear()

    async def load_ccxt(self) -> None:
        try:
            import ccxt.pro as ccxtpro

            self.ccxt = ccxtpro
            self.available = True
        except Exception as exc:
            try:
                import ccxt.async_support as ccxt_async

                self.ccxt = ccxt_async
                self.available = True
                self.unavailable_reason = "ccxt.pro unavailable; using async REST fallback where needed"
            except Exception:
                self.available = False
                self.unavailable_reason = f"ccxt unavailable: {exc}"

    def state(self, exchange: ExchangeConfig, symbol: str) -> dict:
        key = f"{exchange.id}:{symbol}"
        if key not in self.states:
            self.states[key] = {
                "key": key,
                "exchange": exchange,
                "symbol": symbol,
                "orderBookLimit": self.order_book_limit(exchange),
                "mode": "websocket",
                "failures": 0,
                "reconnects": 0,
                "updates": 0,
                "lastUpdate": 0,
                "lastError": "",
                "restStartedAt": 0,
                "restFailures": 0,
                "disabledReason": "",
                "healthScore": 100,
                "healthStatus": "warming",
                "lastLatencyMs": 0,
                "latencyWindow": [],
            }
        return self.states[key]

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(len(ordered) * q))
        return ordered[idx]

    def client(self, exchange: ExchangeConfig):
        if exchange.id in self.clients:
            return self.clients[exchange.id]
        klass = getattr(self.ccxt, exchange.ccxt_id)
        client = klass({
            "enableRateLimit": True,
            "timeout": self.settings.request_timeout_ms,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        })
        self.clients[exchange.id] = client
        return client

    def order_book_limit(self, exchange: ExchangeConfig) -> int:
        configured = exchange.order_book_limit if exchange.order_book_limit is not None else self.settings.order_book_limit
        valid = VALID_ORDER_BOOK_LIMITS.get(exchange.ccxt_id)
        if not valid or configured in valid:
            return configured
        larger = [limit for limit in valid if limit >= configured]
        return larger[0] if larger else valid[-1]

    async def watch_loop(self, state: dict) -> None:
        while self.active and state["mode"] == "websocket":
            exchange = state["exchange"]
            symbol = state["symbol"]
            try:
                client = self.client(exchange)
                watch = getattr(client, "watch_order_book", None) or getattr(client, "watchOrderBook", None)
                if not watch:
                    raise RuntimeError(f"{exchange.name} has no watchOrderBook")
                started = time.perf_counter()
                orderbook = await watch(symbol, self.order_book_limit(exchange))
                latency_ms = max(1, round((time.perf_counter() - started) * 1000))
                state["failures"] = 0
                state["restFailures"] = 0
                state["updates"] += 1
                state["lastUpdate"] = int(time.time() * 1000)
                state["lastError"] = ""
                self.mark_success(state, latency_ms, "healthy")
                self.on_book(self.normalize(exchange, symbol, orderbook, "websocket", latency_ms))
            except Exception as exc:
                state["failures"] += 1
                state["reconnects"] += 1
                state["lastError"] = str(exc)
                self.mark_error(state)
                await self.emit("websocket-error", {"exchange": exchange.name, "symbol": symbol, "failures": state["failures"], "reason": str(exc)})
                if state["failures"] >= self.settings.ws_failure_threshold:
                    state["mode"] = "rest"
                    state["restStartedAt"] = int(time.time() * 1000)
                    await self.emit("rest-fallback", {"exchange": exchange.name, "symbol": symbol, "reason": "WebSocket failed 5 times; REST polling activated"})
                    asyncio.create_task(self.rest_loop(state))
                    return
                await asyncio.sleep(self.settings.ws_reconnect_delay_ms / 1000)

    async def rest_loop(self, state: dict) -> None:
        while self.active and state["mode"] == "rest":
            exchange = state["exchange"]
            symbol = state["symbol"]
            try:
                client = self.client(exchange)
                started = time.perf_counter()
                orderbook = await client.fetch_order_book(symbol, self.order_book_limit(exchange))
                latency_ms = max(1, round((time.perf_counter() - started) * 1000))
                state["updates"] += 1
                state["restFailures"] = 0
                state["lastUpdate"] = int(time.time() * 1000)
                self.mark_success(state, latency_ms, "rest-watch")
                self.on_book(self.normalize(exchange, symbol, orderbook, "rest", latency_ms))
            except Exception as exc:
                state["restFailures"] += 1
                state["lastError"] = str(exc)
                self.mark_error(state, penalty=16)
                await self.emit("rest-error", {"exchange": exchange.name, "symbol": symbol, "reason": str(exc)})
                if state["restFailures"] >= 3:
                    state["mode"] = "disabled"
                    state["disabledReason"] = f"REST fallback failed {state['restFailures']} times"
                    state["healthScore"] = 0
                    state["healthStatus"] = "disabled"
                    await self.emit("stream-disabled", {"exchange": exchange.name, "symbol": symbol, "reason": state["disabledReason"], "lastError": str(exc)})
                    return
            if int(time.time() * 1000) - state["restStartedAt"] >= self.settings.rest_recovery_attempt_ms:
                state["mode"] = "websocket"
                state["failures"] = 0
                asyncio.create_task(self.watch_loop(state))
                return
            await asyncio.sleep(self.settings.poll_interval_ms / 1000)

    def normalize(self, exchange: ExchangeConfig, symbol: str, orderbook: dict, source: str, latency_ms: float) -> OrderBook:
        limit = self.order_book_limit(exchange)
        asks = [Level(float(level[0]), float(level[1])) for level in orderbook.get("asks", [])[:limit]]
        bids = [Level(float(level[0]), float(level[1])) for level in orderbook.get("bids", [])[:limit]]
        timestamp = orderbook.get("timestamp") or int(time.time() * 1000)
        return OrderBook(
            key=f"{exchange.id}:{symbol}",
            exchange_id=exchange.id,
            exchange_name=exchange.name,
            symbol=symbol,
            primary=symbol == exchange.primary_symbol,
            source=source,
            status=source,
            fee_bps=exchange.taker_fee_bps,
            slippage_bps=exchange.slippage_bps,
            confidence=exchange.confidence,
            asks=sort_levels(asks, "ask"),
            bids=sort_levels(bids, "bid"),
            latency_ms=latency_ms,
            timestamp=int(timestamp),
        )

    def mark_success(self, state: dict, latency_ms: float, status: str) -> None:
        state["lastLatencyMs"] = latency_ms
        window = state.setdefault("latencyWindow", [])
        window.append(latency_ms)
        if len(window) > 100:
            del window[: len(window) - 100]
        recovery = 3 if status == "healthy" else 1
        state["healthScore"] = min(100, state.get("healthScore", 100) + recovery)
        if latency_ms > self.settings.health_slow_latency_ms:
            state["healthScore"] = max(0, state["healthScore"] - 5)
            state["healthStatus"] = "latency-watch"
        else:
            state["healthStatus"] = status

    def mark_error(self, state: dict, penalty: int = 12) -> None:
        state["healthScore"] = max(0, state.get("healthScore", 100) - penalty)
        state["healthStatus"] = "degraded" if state["healthScore"] >= 35 else "critical"

    async def emit(self, event_type: str, payload: dict) -> None:
        await self.on_event({"type": event_type, "time": int(time.time() * 1000), **payload})

    def snapshot(self) -> dict:
        return {
            "available": self.available,
            "unavailableReason": self.unavailable_reason,
            "streams": [
                {
                    "key": state["key"],
                    "exchangeId": state["exchange"].id,
                    "exchangeName": state["exchange"].name,
                    "symbol": state["symbol"],
                    "orderBookLimit": state["orderBookLimit"],
                    "mode": state["mode"],
                    "failures": state["failures"],
                    "reconnects": state["reconnects"],
                    "updates": state["updates"],
                    "lastUpdate": state["lastUpdate"],
                    "lastError": state["lastError"],
                    "disabledReason": state["disabledReason"],
                    "restFallback": state["mode"] == "rest",
                    "disabled": state["mode"] == "disabled",
                    "healthScore": state["healthScore"],
                    "healthStatus": state["healthStatus"],
                    "lastLatencyMs": state["lastLatencyMs"],
                    "latencyP50Ms": round(self._percentile(state.get("latencyWindow", []), 0.5)),
                    "latencyP95Ms": round(self._percentile(state.get("latencyWindow", []), 0.95)),
                }
                for state in self.states.values()
            ],
        }
