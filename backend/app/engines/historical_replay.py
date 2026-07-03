from __future__ import annotations

import random
import time

from backend.app.core.config import ExchangeConfig
from backend.app.core.models import Level, OrderBook
from backend.app.engines.fills import sort_levels
from backend.app.integrations.historical_data import Candle

MIN_DEPTH_QTY = 0.04


class HistoricalMarket:
    """Replays real OHLCV candles, aligned across exchanges and symbols, as
    order books — now including the triangular legs (ETH/BTC, ETH/quote), so the
    triangular engine can scan real cross-rate history, not just cross-exchange
    BTC divergence.

    Real closing prices drive the mid of every book; book *depth* is synthesized
    around each candle (scaled from its traded volume) because free historical
    level-2 data is not available — a disclosed simplification. All book geometry
    (spread and level gaps) is expressed relative to the mid price, so an ETH/BTC
    book near 0.05 is as well-formed as a BTC/USDT book near 60,000. Exposes the
    same advance()/generate() shape as SimulatedMarket so the backtest runner can
    swap sources without changing its replay loop.
    """

    def __init__(self, exchanges: tuple[ExchangeConfig, ...], candles_by_key: dict[str, list[Candle]]):
        self.exchanges_by_id = {exchange.id: exchange for exchange in exchanges}
        self.candles = {
            key: rows for key, rows in candles_by_key.items()
            if rows and key.split(":", 1)[0] in self.exchanges_by_id
        }
        self.random = random.Random(20260112)
        self.index = -1
        self.length = min((len(rows) for rows in self.candles.values()), default=0)

    def covered_exchange_ids(self) -> list[str]:
        """Exchanges whose PRIMARY symbol has real history (the minimum needed
        for the cross-exchange scan)."""
        covered = []
        for exchange_id, exchange in self.exchanges_by_id.items():
            if f"{exchange_id}:{exchange.primary_symbol}" in self.candles:
                covered.append(exchange_id)
        return covered

    def covered_series_count(self) -> int:
        return len(self.candles)

    def advance(self, exchanges: tuple[ExchangeConfig, ...]) -> None:
        self.index += 1

    def generate(self, exchange: ExchangeConfig, exchanges: tuple[ExchangeConfig, ...], symbol: str, anchor_mid: float | None = None) -> OrderBook | None:
        rows = self.candles.get(f"{exchange.id}:{symbol}")
        if not rows:
            return None
        position = max(0, min(self.index, len(rows) - 1))
        candle = rows[position]
        mid = candle.close
        if mid <= 0:
            return None

        spread_bps = max(1.0, exchange.slippage_bps)
        half_spread = mid * spread_bps / 20000
        base_qty = max(MIN_DEPTH_QTY, (candle.volume / 240.0) if candle.volume else MIN_DEPTH_QTY)
        # Sub-dollar pairs (ETH/BTC ~0.05) need more precision than USD pairs.
        decimals = 8 if mid < 10 else 2

        asks: list[Level] = []
        bids: list[Level] = []
        for i in range(12):
            # Level gaps in relative terms: 0.2-0.8 bps of mid per level, so the
            # ladder is meaningful at any price scale.
            gap = mid * i * self.random.uniform(0.2, 0.8) / 10000
            qty = base_qty * self.random.uniform(0.5, 1.4) * (1 + i / 8)
            asks.append(Level(round(mid + half_spread + gap, decimals), round(qty, 6)))
            bids.append(Level(round(mid - half_spread - gap, decimals), round(qty * self.random.uniform(0.85, 1.15), 6)))

        now = int(time.time() * 1000)
        return OrderBook(
            key=f"{exchange.id}:{symbol}",
            exchange_id=exchange.id,
            exchange_name=exchange.name,
            symbol=symbol,
            primary=symbol == exchange.primary_symbol,
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
