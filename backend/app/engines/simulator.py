from __future__ import annotations

import random
import time

from backend.app.core.config import ExchangeConfig
from backend.app.core.models import Level, OrderBook
from backend.app.engines.fills import sort_levels


class SimulatedMarket:
    def __init__(self, exchanges: tuple[ExchangeConfig, ...]):
        self.random = random.Random(71021)
        self.tick = 0
        self.book_tick = 0
        self.global_btc = 70000.0
        self.global_eth_btc = 0.052
        self.global_sol_eth = 0.047
        self.state: dict[str, dict[str, float]] = {}
        self.shock: dict | None = None
        self.triangular_shock: dict | None = None
        self.dynamic_shock: dict | None = None
        self.volatility_stress_until = 0
        for index, exchange in enumerate(exchanges):
            self.state[f"{exchange.id}:BTC"] = {"basis_bps": (index - len(exchanges) / 2) * 0.35, "liq": self.random.uniform(0.7, 1.35)}
            self.state[f"{exchange.id}:ETHBTC"] = {"basis_bps": (index - len(exchanges) / 2) * 0.22, "liq": self.random.uniform(8, 26)}
            self.state[f"{exchange.id}:SOL"] = {"basis_bps": (index - len(exchanges) / 2) * 0.2, "liq": self.random.uniform(18, 46)}

    def advance(self, exchanges: tuple[ExchangeConfig, ...]) -> None:
        self.tick += 1
        if self.tick < self.volatility_stress_until:
            self.global_btc *= 1 + self.random.uniform(-8.0, 8.0) / 10000
        else:
            self.global_btc *= 1 + self.random.uniform(-0.65, 0.65) / 10000
        self.global_eth_btc *= 1 + self.random.uniform(-0.35, 0.35) / 10000
        self.global_sol_eth *= 1 + self.random.uniform(-0.28, 0.28) / 10000
        self.maybe_shock(exchanges)

    def inject_volatility_stress(self, change_pct: float = 3.2, duration_ticks: int = 18) -> None:
        direction = 1 if self.random.random() >= 0.5 else -1
        self.global_btc *= 1 + direction * change_pct / 100
        self.volatility_stress_until = self.tick + duration_ticks

    def maybe_shock(self, exchanges: tuple[ExchangeConfig, ...]) -> None:
        cross_active = self.shock and self.shock["until"] > self.tick
        if not cross_active and self.tick % 37 == 8:
            cheap = self.random.choice(exchanges).id
            rich = self.random.choice(exchanges).id
            if rich == cheap:
                rich = exchanges[(list(exchange.id for exchange in exchanges).index(rich) + 1) % len(exchanges)].id
            self.shock = {
                "started": self.tick,
                "cheap": cheap,
                "rich": rich,
                "cheap_bps": self.random.uniform(-30, -21),
                "rich_bps": self.random.uniform(21, 33),
                "until": self.tick + 7,
            }
        triangular_active = self.triangular_shock and self.triangular_shock["until"] > self.tick
        if not triangular_active and self.tick % 67 == 31:
            candidates = [exchange for exchange in exchanges if exchange.taker_fee_bps <= 12] or list(exchanges)
            exchange = self.random.choice(candidates)
            self.triangular_shock = {
                "started": self.tick,
                "exchange": exchange.id,
                "btc_bps": self.random.uniform(-5, -1),
                "eth_btc_bps": self.random.uniform(-5, -1),
                "eth_quote_bps": self.random.uniform(40, 56),
                "until": self.tick + 6,
            }
        dynamic_active = self.dynamic_shock and self.dynamic_shock["until"] > self.tick
        if not dynamic_active and self.tick % 101 == 58:
            candidates = [exchange for exchange in exchanges if exchange.taker_fee_bps <= 12] or list(exchanges)
            exchange = self.random.choice(candidates)
            self.dynamic_shock = {
                "started": self.tick,
                "exchange": exchange.id,
                "btc_bps": self.random.uniform(-8, -5),
                "eth_btc_bps": self.random.uniform(-8, -5),
                "sol_eth_bps": self.random.uniform(-7, -3),
                "sol_quote_bps": self.random.uniform(92, 108),
                "until": self.tick + 6,
            }

    def generate(self, exchange: ExchangeConfig, exchanges: tuple[ExchangeConfig, ...], symbol: str, anchor_mid: float | None = None) -> OrderBook:
        self.book_tick += 1
        if self.tick == 0:
            self.advance(exchanges)
        kind = (
            "ETHBTC" if symbol == "ETH/BTC"
            else "SOLETH" if symbol == "SOL/ETH"
            else "SOLQUOTE" if symbol.startswith("SOL/")
            else "ETHQUOTE" if symbol.startswith("ETH/")
            else "BTC"
        )
        state_key = f"{exchange.id}:ETHBTC" if kind == "ETHBTC" else f"{exchange.id}:SOL" if kind in {"SOLETH", "SOLQUOTE"} else f"{exchange.id}:BTC"
        state = self.state[state_key]
        btc_state = self.state[f"{exchange.id}:BTC"]
        eth_btc_state = self.state[f"{exchange.id}:ETHBTC"]
        sol_state = self.state[f"{exchange.id}:SOL"]

        if kind == "ETHBTC":
            mid = self.global_eth_btc * (1 + (state["basis_bps"] + self.random.uniform(-0.25, 0.25)) / 10000)
        elif kind == "SOLETH":
            mid = self.global_sol_eth * (1 + (state["basis_bps"] + self.random.uniform(-0.2, 0.2)) / 10000)
        elif kind == "SOLQUOTE":
            basis_bps = btc_state["basis_bps"] + eth_btc_state["basis_bps"] + sol_state["basis_bps"]
            mid = self.global_btc * self.global_eth_btc * self.global_sol_eth * (1 + (basis_bps + self.random.uniform(-0.45, 0.45)) / 10000)
        elif kind == "ETHQUOTE":
            basis_bps = btc_state["basis_bps"] + eth_btc_state["basis_bps"]
            mid = self.global_btc * self.global_eth_btc * (1 + (basis_bps + self.random.uniform(-0.55, 0.55)) / 10000)
        else:
            mid = self.global_btc * (1 + (state["basis_bps"] + self.random.uniform(-0.45, 0.45)) / 10000)

        if kind == "BTC" and self.shock and self.shock["cheap"] == exchange.id:
            mid *= 1 + self.shock["cheap_bps"] / 10000
        if kind == "BTC" and self.shock and self.shock["rich"] == exchange.id:
            mid *= 1 + self.shock["rich_bps"] / 10000
        if kind == "BTC" and self.shock and self.shock["until"] > self.tick and self.tick - self.shock["started"] in {2, 5}:
            if self.shock["cheap"] == exchange.id:
                mid *= 1 - 28 / 10000
            if self.shock["rich"] == exchange.id:
                mid *= 1 + 28 / 10000
        if self.triangular_shock and self.triangular_shock["until"] > self.tick and self.triangular_shock["exchange"] == exchange.id:
            if kind == "BTC":
                mid *= 1 + self.triangular_shock["btc_bps"] / 10000
            elif kind == "ETHBTC":
                mid *= 1 + self.triangular_shock["eth_btc_bps"] / 10000
            elif kind == "ETHQUOTE":
                mid *= 1 + self.triangular_shock["eth_quote_bps"] / 10000
        if self.dynamic_shock and self.dynamic_shock["until"] > self.tick and self.dynamic_shock["exchange"] == exchange.id:
            if kind == "BTC":
                mid *= 1 + self.dynamic_shock["btc_bps"] / 10000
            elif kind == "ETHBTC":
                mid *= 1 + self.dynamic_shock["eth_btc_bps"] / 10000
            elif kind == "SOLETH":
                mid *= 1 + self.dynamic_shock["sol_eth_bps"] / 10000
            elif kind == "SOLQUOTE":
                mid *= 1 + self.dynamic_shock["sol_quote_bps"] / 10000

        spread_bps = self.random.uniform(4, 12) if kind in {"ETHBTC", "SOLETH"} else self.random.uniform(1.4, 5.2)
        half_spread = mid * spread_bps / 20000
        asks: list[Level] = []
        bids: list[Level] = []
        book_liquidity_crunch = (
            kind == "BTC"
            and self.shock
            and exchange.id in {self.shock["cheap"], self.shock["rich"]}
            and (self.tick - self.shock["started"] in {0, 2, 5} or self.random.random() < 0.09)
        )
        for i in range(20):
            if kind in {"ETHBTC", "SOLETH"}:
                gap = i * self.random.uniform(0.000004, 0.000018)
            elif kind == "SOLQUOTE":
                gap = i * self.random.uniform(0.02, 0.14)
            else:
                gap = i * self.random.uniform(2, 8)
            qty = (
                self.random.uniform(8, 160) * (sol_state["liq"] / 32) * (1 + i / 12)
                if kind in {"SOLETH", "SOLQUOTE"}
                else self.random.uniform(0.4, 8) * (eth_btc_state["liq"] / 12) * (1 + i / 12)
                if kind in {"ETHBTC", "ETHQUOTE"}
                else self.random.uniform(0.006, 0.18) * state["liq"] * (1 + i / 12)
            )
            if book_liquidity_crunch:
                qty = self.random.uniform(0.00042, 0.00074)
            decimals = 8 if kind in {"ETHBTC", "SOLETH"} else 2
            asks.append(Level(round(mid + half_spread + gap, decimals), round(qty, 6)))
            bids.append(Level(round(mid - half_spread - gap, decimals), round(qty * self.random.uniform(0.85, 1.18), 6)))

        now = int(time.time() * 1000)
        return OrderBook(
            key=f"{exchange.id}:{symbol}",
            exchange_id=exchange.id,
            exchange_name=exchange.name,
            symbol=symbol,
            primary=symbol == exchange.primary_symbol,
            source="simulated",
            status="simulated",
            fee_bps=exchange.taker_fee_bps,
            slippage_bps=exchange.slippage_bps,
            confidence=max(0.5, exchange.confidence - 0.12),
            asks=sort_levels(asks, "ask"),
            bids=sort_levels(bids, "bid"),
            latency_ms=round(self.random.uniform(15, 95)),
            timestamp=now,
        )
