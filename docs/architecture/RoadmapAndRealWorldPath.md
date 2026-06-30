# Aurelion — Roadmap & Real-World Readiness Plan

> Forward plan written after the final-phase build (Phases 0–6) and the three
> iterations (co-pilot streaming, backtest regimes, frontend reorganization). The
> goal is no longer "finish the challenge" — it is to push Aurelion to a level a
> senior, quant-aware panel has not seen in a hackathon: a system that is **one
> connector away from operating for real**, while staying strictly paper-only today.
>
> Integrity is non-negotiable and unchanged: no real orders, no API keys with
> withdrawal permission, read-only credentials only, demo-first for evaluation.

---

## Where we are

Done and on `feature/final-phase-excellence`:

- **Live Control Room** (37 parameters + presets), **advanced quant models**
  (Bellman-Ford, market-impact slippage, Kelly sizing, EWMA volatility),
  **backtesting with market regimes + realized-execution model**, **Bayesian
  self-calibration**, **durable replay**, **adversarial Stress Lab** with
  leg-failure reconciliation, **inventory autonomy**, a **streaming/Q&A AI
  co-pilot**, security hardening, CI, and 53 passing tests.

The conceptual model is already that of a professional product. What remains to
become *unmistakably* exceptional is: (1) make the path to real execution a true,
visible architecture seam; (2) feed it real history and real market data on the
read side; (3) keep raising demo realism and UX polish.

---

## Two tracks

### Track A — Keep improving what exists (continuous)
Polish, realism, performance and UX of the current system.

### Track B — Real-world readiness (the differentiator)
Refactor the execution/market boundary into a clean adapter seam so a real
exchange is a drop-in. Ship paper + read-only-live implementations now; leave the
live-execution implementation as a guarded stub. The pitch becomes: *"everything
is built; only credentials and a flag are missing — by design."*

---

## Track B in detail — the architecture seam

### B1. A single venue/execution gateway interface
Introduce `ExecutionGateway` (a Protocol/ABC) that every market+execution path
implements. Today's `ccxt_provider`, `simulator`, and `execution` collapse behind it.

```text
ExecutionGateway
  - name / capabilities (market_data, paper, live, read_only)
  - watch_order_book(symbol) / fetch_order_book(symbol)
  - fetch_balances()
  - place_order(ClientOrder)   # IOC / limit-aggressive, max/min price
  - cancel_order(order_id)
  - fetch_fills(order_id)
  - withdraw(...)              # explicitly NotImplemented / disabled
```

Implementations:
- `PaperExecutionGateway` — current deterministic simulator + paper ledger (default).
- `ReadOnlyLiveGateway` — real order books via `ccxt`/`ccxt.pro`, **paper fills**
  (real prices, simulated execution). This is the honest "live" path.
- `LiveExecutionGateway` — real order placement. **Guarded stub**: raises unless
  `AURELION_ENABLE_LIVE=1` *and* read-only-safe checks pass; ships disabled. This is
  the seam that proves "only the connector is missing."

`MarketService` depends on the interface, not concretions. Selectable per session.

### B2. A real order lifecycle + reconciliation
Promote the prototyped reconciliation into a real state machine:
`NEW → ACK → PARTIAL → FILLED | CANCELED | FAILED`, with client order IDs,
idempotency keys, an open-exposure tracker, a completion-window timer, and an
automatic corrective-cover routine. Paper gateway drives it today; the same code
path would drive a live gateway. Surface the lifecycle in the trade detail.

### B3. Typed domain at the seam
Replace untyped dicts on the execution boundary with typed `ClientOrder`,
`Fill`, `OrderState`, `Balance`, `OrderBookL2`. (Aligns with the Architecture
Review's Tier-2 typed-domain recommendation; do it where it pays first — the seam.)

### B4. Safety rails (so "real" is responsible)
Kill switch, per-venue and per-hour notional caps, dry-run mode, a confirmation
gate for any state-changing call, and a hard refusal of withdrawal scopes. These
exist partly (circuit breaker, risk budget) — formalize them as a pre-trade guard
the gateway enforces.

---

## Track B — real history & real data (read-only)

### B5. Historical data pipeline
Ingest real OHLCV and periodic order-book snapshots via `ccxt` into the durable
store; add a `HistoricalReplayGateway` so the **backtest runs over real recorded
history**, not only the simulator. This is the single most credible upgrade for a
quant panel: "backtested on real BTC data across real venues."

- Tasks: a fetch job (rate-limited, cached), a schema for candles/snapshots,
  a replay source for the backtest engine, and a data-quality report.

### B6. Live/auto mode deepening (to discuss together)
Strengthen the read-only live path: real WS books from reachable venues, measured
tick-to-decision latency shown on the dashboard, graceful degradation when a venue
is geo-blocked. Decide deployment that can actually reach exchanges (the demo host
may be blocked). **Open question for us:** which venues are reachable from the
target deploy, and do we present live as a second tab alongside demo.

---

## Track A — continuous improvements (backlog, prioritized)

**High value**
1. **Real-history backtest** (B5) — also a Track A win.
2. **Demo realism**: more regimes/scenarios in the *live* demo (not just backtest),
   occasional realistic losers so the live P&L curve isn't monotonic.
3. **Co-pilot depth**: proactive narration on risk events and parameter changes;
   "explain this trade" from the trades list; optional auto-narrate on a timer.
4. **Performance**: compute the snapshot once per tick and diff it over SSE
   (Architecture Review Tier-3) — needed before live/scale.

**Medium value**
5. Typed domain beyond the seam; property-based tests for fills/EV.
6. Snapshot payload tiering (hot vs cold) for lower bandwidth.
7. Frontend: continue the reorganization based on your feedback; optional
   componentization into modules; deeper accessibility pass (axe audit).
8. Observability: structured logging + expanded Prometheus (tick duration, queue
   depth, per-engine timings, error counters).

**Polish**
9. Session export → shareable report (PDF/HTML) for judges.
10. Multi-pair beyond BTC (XRP/LTC/SOL per the committee answers) as a demo toggle.
11. i18n pass (the UI is English, the README is Spanish — make it intentional).

---

## Suggested sequencing

1. **B5 real-history backtest** — highest credibility-per-effort; uses existing engines.
2. **B1–B2 gateway seam + order lifecycle** — the "one connector away" story.
3. **A2 live-demo realism + A3 co-pilot depth** — visible polish for evaluation.
4. **B6 live/auto deepening** — after we discuss reachable venues + deployment.
5. **A4 snapshot diffing / B3 typed seam** — performance + quality foundations.

Each item ships independently and keeps the project demoable.

---

## Guardrails (unchanged)
- Paper-only; no real orders; no withdrawal-capable keys; read-only credentials.
- `LiveExecutionGateway` ships **disabled**; enabling requires explicit env flags
  and passes safety checks — it is a demonstration of readiness, not live trading.
- Demo stays the reliable evaluation path; document every changed assumption.
