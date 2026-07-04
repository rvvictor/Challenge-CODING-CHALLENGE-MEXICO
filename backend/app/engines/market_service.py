from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections.abc import AsyncIterator

from backend.app.core.config import (
    ASSET_BY_SYMBOL,
    LIVE_ALT_BASES,
    PARAMETER_GROUPS,
    PARAMETER_PRESETS,
    Settings,
    apply_parameter_updates,
    parameter_specs_payload,
    parameter_values,
    select_exchanges,
    settings,
)
from backend.app.core.models import OrderBook
from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine
from backend.app.engines.calibration import SuccessCalibrator
from backend.app.engines.discovery import WideNetRadar
from backend.app.engines.edge_analysis import (
    compact_opportunity_record,
    demo_quality,
    explain_opportunity,
    latency_slo,
    session_summary,
    venue_quality,
)
from backend.app.engines.edge_ledger import EdgeLedger
from backend.app.engines.event_store import EventStore
from backend.app.engines.execution import ExecutionSimulator
from backend.app.engines.feed_guard import FeedGuard
from backend.app.engines.fills import best
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.queue import OpportunityQueue
from backend.app.engines.risk import RiskManager
from backend.app.engines.simulator import SimulatedMarket
from backend.app.engines.triangular import TriangularArbitrageEngine
from backend.app.engines.venue_health import VenueHealthTracker
from backend.app.integrations.ccxt_provider import CcxtStreamProvider
from backend.app.integrations.gateways import GATEWAY_MODES, PaperExecutionGateway, PreTradeGuard, build_gateway
from backend.app.integrations.global_market import GlobalMarketIntel
from backend.app.integrations.llm_narrator import DecisionNarrator
from backend.app.integrations.persistence import DurableEventSink
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
        self.persistence = DurableEventSink(cfg)
        self.store = EventStore(persistence=self.persistence)
        self.ledger = WalletLedger(cfg)
        self.risk = RiskManager(cfg)
        self.calibrator = SuccessCalibrator()
        self.cross_engine = CrossExchangeArbitrageEngine(cfg, self.ledger, self.calibrator)
        self.triangular_engine = TriangularArbitrageEngine(cfg, self.ledger, self.calibrator)
        self.queue = OpportunityQueue()
        self.edge_ledger = EdgeLedger()
        self.executor = ExecutionSimulator(cfg, self.ledger, self.store, self.risk)
        self.redis = RedisBus(cfg)
        self.global_market = GlobalMarketIntel(cfg)
        self.venue_health = VenueHealthTracker(cfg)
        self.narrator = DecisionNarrator(cfg)
        self.pre_trade_guard = PreTradeGuard()
        self.gateway_mode = "paper"
        self.gateway = PaperExecutionGateway(self.pre_trade_guard)
        self.executor.gateway = self.gateway
        self.stream_provider: CcxtStreamProvider | None = None
        self.discovery = WideNetRadar(cfg)
        self.started_at = now_ms()
        self.task: asyncio.Task | None = None
        self.discovery_task: asyncio.Task | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.last_scan: list[dict] = []
        self.last_executions: list[dict] = []
        self.degraded_demo = False
        self.scan_tick = 0
        self.recorded_signal_times: dict[str, int] = {}
        # Baseline snapshot of every tunable parameter, captured at startup so the
        # Control Room "reset" returns to the configured (env-derived) defaults.
        self.default_parameters = parameter_values(cfg)
        # Rolling window of internal decision latency (books-read -> ranked +
        # risk-gated), in ms. Isolated from network/exchange latency so the
        # dashboard can show "how fast does Aurelion itself decide."
        self.decision_latency_window: list[float] = []
        # Per-stage breakdown of the same window: where the milliseconds go
        # (ingest, risk gate, scan, rank, execute, publish), rolling 200 samples.
        self.stage_windows: dict[str, list[float]] = {}
        # Engine watchdog: the loop routes every tick through safe_tick(), so a
        # fault inside tick() is contained instead of killing the engine task.
        self.feed_guard = FeedGuard(cfg)
        self.tick_count = 0
        self.tick_errors = 0
        self.consecutive_tick_errors = 0
        self.last_tick_error = ""
        self.last_tick_error_at = 0
        # Queued deliberate faults (Stress Lab): each request faults exactly one
        # tick, and rapid requests queue CONSECUTIVE faulted ticks — so pressing
        # the button three times fast demonstrates the fail-safe pause live.
        self._fault_ticks = 0
        # Cross-session lineage from the durable store, computed at start() and
        # on demand (never per tick — it is a DB query).
        self._continuity: dict = {"driver": self.persistence.driver, "sessions": [], "priorSessions": 0}

    async def start(self) -> None:
        await self.redis.start()
        await self.global_market.start()
        self.refresh_continuity()
        prior = self._continuity.get("priorSessions", 0)
        if prior:
            self.edge_ledger.append("continuity", {
                "priorSessions": prior,
                "lastSessionFinalPnl": self._continuity.get("lastSessionFinalPnl"),
            })
        if self.mode != "demo":
            await self.start_streams()
        if not self.task:
            self.task = asyncio.create_task(self.loop())
        if not self.discovery_task:
            self.discovery_task = asyncio.create_task(self.discovery_loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
        if self.discovery_task:
            self.discovery_task.cancel()
            self.discovery_task = None
        if self.stream_provider:
            await self.stream_provider.stop()
        await self.global_market.stop()
        self.persistence.close()

    async def start_streams(self) -> None:
        if self.stream_provider:
            return
        self.stream_provider = CcxtStreamProvider(self.settings, self.handle_book, self.handle_provider_event)
        await self.stream_provider.start()
        self.degraded_demo = self.mode == "auto" and not self.stream_provider.available

    def handle_book(self, book: OrderBook) -> None:
        # Live-path sanitizer: a poisoned update (NaN price, garbled crossed
        # book, fat-finger jump) is rejected at the boundary so it can never
        # reach the engines, the mids or the P&L. Demo books are generated
        # internally and do not pass through here.
        reason = self.feed_guard.inspect(book)
        if reason:
            if self.feed_guard.record_rejection(book, reason):
                self.edge_ledger.append("feed-rejected", {
                    "exchange": book.exchange_id,
                    "symbol": book.symbol,
                    "reason": reason,
                })
            return
        self.books[book.key] = book

    async def handle_provider_event(self, event: dict) -> None:
        self.store.add_event(event)
        self.edge_ledger.append("market-event", {
            "type": event.get("type"),
            "exchange": event.get("exchange"),
            "symbol": event.get("symbol"),
            "reason": event.get("reason") or event.get("error"),
        })
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
        # Keep the execution gateway consistent with the data source, so market
        # mode and gateway read as ONE concept to the user: demo <-> paper,
        # auto/live <-> read-only-live (real data, paper fills). The "live"
        # gateway (disabled stub) is only ever selected explicitly.
        if mode == "demo":
            self.set_execution_gateway("paper")
            self.apply_alt_inventory(0)
        elif self.gateway_mode == "paper":
            self.set_execution_gateway("read-only-live")
        # Seed paper alt inventory for live modes so read-only-live can paper-trade
        # the alt universe; clear it when returning to demo (done above).
        if mode != "demo" and self.settings.live_alt_enabled:
            self.schedule(self._seed_alt_inventory_when_ready())

    async def _seed_alt_inventory_when_ready(self) -> None:
        # Wait briefly for the first alt books to arrive so inventory is valued at
        # the live mid rather than the catalog hint, then seed.
        await asyncio.sleep(3.0)
        self.apply_alt_inventory(self.settings.live_alt_seed_usd)

    async def set_execution_gateway_unified(self, mode: str) -> None:
        """Gateway switch with the inverse coupling: choosing a live-data gateway
        from demo also moves the market mode to auto (real data, safe degrade)."""
        self.set_execution_gateway(mode)
        if mode in ("read-only-live", "live") and self.mode == "demo":
            await self.set_mode("auto")

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

    async def trigger_scenario(self, name: str) -> dict:
        """Adversarial Stress Lab: inject a one-click crisis and let the bot react
        (circuit breaker, venue demotion, partial/leg-failure handling)."""
        current = now_ms()
        key = (name or "").strip().lower()
        applied = ""
        if self.mode == "demo" or self.degraded_demo:
            applied = self.simulator.inject_scenario(key, self.settings.exchanges)
        if key in ("flash_crash", "volatility"):
            self.risk.activate(
                "volatility",
                "Stress scenario: flash crash 3.20% in <1s",
                current,
                {"changePct": 3.2, "scenario": "flash_crash", "stressTest": True},
            )
            applied = applied or "flash_crash"
        if key == "leg_failure":
            self.executor.leg_failure_until = current + 20000
            applied = applied or "leg_failure"
        if key == "engine_fault":
            self._fault_ticks += 1
            applied = "engine_fault"
        event = {
            "id": f"SC-{current}",
            "type": "scenario",
            "time": current,
            "condition": key or "unknown",
            "reason": f"Adversarial scenario injected: {key or 'unknown'}",
            "metadata": {"scenario": key, "source": "stress-lab"},
        }
        self.store.add_event(event)
        self.edge_ledger.append("scenario", {"scenario": key, "applied": applied})
        await self.redis.publish("risk", event)
        self.flush_risk_events()
        snapshot = self.snapshot()
        await self.redis.publish("snapshots", snapshot)
        self.broadcast(snapshot)
        return {"applied": applied, "active": snapshot["scenarios"]["active"]}

    def set_auto_execution(self, enabled: bool) -> None:
        self.risk.set_auto_execution(enabled)

    # ---- Execution gateway seam ------------------------------------------------
    def execution_status(self) -> dict:
        return {
            "mode": self.gateway_mode,
            "capabilities": self.gateway.capabilities(),
            "supportsWithdrawal": self.gateway.supports_withdrawal(),
            "guard": self.pre_trade_guard.snapshot(),
            "available": list(GATEWAY_MODES),
            "liveEnabled": getattr(self.gateway, "enabled", False),
        }

    def set_execution_gateway(self, mode: str) -> None:
        if mode in GATEWAY_MODES:
            self.gateway_mode = mode
            self.gateway = build_gateway(mode, self.pre_trade_guard)
            self.executor.gateway = self.gateway

    def set_kill_switch(self, enabled: bool) -> None:
        self.pre_trade_guard.kill_switch = bool(enabled)

    # ---- Runtime parameter control (Control Room) ------------------------------
    def parameters(self) -> dict:
        return {
            "groups": [{"key": key, "label": label} for key, label in PARAMETER_GROUPS],
            "specs": parameter_specs_payload(),
            "values": parameter_values(self.settings),
            "defaults": self.default_parameters,
            "presets": list(PARAMETER_PRESETS.keys()),
        }

    def apply_parameters(self, updates: dict) -> dict:
        result = apply_parameter_updates(self.settings, updates)
        self._on_parameters_changed(result)
        return result

    def apply_preset(self, name: str) -> dict:
        key = (name or "").strip().lower()
        preset = PARAMETER_PRESETS.get(key)
        if not preset:
            return {"applied": {}, "changed": {}, "rejected": [{"key": name, "reason": "unknown preset"}]}
        result = apply_parameter_updates(self.settings, dict(preset))
        result["preset"] = key
        self._on_parameters_changed(result)
        return result

    def reset_parameters(self) -> dict:
        result = apply_parameter_updates(self.settings, dict(self.default_parameters))
        result["reset"] = True
        self._on_parameters_changed(result)
        return result

    def _on_parameters_changed(self, result: dict) -> None:
        # Engines read Settings live each (synchronous) tick, so live edits need no
        # rebuild. Record meaningful changes to the edge ledger for auditability.
        if result.get("changed"):
            self.edge_ledger.append("parameter-change", {"changed": result["changed"]})

    def reset(self) -> None:
        self.store.reset()
        self.edge_ledger.reset()
        self.ledger.reset()
        self.risk.reset()
        self.executor.reset()
        self.venue_health.reset()
        self.calibrator.reset()
        self.simulator.scenarios.clear()
        self.simulator.outage_venue = None
        self.simulator.volatility_stress_until = 0
        self.pre_trade_guard.kill_switch = False
        self.set_execution_gateway("paper")
        self.decision_latency_window = []
        self.stage_windows = {}
        self.feed_guard.reset()
        self.tick_count = 0
        self.tick_errors = 0
        self.consecutive_tick_errors = 0
        self.last_tick_error = ""
        self.last_tick_error_at = 0
        self._fault_ticks = 0
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
            self.venue_health.sync(self.settings.exchanges)
            self.risk.reset_market_window()
        else:
            self.store.reset()
            self.ledger = WalletLedger(self.settings)
            self.risk.reset()
            self.venue_health.reset()
            self.started_at = now_ms()
        self.cross_engine = CrossExchangeArbitrageEngine(self.settings, self.ledger, self.calibrator)
        self.triangular_engine = TriangularArbitrageEngine(self.settings, self.ledger, self.calibrator)
        self.queue = OpportunityQueue()
        self.executor = ExecutionSimulator(self.settings, self.ledger, self.store, self.risk, gateway=self.gateway)

    async def loop(self) -> None:
        while True:
            self.safe_tick()
            await asyncio.sleep(self.settings.evaluation_interval_ms / 1000)

    def safe_tick(self) -> None:
        """Watchdog: the engine cannot die. Any exception inside a tick is
        contained, counted and surfaced as a risk event; repeated consecutive
        faults trip the circuit breaker into a fail-safe pause rather than
        trading on a possibly-broken state. The dashboard keeps updating even
        on a failed tick, so an operator always sees what happened."""
        self.tick_count += 1
        try:
            self.tick()
            self.consecutive_tick_errors = 0
        except Exception as exc:  # noqa: BLE001 - the whole point is to survive anything
            current = now_ms()
            self.tick_errors += 1
            self.consecutive_tick_errors += 1
            self.last_tick_error = f"{type(exc).__name__}: {exc}"
            self.last_tick_error_at = current
            traceback.print_exc()
            event = {
                "id": f"EF-{current}-{self.tick_errors}",
                "type": "engine-error",
                "time": current,
                "condition": "engine-fault",
                "reason": f"Tick contained by watchdog: {self.last_tick_error}",
                "metadata": {"consecutive": self.consecutive_tick_errors, "totalErrors": self.tick_errors},
            }
            self.store.add_event(event)
            self.edge_ledger.append("engine-error", {
                "error": self.last_tick_error,
                "consecutive": self.consecutive_tick_errors,
            })
            if self.consecutive_tick_errors >= 3:
                self.risk.activate(
                    "engine-fault",
                    f"Engine fault x{self.consecutive_tick_errors} ({self.last_tick_error}) — fail-safe pause",
                    current,
                    {"consecutive": self.consecutive_tick_errors, "watchdog": True},
                )
                self.flush_risk_events()
            try:
                snapshot = self.snapshot()
                self.schedule(self.redis.publish("snapshots", snapshot))
                self.broadcast(snapshot)
            except Exception:
                pass

    async def discovery_loop(self) -> None:
        """Wide-net lane: sweeps the FULL exchange universe (incl. XRP/LTC/SOL)
        in a worker thread on its own slow cadence. Fully isolated from tick():
        a slow or failing sweep can never add a millisecond to decision latency."""
        await asyncio.sleep(2.0)
        while True:
            if self.settings.discovery_enabled:
                try:
                    await asyncio.to_thread(self.discovery.sweep)
                except Exception:
                    pass
            await asyncio.sleep(max(5.0, self.settings.discovery_interval_ms / 1000))

    async def sweep_discovery(self) -> dict:
        """Manual 'sweep now' for the dashboard; runs off-thread like the loop."""
        try:
            await asyncio.to_thread(self.discovery.sweep)
        except Exception:
            pass
        return self.discovery.snapshot()

    def _record_stage(self, name: str, started: float) -> float:
        ended = time.perf_counter()
        window = self.stage_windows.setdefault(name, [])
        window.append((ended - started) * 1000)
        if len(window) > 200:
            del window[: len(window) - 200]
        return ended

    def tick(self) -> None:
        # Stress Lab "engine_fault": a deliberate crash inside the hot path so
        # an evaluator can watch the watchdog contain it live.
        if self._fault_ticks > 0:
            self._fault_ticks -= 1
            raise RuntimeError("Injected engine fault (Stress Lab): deliberate tick crash")
        self.scan_tick += 1
        if self.mode == "demo" or self.degraded_demo:
            self.generate_demo_books()
            # Demo realism: periodically (~every 72s) inject a brief leg-failure
            # window through the same Stress Lab mechanism a user can trigger
            # manually, so an unattended live demo session occasionally shows a
            # real reconciled loss instead of a monotonically rising P&L curve.
            if self.scan_tick % 160 == 77:
                self.schedule(self.trigger_scenario("leg_failure"))

        # Decision-latency window starts once books are read: it measures
        # Aurelion's own processing time (scan, score, risk-gate), separate from
        # network/exchange latency already tracked per-book (latencyMs/ageMs).
        decision_started = time.perf_counter()
        stage_mark = decision_started
        primary = self.primary_books()
        summaries = self.book_summaries(primary)
        stream_snapshot = self.stream_provider.snapshot() if self.stream_provider else {"streams": []}
        self.venue_health.sync(self.settings.exchanges)
        self.venue_health.record_books(summaries, stream_snapshot)
        summaries = self.venue_health.enrich_summaries(summaries)
        stage_mark = self._record_stage("ingest", stage_mark)
        self.risk.evaluate_market(summaries)
        self.flush_risk_events()
        risk_snapshot = self.risk.snapshot(now_ms())
        stage_mark = self._record_stage("riskGate", stage_mark)
        if risk_snapshot["paused"]:
            self.last_scan = []
            self.last_executions = []
            self.queue.pause(risk_snapshot["reason"])
            snapshot = self.snapshot()
            self.schedule(self.redis.publish("snapshots", snapshot))
            self.broadcast(snapshot)
            return

        adjusted_primary = self.health_adjusted_books(primary)
        adjusted_books = self.health_adjusted_book_map()
        cross_input = self.cross_scan_input(adjusted_primary, adjusted_books)
        opportunities = self.cross_engine.scan(cross_input) + self.triangular_engine.scan(adjusted_books)
        stage_mark = self._record_stage("scan", stage_mark)
        ranked = [explain_opportunity(item) for item in self.queue.rank(opportunities)]
        decision_latency_ms = (time.perf_counter() - decision_started) * 1000
        self.decision_latency_window.append(decision_latency_ms)
        self.decision_latency_window = self.decision_latency_window[-200:]
        self.last_scan = ranked
        if ranked:
            curated = self.curated_opportunities(ranked)
            self.store.add_opportunities(curated)
            self.record_edge_decisions(curated)
        stage_mark = self._record_stage("rank", stage_mark)

        if self.pre_trade_guard.kill_switch:
            self.last_executions = []
        else:
            # Give the executor the live book map so the gateway can settle
            # (paper: passthrough; testnet: place real sandbox orders).
            self.executor.book_map = adjusted_books
            self.last_executions = self.executor.try_execute(ranked, summaries)
        for trade in self.last_executions:
            self._record_calibration(trade)
            self.edge_ledger.append("trade", self.compact_trade_record(trade))
            self.schedule(self.redis.publish("trades", trade))
        self.flush_risk_events()
        stage_mark = self._record_stage("execute", stage_mark)

        snapshot = self.snapshot()
        self.schedule(self.redis.publish("snapshots", snapshot))
        self.broadcast(snapshot)
        self._record_stage("publish", stage_mark)

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

    def record_edge_decisions(self, opportunities: list[dict]) -> None:
        for opportunity in opportunities:
            self.edge_ledger.append("opportunity", compact_opportunity_record(opportunity))

    def _record_calibration(self, trade: dict) -> None:
        success = (not trade.get("partial")) and float(trade.get("netProfit", 0)) >= 0
        if trade.get("strategy") == "triangular":
            self.calibrator.update(trade.get("exchangeId", ""), success)
        else:
            self.calibrator.update(trade.get("buyExchangeId", ""), success)
            self.calibrator.update(trade.get("sellExchangeId", ""), success)

    def run_backtest(self, ticks: int = 250, regime: str = "normal", source: str = "simulated") -> dict:
        from backend.app.engines.backtest import BacktestRunner

        return BacktestRunner(self.settings).run(ticks, regime, source)

    # ---- Research & Training Lab (off the hot loop, called via to_thread) ------
    def run_spread_study(self, timeframe: str = "1m", limit: int = 300) -> dict:
        from backend.app.engines.spread_model import SpreadDynamicsLab
        from backend.app.integrations.research_store import save_research

        study = SpreadDynamicsLab(self.settings).study(timeframe, limit)
        save_research("spread-study", study)
        self.edge_ledger.append("research", {
            "kind": "spread-study",
            "pairsFitted": study.get("pairsFitted"),
            "medianHalfLifeMs": (study.get("summary") or {}).get("medianHalfLifeMs"),
            "executableEpisodes": (study.get("summary") or {}).get("executableEpisodes"),
        })
        return study

    def run_autotune(self, trials: int = 24, ticks: int = 220, regime: str = "normal", source: str = "simulated", seed: int = 7, robust: bool = False) -> dict:
        from backend.app.engines.autotune import ParameterTrainer
        from backend.app.integrations.research_store import save_research

        result = ParameterTrainer(self.settings).train(trials, ticks, regime, source, seed, robust)
        save_research("autotune", result)
        best = result.get("best") or {}
        self.edge_ledger.append("research", {
            "kind": "autotune",
            "trials": result.get("trials"),
            "regime": result.get("regime"),
            "robust": result.get("robust"),
            "source": result.get("source"),
            "baselineScore": (result.get("baseline") or {}).get("score"),
            "bestScore": best.get("score"),
            "bestValidationScore": best.get("validationScore"),
            "improved": result.get("improvedVsBaseline"),
        })
        return result

    def research_history(self, limit: int = 12) -> dict:
        from backend.app.integrations.research_store import load_research

        return {"sessions": load_research(limit)}

    def judge_report_html(self) -> str:
        from backend.app.engines.report import build_report_html
        from backend.app.integrations.research_store import load_research

        return build_report_html(self.snapshot(), load_research(8))

    def narrate(self, question: str | None = None, model: str | None = None, trade_id: str | None = None) -> dict:
        return self.narrator.narrate(self.snapshot(), question, model, trade_id)

    def narrate_stream(self, question: str | None = None, model: str | None = None, trade_id: str | None = None):
        return self.narrator.stream_async(self.snapshot(), question, model, trade_id)

    def refresh_continuity(self) -> dict:
        """Recompute cross-session lineage from the durable store. A restart no
        longer erases the audit trail: previous sessions stay readable."""
        sessions = self.persistence.session_lineage(6)
        prior = [session for session in sessions if not session.get("current")]
        # Headline = most recent prior session that actually traded (short-lived
        # tooling/test sessions leave 1-event rows that would read as noise).
        informative = next((session for session in prior if (session.get("trades") or 0) > 0), None)
        self._continuity = {
            "driver": self.persistence.driver,
            "status": self.persistence.status,
            "sessions": sessions,
            "priorSessions": len(prior),
            "lastSessionFinalPnl": informative.get("finalPnl") if informative else None,
            "lastSessionTrades": informative.get("trades") if informative else None,
        }
        return self._continuity

    def replay_feed(self, limit: int = 120) -> dict:
        durable = self.persistence.read(limit=limit)
        if durable:
            return {
                "source": f"durable-{self.persistence.driver}",
                "eventCount": self.persistence.count(),
                "events": durable,
            }
        return {
            "source": "edge-ledger-memory",
            "eventCount": len(self.edge_ledger.records),
            "events": self.edge_ledger.latest(limit),
        }

    def compact_trade_record(self, trade: dict) -> dict:
        if trade.get("strategy") == "triangular":
            route = f"{trade.get('exchange')} / {' -> '.join(trade.get('cyclePath') or [])}"
        else:
            route = f"{trade.get('buyExchange')} -> {trade.get('sellExchange')}"
        return {
            "id": trade.get("id"),
            "route": route,
            "strategy": trade.get("strategy"),
            "status": trade.get("status"),
            "partial": trade.get("partial"),
            "filledRatio": trade.get("filledRatio"),
            "netProfit": trade.get("netProfit"),
            "netBps": trade.get("netBps"),
            "executionQuality": trade.get("executionQuality"),
            "inventoryRebalance": bool(trade.get("inventoryRebalance")),
        }

    def flush_risk_events(self) -> None:
        for event in self.risk.drain_events():
            self.store.add_event(event)
            self.edge_ledger.append("risk-event", {
                "type": event.get("type"),
                "condition": event.get("condition"),
                "reason": event.get("reason"),
                "metadata": event.get("metadata", {}),
            })
            self.schedule(self.redis.publish("risk", event))

    def schedule(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        loop.create_task(coro)

    def generate_demo_books(self) -> None:
        self.simulator.advance(self.settings.exchanges)
        for exchange in self.settings.exchanges:
            for symbol in self.demo_symbols(exchange):
                previous = self.books.get(f"{exchange.id}:{symbol}")
                anchor = book_mid(previous) if previous else None
                book = self.simulator.generate(exchange, self.settings.exchanges, symbol, anchor)
                self.books[book.key] = book

    def demo_symbols(self, exchange) -> tuple[str, ...]:
        quote = "USD" if exchange.primary_symbol.endswith("/USD") else "USDT"
        dynamic_symbols = ("SOL/ETH", f"SOL/{quote}")
        return tuple(dict.fromkeys((exchange.primary_symbol, *exchange.triangular_symbols, *dynamic_symbols)))

    def primary_books(self) -> list[OrderBook]:
        return [book for book in self.books.values() if book.primary and book.asks and book.bids]

    def cross_scan_input(self, adjusted_primary: list[OrderBook], adjusted_books: dict[str, OrderBook]) -> dict[str, OrderBook]:
        """Books handed to the cross-exchange engine. Demo feeds only the BTC
        primaries (behavior unchanged). Live/auto also feeds the direct alt pairs
        (XRP/LTC/SOL/AVAX vs USDT/USD) so the engine trades where edges exist."""
        result: dict[str, OrderBook] = {book.exchange_id: book for book in adjusted_primary}
        if self.mode == "demo" or self.degraded_demo or not self.settings.live_alt_enabled:
            return result
        for key, book in adjusted_books.items():
            if book.primary:
                continue
            parts = book.symbol.split("/", 1)
            if len(parts) == 2 and parts[1] in ("USDT", "USD") and parts[0] in LIVE_ALT_BASES:
                result[key] = book
        return result

    def live_alt_prices(self) -> dict[str, float]:
        """Mid price per alt base from the live books, for inventory valuation."""
        prices: dict[str, float] = {}
        for book in self.books.values():
            parts = book.symbol.split("/", 1)
            if len(parts) == 2 and parts[0] in LIVE_ALT_BASES and parts[1] in ("USDT", "USD"):
                mid = book_mid(book)
                if mid:
                    prices[parts[0]] = mid
        return prices

    def apply_alt_inventory(self, seed_usd: float) -> None:
        """Seed (or clear) paper alt inventory so the read-only-live path can
        paper-trade alts. Sets both the wallet balances and the starting
        reference (starting_alt_balances) so mark-to-market stays coherent —
        seeded inventory is not phantom P&L. seed_usd=0 clears it (demo)."""
        prices = self.live_alt_prices()
        seeds: dict[str, float] = {}
        for asset in LIVE_ALT_BASES:
            spec = ASSET_BY_SYMBOL.get(asset)
            price = prices.get(asset) or (spec.price_hint if spec else 0.0)
            seeds[asset] = round(seed_usd / price, spec.precision if spec else 4) if (seed_usd and price > 0) else 0.0
        self.settings.starting_alt_balances = {k: v for k, v in seeds.items() if v} if seed_usd else {}
        for wallet in self.ledger.balances.values():
            for asset, qty in seeds.items():
                wallet[asset] = qty

    def health_adjusted_books(self, books: list[OrderBook]) -> list[OrderBook]:
        adjusted = []
        for book in books:
            factor = self.venue_health.confidence_factor(book.exchange_id)
            status = self.venue_health.status(book.exchange_id)
            adjusted.append(book.clone_with(
                confidence=max(0.05, book.confidence * factor),
                status=status if status != "healthy" else book.status,
            ))
        return adjusted

    def health_adjusted_book_map(self) -> dict[str, OrderBook]:
        return {key: self.health_adjusted_books([book])[0] for key, book in self.books.items()}

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
        stream_snapshot = self.stream_provider.snapshot() if self.stream_provider else {"available": False, "unavailableReason": "Demo mode", "streams": []}
        books = self.venue_health.enrich_summaries(self.book_summaries(self.primary_books()))
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
        eth_mark_price = self.eth_mark_price(mark_price)
        trades = self.store.latest_trades(self.store.trades_limit)
        wins = sum(1 for trade in trades if trade["netProfit"] >= 0)
        avg_latency = sum(book["latencyMs"] for book in books) / len(books) if books else 0
        book_ages = sorted(book["ageMs"] for book in books)
        avg_freshness = sum(book_ages) / len(book_ages) if book_ages else 0
        p95_index = min(len(book_ages) - 1, int(len(book_ages) * 0.95)) if book_ages else 0
        p95_freshness = book_ages[p95_index] if book_ages else 0
        latest = self.store.latest_opportunities()
        opportunity_history = self.store.latest_opportunities(120)
        risk_snapshot = self.risk.snapshot(current)
        current_scan = [] if risk_snapshot["paused"] else self.last_scan or latest[:40]
        executable_edges = [item.get("netBps", 0) for item in current_scan if item.get("status") == "profitable"]
        observed_edges = [item.get("netBps", 0) for item in current_scan if item.get("status") != "blocked"]
        metrics = {
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
            "riskBudgetUsedUsd": risk_snapshot["riskBudgetUsedUsd"],
            "riskBudgetRemainingUsd": risk_snapshot["riskBudgetRemainingUsd"],
            "demotedVenues": self.venue_health.snapshot()["demotedCount"],
        }
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
            "totals": self.ledger.totals(mark_price, self.settings.exchanges, eth_mark_price),
            "pnlSeries": self.store.pnl_series,
            "risk": risk_snapshot,
            "riskEvents": self.store.latest_events(),
            "redis": self.redis.snapshot(),
            "database": self.persistence.snapshot(),
            "globalMarket": self.global_market.snapshot(),
            "streams": stream_snapshot,
            "queue": self.queue.snapshot(),
            "venueHealth": self.venue_health.snapshot(),
            "exchangeCoverage": {
                "active": [exchange.__dict__ for exchange in self.settings.exchanges],
                "universe": [exchange.__dict__ for exchange in self.settings.exchange_universe],
                "activeCount": len(self.settings.exchanges),
                "universeCount": len(self.settings.exchange_universe),
                "profile": self.settings.active_exchanges or "all",
            },
            "latencySlo": latency_slo(books, self.decision_latency_window, self.stage_windows),
            "venueQuality": venue_quality(books),
            "demoQuality": demo_quality(self.mode, metrics, current - self.started_at, risk_snapshot),
            "edgeLedger": self.edge_ledger.snapshot(),
            "replay": {
                "source": "edge-ledger-jsonl",
                "eventCount": len(self.edge_ledger.records),
                "events": self.edge_ledger.latest(90),
            },
            "diagnostics": {
                "blockedMeaning": "Spread exists, but Aurelion skipped it because size, balance, depth, or risk gates were not good enough.",
                "redisMeaning": "Redis is optional Pub/Sub. Disabled means no REDIS_URL is configured; the dashboard still uses SSE.",
                "restFallbackActive": any(book["source"] == "rest" for book in books),
                "latencyMeaning": "Book age is the freshness of the latest order book. Update latency is how long the provider waited for the last exchange update.",
            },
            "calibration": self.calibrator.snapshot(),
            "scenarios": {
                "active": self.simulator.active_scenarios() if (self.mode == "demo" or self.degraded_demo) else [],
                "available": [*self.simulator.SCENARIOS, "engine_fault"],
                "legFailureActive": current < self.executor.leg_failure_until,
            },
            "engineHealth": {
                "watchdog": "armed",
                "tickCount": self.tick_count,
                "tickErrors": self.tick_errors,
                "consecutiveTickErrors": self.consecutive_tick_errors,
                "lastTickError": self.last_tick_error or None,
                "lastTickErrorAt": self.last_tick_error_at or None,
                "feedGuard": self.feed_guard.snapshot(),
            },
            "inventoryAutonomy": self.ledger.inventory_autonomy(self.settings.exchanges, mark_price),
            "continuity": self._continuity,
            "discovery": self.discovery.snapshot(),
            "models": {
                "cycleAlgo": self.settings.cycle_algo,
                "slippageModel": self.settings.slippage_model,
                "marketImpactK": self.settings.market_impact_k,
                "sizingMode": self.settings.sizing_mode,
                "kellyFraction": self.settings.kelly_fraction,
                "volatilityModel": self.settings.volatility_model,
                "calibrationEnabled": self.settings.calibration_enabled,
            },
            "coPilot": {
                "available": self.narrator.available(),
                "model": self.narrator.model if self.narrator.available() else None,
                "models": list(self.narrator.allowed_models) if self.narrator.available() else [],
            },
            "execution": self.execution_status(),
            "metrics": metrics,
        }

    def eth_mark_price(self, fallback_btc_price: float) -> float:
        prices = []
        for book in self.books.values():
            if book.symbol in {"ETH/USDT", "ETH/USD"} and book.asks and book.bids:
                mid = book_mid(book)
                if mid:
                    prices.append(mid)
        if prices:
            return sum(prices) / len(prices)
        return fallback_btc_price * 0.052 if fallback_btc_price else 0

    def metrics_snapshot(self) -> dict:
        snapshot = self.snapshot()
        streams = snapshot["streams"].get("streams", [])
        return {
            "botName": snapshot["botName"],
            "mode": snapshot["mode"],
            "uptimeMs": snapshot["uptimeMs"],
            "metrics": snapshot["metrics"],
            "risk": snapshot["risk"],
            "latencySlo": snapshot["latencySlo"],
            "venueHealth": snapshot["venueHealth"],
            "database": snapshot["database"],
            "streams": {
                "available": snapshot["streams"].get("available"),
                "restFallbackCount": sum(1 for stream in streams if stream.get("restFallback")),
                "disabledCount": sum(1 for stream in streams if stream.get("disabled")),
            },
        }

    def export_session(self) -> dict:
        current = now_ms()
        snapshot = self.snapshot()
        opportunities = self.store.latest_opportunities(self.store.opportunities_limit)
        trades = self.store.latest_trades(self.store.trades_limit)
        events = self.store.latest_events(self.store.event_limit)
        return {
            "generatedAt": current,
            "botName": self.settings.app_name,
            "tagline": self.settings.tagline,
            "mode": self.mode,
            "uptimeMs": current - self.started_at,
            "summary": session_summary(opportunities, trades, events),
            "metrics": snapshot["metrics"],
            "latencySlo": snapshot["latencySlo"],
            "demoQuality": snapshot["demoQuality"],
            "venueQuality": snapshot["venueQuality"],
            "risk": snapshot["risk"],
            "opportunities": opportunities,
            "trades": trades,
            "pnlSeries": self.store.pnl_series,
            "riskEvents": events,
            "edgeLedger": self.edge_ledger.latest(self.edge_ledger.memory_limit),
            "config": {
                "exchanges": [exchange.__dict__ for exchange in self.settings.exchanges],
                "exchangeUniverse": [exchange.__dict__ for exchange in self.settings.exchange_universe],
                "evaluationIntervalMs": self.settings.evaluation_interval_ms,
                "maxTradeBtc": self.settings.max_trade_btc,
                "triangularQuoteSize": self.settings.triangular_quote_size,
                "wsFailureThreshold": self.settings.ws_failure_threshold,
                "restFallbackMs": self.settings.poll_interval_ms,
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
