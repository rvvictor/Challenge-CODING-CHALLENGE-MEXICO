from __future__ import annotations

import math

from backend.app.core.config import Settings


def rounded(value: float | int | None, decimals: int = 6) -> float:
    try:
        return round(float(value or 0), decimals)
    except (TypeError, ValueError):
        return 0.0


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def latency_capture_probability(latency_ms: float, settings: Settings) -> float:
    half_life = max(float(settings.latency_half_life_ms), 1.0)
    probability = math.exp(-math.log(2) * max(latency_ms, 0.0) / half_life)
    return clamp(probability, 0.35, 1.0)


def expected_value_score(
    *,
    net_profit: float,
    notional: float,
    confidence: float,
    latency_ms: float,
    latency_risk_cost: float,
    inventory_penalty: float,
    settings: Settings,
) -> dict:
    notional = max(float(notional or 0), 0.000001)
    confidence = clamp(float(confidence or 0), 0, 1)
    capture_probability = latency_capture_probability(latency_ms, settings)
    volatility_risk_cost = notional * max(settings.volatility_ev_risk_bps, 0) / 10000
    weighted_latency_cost = latency_risk_cost * max(settings.ev_latency_cost_weight, 0)
    weighted_inventory_penalty = inventory_penalty * max(settings.inventory_ev_penalty_weight, 0)
    expected_value = (
        float(net_profit or 0) * confidence * capture_probability
        - weighted_latency_cost
        - volatility_risk_cost
        - weighted_inventory_penalty
    )
    ev_bps = expected_value / notional * 10000
    return {
        "expectedValue": rounded(expected_value, 4),
        "evBps": rounded(ev_bps, 3),
        "latencyCaptureProbability": rounded(capture_probability, 4),
        "volatilityRiskCost": rounded(volatility_risk_cost, 4),
        "inventoryPenalty": rounded(weighted_inventory_penalty, 4),
        "latencyPenaltyCost": rounded(weighted_latency_cost, 4),
    }
