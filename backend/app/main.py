from __future__ import annotations

import asyncio
import json
import site
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

USER_SITE = site.getusersitepackages()
if USER_SITE not in sys.path:
    sys.path.append(USER_SITE)

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.app.core.config import settings
from backend.app.engines.market_service import market_service

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await market_service.start()
    try:
        yield
    finally:
        await market_service.stop()


app = FastAPI(title=f"{settings.app_name} API", version="2.0.0", lifespan=lifespan)

# CORS: the previous config (`allow_origins=["*"]` + `allow_credentials=True`) is an
# invalid combination browsers reject. Aurelion uses no cookies/credentials, so we
# disable credentials and read the allowlist from ALLOWED_ORIGINS (default "*").
_origins = [origin.strip() for origin in settings.allowed_origins.split(",") if origin.strip()]
_allow_all = "*" in _origins or not _origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all else _origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


_RATE_BUCKETS: dict[str, deque] = {}
_RATE_WINDOW_S = 10.0


def rate_limit(request: Request) -> None:
    """Sliding-window limiter for state-mutating endpoints (per client IP).

    Defends the control surface against accidental or hostile request floods.
    CONTROL_RATE_LIMIT requests per 10 s window; 0 disables (e.g., load tests)."""
    limit = settings.control_rate_limit
    if limit <= 0:
        return
    client = request.client.host if request.client else "local"
    bucket = _RATE_BUCKETS.setdefault(client, deque())
    now = time.monotonic()
    while bucket and now - bucket[0] > _RATE_WINDOW_S:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded on control surface; retry shortly")
    bucket.append(now)


def require_control_auth(x_aurelion_token: str | None = Header(default=None)) -> None:
    """Guard for state-mutating endpoints.

    Off by default so the public demo dashboard can drive its own controls. When
    CONTROL_TOKEN is set (recommended for any exposed/production deployment), every
    mutating request must send a matching `X-Aurelion-Token` header.
    """
    token = settings.control_token
    if not token:
        return
    if x_aurelion_token != token:
        raise HTTPException(status_code=401, detail="Invalid or missing control token")


class ControlPayload(BaseModel):
    activeExchanges: list[str] | None = Field(default=None)
    autoExecution: bool | None = Field(default=None)
    mode: str | None = Field(default=None)
    volatilityShock: bool = Field(default=False)
    killSwitch: bool | None = Field(default=None)
    executionGateway: str | None = Field(default=None)


class ParameterPayload(BaseModel):
    updates: dict[str, float | int | bool | str] | None = Field(default=None)
    preset: str | None = Field(default=None)
    reset: bool = Field(default=False)


class ScenarioPayload(BaseModel):
    scenario: str = Field(default="")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "botName": settings.app_name, "mode": market_service.mode}


@app.get("/api/snapshot")
async def snapshot() -> dict:
    return market_service.snapshot()


@app.get("/api/metrics")
async def metrics() -> dict:
    return market_service.metrics_snapshot()


@app.get("/metrics")
async def prometheus_metrics() -> StreamingResponse:
    payload = market_service.metrics_snapshot()
    metrics_payload = payload["metrics"]
    risk = payload["risk"]
    lines = [
        "# HELP aurelion_detected_total Opportunities detected by Aurelion.",
        "# TYPE aurelion_detected_total counter",
        f"aurelion_detected_total {metrics_payload['detectedCount']}",
        "# HELP aurelion_executed_total Trades executed by Aurelion.",
        "# TYPE aurelion_executed_total counter",
        f"aurelion_executed_total {metrics_payload['executedCount']}",
        "# HELP aurelion_realized_pnl_usd Realized PnL in USD.",
        "# TYPE aurelion_realized_pnl_usd gauge",
        f"aurelion_realized_pnl_usd {metrics_payload['cumulativePnl']}",
        "# HELP aurelion_avg_freshness_ms Average order book freshness.",
        "# TYPE aurelion_avg_freshness_ms gauge",
        f"aurelion_avg_freshness_ms {metrics_payload['avgFreshnessMs']}",
        "# HELP aurelion_circuit_breaker_active Whether the circuit breaker is active.",
        "# TYPE aurelion_circuit_breaker_active gauge",
        f"aurelion_circuit_breaker_active {1 if risk['paused'] else 0}",
        "# HELP aurelion_demoted_venues Number of auto-demoted venues.",
        "# TYPE aurelion_demoted_venues gauge",
        f"aurelion_demoted_venues {metrics_payload['demotedVenues']}",
    ]
    # Observability depth: internal decision latency (per stage), engine
    # watchdog counters, feed-guard rejections and the discovery radar.
    decision = (payload.get("latencySlo") or {}).get("decisionMs") or {}
    if decision:
        lines += [
            "# HELP aurelion_decision_latency_ms Internal decision latency (books read -> ranked + risk-gated).",
            "# TYPE aurelion_decision_latency_ms gauge",
            f'aurelion_decision_latency_ms{{quantile="p50"}} {decision.get("p50", 0)}',
            f'aurelion_decision_latency_ms{{quantile="p95"}} {decision.get("p95", 0)}',
        ]
    stages = (payload.get("latencySlo") or {}).get("stages") or {}
    if stages:
        lines += [
            "# HELP aurelion_stage_latency_ms Per-stage tick latency (p95).",
            "# TYPE aurelion_stage_latency_ms gauge",
        ]
        lines += [f'aurelion_stage_latency_ms{{stage="{name}",quantile="p95"}} {stat.get("p95", 0)}' for name, stat in stages.items()]
    lines += [
        "# HELP aurelion_ticks_total Engine ticks executed (watchdog-supervised).",
        "# TYPE aurelion_ticks_total counter",
        f"aurelion_ticks_total {market_service.tick_count}",
        "# HELP aurelion_tick_errors_total Tick faults contained by the watchdog.",
        "# TYPE aurelion_tick_errors_total counter",
        f"aurelion_tick_errors_total {market_service.tick_errors}",
        "# HELP aurelion_feed_rejected_total Poisoned live book updates rejected by the feed guard.",
        "# TYPE aurelion_feed_rejected_total counter",
        f"aurelion_feed_rejected_total {market_service.feed_guard.rejected_count}",
    ]
    radar = market_service.discovery.last_result or {}
    if radar:
        lines += [
            "# HELP aurelion_radar_positive_routes Net-positive routes in the last wide-net sweep.",
            "# TYPE aurelion_radar_positive_routes gauge",
            f"aurelion_radar_positive_routes {radar.get('positiveCount', 0)}",
            "# HELP aurelion_radar_best_net_bps Best net edge (bps) found by the last wide-net sweep.",
            "# TYPE aurelion_radar_best_net_bps gauge",
            f"aurelion_radar_best_net_bps {radar.get('bestNetBps') if radar.get('bestNetBps') is not None else 'NaN'}",
        ]
    return StreamingResponse(iter(["\n".join(lines) + "\n"]), media_type="text/plain; version=0.0.4")


@app.get("/api/export/session")
async def export_session() -> dict:
    return market_service.export_session()


@app.get("/api/execution")
async def execution() -> dict:
    return market_service.execution_status()


@app.get("/api/continuity")
async def continuity() -> dict:
    # Cross-session lineage from the durable store (DB query — off-loaded).
    return await asyncio.to_thread(market_service.refresh_continuity)


@app.get("/api/replay")
async def replay(limit: int = 120) -> dict:
    return market_service.replay_feed(limit)


class AutotunePayload(BaseModel):
    trials: int = Field(default=24, ge=2, le=80)
    ticks: int = Field(default=220, ge=40, le=1000)
    regime: str = Field(default="normal")
    source: str = Field(default="simulated")
    seed: int = Field(default=7)
    robust: bool = Field(default=False)


@app.get("/api/research/spread")
async def research_spread(timeframe: str = "1m", limit: int = 300) -> dict:
    # Fits OU spread dynamics on real exchange history. Network-bound —
    # off-loaded so it can never block the live loop or SSE.
    return await asyncio.to_thread(market_service.run_spread_study, timeframe, min(max(limit, 60), 500))


@app.post("/api/research/autotune")
async def research_autotune(body: AutotunePayload, _: None = Depends(require_control_auth), __: None = Depends(rate_limit)) -> dict:
    # Trains a parameter preset by replaying the market through the same
    # engines many times (hyperopt pattern). CPU-bound — off-loaded.
    return await asyncio.to_thread(
        market_service.run_autotune, body.trials, body.ticks, body.regime, body.source, body.seed, body.robust
    )


@app.get("/api/research/history")
async def research_history(limit: int = 12) -> dict:
    # Persisted Research Lab artifacts (.aurelion/research/): the bot keeps
    # what it learned across restarts.
    return await asyncio.to_thread(market_service.research_history, min(max(limit, 1), 40))


@app.get("/api/export/report")
async def export_report() -> HTMLResponse:
    # One-click judge report: a single self-contained HTML page (Spanish)
    # built from the live snapshot + persisted research artifacts.
    html_text = await asyncio.to_thread(market_service.judge_report_html)
    return HTMLResponse(html_text)


@app.get("/api/discovery")
async def discovery() -> dict:
    return market_service.discovery.snapshot()


@app.post("/api/discovery/sweep")
async def discovery_sweep(_: None = Depends(require_control_auth), __: None = Depends(rate_limit)) -> dict:
    # Manual wide-net sweep. Off-loaded to a thread like the scheduled lane so a
    # slow venue can never block the live loop or SSE delivery.
    return await market_service.sweep_discovery()


@app.get("/api/backtest")
async def backtest(ticks: int = 250, regime: str = "normal", source: str = "simulated") -> dict:
    # Runs the engines over a replay (simulated or real-exchange OHLCV history)
    # using the current parameters. Off-loaded to a thread: a real-history fetch
    # makes outbound network calls and must never block the live loop / SSE.
    return await asyncio.to_thread(market_service.run_backtest, ticks, regime, source)


@app.get("/api/narrate")
async def narrate(question: str = "", model: str = "", tradeId: str = "") -> dict:
    # Advisory, explanation-only co-pilot. Off-loaded so a (possibly slow) LLM
    # call never blocks the live tick loop or SSE delivery.
    return await asyncio.to_thread(market_service.narrate, question or None, model or None, tradeId or None)


@app.get("/api/narrate/stream")
async def narrate_stream(q: str = "", model: str = "", tradeId: str = "") -> StreamingResponse:
    # Streams the co-pilot explanation token-by-token over SSE so it feels live.
    async def event_source():
        async for event in market_service.narrate_stream(q or None, model or None, tradeId or None):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@app.get("/api/config")
async def config() -> dict:
    return {
        "botName": settings.app_name,
        "market": {
            "evaluationIntervalMs": settings.evaluation_interval_ms,
            "pollIntervalMs": settings.poll_interval_ms,
            "wsReconnectDelayMs": settings.ws_reconnect_delay_ms,
            "wsFailureThreshold": settings.ws_failure_threshold,
        },
        "risk": {
            "maxVolatilityPct": settings.max_volatility_pct,
            "volatilityWindowMs": settings.volatility_window_ms,
            "volatilityMinSamples": settings.volatility_min_samples,
            "volatilityRearmMs": settings.volatility_rearm_ms,
            "maxBookAgeMs": settings.max_book_age_ms,
            "maxLossStreak": settings.max_loss_streak,
            "riskBudgetHourUsd": settings.risk_budget_hour_usd,
        },
        "redis": {"enabled": settings.redis_enabled, "namespace": settings.redis_namespace},
        "database": {"enabled": settings.persistence_enabled, "driver": market_service.persistence.snapshot()["driver"]},
        "exchanges": [exchange.__dict__ for exchange in settings.exchanges],
        "exchangeUniverse": [exchange.__dict__ for exchange in settings.exchange_universe],
        "exchangeProfile": settings.active_exchanges or "all",
        "profiles": {
            "selected": settings.exchange_profile,
            "active": settings.active_exchanges,
            "maxActive": settings.max_active_exchanges,
        },
    }


@app.post("/api/control")
async def control(body: ControlPayload, _: None = Depends(require_control_auth), __: None = Depends(rate_limit)) -> dict:
    if body.activeExchanges is not None:
        await market_service.set_active_exchanges(body.activeExchanges)
    if body.autoExecution is not None:
        market_service.set_auto_execution(bool(body.autoExecution))
    if body.mode is not None:
        await market_service.set_mode(str(body.mode))
    if body.killSwitch is not None:
        market_service.set_kill_switch(bool(body.killSwitch))
    if body.executionGateway is not None:
        await market_service.set_execution_gateway_unified(str(body.executionGateway))
    if body.volatilityShock:
        await market_service.trigger_volatility_stress()
    return market_service.snapshot()


@app.get("/api/params")
async def get_params() -> dict:
    return market_service.parameters()


@app.post("/api/params")
async def update_params(body: ParameterPayload, _: None = Depends(require_control_auth), __: None = Depends(rate_limit)) -> dict:
    if body.reset:
        applied = market_service.reset_parameters()
    elif body.preset:
        applied = market_service.apply_preset(body.preset)
    else:
        applied = market_service.apply_parameters(body.updates or {})
    result = market_service.parameters()
    result["applied"] = applied
    return result


@app.post("/api/scenario")
async def scenario(body: ScenarioPayload, _: None = Depends(require_control_auth), __: None = Depends(rate_limit)) -> dict:
    result = await market_service.trigger_scenario(body.scenario)
    return {"result": result, "snapshot": market_service.snapshot()}


@app.post("/api/reset")
async def reset(_: None = Depends(require_control_auth), __: None = Depends(rate_limit)) -> dict:
    market_service.reset()
    return market_service.snapshot()


@app.get("/events")
async def events() -> StreamingResponse:
    return StreamingResponse(market_service.event_stream(), media_type="text/event-stream")


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{full_path:path}", response_model=None)
async def spa(full_path: str):
    if FRONTEND_DIST.exists():
        dist_root = FRONTEND_DIST.resolve()
        requested = (dist_root / full_path).resolve()
        # Containment check: only serve files that resolve *inside* the build dir,
        # so crafted paths like `../../etc/passwd` fall back to the SPA shell.
        if requested.is_file() and dist_root in requested.parents:
            return FileResponse(requested)
        return FileResponse(dist_root / "index.html")
    return JSONResponse({"ok": True, "message": "Frontend build not found. Run npm --prefix frontend run build."})


def main() -> None:
    import uvicorn

    print(f"\nAurelion cockpit ready at http://localhost:{settings.port}\n", flush=True)
    uvicorn.run("backend.app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
