from __future__ import annotations

import math
import time

from backend.app.core.config import ExchangeConfig, Settings
from backend.app.integrations.historical_data import fetch_multi_exchange_history

# Spread-dynamics lab: fits an Ornstein-Uhlenbeck (mean-reverting) model to the
# cross-venue spread of each pair from REAL exchange history, via its discrete
# AR(1) form (closed-form OLS — no ML dependencies). The fitted parameters give
# the three numbers Victor promised the committee an observation phase would
# measure: how long dislocations last (half-life + episode durations), how often
# they appear (episodes/hour), and what fraction vanish before they could be
# executed. Theoretical frame: Bertram (2010), "Analytic solutions for optimal
# statistical arbitrage trading" — optimal thresholds for OU spreads; here the
# fit is reported and mapped onto Aurelion's existing tunable parameters rather
# than replacing them.

STUDY_BASES = ("BTC", "ETH")
MIN_ALIGNED_POINTS = 60
SIGMA_EPISODE_K = 2.0


def now_ms() -> int:
    return int(time.time() * 1000)


def fit_ar1(series: list[float], dt_ms: float) -> dict | None:
    """Closed-form OLS fit of s[t+1] = c + phi*s[t] + eps, mapped to OU params.

    Returns half-life, long-run mean and stationary sigma, or None when the
    series is too short or shows no mean reversion (phi outside (0, 1))."""
    n = len(series) - 1
    if n < MIN_ALIGNED_POINTS - 1:
        return None
    xs = series[:-1]
    ys = series[1:]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    phi = cov / var_x
    if not (0.0 < phi < 0.9995):
        return None
    intercept = mean_y - phi * mean_x
    mean = intercept / (1 - phi)
    resid_var = sum((y - (intercept + phi * x)) ** 2 for x, y in zip(xs, ys)) / max(1, n - 2)
    sigma = math.sqrt(max(0.0, resid_var / (1 - phi * phi)))
    half_life_ms = -dt_ms * math.log(2) / math.log(phi)
    return {"phi": round(phi, 4), "halfLifeMs": round(half_life_ms, 1), "meanBps": round(mean, 3), "sigmaBps": round(sigma, 3)}


def scan_episodes(series: list[float], mean: float, threshold: float, dt_ms: float) -> dict:
    """Count contiguous runs where |spread - mean| exceeds `threshold` and how
    long they last. Duration resolution is one sample (dt_ms)."""
    episodes: list[int] = []
    peak_excess = 0.0
    run = 0
    for value in series:
        if abs(value - mean) > threshold:
            run += 1
            peak_excess = max(peak_excess, abs(value - mean) - threshold)
        elif run:
            episodes.append(run)
            run = 0
    if run:
        episodes.append(run)
    hours = (len(series) * dt_ms) / 3_600_000 or 1
    durations = sorted(episodes)
    median_samples = durations[len(durations) // 2] if durations else 0
    return {
        "count": len(episodes),
        "perHour": round(len(episodes) / hours, 2),
        "medianDurationMs": median_samples * dt_ms,
        # Episodes lasting a single sample were gone before the next candle —
        # the measurable upper bound on "disappeared before it could be executed"
        # at this resolution (literature puts the true window at seconds).
        "vanishedWithinOneSamplePct": round(100 * sum(1 for d in episodes if d == 1) / len(episodes), 1) if episodes else 0.0,
        "peakExcessBps": round(peak_excess, 2),
    }


class SpreadDynamicsLab:
    """Fits spread dynamics for every venue pair of the active exchanges from
    real OHLCV history. Read-only, key-free, and fully off the hot loop."""

    def __init__(self, settings: Settings, provider=fetch_multi_exchange_history):
        self.settings = settings
        self.provider = provider
        self.last_study: dict = {}

    def _leg_cost_bps(self, exchange: ExchangeConfig) -> float:
        return exchange.taker_fee_bps + exchange.slippage_bps

    def _series_for(self, candles: dict, exchange: ExchangeConfig, base: str) -> dict[int, float] | None:
        quote = "USD" if exchange.primary_symbol.endswith("/USD") else "USDT"
        rows = candles.get(f"{exchange.id}:{base}/{quote}")
        if not rows:
            return None
        return {candle.timestamp: candle.close for candle in rows}

    def study(self, timeframe: str = "1m", limit: int = 300, use_cache: bool = True) -> dict:
        exchanges = self.settings.exchanges
        fetched = self.provider(exchanges, timeframe, limit, use_cache) or {}
        candles = fetched.get("candles") or {}
        pairs: list[dict] = []

        for base in STUDY_BASES:
            entries = []
            for exchange in exchanges:
                series = self._series_for(candles, exchange, base)
                if series:
                    entries.append((exchange, series))
            for i, (venue_a, series_a) in enumerate(entries):
                for venue_b, series_b in entries[i + 1:]:
                    stamps = sorted(set(series_a) & set(series_b))
                    if len(stamps) < MIN_ALIGNED_POINTS:
                        continue
                    deltas = [b - a for a, b in zip(stamps, stamps[1:])]
                    dt_ms = sorted(deltas)[len(deltas) // 2] if deltas else 60_000
                    spread = [
                        (series_a[t] - series_b[t]) / ((series_a[t] + series_b[t]) / 2) * 10_000
                        for t in stamps
                    ]
                    fit = fit_ar1(spread, dt_ms)
                    costs_bps = self._leg_cost_bps(venue_a) + self._leg_cost_bps(venue_b)
                    if fit is None:
                        pairs.append({
                            "base": base, "venueA": venue_a.name, "venueB": venue_b.name,
                            "points": len(stamps), "costsBps": round(costs_bps, 1),
                            "fitted": False, "verdict": "no mean reversion detected at this resolution",
                        })
                        continue
                    dislocations = scan_episodes(spread, fit["meanBps"], SIGMA_EPISODE_K * fit["sigmaBps"], dt_ms)
                    executable = scan_episodes(spread, fit["meanBps"], costs_bps, dt_ms)
                    verdict = (
                        f"{executable['count']} episode(s) cleared the {round(costs_bps, 1)} bps fee wall"
                        if executable["count"]
                        else f"no dislocation cleared the {round(costs_bps, 1)} bps fee wall"
                    )
                    pairs.append({
                        "base": base, "venueA": venue_a.name, "venueB": venue_b.name,
                        "points": len(stamps), "dtMs": dt_ms, "costsBps": round(costs_bps, 1),
                        "fitted": True, **fit,
                        "dislocations": dislocations,
                        "executable": executable,
                        "verdict": verdict,
                    })

        fitted = [pair for pair in pairs if pair.get("fitted")]
        half_lives = sorted(pair["halfLifeMs"] for pair in fitted)
        median_half_life = half_lives[len(half_lives) // 2] if half_lives else None
        executable_total = sum(pair["executable"]["count"] for pair in fitted)
        self.last_study = {
            "generatedAt": now_ms(),
            "timeframe": timeframe,
            "requestedPoints": limit,
            "pairsFitted": len(fitted),
            "pairsTotal": len(pairs),
            "pairs": pairs,
            "summary": {
                "medianHalfLifeMs": median_half_life,
                "executableEpisodes": executable_total,
                "capturableNow": executable_total > 0,
                "note": (
                    "OU (AR(1)) fit per venue pair on real closes. Half-life = time for a dislocation "
                    "to decay by half; dislocation episodes measured at 2-sigma; executable episodes "
                    "measured against the entry-tier fee wall of both venues. Duration resolution is "
                    "one candle — the true intra-candle window is seconds (Kaiko: <4s on majors)."
                ),
            },
        }
        return self.last_study
