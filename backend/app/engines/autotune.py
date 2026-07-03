from __future__ import annotations

import dataclasses
import random
import time

from backend.app.core.config import (
    PARAMETER_REGISTRY,
    Settings,
    apply_parameter_updates,
    parameter_values,
)
from backend.app.engines.backtest import BacktestRunner

# Parameter trainer: seeded random search over the Control Room registry,
# evaluated by replaying the market through the SAME engines via BacktestRunner
# (the pattern proven by freqtrade's hyperopt: run the backtest many times,
# optimize a loss). Every sampled value passes through the registry's own
# coercion, so the trainer can never propose a value the Control Room would
# reject, and the learned preset is applied through the ordinary /api/params
# path — everything the trainer learns is expressed as parameters the judge
# can see and undo.

TUNABLE_KEYS = (
    # gates & sizing
    "min_net_bps", "min_net_profit_usd", "min_confidence", "max_trade_btc",
    "max_executions_per_tick", "pair_cooldown_ms",
    # expected-value model
    "ev_latency_cost_weight", "latency_half_life_ms", "inventory_ev_penalty_weight",
    # triangular
    "triangular_min_net_bps", "triangular_quote_size",
    # strategy selection
    "cycle_algo", "slippage_model", "sizing_mode", "volatility_model",
)

OBJECTIVE = "totalPnl - 0.5 * maxDrawdown"


def trial_score(metrics: dict) -> float:
    return round(float(metrics.get("totalPnl", 0)) - 0.5 * float(metrics.get("maxDrawdown", 0)), 4)


class ParameterTrainer:
    """Trains a parameter preset for the current market replay conditions."""

    def __init__(self, settings: Settings, historical_provider=None):
        self.settings = settings
        self.historical_provider = historical_provider
        self.specs = [spec for spec in PARAMETER_REGISTRY if spec.key in TUNABLE_KEYS]
        self.last_result: dict = {}

    def _sample(self, rng: random.Random) -> dict:
        sampled: dict = {}
        for spec in self.specs:
            if spec.kind == "choice":
                sampled[spec.key] = rng.choice(spec.options)
            elif spec.kind == "int":
                sampled[spec.key] = rng.randint(int(spec.minimum), int(spec.maximum))
            else:
                sampled[spec.key] = round(rng.uniform(spec.minimum, spec.maximum), 4)
        return sampled

    def _evaluate(self, overrides: dict | None, ticks: int, regime: str, source: str) -> tuple[dict, dict]:
        trial_settings = dataclasses.replace(self.settings)
        applied: dict = {}
        if overrides:
            applied = apply_parameter_updates(trial_settings, overrides)["applied"]
        runner = BacktestRunner(trial_settings, historical_provider=self.historical_provider)
        metrics = runner.run(ticks=ticks, regime=regime, source=source)
        return metrics, applied

    def train(self, trials: int = 24, ticks: int = 220, regime: str = "normal", source: str = "simulated", seed: int = 7) -> dict:
        started = time.perf_counter()
        trials = max(2, min(int(trials or 0), 80))
        rng = random.Random(seed)
        current_values = parameter_values(self.settings)

        # Trial 0 is always the CURRENT configuration, so "improved vs current"
        # is a real, like-for-like comparison on the same replay.
        baseline_metrics, _ = self._evaluate(None, ticks, regime, source)
        baseline = {
            "score": trial_score(baseline_metrics),
            "totalPnl": baseline_metrics["totalPnl"],
            "maxDrawdown": baseline_metrics["maxDrawdown"],
            "sharpeLike": baseline_metrics["sharpeLike"],
            "hitRate": baseline_metrics["hitRate"],
            "executed": baseline_metrics["executed"],
        }

        leaderboard: list[dict] = []
        for index in range(trials - 1):
            sampled = self._sample(rng)
            metrics, applied = self._evaluate(sampled, ticks, regime, source)
            changed = {
                key: {"from": current_values.get(key), "to": value}
                for key, value in applied.items()
                if current_values.get(key) != value
            }
            leaderboard.append({
                "trial": index + 1,
                "score": trial_score(metrics),
                "totalPnl": metrics["totalPnl"],
                "maxDrawdown": metrics["maxDrawdown"],
                "sharpeLike": metrics["sharpeLike"],
                "hitRate": metrics["hitRate"],
                "executed": metrics["executed"],
                "params": applied,
                "changedVsCurrent": changed,
            })

        leaderboard.sort(key=lambda row: (row["score"], row["sharpeLike"]), reverse=True)
        best = leaderboard[0] if leaderboard else None
        self.last_result = {
            "generatedAt": int(time.time() * 1000),
            "objective": OBJECTIVE,
            "trials": trials,
            "ticks": ticks,
            "regime": regime,
            "source": source,
            "seed": seed,
            "searchSpace": [spec.key for spec in self.specs],
            "baseline": baseline,
            "best": best,
            "improvedVsBaseline": bool(best and best["score"] > baseline["score"]),
            "leaderboard": leaderboard[:8],
            "durationMs": round((time.perf_counter() - started) * 1000, 1),
            "note": (
                "Seeded random search over the Control Room registry, evaluated through the same "
                "engines via the backtest replay (freqtrade-hyperopt pattern). The learned preset "
                "is applied through /api/params like any manual change — visible, auditable, reversible."
            ),
        }
        return self.last_result
