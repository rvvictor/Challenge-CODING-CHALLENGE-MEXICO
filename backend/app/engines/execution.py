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
    def __init__(self, settings: Settings, ledger: WalletLedger, store: EventStore, risk: RiskManager, gateway=None):
        self.settings = settings
        self.ledger = ledger
        self.store = store
        self.risk = risk
        # The execution gateway the trade loop settles through. Paper /
        # read-only-live settle as a provenance passthrough (identical P&L);
        # the testnet gateway places real sandbox orders and returns real fills.
        # Defaults to a paper gateway so the simulator works standalone (tests).
        if gateway is None:
            from backend.app.integrations.gateways import PaperExecutionGateway

            gateway = PaperExecutionGateway()
        self.gateway = gateway
        self.book_map: dict = {}
        self.cooldowns: dict[str, int] = {}
        self.last_demo_execution_at = 0
        self.leg_failure_until = 0

    def reset(self) -> None:
        self.cooldowns.clear()
        self.last_demo_execution_at = 0
        self.leg_failure_until = 0

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
            if self.demo_throttled(opportunity, current):
                continue
            key = opportunity.get("dedupeKey") or opportunity.get("id")
            if self.cooldowns.get(key, 0) > current:
                continue
            trade = self.build_trade(opportunity)
            transfers = self.ledger.prepare_inventory_for_trade(trade)
            if transfers:
                trade["inventoryRebalance"] = transfers
            if not self.has_inventory(trade):
                continue
            # Route settlement through the execution gateway. Paper /
            # read-only-live tag provenance and return the modeled trade
            # unchanged; the testnet gateway places real sandbox orders and
            # rewrites the fill. A gateway that rejects returns None.
            settled = self.gateway.settle_trade(trade, opportunity, self.book_map)
            if settled is None:
                continue
            trade = settled
            self.ledger.apply_trade(trade)
            self.cooldowns[key] = current + self.settings.pair_cooldown_ms
            self.risk.record_trade(trade, current)
            self.store.add_trade(trade, self.ledger.realized_pnl)
            if trade.get("source") == "simulated":
                self.last_demo_execution_at = current
            executions.append(trade)
        return executions

    def demo_throttled(self, opportunity: dict, current: int) -> bool:
        return (
            opportunity.get("source") == "simulated"
            and self.settings.demo_min_execution_gap_ms > 0
            and current - self.last_demo_execution_at < self.settings.demo_min_execution_gap_ms
        )

    def has_inventory(self, trade: dict) -> bool:
        if trade["strategy"] == "triangular":
            wallet = self.ledger.get(trade["exchangeId"])
            return float(wallet["USDT"]) >= trade["quoteIn"]

        base = trade.get("baseAsset", "BTC")
        buy = self.ledger.get(trade["buyExchangeId"])
        sell = self.ledger.get(trade["sellExchangeId"])
        buy_debit = trade["buyQuote"] + trade["buyFee"] + trade["slippageCostBuy"] + trade["rebalanceCost"]
        return float(buy["USDT"]) >= buy_debit and float(sell[base]) >= trade["qtyBtc"]

    def reconcile_fills(self, opportunity: dict) -> dict | None:
        """Per-leg reconciliation for cross-exchange trades: intended vs filled on
        each leg, the resulting open exposure, and the corrective cover. Normally
        both legs fill the same size (hedged, zero exposure). Under a `leg_failure`
        scenario the sell leg under-fills, leaving the bot net-long until it covers
        the residual at a worse price (a real cost, charged to P&L)."""
        if opportunity.get("strategy") == "triangular":
            return None
        intended = round(float(opportunity.get("qtyBtc", 0)), 8)
        buy_filled = intended
        sell_filled = intended
        cover_bps = 0.0
        cover_cost = 0.0
        if now_ms() < self.leg_failure_until and intended > 0:
            sell_filled = round(intended * 0.55, 8)
            exposure = round(buy_filled - sell_filled, 8)
            cover_bps = round(self.settings.execution_adverse_max_bps + 6, 3)
            cover_cost = round(exposure * float(opportunity.get("sellPrice", 0)) * cover_bps / 10000, 4)
        exposure = round(buy_filled - sell_filled, 8)
        return {
            "intendedQtyBtc": intended,
            "buyFilledBtc": buy_filled,
            "sellFilledBtc": sell_filled,
            "netExposureBtc": exposure,
            "hedged": abs(exposure) < 1e-9,
            "correctiveAction": "cover-residual" if exposure > 1e-9 else "none",
            "coverBps": cover_bps,
            "coverCost": cover_cost,
        }

    def build_trade(self, opportunity: dict) -> dict:
        adverse = self.adverse_price_movement(opportunity)
        reconciliation = self.reconcile_fills(opportunity)
        cover_cost = reconciliation["coverCost"] if reconciliation else 0.0
        base = {
            "id": f"T-{uuid.uuid4().hex[:10]}",
            "time": now_ms(),
            "strategy": opportunity["strategy"],
            "opportunityId": opportunity["id"],
            "grossProfit": opportunity["grossProfit"],
            "netProfit": round(opportunity["netProfit"] - adverse["cost"] - cover_cost, 4),
            "netBps": opportunity["netBps"],
            "expectedValue": opportunity.get("expectedValue", opportunity["netProfit"]),
            "evBps": opportunity.get("evBps", opportunity["netBps"]),
            "confidence": opportunity["confidence"],
            "partial": opportunity["partial"],
            "filledRatio": opportunity.get("filledRatio", 1),
            "targetQtyBtc": opportunity.get("targetQtyBtc"),
            "targetQuote": opportunity.get("targetQuote"),
            "fills": opportunity.get("fills", {}),
            "legPartials": opportunity.get("legPartials", []),
            "source": opportunity["source"],
            "dynamicCycle": bool(opportunity.get("dynamicCycle")),
            "totalCosts": opportunity.get("costs", {}).get("totalCosts", 0),
            "executionQuality": {
                "edgeCaptureBps": round(opportunity["netBps"] - adverse["bps"], 4),
                "confidence": opportunity["confidence"],
                "costRatio": (opportunity.get("costs", {}).get("totalCosts", 0) / max(abs(opportunity.get("grossProfit", 0)), 0.000001)),
                "adverseMoveBps": adverse["bps"],
                "adverseMoveCost": adverse["cost"],
                "latencyCaptureProbability": opportunity.get("latencyCaptureProbability", 1),
            },
            "adversePriceMove": adverse,
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
            "adverseMoveCost": adverse["cost"],
            "coverCost": cover_cost,
            "reconciliation": reconciliation,
            "status": "leg-failure" if reconciliation and reconciliation["netExposureBtc"] > 1e-9 else base["status"],
        }

    def adverse_price_movement(self, opportunity: dict) -> dict:
        latencies = opportunity.get("latencies") or {}
        latency_ms = float(latencies.get("totalMs") or (latencies.get("buyMs", 0) + latencies.get("sellMs", 0)) or 0)
        notional = float(opportunity.get("quoteIn") or (opportunity.get("buyPrice", 0) * opportunity.get("qtyBtc", 0)) or 0)
        confidence_gap = max(0.0, 1 - float(opportunity.get("confidence") or 0))
        adverse_bps = min(
            self.settings.execution_adverse_max_bps,
            (latency_ms / 1000) * self.settings.execution_adverse_bps_per_second + confidence_gap * 0.18,
        )
        cost = notional * adverse_bps / 10000
        return {
            "bps": round(adverse_bps, 4),
            "cost": round(cost, 4),
            "latencyMs": round(latency_ms, 1),
            "model": "latency-adverse-move",
        }
