from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from backend.app.core.config import ExchangeConfig

# Real-history fetcher for the backtest. Pulls real OHLCV candles per exchange via
# ccxt's synchronous REST client (no keys needed — public market data) and caches
# them to disk so repeated backtests don't re-hit exchange APIs. Any exchange that
# is unreachable, rate-limited, or unsupported by ccxt for this symbol is skipped
# rather than failing the whole fetch — the caller decides what to do with partial
# coverage (BacktestRunner falls back to the simulator if too little data lands).

CACHE_DIR = Path(".aurelion/history")
CACHE_TTL_MS = 15 * 60 * 1000


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _cache_path(exchange_id: str, symbol: str, timeframe: str) -> Path:
    safe_symbol = symbol.replace("/", "-")
    return CACHE_DIR / f"{exchange_id}_{safe_symbol}_{timeframe}.json"


def _load_cache(path: Path) -> list[Candle] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() * 1000 - payload.get("fetchedAt", 0) > CACHE_TTL_MS:
            return None
        return [Candle(**row) for row in payload["candles"]]
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _save_cache(path: Path, candles: list[Candle]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetchedAt": int(time.time() * 1000), "candles": [asdict(candle) for candle in candles]}
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def fetch_ohlcv(exchange: ExchangeConfig, timeframe: str = "1m", limit: int = 300, use_cache: bool = True) -> tuple[list[Candle], str]:
    """Fetch real OHLCV candles for one exchange's primary symbol.

    Returns (candles, status) where status is "cached", "live", or "unavailable".
    Never raises — network/exchange errors degrade to an empty, "unavailable" result.
    """
    path = _cache_path(exchange.id, exchange.primary_symbol, timeframe)
    if use_cache:
        cached = _load_cache(path)
        if cached:
            return cached, "cached"
    try:
        import ccxt

        exchange_class = getattr(ccxt, exchange.ccxt_id, None)
        if exchange_class is None:
            return [], "unavailable"
        client = exchange_class({"enableRateLimit": True, "timeout": 8000})
        raw = client.fetch_ohlcv(exchange.primary_symbol, timeframe=timeframe, limit=limit)
        candles = [
            Candle(timestamp=int(row[0]), open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]), volume=float(row[5]))
            for row in raw
        ]
        if not candles:
            return [], "unavailable"
        _save_cache(path, candles)
        return candles, "live"
    except Exception:
        return [], "unavailable"


def fetch_multi_exchange_history(exchanges: tuple[ExchangeConfig, ...], timeframe: str = "1m", limit: int = 300) -> dict:
    """Fetch + collect real OHLCV across exchanges. Returns
    {"candles": {exchangeId: [Candle, ...]}, "statuses": {exchangeId: status}} —
    only exchanges that actually returned data appear under "candles"."""
    candles_by_exchange: dict[str, list[Candle]] = {}
    statuses: dict[str, str] = {}
    for exchange in exchanges:
        candles, status = fetch_ohlcv(exchange, timeframe=timeframe, limit=limit)
        statuses[exchange.id] = status
        if candles:
            candles_by_exchange[exchange.id] = candles
    return {"candles": candles_by_exchange, "statuses": statuses}
