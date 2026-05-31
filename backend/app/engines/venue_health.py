from __future__ import annotations

from backend.app.core.config import ExchangeConfig, Settings


def rounded(value: float | int | None, decimals: int = 2) -> float:
    try:
        return round(float(value or 0), decimals)
    except (TypeError, ValueError):
        return 0.0


class VenueHealthTracker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.states: dict[str, dict] = {}
        self.sync(settings.exchanges)

    def sync(self, exchanges: tuple[ExchangeConfig, ...]) -> None:
        active_ids = {exchange.id for exchange in exchanges}
        for exchange in exchanges:
            self.states.setdefault(exchange.id, {
                "exchangeId": exchange.id,
                "exchangeName": exchange.name,
                "score": 100.0,
                "status": "healthy",
                "confidenceFactor": 1.0,
                "staleTicks": 0,
                "slowTicks": 0,
                "errorTicks": 0,
                "healthyTicks": 0,
                "lastReason": "Healthy",
                "lastLatencyMs": 0,
                "lastAgeMs": 0,
            })
            self.states[exchange.id]["exchangeName"] = exchange.name
        self.states = {exchange_id: state for exchange_id, state in self.states.items() if exchange_id in active_ids}

    def reset(self) -> None:
        self.states.clear()
        self.sync(self.settings.exchanges)

    def record_books(self, summaries: list[dict], stream_snapshot: dict | None = None) -> None:
        by_exchange = {summary.get("exchangeId"): summary for summary in summaries}
        stream_snapshot = stream_snapshot or {"streams": []}
        stream_errors: dict[str, int] = {}
        rest_modes: dict[str, int] = {}
        disabled: dict[str, int] = {}
        for stream in stream_snapshot.get("streams", []):
            exchange_id = stream.get("exchangeId")
            if not exchange_id:
                continue
            if stream.get("lastError") or stream.get("failures", 0) > 0:
                stream_errors[exchange_id] = stream_errors.get(exchange_id, 0) + 1
            if stream.get("restFallback"):
                rest_modes[exchange_id] = rest_modes.get(exchange_id, 0) + 1
            if stream.get("disabled"):
                disabled[exchange_id] = disabled.get(exchange_id, 0) + 1

        for exchange_id, state in self.states.items():
            summary = by_exchange.get(exchange_id)
            if summary:
                state["lastLatencyMs"] = float(summary.get("latencyMs") or 0)
                state["lastAgeMs"] = float(summary.get("ageMs") or 0)
                stale = state["lastAgeMs"] > self.settings.max_book_age_ms
                slow = state["lastLatencyMs"] > self.settings.health_slow_latency_ms
            else:
                stale = True
                slow = False
                state["lastAgeMs"] = self.settings.max_book_age_ms + 1

            has_error = stream_errors.get(exchange_id, 0) > 0 or disabled.get(exchange_id, 0) > 0
            state["staleTicks"] = state["staleTicks"] + 1 if stale else max(0, state["staleTicks"] - 1)
            state["slowTicks"] = state["slowTicks"] + 1 if slow else max(0, state["slowTicks"] - 1)
            state["errorTicks"] = state["errorTicks"] + 1 if has_error else max(0, state["errorTicks"] - 1)
            healthy = not stale and not slow and not has_error
            state["healthyTicks"] = state["healthyTicks"] + 1 if healthy else 0

            score = 100.0
            score -= min(36, state["staleTicks"] * 9)
            score -= min(26, state["slowTicks"] * 5)
            score -= min(32, state["errorTicks"] * 8)
            score -= min(12, rest_modes.get(exchange_id, 0) * 4)
            score -= min(45, disabled.get(exchange_id, 0) * 15)
            state["score"] = max(0.0, score)

            demote = (
                state["score"] < self.settings.health_min_score
                or state["staleTicks"] >= self.settings.exchange_demotion_ticks
                or state["errorTicks"] >= self.settings.exchange_demotion_ticks
            )
            if demote:
                state["status"] = "demoted"
                state["confidenceFactor"] = max(0.35, state["score"] / 100)
                state["lastReason"] = "Auto-demoted by stale/error/latency health scoring"
            elif state["slowTicks"] > 0 or rest_modes.get(exchange_id, 0) > 0:
                state["status"] = "watch"
                state["confidenceFactor"] = max(0.68, state["score"] / 100)
                state["lastReason"] = "Latency or REST fallback under watch"
            elif state["healthyTicks"] >= self.settings.exchange_recovery_ticks or state["status"] != "demoted":
                state["status"] = "healthy"
                state["confidenceFactor"] = 1.0
                state["lastReason"] = "Healthy"

    def confidence_factor(self, exchange_id: str) -> float:
        return float(self.states.get(exchange_id, {}).get("confidenceFactor", 1.0))

    def status(self, exchange_id: str) -> str:
        return str(self.states.get(exchange_id, {}).get("status", "healthy"))

    def enrich_summaries(self, summaries: list[dict]) -> list[dict]:
        enriched = []
        for summary in summaries:
            state = self.states.get(summary.get("exchangeId"), {})
            payload = dict(summary)
            payload.update({
                "healthScore": rounded(state.get("score", 100), 1),
                "healthStatus": state.get("status", "healthy"),
                "priorityFactor": rounded(state.get("confidenceFactor", 1), 3),
                "demotionReason": state.get("lastReason", ""),
            })
            enriched.append(payload)
        return enriched

    def snapshot(self) -> dict:
        rows = []
        for state in self.states.values():
            rows.append({
                "exchangeId": state["exchangeId"],
                "exchangeName": state["exchangeName"],
                "score": rounded(state["score"], 1),
                "status": state["status"],
                "confidenceFactor": rounded(state["confidenceFactor"], 3),
                "staleTicks": state["staleTicks"],
                "slowTicks": state["slowTicks"],
                "errorTicks": state["errorTicks"],
                "lastLatencyMs": rounded(state["lastLatencyMs"], 0),
                "lastAgeMs": rounded(state["lastAgeMs"], 0),
                "reason": state["lastReason"],
            })
        return {
            "venues": sorted(rows, key=lambda item: item["score"], reverse=True),
            "demotedCount": sum(1 for item in rows if item["status"] == "demoted"),
            "watchCount": sum(1 for item in rows if item["status"] == "watch"),
        }
