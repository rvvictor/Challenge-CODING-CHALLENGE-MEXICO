from __future__ import annotations

import random
import time

from backend.app.core.config import ExchangeConfig
from backend.app.core.models import Level, OrderBook
from backend.app.engines.fills import sort_levels
from backend.app.integrations.historical_data import Candle

MIN_DEPTH_QTY = 0.04


class HistoricalMarket:
    """Replays real OHLCV candles, aligned across exchanges, as order books.

    Real closing prices and real cross-exchange divergence drive the mid price;
    book *depth* is synthesized around each candle (scaled from its traded volume)
    because free, historical level-2 order-book data is not available. This is a
    disclosed simplification — see docs/architecture/RoadmapAndRealWorldPath.md.
    Exposes the same advance()/generate() shape as SimulatedMarket so the backtest
    runner can swap sources without changing its replay loop.
    """

    def __init__(self, exchanges: tuple[ExchangeConfig, ...], candles_by_exchange: dict[str, list[Candle]]):
        self.exchanges_by_id = {exchange.id: exchange for exchange in exchanges}
        self.candles = {key: rows for key, rows in candles_by_exchange.items() if key in self.exchanges_by_id and rows}
        self.random = random.Random(20260112)
        self.index = -1
        self.length = min((len(rows) for rows in self.candles.values()), default=0)

    def covered_exchange_ids(self) -> list[str]:
        return list(self.candles.keys())

    def advance(self, exchanges: tuple[ExchangeConfig, ...]) -> None:
        self.index += 1

    def generate(self, exchange: ExchangeConfig, exchanges: tuple[ExchangeConfig, ...], symbol: str, anchor_mid: float | None = None) -> OrderBook | None:
        rows = self.candles.get(exchange.id)
        if not rows or symbol != exchange.primary_symbol:
            return None
        position = max(0, min(self.index, len(rows) - 1))
        candle = rows[position]
        mid = candle.close
        if mid <= 0:
            return None

        spread_bps = max(1.0, exchange.slippage_bps)
        half_spread = mid * spread_bps / 20000
        base_qty = max(MIN_DEPTH_QTY, (candle.volume / 240.0) if candle.volume else MIN_DEPTH_QTY)

        asks: list[Level] = []
        bids: list[Level] = []
        for i in range(12):
            gap = i * self.random.uniform(1.5, 5.0)
            qty = base_qty * self.random.uniform(0.5, 1.4) * (1 + i / 8)
            asks.append(Level(round(mid + half_spread + gap, 2), round(qty, 6)))
            bids.append(Level(round(mid - half_spread - gap, 2), round(qty * self.random.uniform(0.85, 1.15), 6)))

        now = int(time.time() * 1000)
        return OrderBook(
            key=f"{exchange.id}:{symbol}",
            exchange_id=exchange.id,
            exchange_name=exchange.name,
            symbol=symbol,
            primary=True,
            source="historical",
            status="historical",
            fee_bps=exchange.taker_fee_bps,
            slippage_bps=exchange.slippage_bps,
            confidence=exchange.confidence,
            asks=sort_levels(asks, "ask"),
            bids=sort_levels(bids, "bid"),
            latency_ms=round(self.random.uniform(40, 160)),
            timestamp=now,
        )
