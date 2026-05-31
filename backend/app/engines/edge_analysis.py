from __future__ import annotations

from statistics import mean
from typing import Any


def rounded(value: float | int | None, decimals: int = 2) -> float:
    try:
        return round(float(value or 0), decimals)
    except (TypeError, ValueError):
        return 0.0


def clamp(value: float, lower: float = 0, upper: float = 100) -> float:
    return max(lower, min(upper, value))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[index]


def route_name(item: dict[str, Any]) -> str:
    if item.get("strategy") == "triangular":
        path = " -> ".join(item.get("cyclePath") or ["USDT", "BTC", "ETH", "USDT"])
        return f"{item.get('exchange', 'venue')} / {path}"
    return f"{item.get('buyExchange', 'buy')} -> {item.get('sellExchange', 'sell')}"


def opportunity_notional(item: dict[str, Any]) -> float:
    if item.get("strategy") == "triangular":
        return float(item.get("quoteIn") or item.get("targetQuote") or 0)
    return float(item.get("buyPrice") or 0) * float(item.get("qtyBtc") or 0)


def opportunity_latency_ms(item: dict[str, Any]) -> float:
    latencies = item.get("latencies") or {}
    if "totalMs" in latencies:
        return float(latencies.get("totalMs") or 0)
    return float(latencies.get("buyMs") or 0) + float(latencies.get("sellMs") or 0)


def explain_opportunity(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    costs = payload.get("costs") or {}
    status = payload.get("status")
    net_bps = float(payload.get("netBps") or 0)
    gross_profit = float(payload.get("grossProfit") or 0)
    net_profit = float(payload.get("netProfit") or 0)
    total_costs = float(costs.get("totalCosts") or 0)
    confidence = clamp(float(payload.get("confidence") or 0), 0, 1)
    filled_ratio = clamp(float(payload.get("filledRatio") if payload.get("filledRatio") is not None else 1), 0, 1)
    latency_ms = opportunity_latency_ms(payload)
    notional = max(opportunity_notional(payload), 0.000001)
    rebalance_cost = float(costs.get("rebalanceCost") or 0)
    cost_drag = total_costs / max(abs(gross_profit), 0.000001) if gross_profit else 1

    edge_points = clamp((net_bps + 2.5) * 4.2, 0, 34)
    cost_points = clamp((1 - cost_drag) * 18, 0, 18)
    liquidity_points = clamp(filled_ratio * 18, 0, 18)
    confidence_points = clamp(confidence * 18, 0, 18)
    latency_points = clamp(12 * (1 - latency_ms / 1300), 0, 12)
    explainable_score = rounded(edge_points + cost_points + liquidity_points + confidence_points + latency_points, 1)

    if status == "profitable" and payload.get("partial"):
        decision = "execute-partial"
        decision_summary = "Execute as partial: net edge survives costs, but only part of the target size is liquid enough."
    elif status == "profitable":
        decision = "execute-full"
        decision_summary = "Execute full target: net edge clears fees, slippage, latency and inventory checks."
    elif status == "blocked":
        reason = str(payload.get("reason") or "").lower()
        decision = "inventory-gate" if "wallet" in reason or "inventory" in reason else "liquidity-gate"
        decision_summary = "Do not execute: route failed a hard inventory or depth gate before scoring."
    else:
        decision = "skip-costs"
        decision_summary = "Skip: raw spread exists, but costs, latency or confidence removed the executable edge."

    prefunded_net_profit = net_profit + rebalance_cost
    settlement_net_profit = net_profit
    settlement_drag_bps = rebalance_cost / notional * 10000
    if settlement_net_profit > 0:
        settlement_verdict = "settlement-safe"
    elif prefunded_net_profit > 0:
        settlement_verdict = "prefunded-only"
    else:
        settlement_verdict = "not-viable"

    payload["decision"] = {
        "route": route_name(payload),
        "action": decision,
        "summary": decision_summary,
        "explainableScore": explainable_score,
        "scoreGrade": "A" if explainable_score >= 82 else "B" if explainable_score >= 66 else "C" if explainable_score >= 48 else "D",
        "mainBlocker": payload.get("reason") or decision_summary,
    }
    payload["edgeBreakdown"] = {
        "netBps": rounded(net_bps, 3),
        "expectedValue": rounded(payload.get("expectedValue"), 4),
        "evBps": rounded(payload.get("evBps"), 3),
        "latencyCaptureProbability": rounded(payload.get("latencyCaptureProbability"), 4),
        "grossBps": rounded(payload.get("grossBps"), 3),
        "grossProfit": rounded(gross_profit, 4),
        "totalCosts": rounded(total_costs, 4),
        "costDragPct": rounded(cost_drag * 100, 2),
        "filledRatio": rounded(filled_ratio, 4),
        "confidence": rounded(confidence, 3),
        "latencyMs": rounded(latency_ms, 1),
        "components": [
            {"label": "Net edge", "value": rounded(edge_points, 1), "max": 34},
            {"label": "Cost drag", "value": rounded(cost_points, 1), "max": 18},
            {"label": "Liquidity", "value": rounded(liquidity_points, 1), "max": 18},
            {"label": "Confidence", "value": rounded(confidence_points, 1), "max": 18},
            {"label": "Latency", "value": rounded(latency_points, 1), "max": 12},
        ],
    }
    payload["paperVsSettlement"] = {
        "notionalUsd": rounded(notional, 2),
        "prefundedNetProfit": rounded(prefunded_net_profit, 4),
        "settlementNetProfit": rounded(settlement_net_profit, 4),
        "settlementDrag": rounded(rebalance_cost, 4),
        "settlementDragBps": rounded(settlement_drag_bps, 3),
        "verdict": settlement_verdict,
        "note": "Prefunded assumes inventory already sits on both venues; settlement includes rebalance drag.",
    }
    return payload


def latency_slo(books: list[dict[str, Any]]) -> dict[str, Any]:
    ages = [float(book.get("ageMs") or 0) for book in books]
    updates = [float(book.get("latencyMs") or 0) for book in books]
    p95_age = percentile(ages, 0.95)
    p95_update = percentile(updates, 0.95)
    status = "green"
    if p95_age > 2500 or p95_update > 900:
        status = "breach"
    elif p95_age > 1000 or p95_update > 450:
        status = "watch"
    return {
        "status": status,
        "healthy": status != "breach",
        "summary": "SLO healthy" if status == "green" else "Latency watch" if status == "watch" else "Latency breach",
        "bookAgeMs": {
            "p50": rounded(percentile(ages, 0.5), 0),
            "p95": rounded(p95_age, 0),
            "p99": rounded(percentile(ages, 0.99), 0),
            "targetP95": 1000,
        },
        "updateLatencyMs": {
            "p50": rounded(percentile(updates, 0.5), 0),
            "p95": rounded(p95_update, 0),
            "p99": rounded(percentile(updates, 0.99), 0),
            "targetP95": 450,
        },
    }


def venue_quality(books: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for book in books:
        mid = (float(book.get("bestAsk") or 0) + float(book.get("bestBid") or 0)) / 2
        spread_bps = (float(book.get("spread") or 0) / mid * 10000) if mid else 0
        freshness_score = clamp(32 * (1 - float(book.get("ageMs") or 0) / 4500), 0, 32)
        latency_score = clamp(22 * (1 - float(book.get("latencyMs") or 0) / 1200), 0, 22)
        spread_score = clamp(16 * (1 - spread_bps / 12), 0, 16)
        confidence_score = clamp(float(book.get("confidence") or 0) * 20, 0, 20)
        source_score = 10 if book.get("source") == "websocket" else 7 if book.get("source") == "simulated" else 5 if book.get("source") == "rest" else 2
        score = rounded(freshness_score + latency_score + spread_score + confidence_score + source_score, 1)
        rows.append({
            "exchangeId": book.get("exchangeId"),
            "exchangeName": book.get("exchangeName"),
            "symbol": book.get("symbol"),
            "source": book.get("source"),
            "score": score,
            "grade": "A" if score >= 85 else "B" if score >= 72 else "C" if score >= 58 else "D",
            "status": "leader" if score >= 85 else "healthy" if score >= 72 else "watch" if score >= 58 else "lagging",
            "healthScore": rounded(book.get("healthScore", score), 1),
            "healthStatus": book.get("healthStatus", "healthy"),
            "priorityFactor": rounded(book.get("priorityFactor", 1), 3),
            "ageMs": rounded(book.get("ageMs"), 0),
            "latencyMs": rounded(book.get("latencyMs"), 0),
            "spreadBps": rounded(spread_bps, 3),
        })
    return sorted(rows, key=lambda item: item["score"], reverse=True)


def demo_quality(mode: str, metrics: dict[str, Any], uptime_ms: int, risk: dict[str, Any]) -> dict[str, Any]:
    elapsed_minutes = max(uptime_ms / 60000, 0.1)
    pnl_per_min = float(metrics.get("cumulativePnl") or 0) / elapsed_minutes
    fills_per_min = float(metrics.get("executedCount") or 0) / elapsed_minutes
    signals_per_min = float(metrics.get("detectedCount") or 0) / elapsed_minutes
    executed = max(int(metrics.get("executedCount") or 0), 1)
    partial_rate = float(metrics.get("partialCount") or 0) / executed

    checks = []
    score = 100.0
    if mode == "demo" and pnl_per_min > 18:
        score -= min(35, (pnl_per_min - 18) * 1.4)
        checks.append("P&L is heating up faster than a believable demo.")
    if mode == "demo" and fills_per_min > 4:
        score -= min(24, (fills_per_min - 4) * 5)
        checks.append("Fill frequency is above the intended presentation range.")
    if mode == "demo" and signals_per_min > 75:
        score -= 14
        checks.append("Detection tape is noisy; curated view should stay compact.")
    if mode == "demo" and metrics.get("executedCount", 0) == 0 and uptime_ms > 120000:
        score -= 18
        checks.append("Demo is too quiet after two minutes.")
    if mode == "demo" and metrics.get("executedCount", 0) >= 3 and partial_rate < 0.08:
        score -= 8
        checks.append("Partial fills are underrepresented for judge visibility.")
    if risk.get("paused"):
        score -= 10
        checks.append("Circuit breaker is active; new trades are intentionally paused.")
    if not checks:
        checks.append("Demo cadence is inside the realistic showcase band.")

    score = rounded(clamp(score), 0)
    if score >= 84:
        label = "realistic"
    elif score >= 66:
        label = "watch"
    elif pnl_per_min > 18 or fills_per_min > 4:
        label = "too-hot"
    else:
        label = "too-quiet"
    return {
        "score": score,
        "label": label,
        "pnlPerMinute": rounded(pnl_per_min, 2),
        "fillsPerMinute": rounded(fills_per_min, 2),
        "signalsPerMinute": rounded(signals_per_min, 1),
        "partialRate": rounded(partial_rate, 3),
        "checks": checks[:3],
    }


def compact_opportunity_record(item: dict[str, Any]) -> dict[str, Any]:
    decision = item.get("decision") or {}
    return {
        "id": item.get("id"),
        "route": route_name(item),
        "strategy": item.get("strategy"),
        "status": item.get("status"),
        "action": decision.get("action"),
        "decision": decision.get("summary"),
        "explainableScore": decision.get("explainableScore"),
        "score": item.get("score"),
        "netProfit": item.get("netProfit"),
        "netBps": item.get("netBps"),
        "partial": item.get("partial"),
        "source": item.get("source"),
        "reason": item.get("reason"),
        "paperVsSettlement": item.get("paperVsSettlement"),
    }


def session_summary(opportunities: list[dict[str, Any]], trades: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    trade_pnl = [float(trade.get("netProfit") or 0) for trade in trades]
    return {
        "opportunities": len(opportunities),
        "trades": len(trades),
        "events": len(events),
        "winRate": rounded(sum(1 for pnl in trade_pnl if pnl >= 0) / len(trade_pnl), 3) if trade_pnl else 0,
        "avgTradePnl": rounded(mean(trade_pnl), 4) if trade_pnl else 0,
        "profitableSignals": sum(1 for item in opportunities if item.get("status") == "profitable"),
        "partialSignals": sum(1 for item in opportunities if item.get("partial")),
        "triangularSignals": sum(1 for item in opportunities if item.get("strategy") == "triangular"),
    }
