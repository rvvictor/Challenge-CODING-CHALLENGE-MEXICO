from __future__ import annotations

import dataclasses
import math
import time

from backend.app.core.config import Settings
from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine
from backend.app.engines.event_store import EventStore
from backend.app.engines.execution import ExecutionSimulator
from backend.app.engines.fills import best
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.queue import OpportunityQueue
from backend.app.engines.risk import RiskManager
from backend.app.engines.simulator import SimulatedMarket
from backend.app.engines.triangular import TriangularArbitrageEngine


def now_ms() -> int:
    return int(time.time() * 1000)


def _book_mid(book) -> float | None:
    ask = best(book.asks, "ask")
    bid = best(book.bids, "bid")
    return (ask.price + bid.price) / 2 if ask and bid else None


class BacktestRunner:
    """Event-driven replay of the deterministic market through the *same* engines,
    using a snapshot of the current parameters. Fully isolated from the live
    session (its own ledger/risk/engines), so it answers "how would the strategy
    I've tuned have performed?" without touching live state.
    """

    def __init__(self, settings: Settings):
        # Copy the live parameters but disable wall-clock throttles, since a
        # backtest advances many ticks within the same millisecond.
        self.settings = dataclasses.replace(settings, demo_min_execution_gap_ms=0, pair_cooldown_ms=0)

    def _symbols(self, exchange) -> tuple[str, ...]:
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

    def run(self, ticks: int = 250) -> dict:
        settings = self.settings
        ledger = WalletLedger(settings)
        risk = RiskManager(settings)
        store = EventStore()
        queue = OpportunityQueue()
        cross = CrossExchangeArbitrageEngine(settings, ledger)
        triangular = TriangularArbitrageEngine(settings, ledger)
        executor = ExecutionSimulator(settings, ledger, store, risk)
        simulator = SimulatedMarket(settings.exchanges)
        books: dict = {}

        ticks = max(1, min(int(ticks or 0), 5000))
        equity: list[float] = []
        trade_pnls: list[float] = []
        detected = 0
        executed = 0
        wins = 0
        paused_ticks = 0

        for _ in range(ticks):
            simulator.advance(settings.exchanges)
            for exchange in settings.exchanges:
                for symbol in self._symbols(exchange):
                    previous = books.get(f"{exchange.id}:{symbol}")
                    anchor = _book_mid(previous) if previous else None
                    book = simulator.generate(exchange, settings.exchanges, symbol, anchor)
                    books[book.key] = book

            summaries = self._summaries(books)
            risk.evaluate_market(summaries)
            risk_state = risk.snapshot(now_ms())
            if risk_state["paused"]:
                paused_ticks += 1
                equity.append(round(ledger.realized_pnl, 4))
                continue

            primary = {book.exchange_id: book for book in books.values() if book.primary and book.asks and book.bids}
            opportunities = cross.scan(primary) + triangular.scan(books)
            ranked = queue.rank(opportunities)
            detected += sum(1 for item in ranked if item.get("status") in ("profitable", "rejected", "blocked"))
            trades = executor.try_execute(ranked, summaries)
            for trade in trades:
                executed += 1
                pnl = float(trade["netProfit"])
                trade_pnls.append(pnl)
                if pnl >= 0:
                    wins += 1
            equity.append(round(ledger.realized_pnl, 4))

        return self._metrics(equity, trade_pnls, detected, executed, wins, ticks, paused_ticks)

    def _metrics(self, equity, trade_pnls, detected, executed, wins, ticks, paused_ticks) -> dict:
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
            "pausedTicks": paused_ticks,
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
            "equityCurve": [{"t": index, "pnl": value} for index, value in enumerate(equity[-240:])],
            "params": {
                "minNetBps": self.settings.min_net_bps,
                "maxTradeBtc": self.settings.max_trade_btc,
                "cycleAlgo": self.settings.cycle_algo,
                "slippageModel": self.settings.slippage_model,
                "sizingMode": self.settings.sizing_mode,
                "volatilityModel": self.settings.volatility_model,
            },
        }
