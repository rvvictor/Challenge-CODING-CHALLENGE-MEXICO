from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from backend.app.core.config import Settings, select_exchanges, settings
from backend.app.core.models import OrderBook
from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine
from backend.app.engines.event_store import EventStore
from backend.app.engines.execution import ExecutionSimulator
from backend.app.engines.fills import best
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.queue import OpportunityQueue
from backend.app.engines.risk import RiskManager
from backend.app.engines.simulator import SimulatedMarket
from backend.app.engines.triangular import TriangularArbitrageEngine
from backend.app.integrations.ccxt_provider import CcxtStreamProvider
from backend.app.integrations.global_market import GlobalMarketIntel
from backend.app.integrations.redis_bus import RedisBus


def now_ms() -> int:
    return int(time.time() * 1000)


def book_mid(book: OrderBook) -> float | None:
    ask = best(book.asks, "ask")
    bid = best(book.bids, "bid")
    return (ask.price + bid.price) / 2 if ask and bid else None


class MarketService:
    def __init__(self, cfg: Settings = settings):
        self.settings = cfg
        self.mode = cfg.market_mode
        self.books: dict[str, OrderBook] = {}
        self.simulator = SimulatedMarket(cfg.exchanges)
        self.store = EventStore()
        self.ledger = WalletLedger(cfg)
        self.risk = RiskManager(cfg)
        self.cross_engine = CrossExchangeArbitrageEngine(cfg, self.ledger)
        self.triangular_engine = TriangularArbitrageEngine(cfg, self.ledger)
        self.queue = OpportunityQueue()
        self.executor = ExecutionSimulator(cfg, self.ledger, self.store, self.risk)
        self.redis = RedisBus(cfg)
        self.global_market = GlobalMarketIntel(cfg)
        self.stream_provider: CcxtStreamProvider | None = None
        self.started_at = now_ms()
        self.task: asyncio.Task | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.last_scan: list[dict] = []
        self.last_executions: list[dict] = []
        self.degraded_demo = False
        self.scan_tick = 0
        self.recorded_signal_times: dict[str, int] = {}

    async def start(self) -> None:
        await self.redis.start()
        await self.global_market.start()
        if self.mode != "demo":
            await self.start_streams()
        if not self.task:
            self.task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
        if self.stream_provider:
            await self.stream_provider.stop()
        await self.global_market.stop()

    async def start_streams(self) -> None:
        if self.stream_provider:
            return
        self.stream_provider = CcxtStreamProvider(self.settings, self.handle_book, self.handle_provider_event)
        await self.stream_provider.start()
        self.degraded_demo = self.mode == "auto" and not self.stream_provider.available

    def handle_book(self, book: OrderBook) -> None:
        self.books[book.key] = book

    async def handle_provider_event(self, event: dict) -> None:
        self.store.add_event(event)
        await self.redis.publish("market-events", event)

    async def set_mode(self, mode: str) -> None:
        if mode not in {"auto", "live", "demo"}:
            return
        if mode == self.mode:
            return
        self.mode = mode
        self.books.clear()
        self.degraded_demo = False
        self.risk.reset_market_window()
        if self.stream_provider:
            await self.stream_provider.stop()
            self.stream_provider = None
        if mode != "demo":
            await self.start_streams()

    async def set_active_exchanges(self, exchange_ids: list[str]) -> None:
        profile = ",".join(str(exchange_id).strip().lower() for exchange_id in exchange_ids if str(exchange_id).strip())
        selected = select_exchanges(self.settings.exchange_universe, profile, max_count=5)
        if len(selected) < 2:
            return
        if [exchange.id for exchange in selected] == [exchange.id for exchange in self.settings.exchanges]:
            return

        if self.stream_provider:
            await self.stream_provider.stop()
            self.stream_provider = None

        object.__setattr__(self.settings, "exchanges", tuple(selected))
        object.__setattr__(self.settings, "active_exchanges", ",".join(exchange.id for exchange in selected))
        self.rebuild_runtime_state(preserve_performance=True)
        if self.mode != "demo":
            await self.start_streams()

    async def trigger_volatility_stress(self) -> None:
        current = now_ms()
        if self.mode == "demo" or self.degraded_demo:
            self.simulator.inject_volatility_stress()
        requested = {
            "id": f"ST-{current}",
            "type": "stress-test",
            "time": current,
            "condition": "volatility-test",
            "reason": "Manual volatility stress test requested",
            "metadata": {"changePct": 3.2, "source": "dashboard"},
        }
        self.store.add_event(requested)
        await self.redis.publish("risk", requested)
        self.risk.activate(
            "volatility",
            "Stress test: BTC volatility 3.20% in <1s",
            current,
            {"changePct": 3.2, "windowMs": 1000, "stressTest": True},
        )
        self.flush_risk_events()
        snapshot = self.snapshot()
        await self.redis.publish("snapshots", snapshot)
        self.broadcast(snapshot)

    def set_auto_execution(self, enabled: bool) -> None:
        self.risk.set_auto_execution(enabled)

    def reset(self) -> None:
        self.store.reset()
        self.ledger.reset()
        self.risk.reset()
        self.executor.reset()
        self.started_at = now_ms()
        self.scan_tick = 0
        self.recorded_signal_times.clear()

    def rebuild_runtime_state(self, preserve_performance: bool = True) -> None:
        self.books.clear()
        self.last_scan = []
        self.last_executions = []
        self.degraded_demo = False
        self.scan_tick = 0
        self.recorded_signal_times.clear()
        self.simulator = SimulatedMarket(self.settings.exchanges)
        if preserve_performance:
            self.ledger.sync_exchanges(self.settings.exchanges)
            self.risk.reset_market_window()
        else:
            self.store.reset()
            self.ledger = WalletLedger(self.settings)
            self.risk.reset()
            self.started_at = now_ms()
        self.cross_engine = CrossExchangeArbitrageEngine(self.settings, self.ledger)
        self.triangular_engine = TriangularArbitrageEngine(self.settings, self.ledger)
        self.queue = OpportunityQueue()
        self.executor = ExecutionSimulator(self.settings, self.ledger, self.store, self.risk)

    async def loop(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(self.settings.evaluation_interval_ms / 1000)

    def tick(self) -> None:
        self.scan_tick += 1
        if self.mode == "demo" or self.degraded_demo:
            self.generate_demo_books()

        primary = self.primary_books()
        summaries = self.book_summaries(primary)
        self.risk.evaluate_market(summaries)
        self.flush_risk_events()

        primary_map = {book.exchange_id: book for book in primary}
        opportunities = self.cross_engine.scan(primary_map) + self.triangular_engine.scan(self.books)
        ranked = self.queue.rank(opportunities)
        self.last_scan = ranked
        if ranked:
            self.store.add_opportunities(self.curated_opportunities(ranked))

        self.last_executions = self.executor.try_execute(ranked, summaries)
        for trade in self.last_executions:
            asyncio.create_task(self.redis.publish("trades", trade))
        self.flush_risk_events()

        snapshot = self.snapshot()
        asyncio.create_task(self.redis.publish("snapshots", snapshot))
        self.broadcast(snapshot)

    def curated_opportunities(self, ranked: list[dict]) -> list[dict]:
        current = now_ms()
        profitable = [item for item in ranked if item.get("status") == "profitable"]
        partial = [item for item in ranked if item.get("partial") and item.get("status") != "profitable"]
        demo_mode = self.mode == "demo" or self.degraded_demo
        near_miss_limit = 2 if demo_mode and self.scan_tick % 4 == 0 else 0 if demo_mode else 4
        near_miss = [item for item in ranked if item.get("status") == "rejected" and item.get("netBps", -999) > -12 and not item.get("partial")]
        blocked = [item for item in ranked if item.get("status") == "blocked"]
        target = 7 if demo_mode else 18
        signal_cooldown = 5500 if demo_mode else 1400
        curated: list[dict] = []
        seen: set[str] = set()
        for bucket in (profitable, partial[:3], near_miss[:near_miss_limit], blocked[:1]):
            for item in bucket:
                key = item.get("dedupeKey") or item.get("id")
                if key in seen:
                    continue
                if current - self.recorded_signal_times.get(key, 0) < signal_cooldown:
                    continue
                curated.append(item)
                seen.add(key)
                self.recorded_signal_times[key] = current
                if len(curated) >= target:
                    return curated
        self.recorded_signal_times = {
            key: value for key, value in self.recorded_signal_times.items()
            if current - value < 60000
        }
        return curated

    def flush_risk_events(self) -> None:
        for event in self.risk.drain_events():
            self.store.add_event(event)
            asyncio.create_task(self.redis.publish("risk", event))

    def generate_demo_books(self) -> None:
        self.simulator.advance(self.settings.exchanges)
        for exchange in self.settings.exchanges:
            for symbol in dict.fromkeys((exchange.primary_symbol, *exchange.triangular_symbols)):
                previous = self.books.get(f"{exchange.id}:{symbol}")
                anchor = book_mid(previous) if previous else None
                book = self.simulator.generate(exchange, self.settings.exchanges, symbol, anchor)
                self.books[book.key] = book

    def primary_books(self) -> list[OrderBook]:
        return [book for book in self.books.values() if book.primary and book.asks and book.bids]

    def book_summaries(self, books: list[OrderBook]) -> list[dict]:
        current = now_ms()
        summaries = []
        for book in books:
            ask = best(book.asks, "ask")
            bid = best(book.bids, "bid")
            raw_age = current - book.timestamp
            summaries.append({
                "exchangeId": book.exchange_id,
                "exchangeName": book.exchange_name,
                "symbol": book.symbol,
                "product": book.symbol,
                "source": book.source,
                "status": book.status,
                "bestAsk": ask.price if ask else 0,
                "bestBid": bid.price if bid else 0,
                "spread": ask.price - bid.price if ask and bid else 0,
                "depthAsk": sum(level.qty for level in book.asks),
                "depthBid": sum(level.qty for level in book.bids),
                "feeBps": book.fee_bps,
                "slippageBps": book.slippage_bps,
                "confidence": book.confidence,
                "latencyMs": book.latency_ms,
                "timestamp": book.timestamp,
                "ageMs": max(0, raw_age),
                "clockSkewMs": max(0, -raw_age),
                "error": book.error,
            })
        return summaries

    def snapshot(self) -> dict:
        current = now_ms()
        books = self.book_summaries(self.primary_books())
        triangular_books = [
            {
                "exchangeId": book.exchange_id,
                "exchangeName": book.exchange_name,
                "symbol": book.symbol,
                "source": book.source,
                "timestamp": book.timestamp,
                "ageMs": max(0, current - book.timestamp),
                "clockSkewMs": max(0, book.timestamp - current),
            }
            for book in self.books.values()
            if not book.primary
        ]
        mark_price = sum((book["bestAsk"] + book["bestBid"]) / 2 for book in books) / len(books) if books else 0
        trades = self.store.latest_trades()
        wins = sum(1 for trade in trades if trade["netProfit"] >= 0)
        avg_latency = sum(book["latencyMs"] for book in books) / len(books) if books else 0
        book_ages = sorted(book["ageMs"] for book in books)
        avg_freshness = sum(book_ages) / len(book_ages) if book_ages else 0
        p95_index = min(len(book_ages) - 1, int(len(book_ages) * 0.95)) if book_ages else 0
        p95_freshness = book_ages[p95_index] if book_ages else 0
        latest = self.store.latest_opportunities()
        opportunity_history = self.store.latest_opportunities(120)
        current_scan = self.last_scan or latest[:40]
        executable_edges = [item.get("netBps", 0) for item in current_scan if item.get("status") == "profitable"]
        observed_edges = [item.get("netBps", 0) for item in current_scan if item.get("status") != "blocked"]
        return {
            "now": current,
            "botName": self.settings.app_name,
            "tagline": self.settings.tagline,
            "mode": self.mode,
            "degradedDemo": self.degraded_demo,
            "uptimeMs": current - self.started_at,
            "books": books,
            "triangularBooks": triangular_books,
            "opportunities": latest,
            "opportunityHistory": opportunity_history,
            "queuedOpportunities": self.last_scan[:40],
            "trades": trades,
            "wallets": self.ledger.active(self.settings.exchanges),
            "totals": self.ledger.totals(mark_price, self.settings.exchanges),
            "pnlSeries": self.store.pnl_series,
            "risk": self.risk.snapshot(current),
            "riskEvents": self.store.latest_events(),
            "redis": self.redis.snapshot(),
            "globalMarket": self.global_market.snapshot(),
            "streams": self.stream_provider.snapshot() if self.stream_provider else {"available": False, "unavailableReason": "Demo mode", "streams": []},
            "queue": self.queue.snapshot(),
            "exchangeCoverage": {
                "active": [exchange.__dict__ for exchange in self.settings.exchanges],
                "universe": [exchange.__dict__ for exchange in self.settings.exchange_universe],
                "activeCount": len(self.settings.exchanges),
                "universeCount": len(self.settings.exchange_universe),
                "profile": self.settings.active_exchanges or "all",
            },
            "diagnostics": {
                "blockedMeaning": "Spread exists, but Aurelion skipped it because size, balance, depth, or risk gates were not good enough.",
                "redisMeaning": "Redis is optional Pub/Sub. Disabled means no REDIS_URL is configured; the dashboard still uses SSE.",
                "restFallbackActive": any(book["source"] == "rest" for book in books),
                "latencyMeaning": "Book age is the freshness of the latest order book. Update latency is how long the provider waited for the last exchange update.",
            },
            "metrics": {
                "detectedCount": self.store.detected_count,
                "rejectedCount": self.store.rejected_count,
                "executedCount": self.store.executed_count,
                "simpleCount": self.store.simple_count,
                "triangularCount": self.store.triangular_count,
                "profitableCount": self.store.profitable_count,
                "blockedCount": self.store.blocked_count,
                "partialCount": self.store.partial_count,
                "fullCount": max(0, self.store.executed_count - self.store.partial_count),
                "executedSimpleCount": self.store.executed_simple_count,
                "executedTriangularCount": self.store.executed_triangular_count,
                "cumulativePnl": self.ledger.realized_pnl,
                "winRate": wins / len(trades) if trades else 0,
                "avgLatencyMs": avg_latency,
                "avgFreshnessMs": avg_freshness,
                "p95FreshnessMs": p95_freshness,
                "fastBooks": sum(1 for book in books if book["ageMs"] <= 1000),
                "slowBooks": sum(1 for book in books if book["ageMs"] > 2500),
                "staleBooks": sum(1 for book in books if book["ageMs"] > self.settings.max_book_age_ms),
                "liveBooks": sum(1 for book in books if book["source"] == "websocket"),
                "restBooks": sum(1 for book in books if book["source"] == "rest"),
                "simulatedBooks": sum(1 for book in books if book["source"] == "simulated"),
                "bestNetBps": max([0, *executable_edges]),
                "bestObservedNetBps": max(observed_edges or [0]),
                "nearMissCount": sum(1 for item in current_scan if item.get("status") == "rejected" and item.get("netBps", 0) > -10),
                "partialQueuedCount": sum(1 for item in current_scan if item.get("partial")),
                "historyRetainedCount": len(self.store.opportunities),
                "tradeRetainedCount": len(self.store.trades),
                "opportunityHistoryCapacity": self.store.opportunities_limit,
                "liveSignalCount": sum(1 for item in current_scan if current - item.get("time", 0) <= 1500),
                "maxTradeBtc": self.settings.max_trade_btc,
                "triangularQuoteSize": self.settings.triangular_quote_size,
            },
        }

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def broadcast(self, snapshot: dict) -> None:
        for queue in list(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(snapshot)

    async def event_stream(self) -> AsyncIterator[str]:
        queue = self.subscribe()
        try:
            yield f"event: snapshot\ndata: {json.dumps(self.snapshot())}\n\n"
            while True:
                snapshot = await queue.get()
                yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
        finally:
            self.unsubscribe(queue)


market_service = MarketService()
