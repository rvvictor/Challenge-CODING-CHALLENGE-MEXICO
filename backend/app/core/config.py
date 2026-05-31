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
    order_book_limit: int | None = None


FAST_EXCHANGE_PROFILE = "okx,bybit,kucoin,kraken,bitstamp"
DEMO_EXCHANGE_PROFILE = "okx,bybit,kucoin,kraken,bitstamp"
COVERAGE_EXCHANGE_PROFILE = "okx,bybit,kucoin,kraken,bitstamp,coinbase,gateio,gemini,bitfinex,binance"
EXCHANGE_PROFILES = {
    "speed": FAST_EXCHANGE_PROFILE,
    "demo": DEMO_EXCHANGE_PROFILE,
    "coverage": COVERAGE_EXCHANGE_PROFILE,
}
PROFILE_LIMITS = {"speed": 5, "demo": 5, "coverage": 10}


def exchange_catalog() -> tuple[ExchangeConfig, ...]:
    return (
        ExchangeConfig("binance", "Binance", "binance", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 1.4, 0.0002, 1.5, 0.98),
        ExchangeConfig("okx", "OKX", "okx", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 8, 1.7, 0.0001, 1, 0.96),
        ExchangeConfig("kraken", "Kraken", "kraken", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 26, 2.2, 0.00015, 2, 0.94, order_book_limit=25),
        ExchangeConfig("coinbase", "Coinbase", "coinbase", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 40, 2.5, 0.00012, 3, 0.92),
        ExchangeConfig("bitstamp", "Bitstamp", "bitstamp", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 30, 2.0, 0.00018, 2.5, 0.91),
        ExchangeConfig("bybit", "Bybit", "bybit", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 1.8, 0.0002, 1.5, 0.90, order_book_limit=50),
        ExchangeConfig("kucoin", "KuCoin", "kucoin", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 2.0, 0.0002, 1.5, 0.89, order_book_limit=20),
        ExchangeConfig("gateio", "Gate.io", "gateio", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 20, 2.4, 0.00025, 2, 0.88),
        ExchangeConfig("bitfinex", "Bitfinex", "bitfinex", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 20, 2.2, 0.0004, 2.5, 0.87, order_book_limit=25),
        ExchangeConfig("gemini", "Gemini", "gemini", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 35, 2.8, 0.0001, 3, 0.86),
    )


def select_exchanges(catalog: tuple[ExchangeConfig, ...], profile: str, max_count: int | None = None) -> tuple[ExchangeConfig, ...]:
    if not profile.strip() or profile.strip().lower() in {"all", "*"}:
        return catalog

    lookup: dict[str, ExchangeConfig] = {}
    for exchange in catalog:
        lookup[exchange.id] = exchange
        lookup[exchange.ccxt_id] = exchange
        lookup[exchange.name.lower()] = exchange

    selected: list[ExchangeConfig] = []
    seen: set[str] = set()
    for token in (item.strip().lower() for item in profile.split(",")):
        exchange = lookup.get(token)
        if not exchange or exchange.id in seen:
            continue
        selected.append(exchange)
        seen.add(exchange.id)
        if max_count and len(selected) >= max_count:
            break
    return tuple(selected) if len(selected) >= 2 else catalog


def profile_exchanges(profile_name: str, active_exchanges: str) -> tuple[str, int]:
    profile_key = (profile_name or "speed").strip().lower()
    profile = EXCHANGE_PROFILES.get(profile_key, EXCHANGE_PROFILES["speed"])
    selected = active_exchanges.strip() or profile
    return selected, PROFILE_LIMITS.get(profile_key, 5)


@dataclass(frozen=True)
class Settings:
    app_name: str = "Aurelion"
    tagline: str = "Bitcoin Arbitrage Intelligence"
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int_env("PORT", 8000)
    market_mode: str = os.getenv("MARKET_MODE", "auto")
    exchange_profile: str = os.getenv("EXCHANGE_PROFILE", "speed")
    evaluation_interval_ms: int = int_env("EVALUATION_INTERVAL_MS", 450)
    poll_interval_ms: int = int_env("POLL_INTERVAL_MS", 1200)
    request_timeout_ms: int = int_env("REQUEST_TIMEOUT_MS", 2500)
    order_book_limit: int = int_env("ORDER_BOOK_LIMIT", 20)
    ws_reconnect_delay_ms: int = int_env("WS_RECONNECT_DELAY_MS", 2000)
    ws_failure_threshold: int = int_env("WS_FAILURE_THRESHOLD", 5)
    rest_recovery_attempt_ms: int = int_env("REST_RECOVERY_ATTEMPT_MS", 60000)
    min_trade_btc: float = number_env("MIN_TRADE_BTC", 0.002)
    max_trade_btc: float = number_env("MAX_TRADE_BTC", 0.015)
    min_net_profit_usd: float = number_env("MIN_NET_PROFIT_USD", 0.2)
    min_net_bps: float = number_env("MIN_NET_BPS", 0.75)
    withdrawal_fee_impact: float = number_env("WITHDRAWAL_FEE_IMPACT", 0.18)
    pair_cooldown_ms: int = int_env("PAIR_COOLDOWN_MS", 14000)
    max_executions_per_tick: int = int_env("MAX_EXECUTIONS_PER_TICK", 1)
    inventory_rebalance_enabled: bool = bool_env("INVENTORY_REBALANCE_ENABLED", True)
    inventory_rebalance_buffer: float = number_env("INVENTORY_REBALANCE_BUFFER", 0.35)
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
    latency_half_life_ms: float = number_env("LATENCY_HALF_LIFE_MS", 900)
    ev_latency_cost_weight: float = number_env("EV_LATENCY_COST_WEIGHT", 0.35)
    volatility_ev_risk_bps: float = number_env("VOLATILITY_EV_RISK_BPS", 0.08)
    inventory_ev_penalty_weight: float = number_env("INVENTORY_EV_PENALTY_WEIGHT", 0.35)
    min_confidence: float = number_env("MIN_CONFIDENCE", 0.42)
    triangular_enabled: bool = bool_env("TRIANGULAR_ENABLED", True)
    triangular_quote_size: float = number_env("TRIANGULAR_QUOTE_SIZE", 650)
    triangular_min_net_profit_usd: float = number_env("TRIANGULAR_MIN_NET_PROFIT_USD", 0.18)
    triangular_min_net_bps: float = number_env("TRIANGULAR_MIN_NET_BPS", 0.65)
    triangular_max_legs: int = int_env("TRIANGULAR_MAX_LEGS", 4)
    triangular_max_cycles_per_exchange: int = int_env("TRIANGULAR_MAX_CYCLES_PER_EXCHANGE", 8)
    demo_min_execution_gap_ms: int = int_env("DEMO_MIN_EXECUTION_GAP_MS", 22000)
    execution_adverse_bps_per_second: float = number_env("EXECUTION_ADVERSE_BPS_PER_SECOND", 0.9)
    execution_adverse_max_bps: float = number_env("EXECUTION_ADVERSE_MAX_BPS", 1.4)
    risk_budget_hour_usd: float = number_env("RISK_BUDGET_HOUR_USD", 75)
    exchange_demotion_ticks: int = int_env("EXCHANGE_DEMOTION_TICKS", 5)
    exchange_recovery_ticks: int = int_env("EXCHANGE_RECOVERY_TICKS", 8)
    health_slow_latency_ms: int = int_env("HEALTH_SLOW_LATENCY_MS", 650)
    health_min_score: float = number_env("HEALTH_MIN_SCORE", 58)
    global_market_enabled: bool = bool_env("GLOBAL_MARKET_ENABLED", True)
    global_market_interval_ms: int = int_env("GLOBAL_MARKET_INTERVAL_MS", 60000)
    active_exchanges: str = os.getenv("ACTIVE_EXCHANGES", "")
    max_active_exchanges: int = int_env("MAX_ACTIVE_EXCHANGES", 0)
    redis_url: str = os.getenv("REDIS_URL", "")
    redis_enabled: bool = bool_env("REDIS_ENABLED", bool(os.getenv("REDIS_URL")))
    redis_namespace: str = os.getenv("REDIS_NAMESPACE", "aurelion")
    database_url: str = os.getenv("DATABASE_URL", "")
    persistence_enabled: bool = bool_env("PERSISTENCE_ENABLED", True)
    sqlite_path: str = os.getenv("SQLITE_PATH", ".aurelion/aurelion.db")
    starting_usdt: float = number_env("STARTING_USDT_PER_EXCHANGE", 35000)
    starting_btc: float = number_env("STARTING_BTC_PER_EXCHANGE", 0.25)
    starting_eth: float = number_env("STARTING_ETH_PER_EXCHANGE", 6)
    exchange_universe: tuple[ExchangeConfig, ...] = field(default_factory=exchange_catalog)
    exchanges: tuple[ExchangeConfig, ...] = field(default_factory=exchange_catalog)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange_universe", self.exchange_universe or self.exchanges)
        profile, default_limit = profile_exchanges(self.exchange_profile, self.active_exchanges)
        max_count = self.max_active_exchanges or default_limit
        object.__setattr__(self, "active_exchanges", profile)
        object.__setattr__(self, "exchanges", select_exchanges(self.exchange_universe, profile, max_count=max_count))

    def exchange_by_id(self, exchange_id: str) -> ExchangeConfig:
        for exchange in self.exchanges:
            if exchange.id == exchange_id:
                return exchange
        raise KeyError(exchange_id)


settings = Settings()
