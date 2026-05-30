from __future__ import annotations

import math
import time

from backend.app.core.config import Settings
from backend.app.core.models import Opportunity, OrderBook
from backend.app.engines.fills import estimate_buy_with_quote, estimate_fill
from backend.app.engines.ledger import WalletLedger


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
            symbols = exchange.triangular_symbols
            if len(symbols) < 3:
                continue
            opportunity = self.evaluate_cycle(exchange.id, symbols, books, current)
            if opportunity:
                opportunities.append(opportunity.to_dict())
        return sorted(opportunities, key=lambda item: item["score"], reverse=True)

    def evaluate_cycle(self, exchange_id: str, symbols: tuple[str, ...], books: dict[str, OrderBook], current: int) -> Opportunity | None:
        exchange = self.settings.exchange_by_id(exchange_id)
        btc_quote_symbol, eth_btc_symbol, eth_quote_symbol = symbols[:3]
        btc_quote = books.get(f"{exchange_id}:{btc_quote_symbol}")
        eth_btc = books.get(f"{exchange_id}:{eth_btc_symbol}")
        eth_quote = books.get(f"{exchange_id}:{eth_quote_symbol}")
        if not btc_quote or not eth_btc or not eth_quote or not btc_quote.asks or not eth_btc.asks or not eth_quote.bids:
            return None

        wallet = self.ledger.get(exchange_id)
        quote_in = min(self.settings.triangular_quote_size, float(wallet["USDT"]) * 0.12)
        if quote_in < 100:
            return None

        cost_rate = (exchange.taker_fee_bps + exchange.slippage_bps) / 10000
        step1 = estimate_buy_with_quote(btc_quote.asks, quote_in)
        btc_after_costs = float(step1["base_received"]) * (1 - cost_rate)
        step2 = estimate_buy_with_quote(eth_btc.asks, btc_after_costs)
        eth_after_costs = float(step2["base_received"]) * (1 - cost_rate)
        step3 = estimate_fill(eth_quote.bids, eth_after_costs, "bid")
        if step3.filled_qty <= 0:
            return None

        gross_quote_out = step3.quote
        final_quote_out = gross_quote_out * (1 - cost_rate)
        latency_ms = btc_quote.latency_ms + eth_btc.latency_ms + eth_quote.latency_ms
        latency_risk_bps = max(self.settings.latency_risk_floor_bps, (latency_ms / 3000) * self.settings.latency_bps_per_second)
        latency_risk_cost = quote_in * latency_risk_bps / 10000
        gross_profit = gross_quote_out - quote_in
        net_profit = final_quote_out - quote_in - latency_risk_cost
        net_bps = net_profit / quote_in * 10000
        gross_bps = gross_profit / quote_in * 10000
        max_age = max(current - btc_quote.timestamp, current - eth_btc.timestamp, current - eth_quote.timestamp)
        confidence = min(exchange.confidence, btc_quote.confidence, eth_btc.confidence, eth_quote.confidence, max(0.2, 1 - max_age / self.settings.max_book_age_ms))
        total_costs = quote_in * cost_rate + float(step2["quote_spent"]) * cost_rate * float(step1["avg_price"]) + gross_quote_out * cost_rate + latency_risk_cost
        profitable = net_profit >= self.settings.triangular_min_net_profit_usd and net_bps >= self.settings.triangular_min_net_bps and confidence >= self.settings.min_confidence
        step1_ratio = float(step1["quote_spent"]) / quote_in if quote_in else 0
        step2_ratio = float(step2["quote_spent"]) / btc_after_costs if btc_after_costs else 0
        step3_ratio = step3.filled_qty / eth_after_costs if eth_after_costs else 0
        partial = bool(step1["partial"] or step2["partial"] or step3.partial)
        filled_ratio = min(1, step1_ratio, step2_ratio, step3_ratio)
        score = (net_bps * confidence * math.log10(max(quote_in, 10))) / (1 + latency_ms / 1200) if profitable else net_bps * confidence
        source = "websocket" if all(book.source == "websocket" for book in (btc_quote, eth_btc, eth_quote)) else "mixed"
        cycle_path = ["USDT" if "USDT" in btc_quote_symbol else "USD", "BTC", "ETH", "USDT" if "USDT" in eth_quote_symbol else "USD"]
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
            reason="Triangular cycle cleared risk gates" if profitable else "Triangular costs or risk removed the edge",
            product=" -> ".join(cycle_path),
            costs={"totalCosts": rounded(total_costs, 4), "latencyRiskCost": rounded(latency_risk_cost, 4), "latencyRiskBps": rounded(latency_risk_bps, 3)},
            data={
                "exchangeId": exchange_id,
                "exchange": exchange.name,
                "cycleId": cycle_id,
                "cyclePath": cycle_path,
                "quoteIn": rounded(quote_in, 4),
                "targetQuote": self.settings.triangular_quote_size,
                "quoteOut": rounded(final_quote_out, 4),
                "qtyBtc": rounded(float(step1["base_received"]), 8),
                "qtyEth": rounded(float(step2["base_received"]), 8),
                "filledRatio": rounded(filled_ratio, 4),
                "buyPrice": rounded(float(step1["avg_price"]), 8),
                "sellPrice": rounded(step3.avg_price, 8),
                "legPartials": [bool(step1["partial"]), bool(step2["partial"]), bool(step3.partial)],
                "legs": [
                    {"action": "buy", "symbol": btc_quote_symbol, "from": cycle_path[0], "to": "BTC", "avgPrice": rounded(float(step1["avg_price"]), 8), "levels": step1["level_count"], "partial": bool(step1["partial"]), "filledRatio": rounded(step1_ratio, 4)},
                    {"action": "buy", "symbol": eth_btc_symbol, "from": "BTC", "to": "ETH", "avgPrice": rounded(float(step2["avg_price"]), 8), "levels": step2["level_count"], "partial": bool(step2["partial"]), "filledRatio": rounded(step2_ratio, 4)},
                    {"action": "sell", "symbol": eth_quote_symbol, "from": "ETH", "to": cycle_path[3], "avgPrice": rounded(step3.avg_price, 8), "levels": step3.level_count, "partial": bool(step3.partial), "filledRatio": rounded(step3_ratio, 4)},
                ],
                "latencies": {"totalMs": latency_ms},
            },
        )
