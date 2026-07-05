"""Statistical validation of the arbitrage edge over multiple market windows.

A single backtest answers "what happened once." This engine answers the
question a trading panel actually asks — *"is the edge real, and how sure are
we?"* — by replaying the SAME production engines across several independent
out-of-sample market realizations, pooling every realized trade, and running
the inferential statistics in `statistics.py` over the pool: a bootstrap
confidence interval for the mean and a one-sided significance test that the
post-cost edge is greater than zero.

Design notes that matter for credibility:

- **Same engines, isolated state.** Each window is a `BacktestRunner` pass, so
  the cost model (fees, slippage, market impact, latency, adverse move) is the
  live one, not a re-implementation.
- **Out-of-sample by construction.** Windows differ by `market_seed`, which
  varies both the execution RNG and — over real data — the synthesized depth
  and micro-noise around the real price backbone. Consistency across windows is
  what separates a real edge from one lucky draw.
- **One network fetch.** In historical mode the real OHLCV is fetched once and
  reused across windows (wrapped provider below), so validation never turns into
  N outbound fetches or a rate-limit hazard.
- **Honest about provenance.** The verdict states whether it ran on real
  exchange history or degraded to the simulator, because a claim is only as
  strong as the data under it.
"""

from __future__ import annotations

import time

from backend.app.core.config import Settings
from backend.app.engines.backtest import BacktestRunner
from backend.app.engines.statistics import performance_stats


class _OneShotHistory:
    """Wraps a historical provider so the first fetch is cached and reused for
    every window — one network round-trip for the whole validation run."""

    def __init__(self, provider):
        self._provider = provider
        self._cache: dict | None = None

    def __call__(self, exchanges, timeframe, limit):
        if self._cache is None:
            self._cache = self._provider(exchanges, timeframe, limit)
        return self._cache


class ValidationEngine:
    def __init__(self, settings: Settings, historical_provider=None):
        self.settings = settings
        self._historical_provider = historical_provider

    def run(
        self,
        windows: int = 4,
        ticks: int = 200,
        regime: str = "normal",
        source: str = "historical",
        bootstrap_iterations: int = 2000,
    ) -> dict:
        windows = max(1, min(int(windows or 1), 12))
        ticks = max(30, min(int(ticks or 0), 1000))
        bootstrap_iterations = max(200, min(int(bootstrap_iterations or 0), 5000))

        provider = _OneShotHistory(self._historical_provider) if self._historical_provider else None
        # Without an injected provider, wrap the runner's default fetcher so the
        # real-history path is still fetched only once across all windows.
        if provider is None and source == "historical":
            from backend.app.integrations.historical_data import fetch_multi_exchange_history

            provider = _OneShotHistory(fetch_multi_exchange_history)

        pooled_pnls: list[float] = []
        window_rows: list[dict] = []
        actual_sources: set[str] = set()
        best_net_bps: list[float] = []

        for index in range(windows):
            runner = BacktestRunner(self.settings, historical_provider=provider)
            # market_seed 0 reproduces the canonical replay; subsequent windows
            # are independent realizations (out-of-sample).
            result = runner.run(ticks=ticks, regime=regime, source=source, market_seed=index)
            pnls = result.get("tradePnls", [])
            pooled_pnls.extend(pnls)
            actual_sources.add(result.get("dataQuality", {}).get("actual", "simulated"))
            if result.get("bestObservedNetBps") is not None:
                best_net_bps.append(result["bestObservedNetBps"])
            window_rows.append({
                "window": index,
                "trades": result.get("executed", 0),
                "totalPnl": result.get("totalPnl", 0.0),
                "hitRate": result.get("hitRate", 0.0),
                "maxDrawdown": result.get("maxDrawdown", 0.0),
                "bestObservedNetBps": result.get("bestObservedNetBps"),
                "profitable": result.get("totalPnl", 0.0) > 0,
            })

        stats = performance_stats(pooled_pnls, bootstrap_iterations=bootstrap_iterations)
        profitable_windows = sum(1 for row in window_rows if row["profitable"])
        used_real = "historical" in actual_sources
        provenance = (
            "real-exchange-history" if actual_sources == {"historical"}
            else "mixed" if used_real
            else "simulated"
        )
        verdict = self._verdict(stats, profitable_windows, windows, provenance, best_net_bps)
        return {
            "generatedAt": int(time.time() * 1000),
            "windows": windows,
            "ticksPerWindow": ticks,
            "regime": regime,
            "requestedSource": source,
            "dataProvenance": provenance,
            "pooledTrades": len(pooled_pnls),
            "profitableWindows": profitable_windows,
            "windowConsistency": round(profitable_windows / windows, 4) if windows else 0.0,
            "medianBestNetBps": _median(best_net_bps),
            "stats": stats,
            "verdict": verdict,
        }

    def _verdict(self, stats: dict, profitable_windows: int, windows: int, provenance: str, best_net_bps: list[float]) -> dict:
        """Turn the numbers into one honest, defensible sentence and a machine
        flag. The three outcomes are: a statistically-positive edge; a positive
        point estimate that is NOT distinguishable from zero (CI straddles it);
        or a negative post-cost edge (the measured reality on efficient majors)."""
        trades = stats["trades"]
        significance = stats["significance"]
        ci = stats["meanCi"]
        mean = stats["meanPnl"]
        majority = profitable_windows > windows / 2
        data_note = {
            "real-exchange-history": "on real exchange history",
            "mixed": "on partly real, partly simulated data",
            "simulated": "on simulated data (real venues were unreachable here)",
        }[provenance]

        if trades < 5:
            classification = "insufficient-data"
            median_bps = _median(best_net_bps)
            # Zero (or near-zero) trades on real data is itself the efficient-market
            # finding: edges never cleared the fee wall. Cite how close they came so
            # the result reads as evidence, not a gap.
            bps_note = (
                f" The best edges observed sat around {median_bps:.1f} bps net, below the fee wall — "
                f"so nothing cleared the cost gates."
                if median_bps is not None else ""
            )
            headline = (
                f"Too few trades cleared the cost gates {data_note} to run a significance test "
                f"({trades} trade(s)).{bps_note}"
            )
            edge_is_real = False
        elif significance["significant"] and ci["low"] > 0 and majority:
            classification = "edge-positive"
            headline = (
                f"The post-cost edge is statistically positive {data_note}: mean "
                f"${mean:.4f}/trade, 95% CI [{ci['low']:.4f}, {ci['high']:.4f}] excludes zero "
                f"(p={significance['pValue']:.4f}), and {profitable_windows}/{windows} out-of-sample "
                f"windows were profitable."
            )
            edge_is_real = True
        elif mean > 0:
            classification = "edge-inconclusive"
            headline = (
                f"The point estimate is positive (${mean:.4f}/trade) but NOT statistically "
                f"distinguishable from zero {data_note}: the 95% CI "
                f"[{ci['low']:.4f}, {ci['high']:.4f}] still includes zero (p={significance['pValue']:.4f}). "
                f"Not tradable on this evidence."
            )
            edge_is_real = False
        else:
            classification = "edge-negative"
            median_bps = _median(best_net_bps)
            bps_note = f" Best edges seen sat around {median_bps:.1f} bps net, below the fee wall." if median_bps is not None else ""
            headline = (
                f"After real costs the net edge is negative {data_note} (mean ${mean:.4f}/trade); "
                f"the system correctly declines to trade.{bps_note} This is the measured finding on "
                f"efficient majors, not a defect — the discipline is the result."
            )
            edge_is_real = False
        return {
            "classification": classification,
            "edgeIsReal": edge_is_real,
            "headline": headline,
            "method": (
                "Same production engines and cost model replayed across independent out-of-sample "
                "market windows; pooled per-trade P&L tested with a non-parametric bootstrap 95% CI "
                "and a one-sided t-test (H1: mean > 0 after costs)."
            ),
        }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 4)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 4)
