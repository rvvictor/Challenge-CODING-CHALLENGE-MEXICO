from __future__ import annotations

import math
import time

from backend.app.core.config import Settings
from backend.app.core.models import Opportunity, OrderBook
from backend.app.engines.fills import estimate_buy_with_quote, estimate_fill
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.scoring import expected_value_score


def now_ms() -> int:
    return int(time.time() * 1000)


def rounded(value: float, decimals: int = 6) -> float:
    return round(value or 0.0, decimals)


class TriangularArbitrageEngine:
    def __init__(self, settings: Settings, ledger: WalletLedger):
        self.settings = settings
        self.ledger = ledger

    def scan(self, books: dict[str, OrderBook]) -> list[dict]:
        if not self.settings.triangular_enabled:
            return []
        current = now_ms()
        opportunities = []
        for exchange in self.settings.exchanges:
            cycles = self.find_cycles(exchange.id, books)
            for cycle in cycles[: self.settings.triangular_max_cycles_per_exchange]:
                opportunity = self.evaluate_cycle(exchange.id, cycle, books, current)
                if opportunity:
                    opportunities.append(opportunity.to_dict())
        return sorted(opportunities, key=lambda item: item["score"], reverse=True)

    def build_edges(self, exchange_id: str, books: dict[str, OrderBook]) -> dict[str, list[dict]]:
        edges: dict[str, list[dict]] = {}
        for key, book in books.items():
            if not key.startswith(f"{exchange_id}:") or "/" not in book.symbol:
                continue
            base, quote = book.symbol.split("/", 1)
            if book.asks:
                edges.setdefault(quote, []).append({"from": quote, "to": base, "symbol": book.symbol, "side": "buy"})
            if book.bids:
                edges.setdefault(base, []).append({"from": base, "to": quote, "symbol": book.symbol, "side": "sell"})
        return edges

    def find_cycles(self, exchange_id: str, books: dict[str, OrderBook]) -> list[list[dict]]:
        edges = self.build_edges(exchange_id, books)
        start_assets = [asset for asset in ("USDT", "USD") if asset in edges]
        cycles: list[list[dict]] = []
        seen: set[str] = set()

        def walk(start: str, asset: str, path: list[dict], visited_assets: set[str], used_symbols: set[str]) -> None:
            if len(path) >= self.settings.triangular_max_legs:
                return
            for edge in edges.get(asset, []):
                edge_key = f"{edge['symbol']}:{edge['side']}"
                if edge_key in used_symbols:
                    continue
                next_asset = edge["to"]
                next_path = [*path, edge]
                if next_asset == start and len(next_path) >= 3:
                    cycle_id = "->".join([start, *[item["to"] for item in next_path]])
                    if cycle_id not in seen:
                        seen.add(cycle_id)
                        cycles.append(next_path)
                    continue
                if next_asset in visited_assets:
                    continue
                walk(start, next_asset, next_path, {*visited_assets, next_asset}, {*used_symbols, edge_key})

        for start in start_assets:
            walk(start, start, [], {start}, set())
        return cycles

    def evaluate_cycle(self, exchange_id: str, cycle: list[dict], books: dict[str, OrderBook], current: int) -> Opportunity | None:
        exchange = self.settings.exchange_by_id(exchange_id)
        if len(cycle) < 3:
            return None
        cycle_books = [books.get(f"{exchange_id}:{edge['symbol']}") for edge in cycle]
        if any(book is None for book in cycle_books):
            return None

        wallet = self.ledger.get(exchange_id)
        start_asset = cycle[0]["from"]
        wallet_quote = float(wallet.get(start_asset, wallet.get("USDT", 0)))
        quote_in = min(self.settings.triangular_quote_size, wallet_quote * 0.12)
        if quote_in < 100:
            return None

        cost_rate = (exchange.taker_fee_bps + exchange.slippage_bps) / 10000
        amount = quote_in
        gross_amount = quote_in
        legs: list[dict] = []
        filled_ratios: list[float] = []
        partials: list[bool] = []
        path = [start_asset]
        asset_outputs: dict[str, float] = {}
        for edge, book in zip(cycle, cycle_books):
            if book is None:
                return None
            if edge["side"] == "buy":
                step = estimate_buy_with_quote(book.asks, amount)
                if float(step["base_received"]) <= 0:
                    return None
                spent = float(step["quote_spent"])
                ratio = spent / amount if amount else 0
                gross_amount = float(step["base_received"])
                amount = gross_amount * (1 - cost_rate)
                avg_price = float(step["avg_price"])
                levels = int(step["level_count"])
                partial = bool(step["partial"])
            else:
                step = estimate_fill(book.bids, amount, "bid")
                if step.filled_qty <= 0:
                    return None
                ratio = step.filled_qty / amount if amount else 0
                gross_amount = step.quote
                amount = gross_amount * (1 - cost_rate)
                avg_price = step.avg_price
                levels = step.level_count
                partial = bool(step.partial)
            filled_ratios.append(ratio)
            partials.append(partial)
            asset_outputs[edge["to"]] = amount
            legs.append({
                "action": edge["side"],
                "symbol": edge["symbol"],
                "from": edge["from"],
                "to": edge["to"],
                "avgPrice": rounded(avg_price, 8),
                "levels": levels,
                "partial": partial,
                "filledRatio": rounded(ratio, 4),
            })
            path.append(edge["to"])

        gross_quote_out = gross_amount
        final_quote_out = amount
        latency_ms = sum(book.latency_ms for book in cycle_books if book)
        latency_risk_bps = max(self.settings.latency_risk_floor_bps, (latency_ms / max(len(cycle), 1) / 1000) * self.settings.latency_bps_per_second)
        latency_risk_cost = quote_in * latency_risk_bps / 10000
        gross_profit = gross_quote_out - quote_in
        net_profit = final_quote_out - quote_in - latency_risk_cost
        net_bps = net_profit / quote_in * 10000
        gross_bps = gross_profit / quote_in * 10000
        max_age = max(current - book.timestamp for book in cycle_books if book)
        confidence = min(exchange.confidence, *(book.confidence for book in cycle_books if book), max(0.2, 1 - max_age / self.settings.max_book_age_ms))
        total_costs = max(0, gross_quote_out - final_quote_out) + latency_risk_cost
        ev = expected_value_score(
            net_profit=net_profit,
            notional=quote_in,
            confidence=confidence,
            latency_ms=latency_ms,
            latency_risk_cost=latency_risk_cost,
            inventory_penalty=0,
            settings=self.settings,
        )
        profitable = net_profit >= self.settings.triangular_min_net_profit_usd and net_bps >= self.settings.triangular_min_net_bps and confidence >= self.settings.min_confidence
        partial = any(partials)
        filled_ratio = min(1, *filled_ratios) if filled_ratios else 0
        score = (ev["evBps"] * math.log10(max(quote_in, 10))) / (1 + latency_ms / 1200) if profitable else ev["evBps"] * confidence
        source = "websocket" if all(book.source == "websocket" for book in cycle_books if book) else "mixed"
        cycle_path = path
        cycle_id = "-".join(cycle_path)

        return Opportunity(
            id=f"{current}-{exchange_id}-{cycle_id}",
            strategy="triangular",
            time=current,
            score=rounded(score, 5),
            status="profitable" if profitable else "rejected",
            net_profit=rounded(net_profit, 4),
            net_bps=rounded(net_bps, 3),
            gross_profit=rounded(gross_profit, 4),
            gross_bps=rounded(gross_bps, 3),
            confidence=rounded(confidence, 3),
            partial=partial,
            source=source,
            reason="Dynamic triangular cycle cleared risk gates" if profitable else "Triangular costs or risk removed the edge",
            product=" -> ".join(cycle_path),
            costs={
                "totalCosts": rounded(total_costs, 4),
                "latencyRiskCost": rounded(latency_risk_cost, 4),
                "latencyRiskBps": rounded(latency_risk_bps, 3),
                "volatilityRiskCost": ev["volatilityRiskCost"],
                "inventoryPenalty": ev["inventoryPenalty"],
                "latencyPenaltyCost": ev["latencyPenaltyCost"],
            },
            data={
                "exchangeId": exchange_id,
                "exchange": exchange.name,
                "cycleId": cycle_id,
                "cyclePath": cycle_path,
                "quoteIn": rounded(quote_in, 4),
                "targetQuote": self.settings.triangular_quote_size,
                "quoteOut": rounded(final_quote_out, 4),
                "qtyBtc": rounded(asset_outputs.get("BTC", 0), 8),
                "qtyEth": rounded(asset_outputs.get("ETH", 0), 8),
                "filledRatio": rounded(filled_ratio, 4),
                "buyPrice": legs[0]["avgPrice"],
                "sellPrice": legs[-1]["avgPrice"],
                "legPartials": partials,
                "legs": legs,
                "latencies": {"totalMs": latency_ms},
                "dynamicCycle": len(cycle) > 3,
                **ev,
            },
        )
