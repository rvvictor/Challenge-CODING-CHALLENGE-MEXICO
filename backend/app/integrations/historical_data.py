from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from backend.app.core.config import ExchangeConfig

# Real-history fetcher for the backtest. Pulls real OHLCV candles via ccxt's
# synchronous REST client (public market data, no keys) for each exchange's
# primary AND triangular symbols, caches them to disk, and fetches exchanges in
# parallel (one thread per exchange, one shared client per exchange) so a cold
# multi-venue, multi-symbol fetch stays fast and rate-limit friendly. Any
# unreachable/unsupported (exchange, symbol) is skipped rather than failing the
# whole fetch — the caller decides what to do with partial coverage.

CACHE_DIR = Path(".aurelion/history")
CACHE_TTL_MS = 15 * 60 * 1000
FETCH_WORKERS = 10


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


def history_symbols(exchange: ExchangeConfig) -> tuple[str, ...]:
    """Symbols worth real history per exchange: the primary BTC pair plus the
    triangular legs (ETH/BTC, ETH/quote). SOL dynamic legs are intentionally
    excluded to keep the cold fetch fast; add them when a demo needs them."""
    return tuple(dict.fromkeys((exchange.primary_symbol, *exchange.triangular_symbols)))


def _fetch_exchange_symbols(exchange: ExchangeConfig, timeframe: str, limit: int, use_cache: bool) -> tuple[dict[str, list[Candle]], dict[str, str]]:
    """Fetch all of one exchange's history symbols with a single shared client.
    Runs inside a worker thread; never raises."""
    candles_by_key: dict[str, list[Candle]] = {}
    statuses: dict[str, str] = {}
    pending: list[str] = []
    for symbol in history_symbols(exchange):
        key = f"{exchange.id}:{symbol}"
        if use_cache:
            cached = _load_cache(_cache_path(exchange.id, symbol, timeframe))
            if cached:
                candles_by_key[key] = cached
                statuses[key] = "cached"
                continue
        pending.append(symbol)
    if not pending:
        return candles_by_key, statuses

    client = None
    try:
        import ccxt

        exchange_class = getattr(ccxt, exchange.ccxt_id, None)
        if exchange_class is not None:
            client = exchange_class({"enableRateLimit": True, "timeout": 8000})
    except Exception:
        client = None

    for symbol in pending:
        key = f"{exchange.id}:{symbol}"
        if client is None:
            statuses[key] = "unavailable"
            continue
        try:
            raw = client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            candles = [
                Candle(timestamp=int(row[0]), open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]), volume=float(row[5]))
                for row in raw
            ]
            if candles:
                _save_cache(_cache_path(exchange.id, symbol, timeframe), candles)
                candles_by_key[key] = candles
                statuses[key] = "live"
            else:
                statuses[key] = "unavailable"
        except Exception:
            statuses[key] = "unavailable"
    return candles_by_key, statuses


def fetch_multi_exchange_history(exchanges: tuple[ExchangeConfig, ...], timeframe: str = "1m", limit: int = 300, use_cache: bool = True) -> dict:
    """Fetch real OHLCV across exchanges and symbols, in parallel per exchange.

    Returns {"candles": {"exchangeId:symbol": [Candle, ...]}, "statuses":
    {"exchangeId:symbol": "cached"|"live"|"unavailable"}} — only series that
    actually returned data appear under "candles"."""
    candles: dict[str, list[Candle]] = {}
    statuses: dict[str, str] = {}
    if not exchanges:
        return {"candles": candles, "statuses": statuses}
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(exchanges))) as pool:
        futures = [pool.submit(_fetch_exchange_symbols, exchange, timeframe, limit, use_cache) for exchange in exchanges]
        for future in futures:
            try:
                exchange_candles, exchange_statuses = future.result()
                candles.update(exchange_candles)
                statuses.update(exchange_statuses)
            except Exception:
                continue
    return {"candles": candles, "statuses": statuses}
