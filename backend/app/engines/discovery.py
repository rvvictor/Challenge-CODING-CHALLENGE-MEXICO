from __future__ import annotations

import threading
import time

from backend.app.core.config import ExchangeConfig, Settings
from backend.app.integrations.market_scout import (
    BTC_LEG_BASES,
    DISCOVERY_BASES,
    scout_universe,
)


def now_ms() -> int:
    return int(time.time() * 1000)


class WideNetRadar:
    """Wide-net discovery lane ("scout"), fully decoupled from the hot loop.

    The hot lane keeps the fastest venues and its ~3 ms decision path. This
    engine sweeps the FULL exchange universe — including XRP/LTC/SOL pairs the
    hot lane does not trade — from batched public tickers on a slow cadence,
    prices every cross-exchange and ticker-triangular route with the same
    entry-tier fee catalog the hot lane uses, and tracks how many consecutive
    sweeps each edge survives. A route that clears the net-bps threshold for
    enough sweeps in a row is flagged "promotable": statistical evidence that a
    venue/pair deserves a slot in the hot lane. Promotion stays a human call.
    """

    MAX_TOP_ROUTES = 12
    STALE_AFTER_SWEEPS = 20

    def __init__(self, settings: Settings, scout=scout_universe):
        self.settings = settings
        self.scout = scout
        self.sweep_count = 0
        self.persistence: dict[str, dict] = {}
        self.last_result: dict = {}
        self._lock = threading.Lock()

    # ---- edge math ---------------------------------------------------------
    def _leg_cost_bps(self, exchange: ExchangeConfig) -> float:
        # Tickers carry no depth, so each leg is charged the venue's taker fee
        # plus its slippage buffer — the conservative stand-in for the book-walk
        # the hot lane performs when it has full order books.
        return exchange.taker_fee_bps + exchange.slippage_bps

    def _cross_routes(self, quotes: dict, universe: tuple[ExchangeConfig, ...]) -> list[dict]:
        routes: list[dict] = []
        for base in DISCOVERY_BASES:
            entries = []
            for exchange in universe:
                venue_quotes = quotes.get(exchange.id) or {}
                for quote_ccy in ("USDT", "USD"):
                    quote = venue_quotes.get(f"{base}/{quote_ccy}")
                    if quote:
                        entries.append((exchange, quote))
                        break
            for buy_exchange, buy_quote in entries:
                for sell_exchange, sell_quote in entries:
                    if buy_exchange.id == sell_exchange.id:
                        continue
                    gross_bps = (sell_quote.bid / buy_quote.ask - 1) * 10000
                    costs_bps = self._leg_cost_bps(buy_exchange) + self._leg_cost_bps(sell_exchange)
                    routes.append({
                        "id": f"cross:{base}:{buy_exchange.id}>{sell_exchange.id}",
                        "kind": "cross",
                        "base": base,
                        "route": f"{buy_exchange.name} -> {sell_exchange.name}",
                        "detail": f"buy {buy_quote.symbol} @ {buy_quote.ask:g} / sell {sell_quote.symbol} @ {sell_quote.bid:g}",
                        "grossBps": round(gross_bps, 2),
                        "costsBps": round(costs_bps, 2),
                        "netBps": round(gross_bps - costs_bps, 2),
                        "crossQuote": buy_quote.symbol.split("/")[1] != sell_quote.symbol.split("/")[1],
                    })
        return routes

    def _triangular_routes(self, quotes: dict, universe: tuple[ExchangeConfig, ...]) -> list[dict]:
        routes: list[dict] = []
        for exchange in universe:
            venue_quotes = quotes.get(exchange.id) or {}
            quote_ccy = "USD" if exchange.primary_symbol.endswith("/USD") else "USDT"
            btc = venue_quotes.get(f"BTC/{quote_ccy}")
            if not btc:
                continue
            leg_keep = 1 - self._leg_cost_bps(exchange) / 10000
            for base in BTC_LEG_BASES:
                direct = venue_quotes.get(f"{base}/{quote_ccy}")
                bridge = venue_quotes.get(f"{base}/BTC")
                if not direct or not bridge:
                    continue
                # Forward: quote -> base (buy direct) -> BTC (sell bridge) -> quote (sell BTC)
                forward_gross = (1 / direct.ask) * bridge.bid * btc.bid
                # Reverse: quote -> BTC (buy) -> base (buy bridge) -> quote (sell direct)
                reverse_gross = (1 / btc.ask) * (1 / bridge.ask) * direct.bid
                for gross, path in (
                    (forward_gross, (quote_ccy, base, "BTC", quote_ccy)),
                    (reverse_gross, (quote_ccy, "BTC", base, quote_ccy)),
                ):
                    net = gross * leg_keep ** 3
                    gross_bps = (gross - 1) * 10000
                    net_bps = (net - 1) * 10000
                    routes.append({
                        "id": f"tri:{exchange.id}:{'>'.join(path)}",
                        "kind": "triangular",
                        "base": base,
                        "route": f"{exchange.name} {' -> '.join(path)}",
                        "detail": f"3 legs via {base}/BTC, taker+slippage charged per leg",
                        "grossBps": round(gross_bps, 2),
                        "costsBps": round(gross_bps - net_bps, 2),
                        "netBps": round(net_bps, 2),
                        "crossQuote": False,
                    })
        return routes

    # ---- persistence across sweeps ------------------------------------------
    def _update_persistence(self, routes: list[dict], threshold: float) -> None:
        current = now_ms()
        sweep = self.sweep_count
        for route in routes:
            if route["netBps"] < threshold:
                continue
            entry = self.persistence.get(route["id"])
            if entry and entry["sweep"] == sweep - 1:
                entry["streak"] += 1
            else:
                entry = {"streak": 1, "firstSeen": current}
                self.persistence[route["id"]] = entry
            entry["sweep"] = sweep
            entry["lastSeen"] = current
            entry["lastNetBps"] = route["netBps"]
            entry["bestNetBps"] = max(route["netBps"], entry.get("bestNetBps", route["netBps"]))
        self.persistence = {
            key: value for key, value in self.persistence.items()
            if sweep - value["sweep"] <= self.STALE_AFTER_SWEEPS
        }

    # ---- sweep + snapshot ----------------------------------------------------
    def sweep(self) -> dict:
        # Runs in a worker thread. Non-blocking lock: if a scheduled sweep and a
        # manual "sweep now" overlap, the second caller just reads the snapshot
        # instead of stacking a duplicate network sweep.
        if not self._lock.acquire(blocking=False):
            return self.snapshot()
        try:
            universe = self.settings.exchange_universe
            scouted = self.scout(universe) or {}
            quotes = scouted.get("quotes") or {}
            statuses = scouted.get("statuses") or {}
            self.sweep_count += 1
            routes = self._cross_routes(quotes, universe) + self._triangular_routes(quotes, universe)
            routes.sort(key=lambda route: route["netBps"], reverse=True)
            self._update_persistence(routes, self.settings.discovery_min_net_bps)
            min_streak = max(1, int(self.settings.discovery_min_persistence))
            top = []
            for route in routes[: self.MAX_TOP_ROUTES]:
                entry = self.persistence.get(route["id"])
                streak = entry["streak"] if entry and entry["sweep"] == self.sweep_count else 0
                top.append({**route, "streak": streak, "promotable": streak >= min_streak})
            self.last_result = {
                "sweep": self.sweep_count,
                "at": now_ms(),
                "durationMs": scouted.get("durationMs", 0),
                "statuses": statuses,
                "venuesLive": sum(1 for status in statuses.values() if status == "live"),
                "seriesCount": sum(len(symbol_map) for symbol_map in quotes.values()),
                "routesPriced": len(routes),
                "positiveCount": sum(1 for route in routes if route["netBps"] > 0),
                "bestNetBps": routes[0]["netBps"] if routes else None,
                "topRoutes": top,
            }
            return self.last_result
        finally:
            self._lock.release()

    def snapshot(self) -> dict:
        result = self.last_result
        return {
            "enabled": self.settings.discovery_enabled,
            "intervalMs": self.settings.discovery_interval_ms,
            "minPersistence": max(1, int(self.settings.discovery_min_persistence)),
            "minNetBps": self.settings.discovery_min_net_bps,
            "universeCount": len(self.settings.exchange_universe),
            "bases": list(DISCOVERY_BASES),
            "sweepCount": self.sweep_count,
            "lastSweep": result,
            "promotableCount": sum(1 for route in (result.get("topRoutes") or []) if route.get("promotable")),
            "note": (
                "Discovery lane: the full venue universe plus XRP/LTC/SOL/AVAX pairs, priced from batched "
                "public tickers on a slow background cadence. The hot loop and its decision latency are untouched."
            ),
        }
