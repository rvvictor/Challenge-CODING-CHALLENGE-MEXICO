from __future__ import annotations


class SuccessCalibrator:
    """Beta-Bernoulli posterior of execution success per venue.

    Each completed trade updates a per-venue Beta(alpha, beta): a clean, profitable
    fill is a success; a partial or losing fill is a failure. The posterior mean is
    a calibrated probability that the next trade on that venue completes well.

    When calibration is enabled it multiplies into opportunity confidence, so the
    bot trusts venues that keep failing less over time and recovers them as they
    behave again. Tracking is always on (so the dashboard shows the learning); the
    multiplier is only applied to scoring when the operator turns it on.
    """

    def __init__(self, alpha_prior: float = 9.0, beta_prior: float = 1.0, min_samples: int = 4):
        self.alpha_prior = alpha_prior
        self.beta_prior = beta_prior
        self.min_samples = min_samples
        self.stats: dict[str, dict[str, int]] = {}

    def reset(self) -> None:
        self.stats = {}

    def update(self, key: str, success: bool) -> None:
        if not key:
            return
        entry = self.stats.setdefault(key, {"success": 0, "failure": 0})
        entry["success" if success else "failure"] += 1

    def probability(self, key: str) -> float:
        entry = self.stats.get(key)
        success = entry["success"] if entry else 0
        failure = entry["failure"] if entry else 0
        return (self.alpha_prior + success) / (self.alpha_prior + self.beta_prior + success + failure)

    def factor(self, *keys: str) -> float:
        """Joint calibrated reliability across the venues in a route (product of
        per-venue probabilities). Returns 1.0 for any venue with fewer than
        min_samples observations, so a cold start does not penalize execution."""
        factor = 1.0
        for key in keys:
            if not key:
                continue
            entry = self.stats.get(key)
            samples = (entry["success"] + entry["failure"]) if entry else 0
            if samples >= self.min_samples:
                factor *= self.probability(key)
        return factor

    def snapshot(self, limit: int = 12) -> dict:
        rows = []
        for key, entry in self.stats.items():
            samples = entry["success"] + entry["failure"]
            rows.append({
                "venue": key,
                "successes": entry["success"],
                "failures": entry["failure"],
                "samples": samples,
                "probability": round(self.probability(key), 4),
                "applied": samples >= self.min_samples,
            })
        rows.sort(key=lambda row: row["samples"], reverse=True)
        return {
            "priorProbability": round(self.alpha_prior / (self.alpha_prior + self.beta_prior), 4),
            "minSamples": self.min_samples,
            "venues": rows[:limit],
        }
