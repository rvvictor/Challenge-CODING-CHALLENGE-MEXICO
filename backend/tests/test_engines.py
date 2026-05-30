from __future__ import annotations

import unittest
import time

from backend.app.core.config import Settings
from backend.app.core.models import Level, OrderBook
from backend.app.engines.fills import estimate_fill
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.queue import OpportunityQueue
from backend.app.engines.risk import RiskManager
from backend.app.engines.triangular import TriangularArbitrageEngine


def book(exchange, symbol, asks, bids) -> OrderBook:
    return OrderBook(
        key=f"{exchange.id}:{symbol}",
        exchange_id=exchange.id,
        exchange_name=exchange.name,
        symbol=symbol,
        primary=symbol == exchange.primary_symbol,
        source="test",
        status="test",
        fee_bps=exchange.taker_fee_bps,
        slippage_bps=exchange.slippage_bps,
        confidence=1,
        asks=[Level(*level) for level in asks],
        bids=[Level(*level) for level in bids],
        latency_ms=20,
        timestamp=int(time.time() * 1000),
    )


class EngineTests(unittest.TestCase):
    def test_fill_consumes_multiple_levels(self):
        fill = estimate_fill([Level(100, 0.5), Level(101, 0.75)], 1, "ask")
        self.assertAlmostEqual(fill.filled_qty, 1)
        self.assertAlmostEqual(fill.quote, 100.5)
        self.assertEqual(fill.level_count, 2)

    def test_queue_dedupes_opposite_simple_routes(self):
        queue = OpportunityQueue()
        ranked = queue.rank([
            {"strategy": "simple", "product": "BTC/USDT", "buyExchangeId": "binance", "sellExchangeId": "kraken", "score": 1, "status": "profitable"},
            {"strategy": "simple", "product": "BTC/USDT", "buyExchangeId": "kraken", "sellExchangeId": "binance", "score": 4, "status": "profitable"},
        ])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["buyExchangeId"], "kraken")
        self.assertEqual(queue.snapshot()["deduped"], 1)

    def test_risk_rearms_after_cooldown_and_uses_min_samples(self):
        settings = Settings(max_volatility_pct=1.5, volatility_min_samples=3, pause_after_loss_ms=1000, volatility_rearm_ms=100)
        risk = RiskManager(settings)
        base = {"exchangeName": "Binance", "timestamp": 1000, "bestAsk": 70001, "bestBid": 69999}
        risk.evaluate_market([base], 1000)
        risk.evaluate_market([{**base, "timestamp": 1100, "bestAsk": 70011, "bestBid": 70009}], 1100)
        self.assertFalse(risk.snapshot(1100)["paused"])
        risk.evaluate_market([{**base, "timestamp": 1200, "bestAsk": 71401, "bestBid": 71399}], 1200)
        self.assertTrue(risk.snapshot(1200)["paused"])
        self.assertFalse(risk.snapshot(2301)["paused"])

    def test_triangular_profitable_cycle(self):
        settings = Settings(triangular_quote_size=2500, triangular_min_net_bps=0.5, triangular_min_net_profit_usd=0.1)
        exchange = settings.exchanges[0]
        books = {
            f"{exchange.id}:BTC/USDT": book(exchange, "BTC/USDT", [(70000, 2)], [(69980, 2)]),
            f"{exchange.id}:ETH/BTC": book(exchange, "ETH/BTC", [(0.05, 100)], [(0.0499, 100)]),
            f"{exchange.id}:ETH/USDT": book(exchange, "ETH/USDT", [(3570, 100)], [(3700, 100)]),
        }
        engine = TriangularArbitrageEngine(settings, WalletLedger(settings))
        opportunities = engine.scan(books)
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0]["strategy"], "triangular")
        self.assertEqual(opportunities[0]["status"], "profitable")


if __name__ == "__main__":
    unittest.main()
