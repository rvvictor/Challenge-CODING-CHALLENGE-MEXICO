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
# optimize a loss). Two rigor upgrades over a plain search:
#
# - OUT-OF-SAMPLE VALIDATION: the top candidates by training score are
#   re-evaluated on an INDEPENDENT market realization (different market seed).
#   The winner is chosen by validation score, and the train-vs-validation gap is
#   reported — the standard defense against an overfit preset.
# - ROBUST MODE: candidates are scored across several regimes (normal /
#   volatile / stressed) and aggregated, so the learned preset must hold up in
#   bad weather, not just the regime it was tuned in.
#
# Every sampled value passes through the registry's own coercion, so the
# trainer can never propose a value the Control Room would reject, and the
# learned preset is applied through the ordinary /api/params path — visible,
# auditable, reversible.

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
ROBUST_REGIMES = ("normal", "volatile", "stressed")
VALIDATION_SEED = 104729
VALIDATE_TOP = 5


def trial_score(metrics: dict) -> float:
    return round(float(metrics.get("totalPnl", 0)) - 0.5 * float(metrics.get("maxDrawdown", 0)), 4)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


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

    def _evaluate(self, overrides: dict | None, ticks: int, regimes: tuple[str, ...], source: str, market_seed: int) -> tuple[dict, dict]:
        trial_settings = dataclasses.replace(self.settings)
        applied: dict = {}
        if overrides:
            applied = apply_parameter_updates(trial_settings, overrides)["applied"]
        runner = BacktestRunner(trial_settings, historical_provider=self.historical_provider)
        per_regime: dict[str, float] = {}
        rows: list[dict] = []
        for regime in regimes:
            metrics = runner.run(ticks=ticks, regime=regime, source=source, market_seed=market_seed)
            per_regime[regime] = trial_score(metrics)
            rows.append(metrics)
        aggregate = {
            "score": round(_mean(list(per_regime.values())), 4),
            "worstRegimeScore": round(min(per_regime.values()), 4),
            "totalPnl": round(_mean([row["totalPnl"] for row in rows]), 4),
            "maxDrawdown": round(_mean([row["maxDrawdown"] for row in rows]), 4),
            "sharpeLike": round(_mean([row["sharpeLike"] for row in rows]), 3),
            "hitRate": round(_mean([row["hitRate"] for row in rows]), 4),
            "executed": round(_mean([row["executed"] for row in rows])),
            "perRegime": {regime: score for regime, score in per_regime.items()},
        }
        return aggregate, applied

    def train(self, trials: int = 24, ticks: int = 220, regime: str = "normal", source: str = "simulated", seed: int = 7, robust: bool = False) -> dict:
        started = time.perf_counter()
        trials = max(2, min(int(trials or 0), 80))
        rng = random.Random(seed)
        current_values = parameter_values(self.settings)
        regimes = ROBUST_REGIMES if robust else (regime,)

        # Trial 0 is always the CURRENT configuration, evaluated on both the
        # training and the validation realization, so every comparison is
        # like-for-like.
        baseline_train, _ = self._evaluate(None, ticks, regimes, source, market_seed=0)
        baseline_val, _ = self._evaluate(None, ticks, regimes, source, market_seed=VALIDATION_SEED)
        baseline = {
            **baseline_train,
            "validationScore": baseline_val["score"],
            "overfitGap": round(baseline_train["score"] - baseline_val["score"], 4),
        }

        leaderboard: list[dict] = []
        for index in range(trials - 1):
            sampled = self._sample(rng)
            aggregate, applied = self._evaluate(sampled, ticks, regimes, source, market_seed=0)
            changed = {
                key: {"from": current_values.get(key), "to": value}
                for key, value in applied.items()
                if current_values.get(key) != value
            }
            leaderboard.append({
                "trial": index + 1,
                **aggregate,
                "validationScore": None,
                "overfitGap": None,
                "params": applied,
                "changedVsCurrent": changed,
            })

        # Out-of-sample pass: the top candidates by TRAINING score replay on an
        # independent realization; the winner is chosen by VALIDATION score.
        leaderboard.sort(key=lambda row: (row["score"], row["sharpeLike"]), reverse=True)
        for row in leaderboard[:VALIDATE_TOP]:
            validation, _ = self._evaluate(row["params"], ticks, regimes, source, market_seed=VALIDATION_SEED)
            row["validationScore"] = validation["score"]
            row["overfitGap"] = round(row["score"] - validation["score"], 4)

        validated = [row for row in leaderboard if row["validationScore"] is not None]
        best = max(validated, key=lambda row: row["validationScore"]) if validated else (leaderboard[0] if leaderboard else None)
        self.last_result = {
            "generatedAt": int(time.time() * 1000),
            "objective": OBJECTIVE,
            "trials": trials,
            "ticks": ticks,
            "regime": regime,
            "regimes": list(regimes),
            "robust": robust,
            "source": source,
            "seed": seed,
            "validationSeed": VALIDATION_SEED,
            "validatedTop": min(VALIDATE_TOP, len(leaderboard)),
            "searchSpace": [spec.key for spec in self.specs],
            "baseline": baseline,
            "best": best,
            "improvedVsBaseline": bool(
                best is not None
                and best.get("validationScore") is not None
                and best["validationScore"] > baseline["validationScore"]
            ),
            "leaderboard": leaderboard[:8],
            "durationMs": round((time.perf_counter() - started) * 1000, 1),
            "note": (
                "Seeded random search over the Control Room registry, evaluated through the same "
                "engines via the backtest replay (freqtrade-hyperopt pattern). Top candidates are "
                "re-scored on an independent market realization and the winner is chosen by "
                "validation score — an overfit preset shows up as a large train/validation gap. "
                "The learned preset applies through /api/params like any manual change."
            ),
        }
        return self.last_result
