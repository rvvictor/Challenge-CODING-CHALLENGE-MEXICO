from __future__ import annotations

import site
import sys
from contextlib import asynccontextmanager
from pathlib import Path

USER_SITE = site.getusersitepackages()
if USER_SITE not in sys.path:
    sys.path.append(USER_SITE)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ControlPayload(BaseModel):
    activeExchanges: list[str] | None = Field(default=None)
    autoExecution: bool | None = Field(default=None)
    mode: str | None = Field(default=None)
    volatilityShock: bool = Field(default=False)


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
    return StreamingResponse(iter(["\n".join(lines) + "\n"]), media_type="text/plain; version=0.0.4")


@app.get("/api/export/session")
async def export_session() -> dict:
    return market_service.export_session()


@app.get("/api/replay")
async def replay() -> dict:
    snapshot = market_service.snapshot()
    return snapshot["replay"]


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
async def control(body: ControlPayload) -> dict:
    if body.activeExchanges is not None:
        await market_service.set_active_exchanges(body.activeExchanges)
    if body.autoExecution is not None:
        market_service.set_auto_execution(bool(body.autoExecution))
    if body.mode is not None:
        await market_service.set_mode(str(body.mode))
    if body.volatilityShock:
        await market_service.trigger_volatility_stress()
    return market_service.snapshot()


@app.post("/api/reset")
async def reset() -> dict:
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
        requested = FRONTEND_DIST / full_path
        if requested.is_file():
            return FileResponse(requested)
        return FileResponse(FRONTEND_DIST / "index.html")
    return JSONResponse({"ok": True, "message": "Frontend build not found. Run npm --prefix frontend run build."})


def main() -> None:
    import uvicorn

    print(f"\nAurelion cockpit ready at http://localhost:{settings.port}\n", flush=True)
    uvicorn.run("backend.app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
