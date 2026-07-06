# Aurelion — Live-Readiness & Security Runbook

**Status: real-money trading is NOT enabled and is NOT implemented in code.**
The highest execution path Aurelion ships is the **testnet** gateway (exchange
sandboxes, fake money). This document is the checklist that would have to be
completed — deliberately, by a human, in a separate reviewed step — to graduate
from testnet to a tiny real-capital *canary*. It exists so the path is honest
and auditable, not so it can be flipped on casually.

## The staged path (matches the committee answers)

1. **Observation** (built): run `auto` mode, record real markets with the
   Observation recorder for days/weeks. Read `GET /api/observation` — which
   routes are repeatable, how long edges last, what fraction clears the fee wall.
   Decision gate: *do any routes clear fees often enough to be worth trading?*
2. **Paper-live** (built): `read-only-live` — real data, paper fills through the
   gateway. Watch `edgeCaptureRatio`, the circuit breaker, reconciliation, and
   inventory autonomy over a sustained session.
3. **Testnet** (built): `testnet` gateway with `AURELION_ENABLE_LIVE=1` and
   trading-only sandbox keys. Real order lifecycle, fake money. Validate fills,
   partials, rejects, caps, and the kill switch against a live sandbox.
4. **Real-capital canary** (NOT built — this runbook): only after 1–3 hold up.

## Hard invariants (must never be violated)

- **No withdrawal scope, ever.** Every API key is trading-only. Aurelion's
  gateways return `supports_withdrawal() == False` by construction; there is no
  code path that transfers funds off an exchange.
- **No secrets in the repo.** Keys live only in the environment / a secrets
  manager. `.env*` is gitignored. `.env.example` documents variable *names* only.
- **Testnet before mainnet.** The testnet gateway pins ccxt `set_sandbox_mode(True)`
  and refuses venues without a sandbox. The mainnet `live` gateway is a disabled
  `NotImplementedError` stub.

## Pre-canary checklist (all must pass, signed off by a human)

**Credentials & access**
- [ ] Keys are trading-only. Verify the exchange shows *no* withdrawal permission.
- [ ] Withdrawal-scope **refusal test**: attempt any withdrawal call path →
      confirm it is impossible/refused. Record the evidence.
- [ ] API keys are IP-allowlisted to the deploy host.
- [ ] Keys are stored in a secrets manager, not a file on disk.

**Risk & capital controls**
- [ ] `TESTNET_MAX_ORDER_USD` (and the future mainnet equivalent) set to the
      canary size (e.g. $20–50), enforced by `PreTradeGuard`.
- [ ] Per-venue and per-asset caps configured (`PreTradeGuard.venue_caps` /
      `asset_caps`).
- [ ] Hourly loss budget (`RISK_BUDGET_HOUR_USD`) and loss-streak breaker set
      conservatively; verified they pause execution in a drill.
- [ ] Max-open-exposure halt configured and drilled (see the live-hardening
      pillar): the bot stops opening new legs when net open exposure exceeds the
      cap.
- [ ] Kill switch drilled: toggling it halts execution within one tick.

**Reconciliation & audit**
- [ ] Every order carries a client ID; venue order IDs are recorded.
- [ ] Partial/failed-leg reconciliation verified end-to-end on testnet: an
      under-filled leg leaves measured open exposure and a recorded corrective
      cover (a real cost, booked to P&L).
- [ ] Durable audit trail (`DATABASE_URL` / SQLite) confirmed writing; session
      continuity survives a restart.

**Operational**
- [ ] Latency measured per venue (`latencyP50Ms/latencyP95Ms`) is within the
      route's observed opportunity lifetime (seconds).
- [ ] Feed guard on; poisoned-book rejections monitored.
- [ ] Watchdog armed; a deliberate fault is contained without downtime.
- [ ] Alerting on: breaker trips, exposure halts, feed-guard spikes, order
      rejects.

## Enabling real money (explicitly out of scope here)

Turning on mainnet real-capital trading would require: (a) implementing a
mainnet execution gateway distinct from the disabled stub, (b) a separate,
written security review approving it, (c) starting at canary size with every
control above enforced, and (d) a rollback/stop plan. **None of that is done or
enabled in this codebase.** The value today is that everything *up to* real
money is built, measured, and safe — the system is provably "one reviewed
connector away," not pretending to be more.
