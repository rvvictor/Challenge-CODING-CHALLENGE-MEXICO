from __future__ import annotations

import unittest
import time
import asyncio

from backend.app.core.config import Settings
from backend.app.core.models import Level, OrderBook
from backend.app.engines.fills import estimate_fill
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.queue import OpportunityQueue
from backend.app.engines.risk import RiskManager
from backend.app.engines.simulator import SimulatedMarket
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

    def test_risk_market_window_can_reset_on_mode_change(self):
        settings = Settings(volatility_min_samples=2)
        risk = RiskManager(settings)
        risk.evaluate_market([{"exchangeName": "Demo", "timestamp": 1000, "bestAsk": 70001, "bestBid": 69999}], 1000)
        self.assertEqual(risk.snapshot(1000)["volatilityWindowPoints"], 1)
        risk.reset_market_window()
        self.assertEqual(risk.snapshot(1001)["volatilityWindowPoints"], 0)

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
        self.assertTrue(any(item["strategy"] == "triangular" and item["status"] == "profitable" for item in opportunities))

    def test_triangular_engine_supports_dynamic_four_leg_cycles(self):
        settings = Settings(active_exchanges="okx,bybit", triangular_quote_size=650, triangular_min_net_bps=0.5, triangular_min_net_profit_usd=0.1)
        exchange = settings.exchanges[0]
        books = {
            f"{exchange.id}:BTC/USDT": book(exchange, "BTC/USDT", [(70000, 2)], [(69980, 2)]),
            f"{exchange.id}:ETH/BTC": book(exchange, "ETH/BTC", [(0.05, 100)], [(0.0499, 100)]),
            f"{exchange.id}:SOL/ETH": book(exchange, "SOL/ETH", [(0.10, 500)], [(0.099, 500)]),
            f"{exchange.id}:SOL/USDT": book(exchange, "SOL/USDT", [(370, 500)], [(390, 500)]),
        }
        engine = TriangularArbitrageEngine(settings, WalletLedger(settings))
        opportunities = engine.scan(books)
        self.assertTrue(any(item["strategy"] == "triangular" and item["dynamicCycle"] for item in opportunities))

    def test_partial_fill_is_detected_when_book_depth_is_small(self):
        from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine

        settings = Settings(max_trade_btc=0.09, min_trade_btc=0.004, min_net_bps=0.1, min_net_profit_usd=0.1)
        ledger = WalletLedger(settings)
        engine = CrossExchangeArbitrageEngine(settings, ledger)
        buy = settings.exchanges[0]
        sell = settings.exchanges[1]
        opportunity = engine.evaluate_pair(
            book(buy, "BTC/USDT", [(70000, 0.01), (70001, 0.006)], [(69990, 0.02)]),
            book(sell, "BTC/USDT", [(70200, 0.02)], [(70480, 0.008), (70470, 0.006)]),
            int(time.time() * 1000),
        )
        self.assertIsNotNone(opportunity)
        payload = opportunity.to_dict()
        self.assertTrue(payload["partial"])
        self.assertGreater(payload["qtyBtc"], 0.004)
        self.assertLess(payload["qtyBtc"], settings.max_trade_btc)

    def test_demo_market_can_create_liquidity_crunch(self):
        settings = Settings()
        simulator = SimulatedMarket(settings.exchanges)
        seen_low_depth = False
        for _ in range(220):
            simulator.advance(settings.exchanges)
            for exchange in settings.exchanges:
                orderbook = simulator.generate(exchange, settings.exchanges, exchange.primary_symbol)
                total_ask = sum(level.qty for level in orderbook.asks)
                if 0 < total_ask < settings.max_trade_btc:
                    seen_low_depth = True
                    break
            if seen_low_depth:
                break
        self.assertTrue(seen_low_depth)

    def test_demo_market_can_create_triangular_showcase_edge(self):
        settings = Settings(active_exchanges="okx,bybit,kucoin", triangular_quote_size=650)
        simulator = SimulatedMarket(settings.exchanges)
        ledger = WalletLedger(settings)
        engine = TriangularArbitrageEngine(settings, ledger)
        seen_triangular = False
        books = {}
        for _ in range(90):
            simulator.advance(settings.exchanges)
            for exchange in settings.exchanges:
                for symbol in exchange.triangular_symbols:
                    books[f"{exchange.id}:{symbol}"] = simulator.generate(exchange, settings.exchanges, symbol)
            opportunities = engine.scan(books)
            if any(item["strategy"] == "triangular" and item["status"] == "profitable" for item in opportunities):
                seen_triangular = True
                break
        self.assertTrue(seen_triangular)

    def test_demo_market_can_create_dynamic_four_leg_cycle(self):
        settings = Settings(active_exchanges="okx,bybit,kucoin", triangular_quote_size=650)
        from backend.app.engines.market_service import MarketService

        service = MarketService(settings)
        seen_dynamic = False
        for _ in range(130):
            service.generate_demo_books()
            opportunities = service.triangular_engine.scan(service.books)
            if any(item.get("dynamicCycle") and item["status"] == "profitable" for item in opportunities):
                seen_dynamic = True
                break
        service.persistence.close()
        self.assertTrue(seen_dynamic)

    def test_demo_showcase_balances_cross_partial_and_cycle_signals(self):
        settings = Settings(market_mode="demo")
        from backend.app.engines.market_service import MarketService

        service = MarketService(settings)
        seen_simple = False
        seen_partial_simple = False
        seen_triangular = False
        seen_dynamic = False
        for _ in range(170):
            service.generate_demo_books()
            primary = service.primary_books()
            primary_map = {book.exchange_id: book for book in service.health_adjusted_books(primary)}
            ranked = service.queue.rank(
                service.cross_engine.scan(primary_map)
                + service.triangular_engine.scan(service.health_adjusted_book_map())
            )
            for opportunity in ranked:
                if opportunity.get("status") != "profitable":
                    continue
                seen_simple = seen_simple or opportunity.get("strategy") == "simple"
                seen_partial_simple = seen_partial_simple or (
                    opportunity.get("strategy") == "simple" and opportunity.get("partial")
                )
                seen_triangular = seen_triangular or (
                    opportunity.get("strategy") == "triangular" and not opportunity.get("dynamicCycle")
                )
                seen_dynamic = seen_dynamic or bool(opportunity.get("dynamicCycle"))
        service.persistence.close()
        self.assertTrue(seen_simple)
        self.assertTrue(seen_partial_simple)
        self.assertTrue(seen_triangular)
        self.assertTrue(seen_dynamic)

    def test_ccxt_provider_uses_exchange_safe_order_book_limits(self):
        from backend.app.integrations.ccxt_provider import CcxtStreamProvider

        async def noop_event(_event):
            return None

        settings = Settings(order_book_limit=25, active_exchanges="all")
        provider = CcxtStreamProvider(settings, lambda _book: None, noop_event)
        self.assertEqual(provider.order_book_limit(settings.exchange_by_id("kucoin")), 20)
        self.assertEqual(provider.order_book_limit(settings.exchange_by_id("bybit")), 50)
        self.assertEqual(provider.order_book_limit(settings.exchange_by_id("kraken")), 25)
        self.assertEqual(provider.order_book_limit(settings.exchange_by_id("bitfinex")), 25)

    def test_ccxt_provider_snapshot_exposes_health_scoring(self):
        from backend.app.integrations.ccxt_provider import CcxtStreamProvider

        async def noop_event(_event):
            return None

        settings = Settings(active_exchanges="okx,bybit")
        provider = CcxtStreamProvider(settings, lambda _book: None, noop_event)
        state = provider.state(settings.exchange_by_id("okx"), "BTC/USDT")
        provider.mark_error(state)
        snapshot = provider.snapshot()
        stream = snapshot["streams"][0]
        self.assertIn("healthScore", stream)
        self.assertEqual(stream["healthStatus"], "degraded")

    def test_settings_supports_named_exchange_profiles(self):
        speed = Settings(exchange_profile="speed")
        coverage = Settings(exchange_profile="coverage")
        self.assertEqual(len(speed.exchanges), 5)
        self.assertEqual(len(coverage.exchanges), 10)

    def test_settings_can_filter_active_exchanges_for_speed_profile(self):
        settings = Settings(active_exchanges="binance,okx,bybit")
        self.assertEqual([exchange.id for exchange in settings.exchanges], ["binance", "okx", "bybit"])
        self.assertEqual(len(settings.exchange_universe), 10)

    def test_settings_defaults_to_fast_profile(self):
        settings = Settings()
        self.assertEqual([exchange.id for exchange in settings.exchanges], ["okx", "bybit", "kucoin", "kraken", "bitstamp"])

    def test_market_service_can_switch_active_exchanges(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(active_exchanges="okx,kraken,bybit"))
        asyncio.run(service.set_active_exchanges(["kucoin", "bitstamp", "kraken", "okx", "bybit", "binance"]))
        self.assertEqual([exchange.id for exchange in service.settings.exchanges], ["kucoin", "bitstamp", "kraken", "okx", "bybit"])
        self.assertEqual(service.books, {})
        self.assertIn("kucoin", service.ledger.balances)

    def test_exchange_switch_preserves_performance_history(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(active_exchanges="okx,kraken,bybit"))
        service.ledger.realized_pnl = 12.5
        service.store.add_trade(
            {"id": "T-test", "time": int(time.time() * 1000), "strategy": "simple", "partial": True, "netProfit": 12.5},
            service.ledger.realized_pnl,
        )
        started_at = service.started_at
        asyncio.run(service.set_active_exchanges(["kucoin", "bitstamp", "kraken", "okx", "bybit"]))
        self.assertEqual(service.store.executed_count, 1)
        self.assertEqual(service.store.partial_count, 1)
        self.assertAlmostEqual(service.ledger.realized_pnl, 12.5)
        self.assertEqual(service.started_at, started_at)

    def test_volatility_stress_button_activates_circuit_breaker(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo", pause_after_loss_ms=1000))
        asyncio.run(service.trigger_volatility_stress())
        snapshot = service.snapshot()
        self.assertTrue(snapshot["risk"]["paused"])
        self.assertEqual(snapshot["risk"]["condition"], "volatility")
        self.assertTrue(any(event.get("type") == "stress-test" for event in snapshot["riskEvents"]))

    def test_circuit_breaker_halts_signal_generation_while_monitoring_books(self):
        from backend.app.engines.market_service import MarketService

        async def run():
            service = MarketService(Settings(market_mode="demo", pause_after_loss_ms=1000))
            await service.trigger_volatility_stress()
            service.tick()
            await asyncio.sleep(0)
            return service.snapshot()

        snapshot = asyncio.run(run())
        self.assertTrue(snapshot["risk"]["paused"])
        self.assertEqual(snapshot["queuedOpportunities"], [])
        self.assertEqual(snapshot["queue"]["paused"], True)
        self.assertGreater(len(snapshot["books"]), 0)
        self.assertEqual(snapshot["metrics"]["detectedCount"], 0)

    def test_executor_revalidates_inventory_between_same_tick_trades(self):
        from backend.app.engines.event_store import EventStore
        from backend.app.engines.execution import ExecutionSimulator

        settings = Settings(max_executions_per_tick=2, active_exchanges="all", inventory_rebalance_enabled=False)
        ledger = WalletLedger(settings)
        executor = ExecutionSimulator(settings, ledger, EventStore(), RiskManager(settings))
        base = {
            "strategy": "simple",
            "status": "profitable",
            "grossProfit": 300,
            "netProfit": 120,
            "netBps": 28,
            "confidence": 0.9,
            "partial": False,
            "source": "test",
            "product": "BTC/USDT",
            "qtyBtc": 0.2,
            "targetQtyBtc": 0.2,
            "filledRatio": 1,
            "buyPrice": 70000,
            "sellPrice": 70500,
            "costs": {
                "buyFee": 10,
                "sellFee": 10,
                "slippageCostBuy": 2,
                "slippageCostSell": 2,
                "latencyRiskCost": 1,
                "rebalanceCost": 1,
                "totalCosts": 26,
            },
        }
        opportunities = [
            {**base, "id": "one", "dedupeKey": "one", "buyExchangeId": "binance", "sellExchangeId": "bybit", "buyExchange": "Binance", "sellExchange": "Bybit"},
            {**base, "id": "two", "dedupeKey": "two", "buyExchangeId": "okx", "sellExchangeId": "bybit", "buyExchange": "OKX", "sellExchange": "Bybit"},
        ]
        trades = executor.try_execute(opportunities, [])
        self.assertEqual(len(trades), 1)
        self.assertGreaterEqual(float(ledger.get("bybit")["BTC"]), 0)

    def test_executor_throttles_simulated_demo_fills(self):
        from backend.app.engines.event_store import EventStore
        from backend.app.engines.execution import ExecutionSimulator

        settings = Settings(demo_min_execution_gap_ms=60000, max_executions_per_tick=1)
        ledger = WalletLedger(settings)
        executor = ExecutionSimulator(settings, ledger, EventStore(), RiskManager(settings))
        opportunity = {
            "id": "demo-one",
            "dedupeKey": "demo-one",
            "strategy": "triangular",
            "status": "profitable",
            "grossProfit": 4,
            "netProfit": 2,
            "netBps": 3,
            "expectedValue": 1.4,
            "evBps": 2,
            "confidence": 0.9,
            "partial": False,
            "filledRatio": 1,
            "source": "simulated",
            "product": "USDT -> BTC -> ETH -> USDT",
            "exchangeId": settings.exchanges[0].id,
            "exchange": settings.exchanges[0].name,
            "cycleId": "USDT-BTC-ETH-USDT",
            "cyclePath": ["USDT", "BTC", "ETH", "USDT"],
            "quoteIn": 650,
            "quoteOut": 652,
            "qtyBtc": 0.009,
            "qtyEth": 0.17,
            "targetQuote": 650,
            "legs": [],
            "latencies": {"totalMs": 90},
            "costs": {"totalCosts": 1},
        }
        first = executor.try_execute([opportunity], [])
        second = executor.try_execute([{**opportunity, "id": "demo-two", "dedupeKey": "demo-two"}], [])
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def test_risk_budget_pauses_after_hourly_losses(self):
        settings = Settings(risk_budget_hour_usd=5, pause_after_loss_ms=1000)
        risk = RiskManager(settings)
        risk.record_trade({"netProfit": -6}, 1000)
        snapshot = risk.snapshot(1000)
        self.assertTrue(snapshot["paused"])
        self.assertEqual(snapshot["condition"], "risk-budget")

    def test_queue_prefers_higher_expected_value(self):
        queue = OpportunityQueue()
        ranked = queue.rank([
            {"strategy": "simple", "product": "BTC/USDT", "buyExchangeId": "okx", "sellExchangeId": "kraken", "score": 50, "expectedValue": 1, "status": "profitable"},
            {"strategy": "simple", "product": "BTC/USDT", "buyExchangeId": "kraken", "sellExchangeId": "okx", "score": 2, "expectedValue": 4, "status": "profitable"},
        ])
        self.assertEqual(ranked[0]["buyExchangeId"], "kraken")

    def test_market_service_metrics_snapshot_has_operational_sections(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        service.tick()
        metrics = service.metrics_snapshot()
        self.assertIn("venueHealth", metrics)
        self.assertIn("database", metrics)
        self.assertIn("risk", metrics)

    def test_api_metrics_endpoint_when_fastapi_available(self):
        try:
            import fastapi  # noqa: F401
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("FastAPI test client is not installed in this local Python environment")
        from backend.app.main import app

        client = TestClient(app)
        self.assertEqual(client.get("/api/health").status_code, 200)
        metrics = client.get("/api/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("venueHealth", metrics.json())

    def test_inventory_rebalance_prevents_local_wallet_gate(self):
        from backend.app.engines.event_store import EventStore
        from backend.app.engines.execution import ExecutionSimulator

        settings = Settings(active_exchanges="okx,bybit,kraken", min_trade_btc=0.002)
        ledger = WalletLedger(settings)
        ledger.get("bybit")["BTC"] = 0.0
        ledger.get("okx")["BTC"] = 0.5
        executor = ExecutionSimulator(settings, ledger, EventStore(), RiskManager(settings))
        opportunity = {
            "id": "rebalance",
            "dedupeKey": "rebalance",
            "strategy": "simple",
            "status": "profitable",
            "grossProfit": 20,
            "netProfit": 3,
            "netBps": 1.7,
            "confidence": 0.9,
            "partial": False,
            "filledRatio": 1,
            "source": "test",
            "product": "BTC/USDT",
            "qtyBtc": 0.02,
            "targetQtyBtc": 0.02,
            "buyPrice": 70000,
            "sellPrice": 70130,
            "buyExchangeId": "kraken",
            "sellExchangeId": "bybit",
            "buyExchange": "Kraken",
            "sellExchange": "Bybit",
            "costs": {
                "buyFee": 1,
                "sellFee": 1,
                "slippageCostBuy": 0.5,
                "slippageCostSell": 0.5,
                "latencyRiskCost": 0.2,
                "rebalanceCost": 0.2,
                "totalCosts": 3.4,
            },
        }
        trades = executor.try_execute([opportunity], [])
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0].get("inventoryRebalance"))
        self.assertGreaterEqual(float(ledger.get("bybit")["BTC"]), 0)

    def test_explainable_edge_adds_decision_and_settlement_reality(self):
        from backend.app.engines.edge_analysis import explain_opportunity

        opportunity = explain_opportunity({
            "id": "edge",
            "strategy": "simple",
            "status": "profitable",
            "grossProfit": 8,
            "grossBps": 6.2,
            "netProfit": 2.4,
            "netBps": 1.7,
            "score": 0.5,
            "confidence": 0.91,
            "partial": True,
            "filledRatio": 0.63,
            "buyExchange": "OKX",
            "sellExchange": "Kraken",
            "buyPrice": 70000,
            "qtyBtc": 0.01,
            "costs": {"totalCosts": 5.6, "rebalanceCost": 0.8},
            "latencies": {"buyMs": 30, "sellMs": 40},
        })
        self.assertEqual(opportunity["decision"]["action"], "execute-partial")
        self.assertIn("components", opportunity["edgeBreakdown"])
        self.assertEqual(opportunity["paperVsSettlement"]["verdict"], "settlement-safe")

    def test_market_service_exports_session_and_replay_ledger(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        service.edge_ledger.append("opportunity", {"route": "OKX -> Kraken", "netBps": 1.2})
        export = service.export_session()
        replay = service.snapshot()["replay"]
        self.assertEqual(export["botName"], "Aurelion")
        self.assertIn("latencySlo", export)
        self.assertEqual(replay["eventCount"], 1)
        self.assertEqual(export["edgeLedger"][0]["type"], "opportunity")


if __name__ == "__main__":
    unittest.main()
