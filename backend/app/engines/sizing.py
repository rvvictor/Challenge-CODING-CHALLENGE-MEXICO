from __future__ import annotations


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def kelly_multiplier(win_prob: float, payoff_ratio: float, fraction: float) -> float:
    """Fractional-Kelly multiplier in [0, 1].

    f* = p - (1 - p) / b   where p is the probability the trade completes
    profitably and b is the reward-to-risk ratio (expected gain / expected loss).
    The result is scaled by `fraction` (fractional Kelly) and clamped to [0, 1],
    so a weak or negative edge sizes toward zero and a strong edge toward `fraction`.
    """
    p = clamp(float(win_prob or 0.0), 0.0, 1.0)
    b = max(float(payoff_ratio or 0.0), 1e-9)
    f_star = p - (1.0 - p) / b
    return clamp(float(fraction or 0.0) * f_star, 0.0, 1.0)
