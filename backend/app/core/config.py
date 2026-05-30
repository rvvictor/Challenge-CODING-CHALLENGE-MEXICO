from __future__ import annotations

import os
from dataclasses import dataclass, field


def number_env(name: str, fallback: float) -> float:
    try:
        return float(os.getenv(name, fallback))
    except (TypeError, ValueError):
        return fallback


def int_env(name: str, fallback: int) -> int:
    return int(number_env(name, fallback))


def bool_env(name: str, fallback: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return fallback
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ExchangeConfig:
    id: str
    name: str
    ccxt_id: str
    primary_symbol: str
    triangular_symbols: tuple[str, ...]
    taker_fee_bps: float
    slippage_bps: float
    withdrawal_fee_btc: float
    withdrawal_fee_quote: float
    confidence: float


@dataclass(frozen=True)
class Settings:
    app_name: str = "Aurelion"
    tagline: str = "Bitcoin Arbitrage Intelligence"
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int_env("PORT", 8000)
    market_mode: str = os.getenv("MARKET_MODE", "auto")
    evaluation_interval_ms: int = int_env("EVALUATION_INTERVAL_MS", 450)
    poll_interval_ms: int = int_env("POLL_INTERVAL_MS", 1200)
    request_timeout_ms: int = int_env("REQUEST_TIMEOUT_MS", 2500)
    order_book_limit: int = int_env("ORDER_BOOK_LIMIT", 25)
    ws_reconnect_delay_ms: int = int_env("WS_RECONNECT_DELAY_MS", 2000)
    ws_failure_threshold: int = int_env("WS_FAILURE_THRESHOLD", 5)
    rest_recovery_attempt_ms: int = int_env("REST_RECOVERY_ATTEMPT_MS", 60000)
    min_trade_btc: float = number_env("MIN_TRADE_BTC", 0.004)
    max_trade_btc: float = number_env("MAX_TRADE_BTC", 0.09)
    min_net_profit_usd: float = number_env("MIN_NET_PROFIT_USD", 0.75)
    min_net_bps: float = number_env("MIN_NET_BPS", 1.25)
    withdrawal_fee_impact: float = number_env("WITHDRAWAL_FEE_IMPACT", 0.18)
    pair_cooldown_ms: int = int_env("PAIR_COOLDOWN_MS", 7000)
    max_executions_per_tick: int = int_env("MAX_EXECUTIONS_PER_TICK", 2)
    auto_execution: bool = bool_env("AUTO_EXECUTION", True)
    max_book_age_ms: int = int_env("MAX_BOOK_AGE_MS", 5000)
    max_loss_streak: int = int_env("MAX_LOSS_STREAK", 5)
    pause_after_loss_ms: int = int_env("PAUSE_AFTER_LOSS_MS", 60000)
    volatility_window_ms: int = int_env("VOLATILITY_WINDOW_MS", 30000)
    max_volatility_pct: float = number_env("MAX_VOLATILITY_PCT", 2.4)
    volatility_min_samples: int = int_env("VOLATILITY_MIN_SAMPLES", 8)
    volatility_rearm_ms: int = int_env("VOLATILITY_REARM_MS", 45000)
    latency_bps_per_second: float = number_env("LATENCY_BPS_PER_SECOND", 1.1)
    latency_risk_floor_bps: float = number_env("LATENCY_RISK_FLOOR_BPS", 0.15)
    min_confidence: float = number_env("MIN_CONFIDENCE", 0.42)
    triangular_enabled: bool = bool_env("TRIANGULAR_ENABLED", True)
    triangular_quote_size: float = number_env("TRIANGULAR_QUOTE_SIZE", 2500)
    triangular_min_net_profit_usd: float = number_env("TRIANGULAR_MIN_NET_PROFIT_USD", 0.35)
    triangular_min_net_bps: float = number_env("TRIANGULAR_MIN_NET_BPS", 0.9)
    global_market_enabled: bool = bool_env("GLOBAL_MARKET_ENABLED", True)
    global_market_interval_ms: int = int_env("GLOBAL_MARKET_INTERVAL_MS", 60000)
    redis_url: str = os.getenv("REDIS_URL", "")
    redis_enabled: bool = bool_env("REDIS_ENABLED", bool(os.getenv("REDIS_URL")))
    redis_namespace: str = os.getenv("REDIS_NAMESPACE", "aurelion")
    starting_usdt: float = number_env("STARTING_USDT_PER_EXCHANGE", 120000)
    starting_btc: float = number_env("STARTING_BTC_PER_EXCHANGE", 0.75)
    starting_eth: float = number_env("STARTING_ETH_PER_EXCHANGE", 18)
    exchanges: tuple[ExchangeConfig, ...] = field(default_factory=lambda: (
        ExchangeConfig("binance", "Binance", "binance", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 1.4, 0.0002, 1.5, 0.98),
        ExchangeConfig("okx", "OKX", "okx", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 8, 1.7, 0.0001, 1, 0.96),
        ExchangeConfig("kraken", "Kraken", "kraken", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 26, 2.2, 0.00015, 2, 0.94),
        ExchangeConfig("coinbase", "Coinbase", "coinbase", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 40, 2.5, 0.00012, 3, 0.92),
        ExchangeConfig("bitstamp", "Bitstamp", "bitstamp", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 30, 2.0, 0.00018, 2.5, 0.91),
        ExchangeConfig("bybit", "Bybit", "bybit", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 1.8, 0.0002, 1.5, 0.90),
        ExchangeConfig("kucoin", "KuCoin", "kucoin", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 2.0, 0.0002, 1.5, 0.89),
        ExchangeConfig("gateio", "Gate.io", "gateio", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 20, 2.4, 0.00025, 2, 0.88),
        ExchangeConfig("bitfinex", "Bitfinex", "bitfinex", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 20, 2.2, 0.0004, 2.5, 0.87),
        ExchangeConfig("gemini", "Gemini", "gemini", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 35, 2.8, 0.0001, 3, 0.86),
    ))

    def exchange_by_id(self, exchange_id: str) -> ExchangeConfig:
        for exchange in self.exchanges:
            if exchange.id == exchange_id:
                return exchange
        raise KeyError(exchange_id)


settings = Settings()
