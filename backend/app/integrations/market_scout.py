from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from backend.app.core.config import ExchangeConfig

# Read-only, key-free ticker scout for the wide-net discovery lane. One batched
# public ticker request per exchange (fetch_tickers) executed in parallel worker
# threads, so sweeping the FULL exchange universe costs roughly one HTTP
# round-trip of wall time — and it runs entirely off the hot tick loop, so the
# 5-venue decision path keeps its measured ~3 ms latency untouched.

DISCOVERY_BASES = ("BTC", "ETH", "XRP", "LTC", "SOL", "AVAX")
BTC_LEG_BASES = ("ETH", "XRP", "LTC", "SOL", "AVAX")
SCOUT_WORKERS = 10
SCOUT_TIMEOUT_MS = 8000


@dataclass
class TickerQuote:
    exchange_id: str
    symbol: str
    bid: float
    ask: float
    timestamp: int


def discovery_symbols(exchange: ExchangeConfig) -> tuple[str, ...]:
    """Direct major/alt pairs plus the X/BTC legs used for ticker triangulars."""
    quote = "USD" if exchange.primary_symbol.endswith("/USD") else "USDT"
    return tuple(f"{base}/{quote}" for base in DISCOVERY_BASES) + tuple(f"{base}/BTC" for base in BTC_LEG_BASES)


def _quote_from_ticker(exchange_id: str, symbol: str, ticker: dict) -> TickerQuote | None:
    try:
        bid = float(ticker.get("bid") or 0)
        ask = float(ticker.get("ask") or 0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    # A book crossed by >2% is bad ticker data, not an opportunity — drop it so
    # the radar never reports a phantom edge from a stale/garbled quote.
    if bid / ask > 1.02:
        return None
    timestamp = int(ticker.get("timestamp") or time.time() * 1000)
    return TickerQuote(exchange_id, symbol, bid, ask, timestamp)


def _scout_exchange(exchange: ExchangeConfig, symbols: tuple[str, ...]) -> tuple[list[TickerQuote], str]:
    """Fetch one venue's tickers, preferring a single batched call.

    Runs inside a worker thread; never raises. Falls back to per-symbol
    fetch_ticker (rate-limited) for venues without a batch endpoint."""
    try:
        import ccxt

        exchange_class = getattr(ccxt, exchange.ccxt_id, None)
        if exchange_class is None:
            return [], "unsupported"
        client = exchange_class({"enableRateLimit": True, "timeout": SCOUT_TIMEOUT_MS})
    except Exception:
        return [], "unavailable"

    raw: dict = {}
    try:
        if not client.has.get("fetchTickers"):
            raise TypeError("no batch ticker endpoint")
        try:
            raw = client.fetch_tickers(list(symbols))
        except Exception:
            raw = client.fetch_tickers()
    except Exception:
        for symbol in symbols:
            try:
                raw[symbol] = client.fetch_ticker(symbol)
            except Exception:
                continue

    quotes: list[TickerQuote] = []
    wanted = set(symbols)
    for symbol, ticker in (raw or {}).items():
        if symbol not in wanted or not isinstance(ticker, dict):
            continue
        quote = _quote_from_ticker(exchange.id, symbol, ticker)
        if quote:
            quotes.append(quote)
    return quotes, ("live" if quotes else "unavailable")


def scout_universe(exchanges: tuple[ExchangeConfig, ...]) -> dict:
    """Sweep every venue in parallel (one worker per exchange).

    Returns {"quotes": {exchangeId: {symbol: TickerQuote}}, "statuses":
    {exchangeId: "live"|"unavailable"|"unsupported"}, "durationMs": float}."""
    started = time.perf_counter()
    quotes: dict[str, dict[str, TickerQuote]] = {}
    statuses: dict[str, str] = {}
    if exchanges:
        with ThreadPoolExecutor(max_workers=min(SCOUT_WORKERS, len(exchanges))) as pool:
            futures = {pool.submit(_scout_exchange, exchange, discovery_symbols(exchange)): exchange for exchange in exchanges}
            for future, exchange in futures.items():
                try:
                    exchange_quotes, status = future.result()
                except Exception:
                    exchange_quotes, status = [], "unavailable"
                statuses[exchange.id] = status
                if exchange_quotes:
                    quotes[exchange.id] = {quote.symbol: quote for quote in exchange_quotes}
    return {"quotes": quotes, "statuses": statuses, "durationMs": round((time.perf_counter() - started) * 1000, 1)}
