from __future__ import annotations

import site
import sys
from contextlib import asynccontextmanager
from pathlib import Path

USER_SITE = site.getusersitepackages()
if USER_SITE not in sys.path:
    sys.path.append(USER_SITE)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

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


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "botName": settings.app_name, "mode": market_service.mode}


@app.get("/api/snapshot")
async def snapshot() -> dict:
    return market_service.snapshot()


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
        },
        "redis": {"enabled": settings.redis_enabled, "namespace": settings.redis_namespace},
        "exchanges": [exchange.__dict__ for exchange in settings.exchanges],
        "exchangeUniverse": [exchange.__dict__ for exchange in settings.exchange_universe],
        "exchangeProfile": settings.active_exchanges or "all",
    }


@app.post("/api/control")
async def control(request: Request) -> dict:
    body = await request.json()
    if "activeExchanges" in body and isinstance(body["activeExchanges"], list):
        await market_service.set_active_exchanges(body["activeExchanges"])
    if "autoExecution" in body:
        market_service.set_auto_execution(bool(body["autoExecution"]))
    if "mode" in body:
        await market_service.set_mode(str(body["mode"]))
    if body.get("volatilityShock"):
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
