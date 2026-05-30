from __future__ import annotations

import math
import time

from backend.app.core.config import Settings
from backend.app.core.models import Opportunity, OrderBook
from backend.app.engines.fills import best, depth_qty, estimate_fill
from backend.app.engines.ledger import WalletLedger


def now_ms() -> int:
    return int(time.time() * 1000)


def rounded(value: float, decimals: int = 6) -> float:
    return round(value or 0.0, decimals)


class CrossExchangeArbitrageEngine:
    def __init__(self, settings: Settings, ledger: WalletLedger):
        self.settings = settings
        self.ledger = ledger

    def scan(self, books_by_exchange: dict[str, OrderBook]) -> list[dict]:
        current = now_ms()
        books = [book for book in books_by_exchange.values() if book.asks and book.bids]
        opportunities = []
        for buy_book in books:
            for sell_book in books:
                if buy_book.exchange_id == sell_book.exchange_id:
                    continue
                opportunity = self.evaluate_pair(buy_book, sell_book, current)
                if opportunity:
                    opportunities.append(opportunity.to_dict())
        return sorted(opportunities, key=lambda item: item["score"], reverse=True)

    def evaluate_pair(self, buy_book: OrderBook, sell_book: OrderBook, current: int) -> Opportunity | None:
        ask = best(buy_book.asks, "ask")
        bid = best(sell_book.bids, "bid")
        if not ask or not bid or ask.price >= bid.price:
            return None

        buy_exchange = self.settings.exchange_by_id(buy_book.exchange_id)
        sell_exchange = self.settings.exchange_by_id(sell_book.exchange_id)
        buy_wallet = self.ledger.get(buy_book.exchange_id)
        sell_wallet = self.ledger.get(sell_book.exchange_id)
        wallet_qty = min(float(sell_wallet["BTC"]), (float(buy_wallet["USDT"]) * 0.985) / ask.price)
        target_qty = min(self.settings.max_trade_btc, depth_qty(buy_book.asks), depth_qty(sell_book.bids), wallet_qty)

        if target_qty < self.settings.min_trade_btc:
            return Opportunity(
                id=f"{current}-{buy_book.exchange_id}-{sell_book.exchange_id}-blocked",
                strategy="simple",
                time=current,
                score=-1,
                status="blocked",
                net_profit=0,
                net_bps=0,
                gross_profit=0,
                gross_bps=((bid.price - ask.price) / ask.price) * 10000,
                confidence=0,
                partial=True,
                source="mixed",
                reason="Insufficient wallet balance or book depth",
                product=buy_book.symbol,
                data={
                    "buyExchangeId": buy_book.exchange_id,
                    "sellExchangeId": sell_book.exchange_id,
                    "buyExchange": buy_book.exchange_name,
                    "sellExchange": sell_book.exchange_name,
                    "qtyBtc": target_qty,
                    "targetQtyBtc": self.settings.max_trade_btc,
                    "filledRatio": rounded(target_qty / self.settings.max_trade_btc, 4) if self.settings.max_trade_btc else 0,
                    "buyPrice": ask.price,
                    "sellPrice": bid.price,
                    "buyDepthBtc": rounded(depth_qty(buy_book.asks), 8),
                    "sellDepthBtc": rounded(depth_qty(sell_book.bids), 8),
                },
            )

        buy_fill = estimate_fill(buy_book.asks, target_qty, "ask")
        sell_fill = estimate_fill(sell_book.bids, target_qty, "bid")
        qty = min(buy_fill.filled_qty, sell_fill.filled_qty)
        if qty < buy_fill.filled_qty:
            buy_fill = estimate_fill(buy_book.asks, qty, "ask")
        if qty < sell_fill.filled_qty:
            sell_fill = estimate_fill(sell_book.bids, qty, "bid")

        buy_fee = buy_fill.quote * buy_exchange.taker_fee_bps / 10000
        sell_fee = sell_fill.quote * sell_exchange.taker_fee_bps / 10000
        slippage_buy = buy_fill.quote * buy_exchange.slippage_bps / 10000
        slippage_sell = sell_fill.quote * sell_exchange.slippage_bps / 10000
        latency_seconds = (buy_book.latency_ms + sell_book.latency_ms) / 2000
        latency_risk_bps = max(self.settings.latency_risk_floor_bps, latency_seconds * self.settings.latency_bps_per_second)
        latency_risk_cost = buy_fill.quote * latency_risk_bps / 10000
        rebalance_cost = (buy_exchange.withdrawal_fee_btc * sell_fill.avg_price + sell_exchange.withdrawal_fee_quote) * self.settings.withdrawal_fee_impact
        gross_profit = sell_fill.quote - buy_fill.quote
        total_costs = buy_fee + sell_fee + slippage_buy + slippage_sell + latency_risk_cost + rebalance_cost
        net_profit = gross_profit - total_costs
        net_bps = net_profit / buy_fill.quote * 10000 if buy_fill.quote else 0
        gross_bps = gross_profit / buy_fill.quote * 10000 if buy_fill.quote else 0
        age_confidence = max(0.2, 1 - max(current - buy_book.timestamp, current - sell_book.timestamp) / self.settings.max_book_age_ms)
        confidence = min(buy_book.confidence, sell_book.confidence, age_confidence)
        profitable = net_profit >= self.settings.min_net_profit_usd and net_bps >= self.settings.min_net_bps and confidence >= self.settings.min_confidence
        partial = qty < self.settings.max_trade_btc or buy_fill.partial or sell_fill.partial
        latency_penalty = 1 + (buy_book.latency_ms + sell_book.latency_ms) / 800
        score = (net_bps * confidence * math.sqrt(max(qty, 0.000001))) / latency_penalty if profitable else net_bps * confidence
        source = buy_book.source if buy_book.source == sell_book.source else "mixed"

        return Opportunity(
            id=f"{current}-{buy_book.exchange_id}-{sell_book.exchange_id}-{round(buy_fill.avg_price)}-{round(sell_fill.avg_price)}",
            strategy="simple",
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
            reason="Net edge cleared risk gates" if profitable else "Costs or risk removed the edge",
            product=buy_book.symbol,
            costs={
                "buyFee": rounded(buy_fee, 4),
                "sellFee": rounded(sell_fee, 4),
                "slippageCostBuy": rounded(slippage_buy, 4),
                "slippageCostSell": rounded(slippage_sell, 4),
                "latencyRiskCost": rounded(latency_risk_cost, 4),
                "latencyRiskBps": rounded(latency_risk_bps, 3),
                "rebalanceCost": rounded(rebalance_cost, 4),
                "totalCosts": rounded(total_costs, 4),
            },
            data={
                "buyExchangeId": buy_book.exchange_id,
                "sellExchangeId": sell_book.exchange_id,
                "buyExchange": buy_book.exchange_name,
                "sellExchange": sell_book.exchange_name,
                "qtyBtc": rounded(qty, 8),
                "targetQtyBtc": self.settings.max_trade_btc,
                "filledRatio": rounded(qty / self.settings.max_trade_btc, 4) if self.settings.max_trade_btc else 1,
                "buyPrice": rounded(buy_fill.avg_price, 2),
                "sellPrice": rounded(sell_fill.avg_price, 2),
                "bestAsk": ask.price,
                "bestBid": bid.price,
                "grossSpread": rounded(bid.price - ask.price, 2),
                "fills": {"buyLevels": buy_fill.level_count, "sellLevels": sell_fill.level_count},
                "buyDepthBtc": rounded(depth_qty(buy_book.asks), 8),
                "sellDepthBtc": rounded(depth_qty(sell_book.bids), 8),
                "latencies": {"buyMs": buy_book.latency_ms, "sellMs": sell_book.latency_ms},
            },
        )
