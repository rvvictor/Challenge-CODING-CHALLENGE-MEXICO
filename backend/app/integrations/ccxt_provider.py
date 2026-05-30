from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from backend.app.core.config import ExchangeConfig, Settings
from backend.app.core.models import Level, OrderBook
from backend.app.engines.fills import sort_levels


BookCallback = Callable[[OrderBook], None]
EventCallback = Callable[[dict], Awaitable[None]]


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
        for exchange in self.settings.exchanges:
            for symbol in dict.fromkeys((exchange.primary_symbol, *exchange.triangular_symbols)):
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
                "mode": "websocket",
                "failures": 0,
                "reconnects": 0,
                "updates": 0,
                "lastUpdate": 0,
                "lastError": "",
                "restStartedAt": 0,
            }
        return self.states[key]

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
                orderbook = await watch(symbol, self.settings.order_book_limit)
                latency_ms = max(1, round((time.perf_counter() - started) * 1000))
                state["failures"] = 0
                state["updates"] += 1
                state["lastUpdate"] = int(time.time() * 1000)
                state["lastError"] = ""
                self.on_book(self.normalize(exchange, symbol, orderbook, "websocket", latency_ms))
            except Exception as exc:
                state["failures"] += 1
                state["reconnects"] += 1
                state["lastError"] = str(exc)
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
                orderbook = await client.fetch_order_book(symbol, self.settings.order_book_limit)
                state["updates"] += 1
                state["lastUpdate"] = int(time.time() * 1000)
                self.on_book(self.normalize(exchange, symbol, orderbook, "rest", self.settings.request_timeout_ms))
            except Exception as exc:
                state["lastError"] = str(exc)
                await self.emit("rest-error", {"exchange": exchange.name, "symbol": symbol, "reason": str(exc)})
            if int(time.time() * 1000) - state["restStartedAt"] >= self.settings.rest_recovery_attempt_ms:
                state["mode"] = "websocket"
                state["failures"] = 0
                asyncio.create_task(self.watch_loop(state))
                return
            await asyncio.sleep(self.settings.poll_interval_ms / 1000)

    def normalize(self, exchange: ExchangeConfig, symbol: str, orderbook: dict, source: str, latency_ms: float) -> OrderBook:
        asks = [Level(float(level[0]), float(level[1])) for level in orderbook.get("asks", [])[: self.settings.order_book_limit]]
        bids = [Level(float(level[0]), float(level[1])) for level in orderbook.get("bids", [])[: self.settings.order_book_limit]]
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
                    "mode": state["mode"],
                    "failures": state["failures"],
                    "reconnects": state["reconnects"],
                    "updates": state["updates"],
                    "lastUpdate": state["lastUpdate"],
                    "lastError": state["lastError"],
                    "restFallback": state["mode"] == "rest",
                }
                for state in self.states.values()
            ],
        }
