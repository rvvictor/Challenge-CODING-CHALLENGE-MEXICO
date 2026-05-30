from __future__ import annotations

import time
import uuid

from backend.app.core.config import Settings
from backend.app.engines.ledger import WalletLedger
from backend.app.engines.risk import RiskManager
from backend.app.engines.event_store import EventStore


def now_ms() -> int:
    return int(time.time() * 1000)


class ExecutionSimulator:
    def __init__(self, settings: Settings, ledger: WalletLedger, store: EventStore, risk: RiskManager):
        self.settings = settings
        self.ledger = ledger
        self.store = store
        self.risk = risk
        self.cooldowns: dict[str, int] = {}

    def reset(self) -> None:
        self.cooldowns.clear()

    def try_execute(self, opportunities: list[dict], books: list[dict]) -> list[dict]:
        current = now_ms()
        risk = self.risk.can_execute(books, current)
        if not risk["allowed"]:
            return []
        executions = []
        for opportunity in opportunities:
            if len(executions) >= self.settings.max_executions_per_tick:
                break
            if opportunity.get("status") != "profitable":
                continue
            key = opportunity.get("dedupeKey") or opportunity.get("id")
            if self.cooldowns.get(key, 0) > current:
                continue
            trade = self.build_trade(opportunity)
            if not self.has_inventory(trade):
                continue
            self.ledger.apply_trade(trade)
            self.cooldowns[key] = current + self.settings.pair_cooldown_ms
            self.risk.record_trade(trade, current)
            self.store.add_trade(trade, self.ledger.realized_pnl)
            executions.append(trade)
        return executions

    def has_inventory(self, trade: dict) -> bool:
        if trade["strategy"] == "triangular":
            wallet = self.ledger.get(trade["exchangeId"])
            return float(wallet["USDT"]) >= trade["quoteIn"]

        buy = self.ledger.get(trade["buyExchangeId"])
        sell = self.ledger.get(trade["sellExchangeId"])
        buy_debit = trade["buyQuote"] + trade["buyFee"] + trade["slippageCostBuy"] + trade["rebalanceCost"]
        return float(buy["USDT"]) >= buy_debit and float(sell["BTC"]) >= trade["qtyBtc"]

    def build_trade(self, opportunity: dict) -> dict:
        base = {
            "id": f"T-{uuid.uuid4().hex[:10]}",
            "time": now_ms(),
            "strategy": opportunity["strategy"],
            "opportunityId": opportunity["id"],
            "grossProfit": opportunity["grossProfit"],
            "netProfit": opportunity["netProfit"],
            "netBps": opportunity["netBps"],
            "confidence": opportunity["confidence"],
            "partial": opportunity["partial"],
            "filledRatio": opportunity.get("filledRatio", 1),
            "targetQtyBtc": opportunity.get("targetQtyBtc"),
            "targetQuote": opportunity.get("targetQuote"),
            "fills": opportunity.get("fills", {}),
            "legPartials": opportunity.get("legPartials", []),
            "source": opportunity["source"],
            "totalCosts": opportunity.get("costs", {}).get("totalCosts", 0),
            "executionQuality": {
                "edgeCaptureBps": opportunity["netBps"],
                "confidence": opportunity["confidence"],
                "costRatio": (opportunity.get("costs", {}).get("totalCosts", 0) / max(abs(opportunity.get("grossProfit", 0)), 0.000001)),
            },
            "status": "partial-cycle" if opportunity["strategy"] == "triangular" and opportunity["partial"] else "partial-fill" if opportunity["partial"] else "filled",
        }
        if opportunity["strategy"] == "triangular":
            return {
                **base,
                "exchangeId": opportunity["exchangeId"],
                "exchange": opportunity["exchange"],
                "product": opportunity["product"],
                "cycleId": opportunity["cycleId"],
                "cyclePath": opportunity["cyclePath"],
                "quoteIn": opportunity["quoteIn"],
                "quoteOut": opportunity["quoteOut"],
                "qtyBtc": opportunity["qtyBtc"],
                "qtyEth": opportunity["qtyEth"],
                "legs": opportunity["legs"],
            }
        return {
            **base,
            "buyExchangeId": opportunity["buyExchangeId"],
            "sellExchangeId": opportunity["sellExchangeId"],
            "buyExchange": opportunity["buyExchange"],
            "sellExchange": opportunity["sellExchange"],
            "product": opportunity["product"],
            "qtyBtc": opportunity["qtyBtc"],
            "buyPrice": opportunity["buyPrice"],
            "sellPrice": opportunity["sellPrice"],
            "buyQuote": opportunity["buyPrice"] * opportunity["qtyBtc"],
            "sellQuote": opportunity["sellPrice"] * opportunity["qtyBtc"],
            "buyFee": opportunity["costs"]["buyFee"],
            "sellFee": opportunity["costs"]["sellFee"],
            "slippageCostBuy": opportunity["costs"]["slippageCostBuy"],
            "slippageCostSell": opportunity["costs"]["slippageCostSell"],
            "latencyRiskCost": opportunity["costs"]["latencyRiskCost"],
            "rebalanceCost": opportunity["costs"]["rebalanceCost"],
        }
