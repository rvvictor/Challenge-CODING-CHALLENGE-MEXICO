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
        self.state: dict[str, dict[str, float]] = {}
        self.shock: dict | None = None
        for index, exchange in enumerate(exchanges):
            self.state[f"{exchange.id}:BTC"] = {"mid": 70000 + index * 22, "drift": self.random.uniform(-3, 3), "liq": self.random.uniform(0.7, 1.35)}
            self.state[f"{exchange.id}:ETHBTC"] = {"mid": 0.052 + index * 0.00003, "drift": self.random.uniform(-0.00002, 0.00002), "liq": self.random.uniform(8, 26)}

    def maybe_shock(self, exchanges: tuple[ExchangeConfig, ...]) -> None:
        if self.shock and self.shock["until"] > self.tick:
            return
        if self.tick % 9 == 0:
            cheap = self.random.choice(exchanges).id
            rich = self.random.choice(exchanges).id
            if rich == cheap:
                rich = exchanges[(list(exchange.id for exchange in exchanges).index(rich) + 1) % len(exchanges)].id
            self.shock = {
                "cheap": cheap,
                "rich": rich,
                "cheap_bps": self.random.uniform(-18, -7),
                "rich_bps": self.random.uniform(7, 20),
                "until": self.tick + 4,
            }

    def generate(self, exchange: ExchangeConfig, exchanges: tuple[ExchangeConfig, ...], symbol: str, anchor_mid: float | None = None) -> OrderBook:
        self.tick += 1
        self.maybe_shock(exchanges)
        kind = "ETHBTC" if "ETH/BTC" in symbol else "ETHQUOTE" if "ETH/" in symbol else "BTC"
        state_key = f"{exchange.id}:ETHBTC" if kind == "ETHBTC" else f"{exchange.id}:BTC"
        state = self.state[state_key]
        btc_state = self.state[f"{exchange.id}:BTC"]
        eth_btc_state = self.state[f"{exchange.id}:ETHBTC"]

        if kind == "ETHBTC":
            state["mid"] = max(0.035, state["mid"] + self.random.uniform(-0.00008, 0.00008) + state["drift"])
            mid = state["mid"]
        elif kind == "ETHQUOTE":
            mid = btc_state["mid"] * eth_btc_state["mid"] * (1 + self.random.uniform(-8, 8) / 10000)
        else:
            anchor = anchor_mid if anchor_mid else state["mid"]
            state["mid"] = anchor * 0.985 + (state["mid"] + self.random.uniform(-18, 18) + state["drift"]) * 0.015
            mid = state["mid"]

        if self.shock and self.shock["cheap"] == exchange.id:
            mid *= 1 + self.shock["cheap_bps"] / 10000
        if self.shock and self.shock["rich"] == exchange.id:
            mid *= 1 + self.shock["rich_bps"] / 10000

        spread_bps = self.random.uniform(4, 12) if kind == "ETHBTC" else self.random.uniform(2.5, 8.5)
        half_spread = mid * spread_bps / 20000
        asks: list[Level] = []
        bids: list[Level] = []
        for i in range(20):
            gap = i * self.random.uniform(0.000004, 0.000018) if kind == "ETHBTC" else i * self.random.uniform(2, 8)
            qty = (
                self.random.uniform(0.4, 8) * (eth_btc_state["liq"] / 12) * (1 + i / 12)
                if kind in {"ETHBTC", "ETHQUOTE"}
                else self.random.uniform(0.012, 0.42) * state["liq"] * (1 + i / 12)
            )
            decimals = 8 if kind == "ETHBTC" else 2
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
