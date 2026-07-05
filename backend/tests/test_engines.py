from __future__ import annotations

import unittest
import time
import asyncio

from backend.app.core.config import Settings
from backend.app.core.models import Level, OrderBook
from backend.app.engines.fills import best, estimate_fill
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


class ParameterRegistryTests(unittest.TestCase):
    def test_registry_values_match_settings_fields(self):
        from backend.app.core.config import PARAMETER_REGISTRY, parameter_values

        settings = Settings()
        values = parameter_values(settings)
        self.assertEqual(len(values), len(PARAMETER_REGISTRY))
        for spec in PARAMETER_REGISTRY:
            self.assertTrue(hasattr(settings, spec.key), spec.key)
            self.assertIn(spec.key, values)

    def test_apply_parameters_clamps_coerces_and_rejects(self):
        from backend.app.core.config import apply_parameter_updates

        settings = Settings()
        result = apply_parameter_updates(settings, {
            "min_net_bps": 3.5,
            "max_trade_btc": 999,            # above max -> clamped to 0.1
            "max_executions_per_tick": 2.9,  # rounds to int 3
            "triangular_enabled": "false",   # bool coercion
            "not_a_param": 1,                # rejected (unknown)
        })
        self.assertEqual(settings.min_net_bps, 3.5)
        self.assertEqual(settings.max_trade_btc, 0.1)
        self.assertEqual(settings.max_executions_per_tick, 3)
        self.assertFalse(settings.triangular_enabled)
        self.assertIn("min_net_bps", result["changed"])
        self.assertEqual([item["key"] for item in result["rejected"]], ["not_a_param"])

    def test_live_parameter_change_flips_execution_gate(self):
        from backend.app.core.config import apply_parameter_updates
        from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine

        settings = Settings(market_mode="demo")
        ledger = WalletLedger(settings)
        engine = CrossExchangeArbitrageEngine(settings, ledger)
        okx = settings.exchange_by_id("okx")
        kraken = settings.exchange_by_id("kraken")
        books = {
            "okx": book(okx, "BTC/USDT", [(70000, 1)], [(69990, 1)]),
            "kraken": book(kraken, "BTC/USDT", [(71010, 1)], [(71000, 1)]),
        }
        profitable = [o for o in engine.scan(books) if o["status"] == "profitable"]
        self.assertTrue(profitable, "expected a profitable opportunity at default gates")
        # Same shared Settings object: raising the profit gate live must reject it
        # on the next scan, with no engine rebuild.
        apply_parameter_updates(settings, {"min_net_profit_usd": 20})
        rescan = [o for o in engine.scan(books) if o["status"] == "profitable"]
        self.assertFalse(rescan, "raising min_net_profit_usd live should reject the opportunity")

    def test_market_service_parameters_preset_and_reset(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        params = service.parameters()
        self.assertIn("specs", params)
        self.assertIn("values", params)
        self.assertTrue(any(spec["key"] == "min_net_bps" for spec in params["specs"]))

        applied = service.apply_preset("aggressive")
        self.assertEqual(applied.get("preset"), "aggressive")
        self.assertEqual(service.settings.min_net_bps, 0.4)

        unknown = service.apply_preset("does-not-exist")
        self.assertTrue(unknown["rejected"])

        reset = service.reset_parameters()
        self.assertTrue(reset.get("reset"))
        self.assertEqual(service.settings.min_net_bps, params["values"]["min_net_bps"])

    def test_api_params_endpoints(self):
        try:
            import fastapi  # noqa: F401
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("FastAPI test client is not installed in this local Python environment")
        from backend.app.main import app

        client = TestClient(app)
        body = client.get("/api/params").json()
        self.assertIn("specs", body)
        self.assertIn("values", body)
        self.assertTrue(body["presets"])

        posted = client.post("/api/params", json={"updates": {"min_net_bps": 2.5, "bogus": 1}})
        self.assertEqual(posted.status_code, 200)
        result = posted.json()
        self.assertEqual(result["values"]["min_net_bps"], 2.5)
        self.assertTrue(any(item["key"] == "bogus" for item in result["applied"]["rejected"]))

        preset = client.post("/api/params", json={"preset": "conservative"})
        self.assertEqual(preset.json()["applied"].get("preset"), "conservative")

        # restore baseline so the shared global service does not leak into other tests
        client.post("/api/params", json={"reset": True})


class QuantModelTests(unittest.TestCase):
    def _triangular_books(self, eth_usdt_bid):
        settings = Settings(cycle_algo="bellman_ford")
        engine = TriangularArbitrageEngine(settings, WalletLedger(settings))
        okx = settings.exchange_by_id("okx")

        def mk(symbol, ask_price, bid_price):
            return book(okx, symbol, [(ask_price, 50)], [(bid_price, 50)])

        books = {
            b.key: b
            for b in [
                mk("BTC/USDT", 100, 99.9),
                mk("ETH/BTC", 0.05, 0.0499),
                mk("ETH/USDT", eth_usdt_bid + 0.01, eth_usdt_bid),
            ]
        }
        return engine, books

    def test_bellman_ford_finds_negative_cycle(self):
        # USDT->BTC->ETH->USDT product = (1/100) * (1/0.05) * 6 = 1.2 (profitable)
        engine, books = self._triangular_books(eth_usdt_bid=6.0)
        cycles = engine.find_cycles("okx", books)
        self.assertTrue(cycles, "Bellman-Ford should detect the profitable cycle")
        first = cycles[0]
        self.assertTrue(all("to" in edge and "side" in edge for edge in first))
        self.assertIn(first[0]["from"], {"USDT", "USD"})

    def test_bellman_ford_rejects_non_arbitrage(self):
        # ETH/USDT == fair value (ETH/BTC * BTC/USDT = 0.05 * 100 = 5): neither
        # direction is profitable after fees, so there is no negative cycle.
        engine, books = self._triangular_books(eth_usdt_bid=5.0)
        self.assertEqual(engine.find_cycles("okx", books), [])

    def test_market_impact_is_monotonic_and_modelled(self):
        from backend.app.engines.market_impact import impact_bps

        self.assertEqual(impact_bps("book_walk", 1.0, 10.0, 8.0), 0.0)
        small = impact_bps("sqrt_impact", 0.1, 10.0, 8.0)
        large = impact_bps("sqrt_impact", 1.0, 10.0, 8.0)
        self.assertGreater(small, 0.0)
        self.assertGreater(large, small)
        self.assertGreaterEqual(impact_bps("almgren_lite", 1.0, 10.0, 8.0), large)

    def test_kelly_multiplier_bounds(self):
        from backend.app.engines.sizing import kelly_multiplier

        self.assertEqual(kelly_multiplier(0.9, 5.0, 0.0), 0.0)
        strong = kelly_multiplier(0.9, 5.0, 1.0)
        weak = kelly_multiplier(0.5, 1.0, 1.0)
        self.assertGreaterEqual(strong, weak)
        self.assertGreaterEqual(strong, 0.0)
        self.assertLessEqual(strong, 1.0)
        self.assertEqual(kelly_multiplier(0.1, 0.001, 1.0), 0.0)

    def test_volatility_models_differ_and_flatten(self):
        window = [{"time": i, "price": p} for i, p in enumerate([100, 101, 102, 101.5, 103])]

        range_risk = RiskManager(Settings(volatility_model="range"))
        range_risk.price_window = window
        range_pct = range_risk._window_volatility_pct(103, 100)

        sd_risk = RiskManager(Settings(volatility_model="stddev"))
        sd_risk.price_window = window
        sd_pct = sd_risk._window_volatility_pct(103, 100)

        self.assertAlmostEqual(range_pct, 3.0)
        self.assertGreater(sd_pct, 0.0)
        self.assertNotAlmostEqual(range_pct, sd_pct)

        sd_risk.price_window = [{"time": i, "price": 100} for i in range(5)]
        self.assertEqual(sd_risk._window_volatility_pct(100, 100), 0.0)


class BacktestAndLearningTests(unittest.TestCase):
    def test_calibrator_learns_and_gates_by_samples(self):
        from backend.app.engines.calibration import SuccessCalibrator

        calibrator = SuccessCalibrator(alpha_prior=9, beta_prior=1, min_samples=4)
        self.assertEqual(calibrator.factor("okx"), 1.0)  # cold start is neutral
        for _ in range(6):
            calibrator.update("okx", False)
        self.assertLess(calibrator.probability("okx"), 0.9)  # prior 0.9 drops with failures
        self.assertLess(calibrator.factor("okx"), 1.0)       # applied once >= min_samples
        for _ in range(50):
            calibrator.update("kraken", True)
        self.assertGreater(calibrator.probability("kraken"), 0.9)

    def test_calibration_reduces_confidence_when_enabled(self):
        from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine
        from backend.app.engines.calibration import SuccessCalibrator

        settings = Settings(market_mode="demo", calibration_enabled=True)
        calibrator = SuccessCalibrator(min_samples=2)
        for _ in range(8):
            calibrator.update("kraken", False)  # make kraken look unreliable
        engine = CrossExchangeArbitrageEngine(settings, WalletLedger(settings), calibrator)
        okx = settings.exchange_by_id("okx")
        kraken = settings.exchange_by_id("kraken")
        books = {
            "okx": book(okx, "BTC/USDT", [(70000, 1)], [(69990, 1)]),
            "kraken": book(kraken, "BTC/USDT", [(71010, 1)], [(71000, 1)]),
        }
        opportunity = next(o for o in engine.scan(books) if o["strategy"] == "simple")
        self.assertLess(opportunity["confidence"], 1.0)

    def test_backtest_runner_produces_metrics(self):
        from backend.app.engines.backtest import BacktestRunner

        result = BacktestRunner(Settings(market_mode="demo")).run(ticks=40, regime="calm")
        for key in ("ticks", "regime", "detected", "executed", "hitRate", "totalPnl", "maxDrawdown", "sharpeLike", "equityCurve", "params"):
            self.assertIn(key, result)
        self.assertEqual(result["ticks"], 40)
        self.assertEqual(result["regime"], "calm")
        self.assertTrue(result["equityCurve"])
        self.assertGreaterEqual(result["executed"], 0)

    def test_backtest_stressed_regime_is_realistic(self):
        from backend.app.engines.backtest import BacktestRunner

        result = BacktestRunner(Settings(market_mode="demo")).run(ticks=90, regime="stressed")
        self.assertEqual(result["regime"], "stressed")
        # A stressed regime must produce real losses and a drawdown, not the
        # best-case demo cadence.
        self.assertGreater(result["executed"], 0)
        self.assertGreater(result["losses"], 0)
        self.assertGreater(result["maxDrawdown"], 0)
        self.assertLess(result["hitRate"], 1.0)

    def test_persistence_read_and_count_roundtrip(self):
        import os
        import tempfile

        from backend.app.integrations.persistence import DurableEventSink

        tmp = os.path.join(tempfile.mkdtemp(), "aurelion-test.db")
        sink = DurableEventSink(Settings(persistence_enabled=True, sqlite_path=tmp))
        if sink.status != "connected":
            self.skipTest("sqlite sink unavailable in this environment")
        sink.append("opportunity", {"route": "OKX -> Kraken", "netBps": 1.2})
        sink.append_many("trade", [{"id": "T1"}, {"id": "T2"}])
        self.assertEqual(sink.count(), 3)
        trades = sink.read(kind="trade")
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["payload"]["id"], "T2")  # newest first
        sink.close()


class StressLabTests(unittest.TestCase):
    def _sim(self):
        settings = Settings()
        sim = SimulatedMarket(settings.exchanges)
        sim.advance(settings.exchanges)
        return settings, sim

    def test_liquidity_crunch_thins_the_book(self):
        settings, sim = self._sim()
        self.assertEqual(sim.inject_scenario("liquidity_crunch", settings.exchanges), "liquidity_crunch")
        self.assertTrue(sim.scenario_active("liquidity_crunch"))
        exchange = settings.exchanges[0]
        book = sim.generate(exchange, settings.exchanges, exchange.primary_symbol)
        self.assertTrue(all(level.qty < 0.001 for level in book.asks))

    def test_venue_outage_makes_books_stale(self):
        settings, sim = self._sim()
        sim.inject_scenario("venue_outage", settings.exchanges)
        exchange = settings.exchange_by_id(sim.outage_venue)
        book = sim.generate(exchange, settings.exchanges, exchange.primary_symbol)
        self.assertLess(book.timestamp, int(time.time() * 1000) - 9000)
        self.assertGreaterEqual(book.latency_ms, 1200)

    def test_latency_spike_inflates_latency(self):
        settings, sim = self._sim()
        sim.inject_scenario("latency_spike", settings.exchanges)
        exchange = settings.exchanges[0]
        book = sim.generate(exchange, settings.exchanges, exchange.primary_symbol)
        self.assertGreaterEqual(book.latency_ms, 700)

    def test_leg_failure_reconciliation_opens_and_covers_exposure(self):
        from backend.app.engines.event_store import EventStore
        from backend.app.engines.execution import ExecutionSimulator

        settings = Settings()
        executor = ExecutionSimulator(settings, WalletLedger(settings), EventStore(), RiskManager(settings))
        opportunity = {"strategy": "simple", "qtyBtc": 0.01, "sellPrice": 71000}

        hedged = executor.reconcile_fills(opportunity)
        self.assertTrue(hedged["hedged"])
        self.assertEqual(hedged["netExposureBtc"], 0)

        executor.leg_failure_until = int(time.time() * 1000) + 10000
        failed = executor.reconcile_fills(opportunity)
        self.assertGreater(failed["netExposureBtc"], 0)
        self.assertGreater(failed["coverCost"], 0)
        self.assertEqual(failed["correctiveAction"], "cover-residual")

    def test_inventory_autonomy_reports_runway(self):
        settings = Settings()
        autonomy = WalletLedger(settings).inventory_autonomy(settings.exchanges, 70000)
        self.assertTrue(autonomy["venues"])
        self.assertGreater(autonomy["sessionAutonomy"], 0)
        self.assertEqual(len(autonomy["venues"]), len(settings.exchanges))


class CoPilotTests(unittest.TestCase):
    def _narrator(self):
        import os

        from backend.app.integrations.llm_narrator import DecisionNarrator

        previous = os.environ.pop("ANTHROPIC_API_KEY", None)
        narrator = DecisionNarrator(Settings())
        if previous is not None:
            os.environ["ANTHROPIC_API_KEY"] = previous
        return narrator

    def test_narrator_falls_back_without_key(self):
        narrator = self._narrator()
        self.assertFalse(narrator.available())
        snapshot = {
            "mode": "demo",
            "risk": {"paused": False, "reason": "Healthy"},
            "models": {"cycleAlgo": "dfs", "slippageModel": "book_walk", "sizingMode": "fixed"},
            "scenarios": {"active": []},
            "metrics": {"cumulativePnl": 12.5, "executedCount": 4, "detectedCount": 200, "bestNetBps": 1.4},
            "queuedOpportunities": [{
                "strategy": "simple", "status": "profitable", "buyExchange": "OKX", "sellExchange": "Kraken",
                "netBps": 1.4, "evBps": 1.1, "confidence": 0.82, "reason": "Net edge cleared risk gates",
            }],
        }
        result = narrator.narrate(snapshot)
        self.assertEqual(result["source"], "deterministic")
        self.assertIn("OKX -> Kraken", result["text"])
        self.assertIn("bps", result["text"])

    def test_narrator_explains_circuit_breaker_and_caches(self):
        narrator = self._narrator()
        snapshot = {
            "mode": "demo",
            "risk": {"paused": True, "reason": "Stale data: Bitstamp"},
            "models": {"cycleAlgo": "bellman_ford", "slippageModel": "sqrt_impact", "sizingMode": "kelly"},
            "scenarios": {"active": ["venue_outage"]},
            "metrics": {},
            "queuedOpportunities": [],
        }
        first = narrator.narrate(snapshot)
        self.assertIn("circuit breaker", first["text"].lower())
        self.assertIn("venue_outage", first["text"])
        second = narrator.narrate(snapshot)
        self.assertTrue(second["cached"])

    def test_fallback_phrasing_varies_between_narrations(self):
        narrator = self._narrator()
        narrator._variety.seed(7)
        ctx = narrator._build_context({
            "mode": "demo",
            "risk": {"paused": False, "reason": "Healthy"},
            "models": {"cycleAlgo": "dfs", "slippageModel": "book_walk", "sizingMode": "fixed"},
            "scenarios": {"active": []},
            "metrics": {"cumulativePnl": 12.5},
            "queuedOpportunities": [{
                "strategy": "simple", "status": "profitable", "buyExchange": "OKX", "sellExchange": "Kraken",
                "netBps": 1.4, "confidence": 0.8, "reason": "Net edge cleared risk gates",
            }],
        })
        texts = {narrator._fallback(ctx) for _ in range(8)}
        self.assertGreater(len(texts), 1, "fallback should rotate phrasing, not repeat one template")
        for text in texts:
            self.assertIn("OKX -> Kraken", text)  # facts stay grounded in every variant
            self.assertIn("bps", text)

    def test_narrator_explains_a_specific_trade_deterministically(self):
        narrator = self._narrator()
        snapshot = {
            "mode": "demo",
            "risk": {"paused": False, "reason": "Healthy"},
            "models": {"cycleAlgo": "dfs", "slippageModel": "book_walk", "sizingMode": "fixed"},
            "scenarios": {"active": []},
            "metrics": {},
            "queuedOpportunities": [],
            "trades": [{
                "id": "T-leg-1",
                "strategy": "simple",
                "buyExchange": "OKX",
                "sellExchange": "Kraken",
                "status": "leg-failure",
                "partial": True,
                "filledRatio": 0.55,
                "netProfit": -3.21,
                "netBps": -4.5,
                "executionQuality": {"edgeCaptureBps": -4.5, "adverseMoveCost": 0.4},
                "reconciliation": {"netExposureBtc": 0.0045, "coverCost": 2.1},
            }],
        }
        result = narrator.narrate(snapshot, trade_id="T-leg-1")
        self.assertEqual(result["source"], "deterministic")
        self.assertIn("OKX -> Kraken", result["text"])
        self.assertIn("55%", result["text"])
        self.assertIn("2.1", result["text"])

    def test_narrate_unknown_trade_id_falls_back_to_general(self):
        narrator = self._narrator()
        snapshot = {
            "mode": "demo",
            "risk": {"paused": False, "reason": "Healthy"},
            "models": {"cycleAlgo": "dfs", "slippageModel": "book_walk", "sizingMode": "fixed"},
            "scenarios": {"active": []},
            "metrics": {},
            "queuedOpportunities": [],
            "trades": [],
        }
        result = narrator.narrate(snapshot, trade_id="does-not-exist")
        self.assertEqual(result["source"], "deterministic")
        # Phrasing rotates between grounded variants; every idle variant explains
        # that visible spreads do not survive costs.
        self.assertIn("costs", result["text"].lower())

    def test_narrator_streams_deterministic_chunks(self):
        import asyncio

        narrator = self._narrator()
        snapshot = {
            "mode": "demo",
            "risk": {"paused": False, "reason": "Healthy"},
            "models": {"cycleAlgo": "dfs", "slippageModel": "book_walk", "sizingMode": "fixed"},
            "scenarios": {"active": []},
            "metrics": {},
            "queuedOpportunities": [{
                "strategy": "simple", "status": "profitable", "buyExchange": "OKX", "sellExchange": "Kraken",
                "netBps": 1.4, "confidence": 0.8, "reason": "Net edge cleared risk gates",
            }],
        }

        async def collect():
            events = []
            async for event in narrator.stream_async(snapshot):
                events.append(event)
            return events

        events = asyncio.run(collect())
        deltas = [event for event in events if event["type"] == "delta"]
        done = [event for event in events if event["type"] == "done"]
        self.assertTrue(deltas)
        self.assertEqual(done[-1]["source"], "deterministic")
        self.assertIn("OKX -> Kraken", "".join(event["text"] for event in deltas))


class SecurityTests(unittest.TestCase):
    def test_control_endpoints_require_token_when_configured(self):
        try:
            import fastapi  # noqa: F401
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("FastAPI test client is not installed in this local Python environment")
        from backend.app.core.config import settings as live_settings
        from backend.app.main import app

        client = TestClient(app)
        original = live_settings.control_token
        live_settings.control_token = "secret-token"
        try:
            denied = client.post("/api/control", json={"autoExecution": True})
            self.assertEqual(denied.status_code, 401)
            allowed = client.post("/api/control", json={"autoExecution": True}, headers={"x-aurelion-token": "secret-token"})
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(client.post("/api/params", json={"reset": True}).status_code, 401)
            self.assertEqual(client.post("/api/scenario", json={"scenario": "flash_crash"}).status_code, 401)
            # read-only endpoints stay open
            self.assertEqual(client.get("/api/snapshot").status_code, 200)
        finally:
            live_settings.control_token = original


class ExecutionGatewayTests(unittest.TestCase):
    def _book(self):
        okx = Settings().exchange_by_id("okx")
        return book(okx, "BTC/USDT", [(70000, 1)], [(69990, 1)])

    def test_paper_gateway_fills_from_book(self):
        from backend.app.integrations.gateways import ClientOrder, PaperExecutionGateway

        gateway = PaperExecutionGateway()
        fill = gateway.place_order(ClientOrder("c1", "okx", "BTC/USDT", "buy", 0.01), self._book())
        self.assertEqual(fill.status, "filled")
        self.assertAlmostEqual(fill.filled_qty, 0.01)
        self.assertGreater(fill.avg_price, 0)
        self.assertFalse(gateway.supports_withdrawal())

    def test_guard_rejects_oversized_and_killswitch(self):
        from backend.app.integrations.gateways import ClientOrder, PaperExecutionGateway, PreTradeGuard

        guard = PreTradeGuard(max_order_notional_usd=100)
        gateway = PaperExecutionGateway(guard)
        order = ClientOrder("c2", "okx", "BTC/USDT", "buy", 0.01)  # ~700 notional > 100
        self.assertEqual(gateway.place_order(order, self._book()).status, "rejected")
        guard.max_order_notional_usd = 0  # disable cap
        guard.kill_switch = True
        self.assertEqual(gateway.place_order(order, self._book()).status, "rejected")

    def test_live_gateway_is_a_disabled_stub(self):
        from backend.app.integrations.gateways import ClientOrder, LiveExecutionGateway

        gateway = LiveExecutionGateway()
        self.assertFalse(gateway.capabilities()["enabled"])
        self.assertFalse(gateway.supports_withdrawal())
        with self.assertRaises(NotImplementedError):
            gateway.place_order(ClientOrder("c3", "okx", "BTC/USDT", "buy", 0.01), self._book())

    def test_readonly_live_capabilities(self):
        from backend.app.integrations.gateways import ReadOnlyLiveGateway

        caps = ReadOnlyLiveGateway().capabilities()
        self.assertTrue(caps["live"])
        self.assertTrue(caps["readOnly"])
        self.assertEqual(caps["execution"], "paper")

    def test_mode_and_gateway_stay_unified(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))

        async def no_streams():
            return None

        service.start_streams = no_streams  # avoid real network in tests

        # Choosing a live-data gateway from demo pulls the mode to auto.
        asyncio.run(service.set_execution_gateway_unified("read-only-live"))
        self.assertEqual(service.gateway_mode, "read-only-live")
        self.assertEqual(service.mode, "auto")

        # Returning to demo resets the gateway to paper.
        asyncio.run(service.set_mode("demo"))
        self.assertEqual(service.gateway_mode, "paper")

        # Leaving demo upgrades a paper gateway to read-only-live automatically.
        asyncio.run(service.set_mode("auto"))
        self.assertEqual(service.gateway_mode, "read-only-live")

    def test_api_execution_status_and_killswitch(self):
        try:
            import fastapi  # noqa: F401
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("FastAPI test client is not installed in this local Python environment")
        from backend.app.main import app

        client = TestClient(app)
        status = client.get("/api/execution").json()
        self.assertEqual(status["mode"], "paper")
        self.assertIn("capabilities", status)
        self.assertFalse(status["supportsWithdrawal"])
        try:
            client.post("/api/control", json={"killSwitch": True})
            self.assertTrue(client.get("/api/execution").json()["guard"]["killSwitch"])
        finally:
            client.post("/api/control", json={"killSwitch": False, "executionGateway": "paper"})


class HistoricalBacktestTests(unittest.TestCase):
    def _fake_candles(self, base_price: float, count: int = 80, step: float = 0.0, volume_override: float = 12.0):
        from backend.app.integrations.historical_data import Candle

        rows = []
        price = base_price
        for i in range(count):
            price = max(0.000001, price + step)
            rows.append(Candle(timestamp=1700000000000 + i * 60000, open=price, high=price * 1.001, low=price * 0.999, close=price, volume=volume_override))
        return rows

    def test_historical_market_synthesizes_book_from_real_candle(self):
        from backend.app.engines.historical_replay import HistoricalMarket

        settings = Settings()
        okx = settings.exchange_by_id("okx")
        candles = {"okx:BTC/USDT": self._fake_candles(70000.0)}
        market = HistoricalMarket(settings.exchanges, candles)
        market.advance(settings.exchanges)
        result = market.generate(okx, settings.exchanges, okx.primary_symbol)
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "historical")
        ask = best(result.asks, "ask")
        bid = best(result.bids, "bid")
        self.assertGreater(ask.price, bid.price)
        self.assertAlmostEqual((ask.price + bid.price) / 2, 70000.0, delta=50)

    def test_historical_market_handles_sub_dollar_triangular_pairs(self):
        from backend.app.engines.historical_replay import HistoricalMarket

        settings = Settings()
        okx = settings.exchange_by_id("okx")
        market = HistoricalMarket(settings.exchanges, {
            "okx:ETH/BTC": self._fake_candles(0.052),
        })
        market.advance(settings.exchanges)
        book_result = market.generate(okx, settings.exchanges, "ETH/BTC")
        self.assertIsNotNone(book_result)
        self.assertFalse(book_result.primary)
        ask = best(book_result.asks, "ask")
        bid = best(book_result.bids, "bid")
        self.assertGreater(ask.price, bid.price)  # spread survives rounding at 8 decimals
        self.assertAlmostEqual((ask.price + bid.price) / 2, 0.052, delta=0.001)
        # No-history symbol still degrades to None
        self.assertIsNone(market.generate(okx, settings.exchanges, "SOL/ETH"))

    def test_triangular_engine_finds_cycle_on_real_history_books(self):
        from backend.app.engines.historical_replay import HistoricalMarket

        settings = Settings(market_mode="demo")
        okx = settings.exchange_by_id("okx")
        # Real-shaped series with a deliberate cross-rate dislocation:
        # ETH/USDT (6.0) rich vs ETH/BTC * BTC/USDT (0.05 * 100 = 5.0).
        market = HistoricalMarket(settings.exchanges, {
            "okx:BTC/USDT": self._fake_candles(100.0, volume_override=4800),
            "okx:ETH/BTC": self._fake_candles(0.05, volume_override=4800),
            "okx:ETH/USDT": self._fake_candles(6.0, volume_override=4800),
        })
        market.advance(settings.exchanges)
        books = {}
        for symbol in ("BTC/USDT", "ETH/BTC", "ETH/USDT"):
            generated = market.generate(okx, settings.exchanges, symbol)
            self.assertIsNotNone(generated)
            books[generated.key] = generated
        engine = TriangularArbitrageEngine(settings, WalletLedger(settings))
        opportunities = engine.scan(books)
        profitable = [o for o in opportunities if o["strategy"] == "triangular" and o["status"] == "profitable"]
        self.assertTrue(profitable, "expected a profitable triangular cycle on dislocated real-history books")

    def test_backtest_runs_over_injected_real_history(self):
        from backend.app.engines.backtest import BacktestRunner

        # Two venues with a persistent divergence: okx cheaper, kraken richer —
        # a real, reproducible arbitrage signal without hitting the network.
        def fake_provider(exchanges, timeframe, limit):
            candles = {}
            for exchange in exchanges:
                base = 69800.0 if exchange.id == "okx" else 70200.0
                candles[f"{exchange.id}:{exchange.primary_symbol}"] = self._fake_candles(base, count=80)
            return {"candles": candles, "statuses": {}}

        runner = BacktestRunner(Settings(market_mode="demo"), historical_provider=fake_provider)
        result = runner.run(ticks=60, regime="normal", source="historical")
        self.assertEqual(result["dataQuality"]["actual"], "historical")
        self.assertIn("okx", result["dataQuality"]["exchanges"])
        self.assertGreater(result["executed"], 0)

    def test_backtest_falls_back_to_simulated_without_real_coverage(self):
        from backend.app.engines.backtest import BacktestRunner

        def empty_provider(exchanges, timeframe, limit):
            return {"candles": {}, "statuses": {exchange.id: "unavailable" for exchange in exchanges}}

        runner = BacktestRunner(Settings(market_mode="demo"), historical_provider=empty_provider)
        result = runner.run(ticks=30, source="historical")
        self.assertEqual(result["dataQuality"]["actual"], "simulated-fallback")
        self.assertEqual(result["ticks"], 30)


class DecisionLatencyTests(unittest.TestCase):
    def test_latency_slo_reports_decision_latency_when_given(self):
        from backend.app.engines.edge_analysis import latency_slo

        books = [{"ageMs": 100, "latencyMs": 50}, {"ageMs": 200, "latencyMs": 80}]
        without = latency_slo(books)
        self.assertNotIn("decisionMs", without)
        with_samples = latency_slo(books, [0.4, 0.6, 0.5, 1.2])
        self.assertIn("decisionMs", with_samples)
        self.assertEqual(with_samples["decisionMs"]["samples"], 4)
        self.assertGreater(with_samples["decisionMs"]["p95"], 0)

    def test_market_service_tracks_decision_latency_per_tick(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        service.tick()
        service.tick()
        self.assertTrue(len(service.decision_latency_window) >= 1)
        snapshot = service.snapshot()
        self.assertIn("decisionMs", snapshot["latencySlo"])
        service.reset()
        self.assertEqual(service.decision_latency_window, [])


class WideNetRadarTests(unittest.TestCase):
    """Discovery lane: wide-universe scouting priced off batched tickers."""

    @staticmethod
    def _exchange(exchange_id, name, quote="USDT", fee_bps=10.0):
        from backend.app.core.config import ExchangeConfig

        return ExchangeConfig(
            exchange_id, name, exchange_id, f"BTC/{quote}",
            (f"BTC/{quote}", "ETH/BTC", f"ETH/{quote}"),
            fee_bps, 0.0, 0.0001, 1.0, 0.95,
        )

    @staticmethod
    def _ticker(exchange_id, symbol, bid, ask):
        from backend.app.integrations.market_scout import TickerQuote

        return TickerQuote(exchange_id, symbol, bid, ask, int(time.time() * 1000))

    def _settings(self, **overrides):
        alpha = self._exchange("alpha", "Alpha")
        beta = self._exchange("beta", "Beta")
        return Settings(exchange_universe=(alpha, beta), active_exchanges="alpha,beta", **overrides)

    def test_radar_finds_cross_exchange_dislocation_after_fees(self):
        from backend.app.engines.discovery import WideNetRadar

        def scout(exchanges):
            return {
                "quotes": {
                    "alpha": {"LTC/USDT": self._ticker("alpha", "LTC/USDT", 99.9, 100.0)},
                    "beta": {"LTC/USDT": self._ticker("beta", "LTC/USDT", 101.5, 101.6)},
                },
                "statuses": {"alpha": "live", "beta": "live"},
                "durationMs": 1.0,
            }

        radar = WideNetRadar(self._settings(), scout=scout)
        result = radar.sweep()
        top = result["topRoutes"][0]
        self.assertEqual(top["kind"], "cross")
        self.assertEqual(top["id"], "cross:LTC:alpha>beta")
        # gross = (101.5/100 - 1) * 1e4 = 150 bps; taker 10 bps per side -> net 130.
        self.assertAlmostEqual(top["netBps"], 130.0, delta=0.5)
        self.assertEqual(result["venuesLive"], 2)

    def test_radar_prices_ticker_triangular_cycle(self):
        from backend.app.engines.discovery import WideNetRadar

        def scout(exchanges):
            return {
                "quotes": {
                    "alpha": {
                        "BTC/USDT": self._ticker("alpha", "BTC/USDT", 50000.0, 50010.0),
                        "XRP/USDT": self._ticker("alpha", "XRP/USDT", 0.499, 0.50),
                        "XRP/BTC": self._ticker("alpha", "XRP/BTC", 0.0000102, 0.0000103),
                    },
                },
                "statuses": {"alpha": "live", "beta": "unavailable"},
                "durationMs": 1.0,
            }

        radar = WideNetRadar(self._settings(), scout=scout)
        result = radar.sweep()
        tri = [route for route in result["topRoutes"] if route["kind"] == "triangular"]
        self.assertTrue(tri, "expected a ticker-triangular route")
        forward = next(route for route in tri if route["id"] == "tri:alpha:USDT>XRP>BTC>USDT")
        # 1/0.50 * 0.0000102 * 50000 = 1.02 gross; 3 legs at 10 bps -> ~169 bps net.
        self.assertGreater(forward["netBps"], 150)
        self.assertLess(forward["netBps"], 200)

    def test_radar_persistence_streak_flags_promotable_and_resets(self):
        from backend.app.engines.discovery import WideNetRadar

        market = {"betaBid": 101.5}

        def scout(exchanges):
            return {
                "quotes": {
                    "alpha": {"LTC/USDT": self._ticker("alpha", "LTC/USDT", 99.9, 100.0)},
                    "beta": {"LTC/USDT": self._ticker("beta", "LTC/USDT", market["betaBid"], market["betaBid"] + 0.1)},
                },
                "statuses": {"alpha": "live", "beta": "live"},
                "durationMs": 1.0,
            }

        radar = WideNetRadar(self._settings(discovery_min_persistence=3), scout=scout)
        first = radar.sweep()["topRoutes"][0]
        self.assertEqual(first["streak"], 1)
        self.assertFalse(first["promotable"])
        radar.sweep()
        third = radar.sweep()["topRoutes"][0]
        self.assertEqual(third["streak"], 3)
        self.assertTrue(third["promotable"])
        # Edge disappears -> the streak breaks; when it returns it starts over.
        market["betaBid"] = 100.0
        radar.sweep()
        market["betaBid"] = 101.5
        again = radar.sweep()["topRoutes"][0]
        self.assertEqual(again["streak"], 1)
        self.assertFalse(again["promotable"])

    def test_radar_survives_empty_scout_and_reports_status(self):
        from backend.app.engines.discovery import WideNetRadar

        def scout(exchanges):
            return {"quotes": {}, "statuses": {"alpha": "unavailable", "beta": "unavailable"}, "durationMs": 5.0}

        radar = WideNetRadar(self._settings(), scout=scout)
        result = radar.sweep()
        self.assertEqual(result["venuesLive"], 0)
        self.assertEqual(result["topRoutes"], [])
        snapshot = radar.snapshot()
        self.assertEqual(snapshot["sweepCount"], 1)
        self.assertEqual(snapshot["promotableCount"], 0)

    def test_discovery_parameters_registered_and_snapshot_wired(self):
        from backend.app.core.config import PARAMETER_REGISTRY
        from backend.app.engines.market_service import MarketService

        keys = {spec.key for spec in PARAMETER_REGISTRY}
        self.assertTrue({"discovery_enabled", "discovery_interval_ms", "discovery_min_persistence", "discovery_min_net_bps"} <= keys)
        service = MarketService(Settings(market_mode="demo"))
        service.tick()
        snapshot = service.snapshot()
        self.assertIn("discovery", snapshot)
        self.assertEqual(snapshot["discovery"]["sweepCount"], 0)
        self.assertEqual(snapshot["discovery"]["universeCount"], 10)


class ResearchLabTests(unittest.TestCase):
    """Spread-dynamics fitting (OU/AR(1)) and the parameter trainer."""

    def test_ar1_fit_recovers_known_ou_parameters(self):
        import math
        import random

        from backend.app.engines.spread_model import fit_ar1

        rng = random.Random(11)
        phi_true, mu_true, dt = 0.9, 5.0, 60000.0
        series = [mu_true]
        for _ in range(360):
            series.append(mu_true * (1 - phi_true) + phi_true * series[-1] + rng.gauss(0, 0.5))
        fit = fit_ar1(series, dt)
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit["phi"], phi_true, delta=0.06)
        self.assertAlmostEqual(fit["meanBps"], mu_true, delta=0.6)
        expected_half_life = -dt * math.log(2) / math.log(phi_true)
        self.assertLess(abs(fit["halfLifeMs"] - expected_half_life) / expected_half_life, 0.65)

    def test_episode_scanner_counts_runs_and_durations(self):
        from backend.app.engines.spread_model import scan_episodes

        series = [0.0] * 10 + [15.0] * 3 + [0.0] * 5 + [12.0] + [0.0] * 10
        result = scan_episodes(series, mean=0.0, threshold=10.0, dt_ms=60000)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["medianDurationMs"], 3 * 60000)
        self.assertEqual(result["vanishedWithinOneSamplePct"], 50.0)
        self.assertAlmostEqual(result["peakExcessBps"], 5.0, delta=0.01)

    def test_spread_lab_finds_executable_episode_with_injected_history(self):
        import random

        from backend.app.core.config import ExchangeConfig
        from backend.app.engines.spread_model import SpreadDynamicsLab
        from backend.app.integrations.historical_data import Candle

        alpha = ExchangeConfig("alpha", "Alpha", "alpha", "BTC/USDT", ("BTC/USDT",), 10, 0.0, 0.0001, 1.0, 0.95)
        beta = ExchangeConfig("beta", "Beta", "beta", "BTC/USDT", ("BTC/USDT",), 10, 0.0, 0.0001, 1.0, 0.95)
        settings = Settings(exchange_universe=(alpha, beta), active_exchanges="alpha,beta")

        rng = random.Random(5)
        spread_bps = [0.0]
        for _ in range(150):
            spread_bps.append(0.85 * spread_bps[-1] + rng.gauss(0, 1.2))
        for i in range(120, 124):  # injected dislocation clearing the 20 bps fee wall
            spread_bps[i] = 40.0

        def provider(exchanges, timeframe, limit, use_cache=True):
            base_ts = 1_780_000_000_000
            alpha_candles = []
            beta_candles = []
            for i, s in enumerate(spread_bps):
                ts = base_ts + i * 60000
                price_a = 50000.0
                price_b = price_a * (1 - s / 10000)
                alpha_candles.append(Candle(ts, price_a, price_a, price_a, price_a, 5.0))
                beta_candles.append(Candle(ts, price_b, price_b, price_b, price_b, 5.0))
            return {"candles": {"alpha:BTC/USDT": alpha_candles, "beta:BTC/USDT": beta_candles}, "statuses": {}}

        lab = SpreadDynamicsLab(settings, provider=provider)
        study = lab.study()
        self.assertEqual(study["pairsFitted"], 1)
        pair = study["pairs"][0]
        self.assertTrue(pair["fitted"])
        self.assertGreaterEqual(pair["executable"]["count"], 1)
        self.assertIn("fee wall", pair["verdict"])
        self.assertTrue(study["summary"]["capturableNow"])

    def test_parameter_trainer_is_deterministic_and_respects_bounds(self):
        from backend.app.engines.autotune import ParameterTrainer

        first = ParameterTrainer(Settings(market_mode="demo")).train(trials=4, ticks=30, regime="calm", seed=3)
        second = ParameterTrainer(Settings(market_mode="demo")).train(trials=4, ticks=30, regime="calm", seed=3)
        self.assertEqual(first["best"]["score"], second["best"]["score"])
        self.assertEqual(first["best"]["params"], second["best"]["params"])
        self.assertEqual(len(first["leaderboard"]), 3)
        trainer = ParameterTrainer(Settings(market_mode="demo"))
        for row in first["leaderboard"]:
            for spec in trainer.specs:
                value = row["params"].get(spec.key)
                if value is None:
                    continue
                if spec.kind == "choice":
                    self.assertIn(value, spec.options)
                else:
                    self.assertGreaterEqual(value, spec.minimum)
                    self.assertLessEqual(value, spec.maximum)
        self.assertIn("baseline", first)
        self.assertIsInstance(first["improvedVsBaseline"], bool)

    def test_simulated_market_seed_changes_realization(self):
        settings = Settings(market_mode="demo")
        first = SimulatedMarket(settings.exchanges, seed=1)
        second = SimulatedMarket(settings.exchanges, seed=2)
        exchange = settings.exchanges[0]
        first.advance(settings.exchanges)
        second.advance(settings.exchanges)
        book_a = first.generate(exchange, settings.exchanges, exchange.primary_symbol)
        book_b = second.generate(exchange, settings.exchanges, exchange.primary_symbol)
        self.assertNotEqual(book_a.asks[0].price, book_b.asks[0].price)

    def test_backtest_default_is_deterministic_and_market_seed_varies_it(self):
        from backend.app.engines.backtest import BacktestRunner

        base_a = BacktestRunner(Settings(market_mode="demo")).run(60, "normal")
        base_b = BacktestRunner(Settings(market_mode="demo")).run(60, "normal")
        self.assertEqual(base_a["equityCurve"], base_b["equityCurve"])
        seeded = BacktestRunner(Settings(market_mode="demo")).run(60, "normal", market_seed=104729)
        self.assertNotEqual(base_a["equityCurve"], seeded["equityCurve"])

    def test_trainer_validation_selects_by_out_of_sample_score(self):
        from backend.app.engines.autotune import ParameterTrainer

        result = ParameterTrainer(Settings(market_mode="demo")).train(trials=4, ticks=30, regime="calm", seed=3)
        self.assertIn("validationSeed", result)
        self.assertIsInstance(result["baseline"]["validationScore"], float)
        validated = [row for row in result["leaderboard"] if row["validationScore"] is not None]
        self.assertTrue(validated, "top candidates must be validated out-of-sample")
        best_validation = max(row["validationScore"] for row in validated)
        self.assertEqual(result["best"]["validationScore"], best_validation)
        for row in validated:
            self.assertIsInstance(row["overfitGap"], float)

    def test_trainer_robust_mode_scores_across_regimes(self):
        from backend.app.engines.autotune import ParameterTrainer

        result = ParameterTrainer(Settings(market_mode="demo")).train(trials=2, ticks=25, robust=True, seed=1)
        self.assertTrue(result["robust"])
        self.assertEqual(result["regimes"], ["normal", "volatile", "stressed"])
        self.assertEqual(set(result["baseline"]["perRegime"].keys()), {"normal", "volatile", "stressed"})
        self.assertIn("worstRegimeScore", result["baseline"])

    def test_research_store_roundtrip(self):
        import tempfile
        from pathlib import Path

        from backend.app.integrations import research_store

        payload = {
            "generatedAt": 1234567890123,
            "baseline": {"score": 1.0, "validationScore": 0.8},
            "best": {"score": 2.0, "validationScore": 1.5},
            "improvedVsBaseline": True,
            "robust": False,
            "source": "simulated",
            "regime": "normal",
        }
        original = research_store.RESEARCH_DIR
        with tempfile.TemporaryDirectory() as tmp:
            research_store.RESEARCH_DIR = Path(tmp)
            try:
                name = research_store.save_research("autotune", payload)
                self.assertIsNotNone(name)
                sessions = research_store.load_research()
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["kind"], "autotune")
                self.assertIn("score", sessions[0]["headline"])
                self.assertEqual(sessions[0]["payload"]["best"]["validationScore"], 1.5)
            finally:
                research_store.RESEARCH_DIR = original

    def test_judge_report_contains_key_sections(self):
        from backend.app.engines.report import build_report_html

        snapshot = {
            "mode": "demo",
            "uptimeMs": 120000,
            "metrics": {"cumulativePnl": 1.23, "executedCount": 3, "winRate": 0.5, "detectedCount": 10},
            "latencySlo": {"decisionMs": {"p50": 4.0, "p95": 8.0}},
            "risk": {"paused": False},
            "models": {"cycleAlgo": "bellman_ford", "slippageModel": "sqrt_impact", "sizingMode": "kelly", "volatilityModel": "ewma", "calibrationEnabled": True},
            "exchangeCoverage": {"activeCount": 5, "universeCount": 10},
            "discovery": {
                "universeCount": 10, "bases": ["BTC", "LTC"],
                "lastSweep": {"venuesLive": 8, "seriesCount": 60, "routesPriced": 300, "topRoutes": [
                    {"route": "KuCoin -> Bybit", "kind": "cross", "base": "LTC", "grossBps": 2.1, "costsBps": 23.4, "netBps": -21.3},
                ]},
            },
            "pnlSeries": [{"time": 1, "pnl": 0.0}, {"time": 2, "pnl": 0.6}, {"time": 3, "pnl": 1.23}],
        }
        research = [{
            "kind": "autotune", "generatedAt": 1234567890123, "headline": "normal / simulated: score 1.0 -> 1.5 (validation), improved",
            "payload": {"baseline": {"score": 1.0, "validationScore": 0.8}, "best": {"score": 2.0, "validationScore": 1.5, "changedVsCurrent": {"min_net_bps": {"from": 0.75, "to": 4.2}}}},
        }]
        report = build_report_html(snapshot, research)
        self.assertIn("Reporte para el jurado", report)
        self.assertIn("bellman_ford", report)
        self.assertIn("KuCoin -&gt; Bybit", report)
        self.assertIn("polyline", report)
        self.assertIn("validación", report)
        self.assertIn("paper trading", report)


class RobustnessTests(unittest.TestCase):
    """The engine cannot die: watchdog containment, live-feed sanitation,
    fuzzed inputs and full-service chaos."""

    def test_watchdog_contains_faults_and_fail_safe_pauses_after_three(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        service.safe_tick()
        self.assertEqual(service.tick_errors, 0)
        for _ in range(3):
            service._fault_ticks += 1
            service.safe_tick()
        self.assertEqual(service.tick_errors, 3)
        self.assertEqual(service.consecutive_tick_errors, 3)
        self.assertIn("Injected engine fault", service.last_tick_error)
        self.assertTrue(service.risk.snapshot(int(time.time() * 1000))["paused"])
        # The engine is still alive: the next (paused) tick runs cleanly.
        service.safe_tick()
        self.assertEqual(service.consecutive_tick_errors, 0)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["engineHealth"]["tickErrors"], 3)
        self.assertEqual(snapshot["engineHealth"]["watchdog"], "armed")
        self.assertIn("engine_fault", snapshot["scenarios"]["available"])

    def test_feed_guard_blocks_poisoned_books_from_live_path(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        exchange = service.settings.exchanges[0]
        symbol = exchange.primary_symbol
        clean = book(exchange, symbol, [(70010, 1)], [(69990, 1)])
        service.handle_book(clean)
        self.assertIs(service.books[clean.key], clean)

        service.handle_book(book(exchange, symbol, [(float("nan"), 1)], [(69990, 1)]))
        service.handle_book(book(exchange, symbol, [(70010, -5)], [(69990, 1)]))
        service.handle_book(book(exchange, symbol, [(70000, 1)], [(75000, 1)]))       # crossed
        service.handle_book(book(exchange, symbol, [(80010, 1)], [(79990, 1)]))       # +14% fat finger
        guard = service.feed_guard.snapshot()
        self.assertEqual(guard["rejectedCount"], 4)
        self.assertIs(service.books[clean.key], clean, "poisoned updates must never replace clean data")
        reasons = " | ".join(guard["byReason"].keys())
        self.assertIn("finite", reasons)
        self.assertIn("quantity", reasons)
        self.assertIn("crossed", reasons)
        self.assertIn("jumped", reasons)

        moved = book(exchange, symbol, [(70210, 1)], [(70190, 1)])                     # +0.3%: fine
        service.handle_book(moved)
        self.assertIs(service.books[clean.key], moved)

    def test_fuzzed_books_never_break_engines_or_produce_nan(self):
        import math
        import random

        from backend.app.engines.market_service import MarketService

        rng = random.Random(4242)
        service = MarketService(Settings(market_mode="demo"))
        venues = service.settings.exchanges[:3]
        poisons = ("nan", "inf", "negative", "crossed", "jump")
        numeric_keys = ("netBps", "netProfit", "grossProfit", "expectedValue", "evBps", "qtyBtc", "score")
        for iteration in range(250):
            for exchange in venues:
                mid = 70000 * (1 + rng.uniform(-0.03, 0.03))
                spread = mid * rng.uniform(0.00001, 0.002)
                depth = rng.randint(1, 20)
                asks = [(mid + spread / 2 + i * rng.uniform(0.01, 9), rng.uniform(1e-6, 50)) for i in range(depth)]
                bids = [(mid - spread / 2 - i * rng.uniform(0.01, 9), rng.uniform(1e-6, 50)) for i in range(depth)]
                if rng.random() < 0.12:
                    poison = rng.choice(poisons)
                    if poison == "nan":
                        asks[0] = (float("nan"), 1.0)
                    elif poison == "inf":
                        bids[0] = (float("inf"), 1.0)
                    elif poison == "negative":
                        asks[0] = (-abs(asks[0][0]), 1.0)
                    elif poison == "crossed":
                        bids[0] = (asks[0][0] * 1.2, 1.0)
                    else:
                        asks = [(price * 1.4, qty) for price, qty in asks]
                        bids = [(price * 1.4, qty) for price, qty in bids]
                service.handle_book(book(exchange, exchange.primary_symbol, asks, bids))
            if iteration % 5 == 0:
                primary = {item.exchange_id: item for item in service.books.values() if item.primary and item.asks and item.bids}
                opportunities = service.cross_engine.scan(primary) + service.triangular_engine.scan(service.books)
                for opportunity in opportunities:
                    payload = opportunity.to_dict() if hasattr(opportunity, "to_dict") else opportunity
                    for key in numeric_keys:
                        value = payload.get(key)
                        if isinstance(value, float):
                            self.assertTrue(math.isfinite(value), f"{key} must stay finite, got {value}")
        self.assertGreater(service.feed_guard.rejected_count, 0, "fuzz must have exercised the guard")

    def test_chaos_service_survives_scenarios_params_and_faults(self):
        import json as json_module
        import random

        from backend.app.core.config import PARAMETER_REGISTRY, apply_parameter_updates
        from backend.app.engines.market_service import MarketService

        rng = random.Random(99)
        service = MarketService(Settings(market_mode="demo"))
        numeric = [spec for spec in PARAMETER_REGISTRY if spec.kind in ("float", "int") and spec.group in ("execution", "ev", "risk", "triangular", "cadence")]
        choices = [spec for spec in PARAMETER_REGISTRY if spec.kind == "choice"]
        injected_faults = 0
        for i in range(140):
            if i % 23 == 7:
                service.simulator.inject_scenario(rng.choice(list(service.simulator.SCENARIOS)), service.settings.exchanges)
            if i % 31 == 11:
                service._fault_ticks += 1
                injected_faults += 1
            if i % 9 == 4:
                spec = rng.choice(numeric)
                apply_parameter_updates(service.settings, {spec.key: rng.uniform(spec.minimum, spec.maximum)})
            if i % 17 == 6:
                spec = rng.choice(choices)
                apply_parameter_updates(service.settings, {spec.key: rng.choice(spec.options)})
            if i % 13 == 3:
                service.pre_trade_guard.kill_switch = not service.pre_trade_guard.kill_switch
            service.safe_tick()
        self.assertEqual(service.tick_errors, injected_faults, "only the deliberately injected faults may fail ticks")
        snapshot = service.snapshot()
        json_module.dumps(snapshot, allow_nan=False)  # no NaN/inf anywhere in the payload
        self.assertEqual(snapshot["engineHealth"]["watchdog"], "armed")
        self.assertGreaterEqual(snapshot["engineHealth"]["tickCount"], 140)


class AccountingIntegrityTests(unittest.TestCase):
    """Net-profit accuracy, proven by invariant: the books must balance."""

    def test_pnl_conservation_and_wallet_sanity_over_long_demo_run(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        for _ in range(150):
            service.safe_tick()
        self.assertEqual(service.tick_errors, 0)
        trades = service.store.latest_trades(10_000)
        self.assertGreater(len(trades), 0, "a 150-tick demo session must execute trades")
        # Conservation: realized P&L equals the sum of every trade's net profit
        # (partials, leg-failure cover costs and rebalances included), and the
        # cumulative P&L series ends at exactly the same number.
        total = sum(float(trade["netProfit"]) for trade in trades)
        self.assertAlmostEqual(total, service.ledger.realized_pnl, places=6)
        self.assertAlmostEqual(service.store.pnl_series[-1]["pnl"], service.ledger.realized_pnl, places=6)
        for trade in trades:
            ratio = trade.get("filledRatio")
            if ratio is not None:
                self.assertGreaterEqual(ratio, 0.0)
                self.assertLessEqual(ratio, 1.000001)
        # Paper wallets can never go negative in any asset on any venue.
        for wallet in service.ledger.active(service.settings.exchanges):
            for asset in ("USDT", "BTC", "ETH"):
                self.assertGreaterEqual(wallet[asset], -1e-9, f"negative {asset} on {wallet['exchangeId']}")

    def test_stage_latency_breakdown_is_recorded(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        for _ in range(3):
            service.safe_tick()
        slo = service.snapshot()["latencySlo"]
        self.assertIn("stages", slo)
        for stage in ("ingest", "riskGate", "scan", "rank", "execute", "publish"):
            self.assertIn(stage, slo["stages"])
            self.assertGreaterEqual(slo["stages"][stage]["p95"], 0.0)


class ContinuityAndControlSurfaceTests(unittest.TestCase):
    """Durable cross-session lineage + rate-limited control surface."""

    def test_session_lineage_survives_restart(self):
        import os
        import tempfile

        from backend.app.integrations.persistence import DurableEventSink

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "lineage.db")
            first = DurableEventSink(Settings(sqlite_path=db, market_mode="demo"))
            first.append("trade", {"trade": {"id": "T1"}, "cumulativePnl": 12.5})
            first.append("trade", {"trade": {"id": "T2"}, "cumulativePnl": 20.75})
            first.close()
            second = DurableEventSink(Settings(sqlite_path=db, market_mode="demo"))
            second.append("risk-event", {"marker": 1})
            lineage = second.session_lineage()
            second.close()
            self.assertEqual(len(lineage), 2)
            prior = next(session for session in lineage if not session["current"])
            current = next(session for session in lineage if session["current"])
            self.assertEqual(prior["trades"], 2)
            self.assertEqual(prior["finalPnl"], 20.75)
            self.assertEqual(current["trades"], 0)

    def test_snapshot_includes_continuity_block(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        block = service.snapshot()["continuity"]
        self.assertIn("priorSessions", block)
        refreshed = service.refresh_continuity()
        self.assertIn("sessions", refreshed)
        self.assertIn("driver", refreshed)

    def test_rate_limit_trips_on_control_surface_flood(self):
        from fastapi.testclient import TestClient

        from backend.app import main as main_module

        client = TestClient(main_module.app)
        original = main_module.settings.control_rate_limit
        main_module._RATE_BUCKETS.clear()
        main_module.settings.control_rate_limit = 3
        try:
            codes = [client.post("/api/scenario", json={"scenario": ""}).status_code for _ in range(5)]
        finally:
            main_module.settings.control_rate_limit = original
            main_module._RATE_BUCKETS.clear()
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3:], [429, 429])


class MultiAssetLedgerTests(unittest.TestCase):
    """Generalized asset model: alts are first-class, demo stays BTC-identical."""

    def test_wallets_carry_all_ledger_assets(self):
        from backend.app.core.config import LEDGER_ASSETS

        ledger = WalletLedger(Settings(market_mode="demo"))
        wallet = ledger.all()[0]
        for asset in LEDGER_ASSETS:
            self.assertIn(asset, wallet)
        # Alts seed at zero in demo, so demo balances/P&L are unchanged.
        for asset in ("XRP", "LTC", "SOL", "AVAX"):
            self.assertEqual(wallet[asset], 0.0)

    def test_alt_cross_trade_moves_the_right_asset_and_conserves_pnl(self):
        settings = Settings(market_mode="demo", starting_alt_balances={"XRP": 10000.0})
        ledger = WalletLedger(settings)
        a, b = settings.exchanges[0].id, settings.exchanges[1].id
        before_xrp = float(ledger.get(a)["XRP"]) + float(ledger.get(b)["XRP"])
        before_pnl = ledger.realized_pnl
        trade = {
            "strategy": "simple", "baseAsset": "XRP",
            "buyExchangeId": a, "sellExchangeId": b,
            "buyExchange": "A", "sellExchange": "B",
            "qtyBtc": 500.0,  # base-asset qty (field name kept for compatibility)
            "buyQuote": 275.0, "sellQuote": 280.0,
            "buyFee": 0.3, "sellFee": 0.3, "slippageCostBuy": 0.1, "slippageCostSell": 0.1,
            "latencyRiskCost": 0.05, "rebalanceCost": 0.0, "adverseMoveCost": 0.0,
            "netProfit": 3.85,
        }
        ledger.apply_trade(trade)
        # XRP is conserved across venues (buy leg gains what sell leg loses).
        after_xrp = float(ledger.get(a)["XRP"]) + float(ledger.get(b)["XRP"])
        self.assertAlmostEqual(after_xrp, before_xrp, places=6)
        self.assertGreater(float(ledger.get(a)["XRP"]), 10000.0)  # buy venue accumulated XRP
        self.assertLess(float(ledger.get(b)["XRP"]), 10000.0)     # sell venue released XRP
        # BTC untouched by an XRP trade.
        self.assertEqual(float(ledger.get(a)["BTC"]), settings.starting_btc)
        self.assertAlmostEqual(ledger.realized_pnl - before_pnl, 3.85, places=6)

    def test_totals_price_alts_from_book_and_stay_finite(self):
        import json as json_module

        settings = Settings(market_mode="demo", starting_alt_balances={"SOL": 100.0})
        ledger = WalletLedger(settings)
        totals = ledger.totals(70000.0, settings.exchanges, eth_mark_price=3600.0, asset_prices={"SOL": 150.0})
        self.assertIn("SOL", totals["exposure"])
        self.assertAlmostEqual(totals["exposure"]["SOL"]["usd"], 100.0 * 150.0 * len(settings.exchanges), places=2)
        json_module.dumps(totals, allow_nan=False)


class GatewayRoutedExecutionTests(unittest.TestCase):
    """The trade loop settles through the ExecutionGateway seam, not around it."""

    def _run_until_trade(self, service, max_ticks=400):
        for _ in range(max_ticks):
            service.safe_tick()
            if service.store.latest_trades(1):
                return service.store.latest_trades(1)[0]
        return None

    def test_paper_trades_carry_gateway_provenance_and_orders(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        trade = self._run_until_trade(service)
        self.assertIsNotNone(trade, "demo must execute a trade within the window")
        self.assertEqual(trade["gateway"], "paper")
        self.assertEqual(trade["execution"], "paper")
        self.assertTrue(trade.get("orders"), "the order lifecycle must be attached")

    def test_gateway_that_rejects_settlement_blocks_the_trade(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))

        class RejectingGateway:
            name = "reject"
            def settle_trade(self, trade, opportunity, book_map):
                return None

        service.executor.gateway = RejectingGateway()
        for _ in range(200):
            service.safe_tick()
        self.assertEqual(service.store.executed_count, 0, "no trade may settle when the gateway rejects")
        self.assertEqual(service.ledger.realized_pnl, 0.0)

    def test_set_execution_gateway_keeps_executor_in_sync(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        service.set_execution_gateway("read-only-live")
        self.assertIs(service.executor.gateway, service.gateway)
        self.assertEqual(service.gateway.name, "read-only-live")


class LiveAltUniverseTests(unittest.TestCase):
    """Cross-exchange engine trades the alt universe; demo stays BTC-only."""

    def test_cross_engine_finds_xrp_dislocation_and_tags_base_asset(self):
        from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine

        settings = Settings(active_exchanges="okx,bybit", min_net_bps=0.1, min_net_profit_usd=0.05,
                            min_confidence=0.1, max_trade_btc=0.05, min_trade_btc=0.001,
                            starting_alt_balances={"XRP": 20000.0})
        ledger = WalletLedger(settings)
        a, b = settings.exchanges[0], settings.exchanges[1]
        # XRP cheaper on A, richer on B -> a real cross-exchange edge.
        books = {
            f"{a.id}:XRP/USDT": book(a, "XRP/USDT", [(0.500, 40000)], [(0.499, 40000)]),
            f"{b.id}:XRP/USDT": book(b, "XRP/USDT", [(0.520, 40000)], [(0.508, 40000)]),
        }
        engine = CrossExchangeArbitrageEngine(settings, ledger)
        opps = engine.scan(books)
        profitable = [o for o in opps if o["status"] == "profitable"]
        self.assertTrue(profitable, "expected a profitable XRP cross-exchange opportunity")
        self.assertEqual(profitable[0]["baseAsset"], "XRP")
        self.assertEqual(profitable[0]["buyExchangeId"], a.id)  # bought where cheap

    def test_cross_engine_never_pairs_across_different_bases(self):
        from backend.app.engines.arbitrage import CrossExchangeArbitrageEngine

        settings = Settings(active_exchanges="okx,bybit", min_net_bps=0.1, min_net_profit_usd=0.05, min_confidence=0.1)
        ledger = WalletLedger(settings)
        a, b = settings.exchanges[0], settings.exchanges[1]
        # A huge apparent 'edge' only if you (wrongly) compare SOL ask to BTC bid.
        books = {
            f"{a.id}:SOL/USDT": book(a, "SOL/USDT", [(150.0, 500)], [(149.5, 500)]),
            f"{b.id}:BTC/USDT": book(b, "BTC/USDT", [(70000, 2)], [(69950, 2)]),
        }
        engine = CrossExchangeArbitrageEngine(settings, ledger)
        opps = engine.scan(books)
        # Different bases and only one venue each -> no valid cross pair at all.
        self.assertEqual(opps, [])

    def test_demo_cross_scan_input_stays_btc_only(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        service.tick()
        adjusted_primary = service.health_adjusted_books(service.primary_books())
        cross_input = service.cross_scan_input(adjusted_primary, service.health_adjusted_book_map())
        bases = {b.symbol.split("/")[0] for b in cross_input.values()}
        self.assertEqual(bases, {"BTC"}, "demo cross scan must remain BTC-only")


class LiveExecutionRealismTests(unittest.TestCase):
    """Realized-vs-detected edge capture + per-venue latency percentiles."""

    def test_edge_capture_metric_reported_and_finite(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        for _ in range(200):
            service.safe_tick()
        metrics = service.snapshot()["metrics"]
        for key in ("detectedEdgeBps", "realizedEdgeBps", "edgeCaptureRatio"):
            self.assertIn(key, metrics)
            self.assertEqual(metrics[key], metrics[key])  # not NaN
        if service.store.executed_count:
            # Realized edge cannot exceed detected edge (costs only subtract).
            self.assertLessEqual(metrics["realizedEdgeBps"], metrics["detectedEdgeBps"] + 1e-6)

    def test_provider_reports_per_stream_latency_percentiles(self):
        from backend.app.integrations.ccxt_provider import CcxtStreamProvider

        provider = CcxtStreamProvider(Settings(market_mode="auto"), lambda b: None, None)
        exchange = Settings(market_mode="auto").exchanges[0]
        state = provider.state(exchange, exchange.primary_symbol)
        for latency in (10, 20, 30, 40, 200):
            provider.mark_success(state, latency, "healthy")
        snap = provider.snapshot()
        stream = snap["streams"][0]
        self.assertIn("latencyP50Ms", stream)
        self.assertIn("latencyP95Ms", stream)
        self.assertGreaterEqual(stream["latencyP95Ms"], stream["latencyP50Ms"])


class ObservationRecorderTests(unittest.TestCase):
    """Live observation: per-route frequency, capturable rate, episode length."""

    def test_records_capturable_episodes_and_frequency(self):
        from backend.app.engines.observation import ObservationRecorder

        rec = ObservationRecorder(Settings(market_mode="auto"))
        prof = {"strategy": "simple", "status": "profitable", "netBps": 6.0, "baseAsset": "XRP",
                "buyExchange": "OKX", "sellExchange": "Bybit"}
        rej = {"strategy": "simple", "status": "rejected", "netBps": -3.0, "baseAsset": "XRP",
               "buyExchange": "OKX", "sellExchange": "Bybit"}
        # 3 consecutive capturable samples, then one rejected -> episode of 3.
        for _ in range(3):
            rec.observe([prof], "auto", False)
        rec.observe([rej], "auto", False)
        snap = rec.snapshot()
        self.assertTrue(snap["recording"])
        self.assertEqual(snap["samples"], 4)
        top = snap["topRoutes"][0]
        self.assertEqual(top["seen"], 4)
        self.assertEqual(top["capturable"], 3)
        self.assertEqual(top["maxEpisodeSamples"], 3)
        self.assertAlmostEqual(top["capturableRate"], 0.75, places=3)
        self.assertEqual(snap["capturableRoutes"], 1)

    def test_demo_is_never_recorded(self):
        from backend.app.engines.observation import ObservationRecorder

        rec = ObservationRecorder(Settings(market_mode="demo"))
        opp = {"strategy": "simple", "status": "profitable", "netBps": 5.0, "baseAsset": "BTC",
               "buyExchange": "OKX", "sellExchange": "Bybit"}
        for _ in range(10):
            rec.observe([opp], "demo", False)
        snap = rec.snapshot()
        self.assertFalse(snap["recording"])
        self.assertEqual(snap["samples"], 0)
        self.assertEqual(snap["routesObserved"], 0)

    def test_snapshot_wired_into_service(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        service.tick()
        self.assertIn("observation", service.snapshot())


class TestnetGatewayTests(unittest.TestCase):
    """Real order lifecycle on a MOCK sandbox exchange: fills, partials, rejects,
    safety caps, and the withdrawal-scope refusal — all without real keys."""

    def setUp(self):
        import os

        self._prev_enable = os.environ.get("AURELION_ENABLE_LIVE")

    def tearDown(self):
        import os

        if self._prev_enable is None:
            os.environ.pop("AURELION_ENABLE_LIVE", None)
        else:
            os.environ["AURELION_ENABLE_LIVE"] = self._prev_enable

    class _MockClient:
        def __init__(self, fill_ratio=1.0, reject=False):
            self.fill_ratio = fill_ratio
            self.reject = reject
            self.orders = []
            self.sandbox = False
        def set_sandbox_mode(self, on):
            self.sandbox = on
        def create_order(self, symbol, type_, side, amount, price, params):
            self.orders.append((symbol, side, amount, price, params))
            filled = 0.0 if self.reject else amount * self.fill_ratio
            return {"id": f"OID-{len(self.orders)}", "filled": filled, "average": price, "status": "closed"}

    def _gateway(self, **kw):
        import os
        from backend.app.integrations.gateways import PreTradeGuard, TestnetExecutionGateway

        os.environ["AURELION_ENABLE_LIVE"] = "1"
        clients = {}
        def factory(ccxt_id):
            return clients.setdefault(ccxt_id, self._MockClient(**kw))
        gw = TestnetExecutionGateway(PreTradeGuard(max_order_notional_usd=1e9), client_factory=factory)
        return gw

    def _trade(self, qty=100.0):
        return {"id": "T-1", "strategy": "simple", "baseAsset": "XRP",
                "buyExchangeId": "okx", "sellExchangeId": "bybit",
                "qtyBtc": qty, "buyPrice": 0.50, "sellPrice": 0.51, "netProfit": 1.0,
                "filledRatio": 1.0, "partial": False, "status": "filled"}

    def test_full_fill_places_real_orders_and_records_ids(self):
        gw = self._gateway(fill_ratio=1.0)
        settled = gw.settle_trade(self._trade(), {}, {})
        self.assertIsNotNone(settled)
        self.assertEqual(settled["gateway"], "testnet")
        self.assertEqual(settled["execution"], "testnet-sandbox")
        self.assertEqual(len(settled["orders"]), 2)
        self.assertTrue(all(o["venueOrderId"] for o in settled["orders"]))
        self.assertEqual(gw.orders_placed, 2)

    def test_partial_fill_scales_qty_and_pnl(self):
        gw = self._gateway(fill_ratio=0.5)
        settled = gw.settle_trade(self._trade(qty=100.0), {}, {})
        self.assertAlmostEqual(settled["qtyBtc"], 50.0, places=6)
        self.assertAlmostEqual(settled["filledRatio"], 0.5, places=4)
        self.assertTrue(settled["partial"])
        self.assertAlmostEqual(settled["netProfit"], 0.5, places=6)  # 1.0 * 0.5

    def test_rejected_leg_books_no_trade(self):
        gw = self._gateway(reject=True)
        self.assertIsNone(gw.settle_trade(self._trade(), {}, {}))
        self.assertIn("rejected", gw.last_error)

    def test_disabled_without_enable_flag(self):
        import os
        from backend.app.integrations.gateways import TestnetExecutionGateway

        os.environ.pop("AURELION_ENABLE_LIVE", None)
        gw = TestnetExecutionGateway(client_factory=lambda x: self._MockClient())
        self.assertFalse(gw.enabled)
        self.assertIsNone(gw.settle_trade(self._trade(), {}, {}))

    def test_notional_cap_blocks_oversized_order(self):
        from backend.app.integrations.gateways import ClientOrder, PreTradeGuard

        guard = PreTradeGuard(max_order_notional_usd=100.0, asset_caps={"XRP": 20.0})
        # 100 XRP * $0.50 = $50 notional, but the XRP asset cap is $20.
        order = ClientOrder("c1", "okx", "XRP/USDT", "buy", 100.0, 0.50)
        allowed, reason = guard.check(order, 0.50)
        self.assertFalse(allowed)
        self.assertIn("cap", reason)

    def test_testnet_gateway_never_supports_withdrawal(self):
        gw = self._gateway()
        self.assertFalse(gw.supports_withdrawal())

    def test_gateway_modes_include_testnet_and_build(self):
        from backend.app.integrations.gateways import GATEWAY_MODES, PreTradeGuard, build_gateway

        self.assertIn("testnet", GATEWAY_MODES)
        gw = build_gateway("testnet", PreTradeGuard())
        self.assertEqual(gw.name, "testnet")
        self.assertFalse(gw.supports_withdrawal())


class LiveSafetyHardeningTests(unittest.TestCase):
    """Max-open-exposure halt + testnet settlement never breaks the loop."""

    def test_open_exposure_zero_for_hedged_book(self):
        service = WalletLedger(Settings(market_mode="demo"))
        self.assertEqual(service.open_exposure_usd(), 0.0)

    def test_exposure_halt_blocks_execution_when_over_cap(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo", max_open_exposure_usd=1000.0))
        # Force a large unhedged BTC position on one venue (deviation from baseline).
        first = service.settings.exchanges[0].id
        service.ledger.get(first)["BTC"] = float(service.ledger.get(first)["BTC"]) + 5.0
        exposure = service.ledger.open_exposure_usd()
        self.assertGreater(exposure, 1000.0)
        before = service.store.executed_count
        for _ in range(60):
            service.safe_tick()
        self.assertTrue(service.exposure_halt)
        self.assertEqual(service.store.executed_count, before, "no new trades may open while exposure-halted")
        health = service.snapshot()["engineHealth"]
        self.assertTrue(health["exposureHalt"])
        self.assertGreater(health["openExposureUsd"], 1000.0)

    def test_demo_never_exposure_halts_in_normal_trading(self):
        from backend.app.engines.market_service import MarketService

        service = MarketService(Settings(market_mode="demo"))
        for _ in range(200):
            service.safe_tick()
        self.assertFalse(service.exposure_halt)
        self.assertLess(service.open_exposure_usd, 1.0)

    def test_testnet_client_exception_returns_none_not_raise(self):
        import os
        from backend.app.integrations.gateways import PreTradeGuard, TestnetExecutionGateway

        os.environ["AURELION_ENABLE_LIVE"] = "1"
        try:
            class Boom:
                def create_order(self, *a, **k):
                    raise TimeoutError("venue timeout")
            gw = TestnetExecutionGateway(PreTradeGuard(max_order_notional_usd=1e9), client_factory=lambda x: Boom())
            trade = {"id": "T-1", "strategy": "simple", "baseAsset": "XRP",
                     "buyExchangeId": "okx", "sellExchangeId": "bybit",
                     "qtyBtc": 100.0, "buyPrice": 0.50, "sellPrice": 0.51, "netProfit": 1.0}
            self.assertIsNone(gw.settle_trade(trade, {}, {}))
            self.assertIn("error", gw.last_error)
        finally:
            os.environ.pop("AURELION_ENABLE_LIVE", None)


class CoPilotModeAwarenessTests(unittest.TestCase):
    """The co-pilot must distinguish demo (scripted) from live (real markets)."""

    def _narrator(self):
        import os
        from backend.app.integrations.llm_narrator import DecisionNarrator

        previous = os.environ.pop("ANTHROPIC_API_KEY", None)
        narrator = DecisionNarrator(Settings())
        if previous is not None:
            os.environ["ANTHROPIC_API_KEY"] = previous
        return narrator

    def _snap(self, mode, degraded=False, executed=0, observation=None):
        return {
            "mode": mode, "degradedDemo": degraded,
            "risk": {"paused": False},
            "models": {"cycleAlgo": "dfs", "slippageModel": "book_walk", "sizingMode": "fixed"},
            "scenarios": {"active": []},
            "metrics": {"executedCount": executed, "cumulativePnl": 0, "detectedCount": 5, "bestObservedNetBps": -21.0},
            "queuedOpportunities": [],
            "observation": observation or {},
        }

    def test_demo_narration_says_it_is_a_showcase(self):
        text = self._narrator().narrate(self._snap("demo"))["text"].lower()
        self.assertTrue("demo" in text or "showcase" in text or "simulated" in text)

    def test_live_narration_explains_the_fee_wall_finding(self):
        obs = {"recording": True, "samples": 57, "routesObserved": 50, "capturableRoutes": 0}
        text = self._narrator().narrate(self._snap("auto", executed=0, observation=obs))["text"].lower()
        self.assertIn("real", text)
        self.assertTrue("fee wall" in text or "not a fault" in text or "measured" in text)
        self.assertIn("57", text)  # grounded in the observation numbers

    def test_degraded_narration_warns_data_is_not_live(self):
        text = self._narrator().narrate(self._snap("auto", degraded=True))["text"].lower()
        self.assertTrue("not live" in text or "fallback" in text)

    def test_mode_change_invalidates_cache(self):
        narrator = self._narrator()
        demo = narrator.narrate(self._snap("demo"))
        live = narrator.narrate(self._snap("auto"))
        self.assertNotEqual(demo["text"], live["text"])
        self.assertFalse(live.get("cached"))


class EnsembleCaptureConfidenceTests(unittest.TestCase):
    """The ensemble capture-confidence combines existing probabilistic signals."""

    def _explain(self, net_bps, confidence, latency_capture):
        from backend.app.engines.edge_analysis import explain_opportunity

        return explain_opportunity({
            "status": "profitable", "netBps": net_bps, "grossProfit": 1.0, "netProfit": 0.5,
            "confidence": confidence, "filledRatio": 1.0, "latencyCaptureProbability": latency_capture,
            "costs": {"totalCosts": 0.5}, "buyPrice": 70000, "qtyBtc": 0.01,
        })["decision"]

    def test_capture_confidence_is_a_probability(self):
        d = self._explain(5.0, 0.9, 0.9)
        self.assertIn("captureConfidence", d)
        self.assertGreaterEqual(d["captureConfidence"], 0.0)
        self.assertLessEqual(d["captureConfidence"], 1.0)

    def test_higher_edge_and_confidence_raise_capture_confidence(self):
        low = self._explain(-3.0, 0.4, 0.5)["captureConfidence"]
        high = self._explain(8.0, 0.95, 0.95)["captureConfidence"]
        self.assertGreater(high, low)

    def test_negative_edge_drops_below_half(self):
        d = self._explain(-6.0, 0.9, 0.9)
        self.assertLess(d["captureConfidence"], 0.5)

    def test_extreme_net_bps_stays_finite(self):
        import math

        for net in (-9999.0, 9999.0):
            value = self._explain(net, 0.9, 0.9)["captureConfidence"]
            self.assertTrue(math.isfinite(value))


if __name__ == "__main__":
    unittest.main()
