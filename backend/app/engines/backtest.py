from __future__ import annotations

import dataclasses
import math
import random
import time

from backend.app.core.config import Settings
from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine
from backend.app.engines.event_store import EventStore
from backend.app.engines.execution import ExecutionSimulator
from backend.app.engines.fills import best
from backend.app.engines.historical_replay import HistoricalMarket
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.queue import OpportunityQueue
from backend.app.engines.risk import RiskManager
from backend.app.engines.simulator import SimulatedMarket
from backend.app.engines.triangular import TriangularArbitrageEngine
from backend.app.integrations.historical_data import fetch_multi_exchange_history

SOURCES = ("simulated", "historical")


def now_ms() -> int:
    return int(time.time() * 1000)


def _book_mid(book) -> float | None:
    ask = best(book.asks, "ask")
    bid = best(book.bids, "bid")
    return (ask.price + bid.price) / 2 if ask and bid else None


# Market regimes for the backtest. `drag`/`vol` parameterize a realized-execution
# model (the gap between the detected edge and the actually-realized P&L); `inject`
# schedules adverse scenarios so the engine itself produces pauses, partials and
# thinner books. `calm` reproduces the original best-case demo behavior.
REGIMES: dict[str, dict] = {
    "calm": {"drag": 0.0, "vol": 0.12, "inject": {}},
    "normal": {"drag": 0.22, "vol": 0.75, "inject": {"latency_spike": 60}},
    "volatile": {"drag": 0.45, "vol": 1.30, "inject": {"flash_crash": 30, "latency_spike": 45}},
    "stressed": {"drag": 0.85, "vol": 1.75, "inject": {"liquidity_crunch": 22, "latency_spike": 30, "venue_outage": 48}},
}


class BacktestRunner:
    """Event-driven replay of the deterministic market through the *same* engines,
    on an isolated copy of the current (tuned) parameters. A selectable market
    regime injects adverse conditions and a realized-execution model, so the
    reported hit rate, drawdown and Sharpe-like ratio are credible rather than the
    best-case demo cadence. Fully isolated from the live session."""

    def __init__(self, settings: Settings, historical_provider=None):
        self.settings = dataclasses.replace(settings, demo_min_execution_gap_ms=0, pair_cooldown_ms=0)
        # Injectable so tests can supply synthetic candles instead of hitting
        # real exchange APIs; defaults to the real fetcher.
        self._historical_provider = historical_provider or fetch_multi_exchange_history

    def _symbols(self, exchange, source: str) -> tuple[str, ...]:
        if source == "historical":
            # Real-history mode covers cross-exchange BTC plus the triangular legs
            # (ETH/BTC, ETH/quote) with real fetched cross-rates. SOL dynamic legs
            # are excluded to keep the fetch fast; missing series degrade per-book.
            from backend.app.integrations.historical_data import history_symbols

            return history_symbols(exchange)
        quote = "USD" if exchange.primary_symbol.endswith("/USD") else "USDT"
        dynamic = ("SOL/ETH", f"SOL/{quote}")
        return tuple(dict.fromkeys((exchange.primary_symbol, *exchange.triangular_symbols, *dynamic)))

    def _summaries(self, books: dict) -> list[dict]:
        current = now_ms()
        summaries = []
        for book in books.values():
            if not book.primary:
                continue
            ask = best(book.asks, "ask")
            bid = best(book.bids, "bid")
            summaries.append({
                "exchangeName": book.exchange_name,
                "timestamp": book.timestamp or current,
                "bestAsk": ask.price if ask else 0,
                "bestBid": bid.price if bid else 0,
            })
        return summaries

    def _realized_pnl(self, detected_pnl: float, regime: dict, rng: random.Random) -> float:
        """Realized-execution model: detected edge degraded by a regime drag plus
        edge-scaled Gaussian noise. Reproducible per regime, can go negative."""
        multiplier = max(-3.0, rng.gauss(1.0 - regime["drag"], regime["vol"]))
        return round(detected_pnl * multiplier, 4)

    def run(self, ticks: int = 250, regime: str = "normal", source: str = "simulated", market_seed: int = 0) -> dict:
        # market_seed=0 preserves the historical default replay exactly; any other
        # value produces an independent market realization (out-of-sample pass).
        settings = self.settings
        regime_key = regime if regime in REGIMES else "normal"
        regime_params = REGIMES[regime_key]
        rng = random.Random(91237 + sum(ord(char) for char in regime_key) + int(market_seed))
        ticks = max(1, min(int(ticks or 0), 5000))

        requested_source = source if source in SOURCES else "simulated"
        data_quality: dict = {"requested": requested_source}
        if requested_source == "historical":
            fetched = self._historical_provider(settings.exchanges, "1m", max(60, min(ticks, 500)))
            data_quality["statuses"] = fetched.get("statuses", {})
            market = HistoricalMarket(settings.exchanges, fetched.get("candles", {}), seed=20260112 + int(market_seed))
            if market.length < 5 or len(market.covered_exchange_ids()) < 2:
                # Not enough real coverage (offline, rate-limited, geo-blocked) —
                # degrade to the simulator rather than fail the request.
                market = SimulatedMarket(settings.exchanges, seed=71021 + int(market_seed))
                actual_source = "simulated-fallback"
            else:
                ticks = min(ticks, market.length)
                actual_source = "historical"
                data_quality["exchanges"] = market.covered_exchange_ids()
                data_quality["series"] = market.covered_series_count()
        else:
            market = SimulatedMarket(settings.exchanges, seed=71021 + int(market_seed))
            actual_source = "simulated"
        data_quality["actual"] = actual_source

        ledger = WalletLedger(settings)
        risk = RiskManager(settings)
        store = EventStore()
        queue = OpportunityQueue()
        cross = CrossExchangeArbitrageEngine(settings, ledger)
        triangular = TriangularArbitrageEngine(settings, ledger)
        executor = ExecutionSimulator(settings, ledger, store, risk)
        books: dict = {}

        equity: list[float] = []
        trade_pnls: list[float] = []
        realized_total = 0.0
        detected = 0
        executed = 0
        wins = 0
        paused_ticks = 0
        best_net_bps_seen = -999.0
        near_miss_count = 0

        symbol_source = "historical" if actual_source == "historical" else "simulated"
        for index in range(ticks):
            if hasattr(market, "inject_scenario"):
                for scenario, cadence in regime_params["inject"].items():
                    if cadence and index % cadence == cadence - 1:
                        market.inject_scenario(scenario, settings.exchanges)

            market.advance(settings.exchanges)
            for exchange in settings.exchanges:
                for symbol in self._symbols(exchange, symbol_source):
                    previous = books.get(f"{exchange.id}:{symbol}")
                    anchor = _book_mid(previous) if previous else None
                    book = market.generate(exchange, settings.exchanges, symbol, anchor)
                    if book is None:
                        continue
                    books[book.key] = book

            summaries = self._summaries(books)
            risk.evaluate_market(summaries)
            if risk.snapshot(now_ms())["paused"]:
                paused_ticks += 1
                equity.append(round(realized_total, 4))
                continue

            primary = {book.exchange_id: book for book in books.values() if book.primary and book.asks and book.bids}
            opportunities = cross.scan(primary) + triangular.scan(books)
            ranked = queue.rank(opportunities)
            detected += sum(1 for item in ranked if item.get("status") in ("profitable", "rejected", "blocked"))
            for item in ranked:
                net_bps = item.get("netBps")
                if net_bps is not None:
                    best_net_bps_seen = max(best_net_bps_seen, net_bps)
                    if item.get("status") == "rejected" and net_bps > -10:
                        near_miss_count += 1
            for trade in executor.try_execute(ranked, summaries):
                executed += 1
                realized = self._realized_pnl(float(trade["netProfit"]), regime_params, rng)
                trade_pnls.append(realized)
                realized_total += realized
                if realized >= 0:
                    wins += 1
            equity.append(round(realized_total, 4))

        return self._metrics(
            equity, trade_pnls, detected, executed, wins, ticks, paused_ticks, regime_key, data_quality,
            best_net_bps_seen, near_miss_count,
        )

    def _metrics(self, equity, trade_pnls, detected, executed, wins, ticks, paused_ticks, regime, data_quality, best_net_bps_seen, near_miss_count) -> dict:
        count = len(trade_pnls)
        total_pnl = round(sum(trade_pnls), 4)

        peak = float("-inf")
        max_drawdown = 0.0
        for value in equity:
            peak = max(peak, value)
            max_drawdown = max(max_drawdown, peak - value)

        if count > 1:
            mean = sum(trade_pnls) / count
            variance = sum((value - mean) ** 2 for value in trade_pnls) / (count - 1)
            std = math.sqrt(variance)
            sharpe_like = round((mean / std) * math.sqrt(count), 3) if std > 0 else 0.0
        else:
            sharpe_like = 0.0

        return {
            "ticks": ticks,
            "regime": regime,
            "regimes": list(REGIMES.keys()),
            "sources": list(SOURCES),
            "dataQuality": data_quality,
            "pausedTicks": paused_ticks,
            "bestObservedNetBps": round(best_net_bps_seen, 3) if best_net_bps_seen > -999 else None,
            "nearMissCount": near_miss_count,
            "detected": detected,
            "executed": executed,
            "wins": wins,
            "losses": count - wins,
            "hitRate": round(wins / count, 4) if count else 0.0,
            "totalPnl": total_pnl,
            "avgPnlPerTrade": round(total_pnl / count, 4) if count else 0.0,
            "maxDrawdown": round(max_drawdown, 4),
            "sharpeLike": sharpe_like,
            "finalEquity": equity[-1] if equity else 0.0,
            "equityCurve": [{"t": position, "pnl": value} for position, value in enumerate(equity[-240:])],
            "params": {
                "minNetBps": self.settings.min_net_bps,
                "maxTradeBtc": self.settings.max_trade_btc,
                "cycleAlgo": self.settings.cycle_algo,
                "slippageModel": self.settings.slippage_model,
                "sizingMode": self.settings.sizing_mode,
                "volatilityModel": self.settings.volatility_model,
            },
        }
