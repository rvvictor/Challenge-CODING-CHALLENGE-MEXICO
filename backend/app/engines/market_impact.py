from __future__ import annotations

import math

# Market-impact / slippage models. These return the *additional* impact in basis
# points charged on top of the level-by-level order-book walk, to model pushing
# price beyond the visible book when an order consumes a meaningful share of depth.
#
# - book_walk:    no extra term; the fill walk already prices visible depth.
# - sqrt_impact:  square-root law of market impact, impact ∝ sqrt(Q / V).
# - almgren_lite: square-root temporary impact + a smaller linear permanent term.


def impact_bps(model: str, qty: float, depth: float, k: float) -> float:
    if qty <= 0 or depth <= 0 or k <= 0:
        return 0.0
    ratio = min(1.0, qty / depth)
    if model == "sqrt_impact":
        return k * math.sqrt(ratio)
    if model == "almgren_lite":
        return k * math.sqrt(ratio) + 0.5 * k * ratio
    return 0.0
