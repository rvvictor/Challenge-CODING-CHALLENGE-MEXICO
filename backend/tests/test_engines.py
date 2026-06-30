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
    def _fake_candles(self, base_price: float, count: int = 80, step: float = 0.0):
        from backend.app.integrations.historical_data import Candle

        rows = []
        price = base_price
        for i in range(count):
            price = max(1.0, price + step)
            rows.append(Candle(timestamp=1700000000000 + i * 60000, open=price, high=price * 1.001, low=price * 0.999, close=price, volume=12.0))
        return rows

    def test_historical_market_synthesizes_book_from_real_candle(self):
        from backend.app.engines.historical_replay import HistoricalMarket

        settings = Settings()
        okx = settings.exchange_by_id("okx")
        candles = {"okx": self._fake_candles(70000.0)}
        market = HistoricalMarket(settings.exchanges, candles)
        market.advance(settings.exchanges)
        result = market.generate(okx, settings.exchanges, okx.primary_symbol)
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "historical")
        ask = best(result.asks, "ask")
        bid = best(result.bids, "bid")
        self.assertGreater(ask.price, bid.price)
        self.assertAlmostEqual((ask.price + bid.price) / 2, 70000.0, delta=50)

    def test_historical_market_returns_none_for_non_primary_symbol(self):
        from backend.app.engines.historical_replay import HistoricalMarket

        settings = Settings()
        okx = settings.exchange_by_id("okx")
        market = HistoricalMarket(settings.exchanges, {"okx": self._fake_candles(70000.0)})
        market.advance(settings.exchanges)
        self.assertIsNone(market.generate(okx, settings.exchanges, "ETH/BTC"))

    def test_backtest_runs_over_injected_real_history(self):
        from backend.app.engines.backtest import BacktestRunner

        # Two venues with a persistent divergence: okx cheaper, kraken richer —
        # a real, reproducible arbitrage signal without hitting the network.
        def fake_provider(exchanges, timeframe, limit):
            candles = {}
            for exchange in exchanges:
                base = 69800.0 if exchange.id == "okx" else 70200.0
                candles[exchange.id] = self._fake_candles(base, count=80)
            return {"candles": candles, "statuses": {exchange.id: "live" for exchange in exchanges}}

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


if __name__ == "__main__":
    unittest.main()
