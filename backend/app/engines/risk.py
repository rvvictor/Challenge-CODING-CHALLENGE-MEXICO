from __future__ import annotations

import math
import time
import uuid
from statistics import median, pstdev

from backend.app.core.config import Settings


def now_ms() -> int:
    return int(time.time() * 1000)


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.auto_execution = settings.auto_execution
        self.reset()

    def reset(self) -> None:
        self.loss_streak = 0
        self.paused_until = 0
        self.last_reason = "Ready"
        self.price_window: list[dict[str, float]] = []
        self.pending_events: list[dict] = []
        self.hourly_losses: list[dict[str, float]] = []
        self.last_volatility_trigger = 0
        self.last_volatility_review = 0
        self.current_volatility_pct = 0.0
        self.last_condition = "healthy"

    def set_auto_execution(self, enabled: bool) -> None:
        self.auto_execution = enabled

    def reset_market_window(self) -> None:
        self.price_window.clear()
        self.last_volatility_trigger = 0
        if self.paused_until <= now_ms():
            self.last_reason = "Healthy"
            self.last_condition = "healthy"

    def evaluate_market(self, books: list[dict], current_ms: int | None = None) -> None:
        current_ms = current_ms or now_ms()
        if not books:
            return

        stale = [book for book in books if current_ms - book["timestamp"] > self.settings.max_book_age_ms]
        if stale:
            self.activate(
                "stale-data",
                f"Stale data: {', '.join(book['exchangeName'] for book in stale)}",
                current_ms,
                {"maxAgeMs": self.settings.max_book_age_ms},
            )
            return

        mid = median((book["bestAsk"] + book["bestBid"]) / 2 for book in books)
        self.price_window.append({"time": current_ms, "price": mid})
        self.price_window = [
            point for point in self.price_window
            if current_ms - point["time"] <= self.settings.volatility_window_ms
        ]

        if len(self.price_window) < self.settings.volatility_min_samples:
            return
        if current_ms - self.last_volatility_trigger < self.settings.volatility_rearm_ms:
            return

        oldest = self.price_window[0]
        if oldest["price"] <= 0:
            return
        change_pct = self._window_volatility_pct(mid, oldest["price"])
        self.current_volatility_pct = change_pct
        if current_ms < self.paused_until and self.last_condition == "volatility":
            if current_ms - self.last_volatility_review >= 2000:
                self.last_volatility_review = current_ms
                if change_pct > self.settings.max_volatility_pct:
                    self.paused_until = max(self.paused_until, current_ms + 2000)
                    self.last_reason = f"BTC still volatile {change_pct:.2f}%"
            return
        if change_pct > self.settings.max_volatility_pct:
            self.last_volatility_trigger = current_ms
            self.activate(
                "volatility",
                f"BTC volatility {change_pct:.2f}% in {round((current_ms - oldest['time']) / 1000)}s",
                current_ms,
                {"changePct": change_pct, "windowMs": self.settings.volatility_window_ms},
            )

    def _window_volatility_pct(self, latest_price: float, oldest_price: float) -> float:
        """Effective volatility (%) under the selected model.

        - range:  absolute oldest->now percent move (original behavior).
        - stddev: rolling sigma of per-tick log returns, scaled by sqrt(N).
        - ewma:   exponentially weighted sigma of returns, scaled by sqrt(N).
        The sqrt(N) scaling expresses a per-tick sigma as an expected cumulative
        move over the window, comparable to the range threshold.
        """
        model = self.settings.volatility_model
        if model == "range":
            return abs((latest_price - oldest_price) / oldest_price) * 100 if oldest_price else 0.0
        prices = [point["price"] for point in self.price_window if point["price"] > 0]
        if len(prices) < 2:
            return 0.0
        returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0]
        if not returns:
            return 0.0
        if model == "stddev":
            sigma = pstdev(returns) if len(returns) > 1 else abs(returns[0])
        else:  # ewma
            lam = 0.94
            variance = 0.0
            for value in returns:
                variance = lam * variance + (1 - lam) * value * value
            sigma = math.sqrt(variance)
        return sigma * math.sqrt(len(returns)) * 100

    def can_execute(self, books: list[dict], current_ms: int | None = None) -> dict:
        current_ms = current_ms or now_ms()
        if not self.auto_execution:
            return {"allowed": False, "reason": "Auto execution disabled"}
        if current_ms < self.paused_until:
            return {"allowed": False, "reason": self.last_reason}
        if self.hourly_loss_total(current_ms) >= self.settings.risk_budget_hour_usd > 0:
            self.activate(
                "risk-budget",
                f"Hourly risk budget used ${self.hourly_loss_total(current_ms):.2f}",
                current_ms,
                {"budgetUsd": self.settings.risk_budget_hour_usd, "usedUsd": self.hourly_loss_total(current_ms)},
            )
            return {"allowed": False, "reason": "Hourly risk budget exhausted"}
        stale = [book for book in books if current_ms - book["timestamp"] > self.settings.max_book_age_ms]
        if stale:
            self.activate("stale-data", "Stale order book", current_ms, {"books": [book["exchangeName"] for book in stale]})
            return {"allowed": False, "reason": "Stale market data"}
        return {"allowed": True, "reason": "Risk checks passed"}

    def record_trade(self, trade: dict, current_ms: int | None = None) -> None:
        current_ms = current_ms or now_ms()
        if trade["netProfit"] < 0:
            self.loss_streak += 1
            self.hourly_losses.append({"time": current_ms, "loss": abs(float(trade["netProfit"]))})
            self.hourly_loss_total(current_ms)
            if self.hourly_loss_total(current_ms) >= self.settings.risk_budget_hour_usd > 0:
                self.activate(
                    "risk-budget",
                    f"Hourly risk budget used ${self.hourly_loss_total(current_ms):.2f}",
                    current_ms,
                    {"budgetUsd": self.settings.risk_budget_hour_usd, "usedUsd": self.hourly_loss_total(current_ms)},
                )
                return
            if self.loss_streak >= self.settings.max_loss_streak:
                self.activate(
                    "loss-streak",
                    f"{self.loss_streak} consecutive losing trades",
                    current_ms,
                    {"lossStreak": self.loss_streak},
                )
            return
        self.loss_streak = 0
        if current_ms >= self.paused_until:
            self.last_reason = "Healthy"

    def hourly_loss_total(self, current_ms: int | None = None) -> float:
        current_ms = current_ms or now_ms()
        cutoff = current_ms - 3600000
        self.hourly_losses = [item for item in self.hourly_losses if item["time"] >= cutoff]
        return sum(float(item["loss"]) for item in self.hourly_losses)

    def activate(self, condition: str, reason: str, current_ms: int, metadata: dict) -> None:
        if current_ms < self.paused_until and self.last_reason == reason:
            return
        if condition == "volatility":
            self.last_volatility_trigger = current_ms
            self.last_volatility_review = current_ms
        self.paused_until = current_ms + self.settings.pause_after_loss_ms
        self.last_reason = reason
        self.last_condition = condition
        self.pending_events.append({
            "id": f"CB-{uuid.uuid4().hex[:10]}",
            "type": "circuit-breaker",
            "time": current_ms,
            "reason": reason,
            "condition": condition,
            "cooldownMs": self.settings.pause_after_loss_ms,
            "pausedUntil": self.paused_until,
            "metadata": metadata,
        })

    def drain_events(self) -> list[dict]:
        events = self.pending_events
        self.pending_events = []
        return events

    def snapshot(self, current_ms: int | None = None) -> dict:
        current_ms = current_ms or now_ms()
        return {
            "autoExecution": self.auto_execution,
            "lossStreak": self.loss_streak,
            "paused": current_ms < self.paused_until,
            "pausedUntil": self.paused_until,
            "reason": self.last_reason if current_ms < self.paused_until else "Healthy",
            "condition": self.last_condition if current_ms < self.paused_until else "healthy",
            "pausedFor": self.last_condition if current_ms < self.paused_until else "",
            "cooldownRemainingMs": max(0, self.paused_until - current_ms),
            "volatilityWindowPoints": len(self.price_window),
            "volatilityThresholdPct": self.settings.max_volatility_pct,
            "volatilityMinSamples": self.settings.volatility_min_samples,
            "currentVolatilityPct": self.current_volatility_pct,
            "operationalHalt": current_ms < self.paused_until,
            "monitoringOnly": current_ms < self.paused_until,
            "riskBudgetHourUsd": self.settings.risk_budget_hour_usd,
            "riskBudgetUsedUsd": self.hourly_loss_total(current_ms),
            "riskBudgetRemainingUsd": max(0, self.settings.risk_budget_hour_usd - self.hourly_loss_total(current_ms)),
        }
