# Research Lab — measured findings (July 3, 2026)

This document records the first full run of Aurelion's Research & Training Lab on
real market data, executed from a consumer connection in Mexico against public,
key-free exchange endpoints. It exists so the numbers quoted in the README and in
committee conversations are reproducible claims, not recollections.

The lab implements, with measured numbers, the observation phase promised in the
committee answers: *how long opportunities last, what fraction disappears before it
could be executed, and which routes deteriorate.*

## 1. Spread dynamics fitted on real history

Method: Ornstein-Uhlenbeck mean-reversion model in its discrete AR(1) form,
closed-form OLS per venue pair, fitted on ~300 aligned 1-minute closes per venue
(5 active venues, BTC and ETH). Theoretical frame: Bertram (2010), *Analytic
solutions for optimal statistical arbitrage trading* — optimal entry/exit
thresholds for OU spreads.

Result: **19 of 20 venue pairs fitted** (one pair showed no mean reversion at this
resolution).

| Measure | Value | Reading |
| --- | --- | --- |
| Median dislocation half-life | **~29 s** (range 0.2–1.1 min) | A cross-venue price dislocation decays by half in well under a minute. |
| Dislocation frequency (2σ) | 1.6–3.8 episodes/hour per pair | Dislocations are common… |
| Median episode duration | ≤ 1 candle (1 min, resolution-bound) | …but brief. |
| Vanished within one candle | **75–100 % of episodes** | The measurable upper bound on "gone before it could be executed". |
| Episodes clearing the entry-tier fee wall (23.5–84 bps per pair) | **0 of ~250** | No opportunity survived real costs in the sample. |

These numbers agree with the literature: Kaiko (2025) puts the average arbitrage
window on major pairs under 4 seconds; Makarov & Schoar (2020) show large
cross-exchange arbitrage concentrates across countries with capital controls, not
between the liquid global venues a retail bot can reach. Our 1-minute resolution
means reported durations are upper bounds — an episode "lasting one candle" almost
certainly lasted seconds.

## 2. Parameter trainer (hyperopt pattern)

Method: seeded random search over 15 Control Room parameters (gates, sizing,
EV model, triangular, strategy selection), each candidate evaluated by replaying
the market through the same engines via the backtest. Objective:
`totalPnl − 0.5·maxDrawdown`. Trial 0 is always the current configuration.
Pattern reference: freqtrade's hyperopt (run the backtest many times, optimize a
loss), the standard in the most-used open-source trading bot.

**Run A — simulated market, "normal" regime, 32 trials, 220 ticks (69 s):**

| | Baseline (current params) | Best learned preset |
| --- | --- | --- |
| Score | 76.3 | **601.1** |
| Total P&L | 77.4 | 601.7 |
| Max drawdown | 2.11 | **1.17** |
| Hit rate | — | 92.6 % |
| Trades | 37 | 54 |

The learned preset chose `bellman_ford` cycle detection with `sqrt_impact`
slippage, a much stricter entry gate (`min_net_bps` ≈ 14.4, `min_confidence`
≈ 0.68) and larger size per trade — a "high-selectivity, high-conviction"
configuration that raised P&L 7.8× while *reducing* drawdown.

**Run B — real-history replay, 16 trials (16 s):**

Baseline score 0.0 → best score 0.0, zero executed trades in every trial.
**No parameterization can conjure profit from real data where no edge survives
the fee wall.** This is the trainer's most important property: it optimizes
aggressively when edge exists and refuses to fabricate it when it doesn't.

**Isolation check:** hot-loop decision latency sampled *during* training —
median 4.0 ms, max 28.3 ms (idle baseline ~4.6 ms median). Training runs
off-thread and does not degrade the live decision path.

**Run C — robust mode with out-of-sample validation (v2), 16 trials × 3
regimes (94 s, 69 backtests):**

Candidates were scored across normal/volatile/stressed simultaneously; the top
5 by training score were re-scored on an *independent market realization*
(different seed) and the winner was chosen by validation score.

| | Train score | Validation score | Overfit gap |
| --- | --- | --- | --- |
| Baseline (current params) | 27.0 | 12.9 | 14.1 |
| Best learned preset | 200.4 | **122.8** | 77.5 |

Three findings worth noting honestly:

1. **The validation pass does real work.** Every top candidate scored 40–78
   points lower out-of-sample than in-sample — exactly the overfit signal the
   pass exists to expose. Selection by validation score is what makes the
   learned preset defensible.
2. **The baseline's per-regime decomposition is diagnostic**: the current
   configuration scores 76.3 in the normal regime but ~1–4 in volatile and
   stressed — it was implicitly tuned for fair weather. The robust winner
   raised the cross-regime average 9.5× *by validation*.
3. **The winner's worst-regime score is 0.0** — in the stressed regime it
   chooses not to trade at all rather than lose. That is the correct behavior
   for an arbitrage system in a storm, and the `worstRegimeScore` field makes
   it visible instead of hiding it in an average.

Post-run checks: the training session persisted to `.aurelion/research/` and
appears in `GET /api/research/history`; the judge report (`/api/export/report`)
renders with all sections; hot-loop decision latency immediately after the run:
p50 3.65 ms / p95 5.92 ms.

## 3. Honest conclusions

1. The market data confirms the design thesis: on reachable, liquid venues,
   entry-tier fees (20–84 bps round trip) exceed observed dislocations (σ of
   0.5–6 bps) by an order of magnitude. A profitable retail deployment needs
   fee-tier improvements, maker execution, or less efficient pairs — which is
   why the Wide-Net Radar tracks XRP/LTC/SOL/AVAX persistence.
2. Opportunity lifetimes measured here (≤1 min upper bound, literature says
   seconds) validate the latency-aware EV model: capture probability must decay
   on the sub-minute scale, and Aurelion's ~5 ms decision path is not the
   bottleneck — the fee wall is.
3. Everything the lab learns is expressed through the parameter registry:
   visible in the Control Room, logged in the edge ledger, reversible with one
   reset. Learning never bypasses the risk model or the paper-only boundary.

## Sources

- Bertram, W.K. (2010). *Analytic solutions for optimal statistical arbitrage
  trading.* Physica A 389(11).
- Makarov, I. & Schoar, A. (2020). *Trading and arbitrage in cryptocurrency
  markets.* Journal of Financial Economics 135(2).
- Kaiko Research (2025), arbitrage window duration on major pairs (<4 s).
- freqtrade documentation, Hyperopt module (backtest-driven parameter search).
