"""Pure performance-and-inference statistics for the validation engine.

Everything here is a pure function over a list of numbers (per-trade P&L or an
equity curve) — no I/O, no engine state, stdlib only — so it is trivially
unit-testable against known series and fully deterministic given a seed.

The point of this module is honesty. A single backtest number ("we made $X")
is an anecdote; a real trading system has to answer *"is this edge
distinguishable from zero after costs, and how sure are we?"* These functions
provide the risk-adjusted ratios a quant panel expects (Sharpe, Sortino,
Calmar, profit factor, drawdown) plus the inferential layer that turns a point
estimate into a defensible claim: a bootstrap confidence interval for the mean
and a significance test that the mean per-trade P&L is greater than zero.
"""

from __future__ import annotations

import math
import random

# Approximate number of ~450 ms evaluation ticks in a trading year, used only to
# annualize the per-trade Sharpe into a familiar figure. Clearly an assumption,
# surfaced as such — the per-trade Sharpe is the primary, assumption-free number.
TICKS_PER_YEAR = 365 * 24 * 60 * 60 * 1000 / 450


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float], ddof: int = 1) -> float:
    n = len(values)
    if n - ddof <= 0:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (n - ddof)
    return math.sqrt(max(0.0, variance))


def _normal_cdf(x: float) -> float:
    """Standard-normal CDF via the error function (stdlib, no SciPy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sharpe_ratio(returns: list[float]) -> float:
    """Per-trade Sharpe: mean / stddev, scaled by sqrt(n) so it reflects the
    whole sample. Zero when there is no dispersion or too few trades."""
    if len(returns) < 2:
        return 0.0
    std = _std(returns)
    if std <= 0:
        return 0.0
    return (_mean(returns) / std) * math.sqrt(len(returns))


def annualized_sharpe(returns: list[float], trades_per_year: float | None = None) -> float:
    """Sharpe annualized by the trading frequency. An estimate (it assumes the
    sample cadence continues), labeled as such wherever it is surfaced."""
    if len(returns) < 2:
        return 0.0
    std = _std(returns)
    if std <= 0:
        return 0.0
    per_trade = _mean(returns) / std
    freq = trades_per_year if trades_per_year and trades_per_year > 0 else len(returns)
    return per_trade * math.sqrt(freq)


def sortino_ratio(returns: list[float]) -> float:
    """Like Sharpe but penalizing only downside dispersion (below zero)."""
    if len(returns) < 2:
        return 0.0
    downside = [min(0.0, value) for value in returns]
    downside_dev = math.sqrt(_mean([value ** 2 for value in downside]))
    if downside_dev <= 0:
        return 0.0
    return (_mean(returns) / downside_dev) * math.sqrt(len(returns))


def profit_factor(returns: list[float]) -> float:
    """Gross gains / gross losses. >1 is net-profitable; inf if there are no
    losing trades (reported capped downstream)."""
    gains = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def hit_rate(returns: list[float]) -> float:
    if not returns:
        return 0.0
    return sum(1 for value in returns if value >= 0) / len(returns)


def max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough drop of a cumulative-P&L curve, in the curve's own
    units (USD). Absolute, because the curve starts near zero (realized cash),
    where a percentage against a ~0 peak is undefined."""
    peak = float("-inf")
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def calmar_ratio(total_return: float, max_dd: float) -> float:
    """Return per unit of worst drawdown. 0 when there was no drawdown (no
    downside experienced) so it never divides by zero."""
    if max_dd <= 0:
        return 0.0
    return total_return / max_dd


def bootstrap_mean_ci(
    values: list[float],
    iterations: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260705,
) -> dict:
    """Non-parametric bootstrap confidence interval for the MEAN.

    Resample the trades with replacement `iterations` times, take each
    resample's mean, and read the empirical (alpha/2, 1-alpha/2) percentiles.
    Makes no normality assumption — appropriate for the fat-tailed, skewed P&L
    of an arbitrage strategy. Deterministic given `seed`."""
    n = len(values)
    point = _mean(values)
    if n < 2:
        return {"low": point, "point": round(point, 6), "high": point, "iterations": 0}
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(max(1, iterations)):
        resample_sum = 0.0
        for _ in range(n):
            resample_sum += values[rng.randrange(n)]
        means.append(resample_sum / n)
    means.sort()
    low_index = max(0, int((alpha / 2) * len(means)))
    high_index = min(len(means) - 1, int((1 - alpha / 2) * len(means)))
    return {
        "low": round(means[low_index], 6),
        "point": round(point, 6),
        "high": round(means[high_index], 6),
        "iterations": len(means),
        "confidence": round(1 - alpha, 4),
    }


def edge_significance(values: list[float]) -> dict:
    """Test whether the mean per-trade P&L is greater than zero after costs.

    Reports the one-sample t-statistic and a one-sided p-value (H1: mean > 0)
    using a normal approximation to the t-distribution — exact enough at the
    sample sizes a backtest produces, and dependency-free. `significant` is the
    honest headline: at the conventional 5% level, is the post-cost edge
    distinguishable from zero?"""
    n = len(values)
    if n < 2:
        return {"n": n, "mean": round(_mean(values), 6), "tStat": 0.0, "pValue": 1.0, "significant": False}
    mean = _mean(values)
    std = _std(values)
    if std <= 0:
        # No dispersion: a nonzero constant is trivially "significant" in sign.
        return {"n": n, "mean": round(mean, 6), "tStat": 0.0, "pValue": 0.0 if mean > 0 else 1.0, "significant": mean > 0}
    t_stat = mean / (std / math.sqrt(n))
    p_value = 1.0 - _normal_cdf(t_stat)  # one-sided H1: mean > 0
    return {
        "n": n,
        "mean": round(mean, 6),
        "std": round(std, 6),
        "tStat": round(t_stat, 4),
        "pValue": round(p_value, 6),
        "significant": bool(p_value < 0.05 and mean > 0),
    }


def performance_stats(
    trade_pnls: list[float],
    equity: list[float] | None = None,
    trades_per_year: float | None = None,
    bootstrap_iterations: int = 2000,
    seed: int = 20260705,
) -> dict:
    """Full risk-adjusted performance + inference report over a P&L sample.

    `trade_pnls` are per-trade realized P&L; `equity` is the cumulative curve
    (defaults to the running sum of `trade_pnls`). Returns a single dict with
    the descriptive stats, the risk-adjusted ratios, and — the reason this
    module exists — the bootstrap CI and significance verdict on the edge."""
    count = len(trade_pnls)
    if equity is None:
        running = 0.0
        equity = []
        for value in trade_pnls:
            running += value
            equity.append(running)
    total = round(sum(trade_pnls), 6)
    dd = max_drawdown(equity)
    pf = profit_factor(trade_pnls)
    ci = bootstrap_mean_ci(trade_pnls, iterations=bootstrap_iterations, seed=seed)
    significance = edge_significance(trade_pnls)
    return {
        "trades": count,
        "totalPnl": total,
        "meanPnl": round(_mean(trade_pnls), 6),
        "stdPnl": round(_std(trade_pnls), 6),
        "hitRate": round(hit_rate(trade_pnls), 4),
        "profitFactor": round(pf, 4) if math.isfinite(pf) else None,
        "maxDrawdown": round(dd, 6),
        "sharpe": round(sharpe_ratio(trade_pnls), 4),
        "annualizedSharpe": round(annualized_sharpe(trade_pnls, trades_per_year), 4),
        "sortino": round(sortino_ratio(trade_pnls), 4),
        "calmar": round(calmar_ratio(total, dd), 4),
        "meanCi": ci,
        "significance": significance,
    }
