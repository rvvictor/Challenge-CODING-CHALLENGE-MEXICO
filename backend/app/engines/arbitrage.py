from __future__ import annotations

import math
import time

from backend.app.core.config import Settings
from backend.app.core.models import Opportunity, OrderBook
from backend.app.engines.fills import best, depth_qty, estimate_fill
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.market_impact import impact_bps
from backend.app.engines.scoring import expected_value_score
from backend.app.engines.sizing import kelly_multiplier


def now_ms() -> int:
    return int(time.time() * 1000)


def rounded(value: float, decimals: int = 6) -> float:
    return round(value or 0.0, decimals)


def base_of(symbol: str) -> str:
    return symbol.split("/", 1)[0]


# USD notional anchor: trade sizes are expressed as MAX_TRADE_BTC BTC, i.e. this
# many USD. For BTC the size stays MAX_TRADE_BTC exactly (price ~= anchor); for
# alts the size is the notional-equivalent in the alt's own units, so a "trade"
# is a comparable dollar amount rather than 0.05 of a $0.50 coin.
NOTIONAL_ANCHOR_USD = 70000.0


class CrossExchangeArbitrageEngine:
    def __init__(self, settings: Settings, ledger: WalletLedger, calibrator=None):
        self.settings = settings
        self.ledger = ledger
        self.calibrator = calibrator

    def scan(self, books_by_exchange: dict[str, OrderBook]) -> list[dict]:
        current = now_ms()
        books = [book for book in books_by_exchange.values() if book.asks and book.bids]
        # Group by base asset so a cross-exchange pair is only ever formed between
        # books of the SAME coin (never XRP-ask vs BTC-bid). Demo passes only BTC
        # primaries, so grouping is a no-op there and behavior is unchanged; the
        # live path passes alt direct pairs too, and each base is scanned on its own.
        by_base: dict[str, list[OrderBook]] = {}
        for book in books:
            by_base.setdefault(base_of(book.symbol), []).append(book)
        opportunities = []
        for group in by_base.values():
            for buy_book in group:
                for sell_book in group:
                    if buy_book.exchange_id == sell_book.exchange_id:
                        continue
                    opportunity = self.evaluate_pair(buy_book, sell_book, current)
                    if opportunity:
                        opportunities.append(opportunity.to_dict())
        return sorted(opportunities, key=lambda item: item["score"], reverse=True)

    def _max_size(self, base_asset: str, ask_price: float) -> float:
        # BTC keeps MAX_TRADE_BTC exactly; alts use the notional-equivalent size.
        if base_asset == "BTC":
            return self.settings.max_trade_btc
        return (self.settings.max_trade_btc * NOTIONAL_ANCHOR_USD) / ask_price if ask_price else 0.0

    def _min_size(self, base_asset: str, ask_price: float) -> float:
        if base_asset == "BTC":
            return self.settings.min_trade_btc
        return (self.settings.min_trade_btc * NOTIONAL_ANCHOR_USD) / ask_price if ask_price else 0.0

    def target_size_btc(self, ask, bid, buy_book: OrderBook, sell_book: OrderBook, buy_exchange, sell_exchange, base_asset: str = "BTC") -> float:
        """Nominal trade size in BASE units. `fixed` uses the max size; `kelly`
        scales it by a fractional-Kelly multiplier derived from a quick
        top-of-book edge estimate, the venue confidences (win probability) and
        the adverse-move ceiling (downside). Sizing toward edge quality, capped
        at the max. For BTC the max is MAX_TRADE_BTC (unchanged); for alts it is
        the notional-equivalent quantity."""
        max_size = self._max_size(base_asset, ask.price)
        if self.settings.sizing_mode != "kelly":
            return max_size
        gross_bps = (bid.price - ask.price) / ask.price * 10000 if ask.price else 0
        cost_bps = (
            buy_exchange.taker_fee_bps + sell_exchange.taker_fee_bps
            + buy_exchange.slippage_bps + sell_exchange.slippage_bps
            + self.settings.latency_risk_floor_bps
        )
        edge_bps = gross_bps - cost_bps
        win_prob = min(buy_book.confidence, sell_book.confidence)
        payoff = edge_bps / max(self.settings.execution_adverse_max_bps, 0.1) if edge_bps > 0 else 0.0
        multiplier = kelly_multiplier(win_prob, payoff, self.settings.kelly_fraction)
        return max(self._min_size(base_asset, ask.price), max_size * multiplier)

    def evaluate_pair(self, buy_book: OrderBook, sell_book: OrderBook, current: int) -> Opportunity | None:
        ask = best(buy_book.asks, "ask")
        bid = best(sell_book.bids, "bid")
        if not ask or not bid or ask.price >= bid.price:
            return None

        buy_exchange = self.settings.exchange_by_id(buy_book.exchange_id)
        sell_exchange = self.settings.exchange_by_id(sell_book.exchange_id)
        base_asset = base_of(buy_book.symbol)
        capacity = self.ledger.route_capacity_btc(buy_book.exchange_id, sell_book.exchange_id, ask.price, self.settings.exchanges, base=base_asset)
        wallet_qty = float(capacity["qty"])
        buy_depth = depth_qty(buy_book.asks)
        sell_depth = depth_qty(sell_book.bids)
        target_size = self.target_size_btc(ask, bid, buy_book, sell_book, buy_exchange, sell_exchange, base_asset)
        target_qty = min(target_size, buy_depth, sell_depth, wallet_qty)
        min_size = self._min_size(base_asset, ask.price)

        if target_qty < min_size:
            gross_bps = ((bid.price - ask.price) / ask.price) * 10000
            blocked_reason = "Insufficient wallet inventory" if wallet_qty < min_size else "Insufficient book depth"
            return Opportunity(
                id=f"{current}-{buy_book.exchange_id}-{sell_book.exchange_id}-blocked",
                strategy="simple",
                time=current,
                score=0,
                status="blocked",
                net_profit=0,
                net_bps=0,
                gross_profit=0,
                gross_bps=rounded(gross_bps, 3),
                confidence=0,
                partial=target_qty > 0,
                source="mixed",
                reason=blocked_reason,
                product=buy_book.symbol,
                data={
                    "baseAsset": base_of(buy_book.symbol),
                    "buyExchangeId": buy_book.exchange_id,
                    "sellExchangeId": sell_book.exchange_id,
                    "buyExchange": buy_book.exchange_name,
                    "sellExchange": sell_book.exchange_name,
                    "qtyBtc": target_qty,
                    "targetQtyBtc": rounded(target_size, 8),
                    "filledRatio": rounded(target_qty / target_size, 4) if target_size else 0,
                    "buyPrice": ask.price,
                    "sellPrice": bid.price,
                    "buyDepthBtc": rounded(buy_depth, 8),
                    "sellDepthBtc": rounded(sell_depth, 8),
                    "walletQtyBtc": rounded(wallet_qty, 8),
                    "inventoryMode": capacity["mode"],
                    "expectedValue": 0,
                    "evBps": 0,
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
        impact_model = self.settings.slippage_model
        impact_bps_buy = impact_bps(impact_model, qty, buy_depth, self.settings.market_impact_k)
        impact_bps_sell = impact_bps(impact_model, qty, sell_depth, self.settings.market_impact_k)
        market_impact_cost = (buy_fill.quote * impact_bps_buy + sell_fill.quote * impact_bps_sell) / 10000
        # Average per-leg latency in seconds. Kept numerically identical to the
        # previous `/ 2000` form ((a+b)/2/1000 == (a+b)/2000) but written
        # explicitly so the averaging is obvious and the unnamed constant is gone.
        latency_seconds = (buy_book.latency_ms + sell_book.latency_ms) / 2 / 1000.0
        latency_risk_bps = max(self.settings.latency_risk_floor_bps, latency_seconds * self.settings.latency_bps_per_second)
        latency_risk_cost = buy_fill.quote * latency_risk_bps / 10000
        rebalance_cost = (buy_exchange.withdrawal_fee_btc * sell_fill.avg_price + sell_exchange.withdrawal_fee_quote) * self.settings.withdrawal_fee_impact
        gross_profit = sell_fill.quote - buy_fill.quote
        total_costs = buy_fee + sell_fee + slippage_buy + slippage_sell + market_impact_cost + latency_risk_cost + rebalance_cost
        net_profit = gross_profit - total_costs
        net_bps = net_profit / buy_fill.quote * 10000 if buy_fill.quote else 0
        gross_bps = gross_profit / buy_fill.quote * 10000 if buy_fill.quote else 0
        age_confidence = max(0.2, 1 - max(current - buy_book.timestamp, current - sell_book.timestamp) / self.settings.max_book_age_ms)
        confidence = min(buy_book.confidence, sell_book.confidence, age_confidence)
        if self.settings.calibration_enabled and self.calibrator:
            confidence *= self.calibrator.factor(buy_book.exchange_id, sell_book.exchange_id)
        inventory_penalty = rebalance_cost if capacity["mode"] == "rebalanced" else 0
        ev = expected_value_score(
            net_profit=net_profit,
            notional=buy_fill.quote,
            confidence=confidence,
            latency_ms=buy_book.latency_ms + sell_book.latency_ms,
            latency_risk_cost=latency_risk_cost,
            inventory_penalty=inventory_penalty,
            settings=self.settings,
        )
        profitable = net_profit >= self.settings.min_net_profit_usd and net_bps >= self.settings.min_net_bps and confidence >= self.settings.min_confidence
        partial = qty < target_size or buy_fill.partial or sell_fill.partial
        latency_penalty = 1 + (buy_book.latency_ms + sell_book.latency_ms) / 800
        ev_score = ev["evBps"] * math.sqrt(max(qty, 0.000001)) / latency_penalty
        score = ev_score if profitable else ev["evBps"] * confidence
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
                "marketImpactCost": rounded(market_impact_cost, 4),
                "marketImpactBps": rounded(impact_bps_buy + impact_bps_sell, 3),
                "latencyRiskCost": rounded(latency_risk_cost, 4),
                "latencyRiskBps": rounded(latency_risk_bps, 3),
                "volatilityRiskCost": ev["volatilityRiskCost"],
                "inventoryPenalty": ev["inventoryPenalty"],
                "latencyPenaltyCost": ev["latencyPenaltyCost"],
                "rebalanceCost": rounded(rebalance_cost, 4),
                "totalCosts": rounded(total_costs, 4),
            },
            data={
                "baseAsset": base_asset,
                "buyExchangeId": buy_book.exchange_id,
                "sellExchangeId": sell_book.exchange_id,
                "buyExchange": buy_book.exchange_name,
                "sellExchange": sell_book.exchange_name,
                "qtyBtc": rounded(qty, 8),
                "targetQtyBtc": rounded(target_size, 8),
                "filledRatio": rounded(qty / target_size, 4) if target_size else 1,
                "sizingMode": self.settings.sizing_mode,
                "slippageModel": impact_model,
                "buyPrice": rounded(buy_fill.avg_price, 2),
                "sellPrice": rounded(sell_fill.avg_price, 2),
                "bestAsk": ask.price,
                "bestBid": bid.price,
                "grossSpread": rounded(bid.price - ask.price, 2),
                "fills": {"buyLevels": buy_fill.level_count, "sellLevels": sell_fill.level_count},
                "buyDepthBtc": rounded(buy_depth, 8),
                "sellDepthBtc": rounded(sell_depth, 8),
                "walletQtyBtc": rounded(wallet_qty, 8),
                "inventoryMode": capacity["mode"],
                "latencies": {"buyMs": buy_book.latency_ms, "sellMs": sell_book.latency_ms},
                **ev,
            },
        )
