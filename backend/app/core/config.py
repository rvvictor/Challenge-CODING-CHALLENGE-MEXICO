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


@dataclass(frozen=True)
class AssetConfig:
    """A tradable asset. `kind` is 'quote' for settlement currencies (USDT/USD)
    and 'base' for coins. `price_hint` is a rough USD price used only as a
    fallback for autonomy/exposure when a live book is unavailable (e.g. demo,
    where alt balances are zero anyway); live/testnet mark to the real book."""
    symbol: str
    kind: str            # "base" | "quote"
    precision: int       # decimals for order quantity
    min_order: float     # minimum tradable size, base units
    withdrawal_fee: float  # network/settlement fee, base units (rebalance cost)
    price_hint: float    # rough USD price (fallback only)


# The asset universe. USDT/BTC/ETH reproduce the original wallet exactly; the
# alts (XRP/LTC/SOL/AVAX) are the venues where the wide-net radar and OU study
# found real edges — enabled for the live/testnet trading universe, seeded at
# zero in demo so demo balances and P&L are numerically unchanged.
ASSET_CATALOG: tuple[AssetConfig, ...] = (
    AssetConfig("USDT", "quote", 2, 1.0, 1.0, 1.0),
    AssetConfig("USD", "quote", 2, 1.0, 1.0, 1.0),
    AssetConfig("BTC", "base", 8, 0.0005, 0.0002, 70000.0),
    AssetConfig("ETH", "base", 6, 0.01, 0.002, 3600.0),
    AssetConfig("XRP", "base", 2, 5.0, 0.2, 0.55),
    AssetConfig("LTC", "base", 4, 0.1, 0.001, 85.0),
    AssetConfig("SOL", "base", 3, 0.05, 0.01, 150.0),
    AssetConfig("AVAX", "base", 3, 0.1, 0.02, 28.0),
)
ASSET_BY_SYMBOL: dict[str, AssetConfig] = {asset.symbol: asset for asset in ASSET_CATALOG}
# Ledger-tracked balance assets (USD normalizes to USDT in the ledger, so it is
# not a separate wallet key — an existing simplification we preserve).
LEDGER_ASSETS: tuple[str, ...] = ("USDT", "BTC", "ETH", "XRP", "LTC", "SOL", "AVAX")
# Alt bases the live/auto engine trades cross-exchange (direct X/quote pairs),
# beyond the BTC primary. These are the assets the wide-net radar and OU study
# flagged as where real edges live. Demo does not trade them (demo feeds only
# the primaries to the cross engine), so demo behavior is unchanged.
LIVE_ALT_BASES: tuple[str, ...] = ("XRP", "LTC", "SOL", "AVAX")


def live_symbols(exchange: ExchangeConfig) -> tuple[str, ...]:
    """Symbols the live stream watches for a venue: the primary BTC pair, the
    triangular legs, and the direct alt pairs (X/USDT or X/USD)."""
    quote = "USD" if exchange.primary_symbol.endswith("/USD") else "USDT"
    alts = tuple(f"{base}/{quote}" for base in LIVE_ALT_BASES)
    return tuple(dict.fromkeys((exchange.primary_symbol, *exchange.triangular_symbols, *alts)))


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
    # taker_fee_bps policy (reviewed July 2026): the published ENTRY-TIER spot
    # taker fee of each venue's professional platform (Kraken Pro, Coinbase
    # Advanced, Gemini ActiveTrader), with no volume or token discounts applied.
    # Deliberately conservative: an always-on bot would quickly earn better tiers
    # (e.g. Coinbase Advanced drops to 40 bps at >=$10K 30-day volume), so real
    # costs would be at or below these numbers. Sources: each venue's public fee
    # schedule as of July 2026.
    return (
        ExchangeConfig("binance", "Binance", "binance", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 1.4, 0.0002, 1.5, 0.98),
        ExchangeConfig("okx", "OKX", "okx", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 1.7, 0.0001, 1, 0.96),
        ExchangeConfig("kraken", "Kraken", "kraken", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 40, 2.2, 0.00015, 2, 0.94, order_book_limit=25),
        ExchangeConfig("coinbase", "Coinbase", "coinbase", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 120, 2.5, 0.00012, 3, 0.92),
        ExchangeConfig("bitstamp", "Bitstamp", "bitstamp", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 40, 2.0, 0.00018, 2.5, 0.91),
        ExchangeConfig("bybit", "Bybit", "bybit", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 1.8, 0.0002, 1.5, 0.90, order_book_limit=50),
        ExchangeConfig("kucoin", "KuCoin", "kucoin", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 10, 2.0, 0.0002, 1.5, 0.89, order_book_limit=20),
        ExchangeConfig("gateio", "Gate.io", "gateio", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 20, 2.4, 0.00025, 2, 0.88),
        ExchangeConfig("bitfinex", "Bitfinex", "bitfinex", "BTC/USDT", ("BTC/USDT", "ETH/BTC", "ETH/USDT"), 20, 2.2, 0.0004, 2.5, 0.87, order_book_limit=25),
        ExchangeConfig("gemini", "Gemini", "gemini", "BTC/USD", ("BTC/USD", "ETH/BTC", "ETH/USD"), 40, 2.8, 0.0001, 3, 0.86),
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


# NOTE: Settings is intentionally a *mutable* dataclass. Every engine shares one
# Settings instance and reads its scalar attributes live during each (synchronous)
# tick, so a value changed between ticks via the parameter registry below takes
# effect on the next tick with no engine rebuild. This replaces the previous
# `frozen=True` + `object.__setattr__` hack, which violated its own immutability
# contract. Exchange selection still flows through MarketService.set_active_exchanges.
@dataclass
class Settings:
    app_name: str = "Aurelion"
    tagline: str = "Bitcoin Arbitrage Intelligence"
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int_env("PORT", 8000)
    market_mode: str = os.getenv("MARKET_MODE", "demo")
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
    # Strategy / model selection (defaults preserve the original behavior).
    cycle_algo: str = os.getenv("CYCLE_ALGO", "dfs")
    slippage_model: str = os.getenv("SLIPPAGE_MODEL", "book_walk")
    market_impact_k: float = number_env("MARKET_IMPACT_K", 8.0)
    sizing_mode: str = os.getenv("SIZING_MODE", "fixed")
    kelly_fraction: float = number_env("KELLY_FRACTION", 0.5)
    volatility_model: str = os.getenv("VOLATILITY_MODEL", "range")
    calibration_enabled: bool = bool_env("CALIBRATION_ENABLED", False)
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
    # Live-feed sanitizer: rejects poisoned order books (non-finite prices,
    # crossed books, fat-finger jumps) at the provider boundary.
    feed_guard_enabled: bool = bool_env("FEED_GUARD_ENABLED", True)
    feed_max_jump_pct: float = number_env("FEED_MAX_JUMP_PCT", 8.0)
    exchange_demotion_ticks: int = int_env("EXCHANGE_DEMOTION_TICKS", 5)
    exchange_recovery_ticks: int = int_env("EXCHANGE_RECOVERY_TICKS", 8)
    health_slow_latency_ms: int = int_env("HEALTH_SLOW_LATENCY_MS", 650)
    health_min_score: float = number_env("HEALTH_MIN_SCORE", 58)
    global_market_enabled: bool = bool_env("GLOBAL_MARKET_ENABLED", True)
    global_market_interval_ms: int = int_env("GLOBAL_MARKET_INTERVAL_MS", 60000)
    # Wide-net discovery lane (scout): sweeps the FULL exchange universe plus
    # XRP/LTC/SOL pairs from batched public tickers, on its own slow cadence,
    # entirely off the hot tick loop so decision latency is unaffected.
    discovery_enabled: bool = bool_env("DISCOVERY_ENABLED", True)
    discovery_interval_ms: int = int_env("DISCOVERY_INTERVAL_MS", 45000)
    discovery_min_persistence: int = int_env("DISCOVERY_MIN_PERSISTENCE", 3)
    discovery_min_net_bps: float = number_env("DISCOVERY_MIN_NET_BPS", 0.0)
    # Live/auto alt trading: when on, live mode watches and trades XRP/LTC/SOL/AVAX
    # cross-exchange (never affects demo, which trades BTC only).
    live_alt_enabled: bool = bool_env("LIVE_ALT_ENABLED", True)
    # Paper inventory seeded per venue for each alt base when entering a live mode,
    # so the read-only-live path can paper-trade alts. In USD-notional terms.
    live_alt_seed_usd: float = number_env("LIVE_ALT_SEED_USD", 4000.0)
    active_exchanges: str = os.getenv("ACTIVE_EXCHANGES", "")
    max_active_exchanges: int = int_env("MAX_ACTIVE_EXCHANGES", 0)
    control_token: str = os.getenv("CONTROL_TOKEN", "")
    # Mutating-endpoint rate limit (requests per 10 s sliding window per client;
    # 0 disables). Env-only, like control_token: a security knob, not a tunable.
    control_rate_limit: int = int_env("CONTROL_RATE_LIMIT", 60)
    # Testnet execution (sandbox, fake money). Real orders only fire when
    # AURELION_ENABLE_LIVE=1 AND trading-only (never withdrawal) testnet keys are
    # present. Mainnet real-money trading is NOT implemented (see the live stub).
    enable_live: bool = bool_env("AURELION_ENABLE_LIVE", False)
    testnet_max_order_usd: float = number_env("TESTNET_MAX_ORDER_USD", 500.0)
    # Live safety: halt opening new positions when net base-asset exposure (the
    # deviation of holdings from the hedged baseline) exceeds this USD cap. 0
    # disables. Hedged demo trades never trip it (base is conserved).
    max_open_exposure_usd: float = number_env("MAX_OPEN_EXPOSURE_USD", 2500.0)
    allowed_origins: str = os.getenv("ALLOWED_ORIGINS", "*")
    redis_url: str = os.getenv("REDIS_URL", "")
    redis_enabled: bool = bool_env("REDIS_ENABLED", bool(os.getenv("REDIS_URL")))
    redis_namespace: str = os.getenv("REDIS_NAMESPACE", "aurelion")
    database_url: str = os.getenv("DATABASE_URL", "")
    persistence_enabled: bool = bool_env("PERSISTENCE_ENABLED", True)
    sqlite_path: str = os.getenv("SQLITE_PATH", ".aurelion/aurelion.db")
    starting_usdt: float = number_env("STARTING_USDT_PER_EXCHANGE", 35000)
    starting_btc: float = number_env("STARTING_BTC_PER_EXCHANGE", 0.25)
    starting_eth: float = number_env("STARTING_ETH_PER_EXCHANGE", 6)
    # Per-exchange starting balances for alt bases (XRP/LTC/SOL/AVAX). Empty in
    # demo (alts start at zero, so demo balances/P&L are unchanged); the testnet
    # trading path seeds this from its sandbox balances.
    starting_alt_balances: dict = field(default_factory=dict)
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


# ---------------------------------------------------------------------------
# Runtime parameter registry
#
# These describe the scalar Settings fields that are safe to tune live from the
# dashboard "Control Room". Each spec carries UI metadata (label, range, step,
# unit) plus the group it belongs to. Provider-construction-only fields
# (order_book_limit, ws_*, starting_*, etc.) are deliberately excluded because
# changing them needs a stream/ledger rebuild, not a live edit.
# ---------------------------------------------------------------------------

PARAMETER_GROUPS: tuple[tuple[str, str], ...] = (
    ("models", "Estrategia y selección de modelos"),
    ("execution", "Ejecución y filtros"),
    ("costs", "Costos y rebalanceo"),
    ("ev", "Valor esperado y latencia"),
    ("risk", "Riesgo y disyuntor"),
    ("triangular", "Triangular / ciclos"),
    ("venue", "Salud de casas"),
    ("discovery", "Descubrimiento amplio"),
    ("cadence", "Cadencia del motor y demo"),
)


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    group: str
    label: str
    description: str
    kind: str  # "float" | "int" | "bool" | "choice"
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str = ""
    options: tuple[str, ...] = ()


PARAMETER_REGISTRY: tuple[ParameterSpec, ...] = (
    # Estrategia / selección de modelos
    ParameterSpec("cycle_algo", "models", "Detección de ciclos", "DFS (rápido, acotado) o detección de ciclos de log negativo Bellman-Ford (encuentra todos los bucles rentables).", "choice", options=("dfs", "bellman_ford")),
    ParameterSpec("slippage_model", "models", "Modelo de deslizamiento", "book_walk (nivel por nivel), sqrt_impact (ley de raíz cuadrada) o almgren_lite (impacto temporal+permanente).", "choice", options=("book_walk", "sqrt_impact", "almgren_lite")),
    ParameterSpec("market_impact_k", "models", "Coeficiente de impacto k", "Fuerza del término de impacto de mercado para sqrt_impact / almgren_lite.", "float", 0.0, 50.0, 0.5, "bps"),
    ParameterSpec("sizing_mode", "models", "Dimensionamiento", "fixed (usa el tamaño máximo) o kelly (dimensionamiento Kelly fraccional por calidad del margen).", "choice", options=("fixed", "kelly")),
    ParameterSpec("kelly_fraction", "models", "Fracción de Kelly", "Fracción del Kelly completo usada cuando el dimensionamiento es kelly.", "float", 0.0, 1.0, 0.05, ""),
    ParameterSpec("volatility_model", "models", "Modelo de volatilidad", "range (más antiguo->ahora %), ewma (ponderado exponencial) o stddev (sigma móvil de retornos).", "choice", options=("range", "ewma", "stddev")),
    ParameterSpec("calibration_enabled", "models", "Calibración bayesiana", "Al activarse, una tasa de éxito Beta-Bernoulli por casa (aprendida de llenados reales) se multiplica en la confianza, para que el bot confíe menos en las casas que fallan.", "bool"),
    # Ejecución y filtros
    ParameterSpec("min_trade_btc", "execution", "Tamaño mín. de operación", "Tamaño ejecutable más pequeño; por debajo de esto una oportunidad se bloquea.", "float", 0.0005, 0.05, 0.0005, "BTC"),
    ParameterSpec("max_trade_btc", "execution", "Tamaño máx. de operación", "Tamaño máximo simulado por operación (tope de posición).", "float", 0.001, 0.1, 0.001, "BTC"),
    ParameterSpec("min_net_profit_usd", "execution", "Ganancia neta mín.", "Ganancia neta absoluta mínima (tras todos los costos) para ejecutar.", "float", 0.0, 20.0, 0.05, "USD"),
    ParameterSpec("min_net_bps", "execution", "Margen neto mín.", "Margen neto mínimo en puntos base para ejecutar una operación entre casas.", "float", 0.0, 25.0, 0.05, "bps"),
    ParameterSpec("min_confidence", "execution", "Confianza mín.", "Piso de confianza (casa + frescura de datos) requerido para ejecutar.", "float", 0.0, 1.0, 0.01, ""),
    ParameterSpec("max_executions_per_tick", "execution", "Máx. operaciones / tick", "Cuántas operaciones puede disparar el bot en un solo ciclo de evaluación.", "int", 1, 10, 1, ""),
    ParameterSpec("pair_cooldown_ms", "execution", "Enfriamiento por par", "Periodo de silencio en un par tras operar en él.", "int", 0, 120000, 500, "ms"),
    # Costos y rebalanceo
    ParameterSpec("withdrawal_fee_impact", "costs", "Peso del costo de rebalanceo", "Multiplicador aplicado a las comisiones de retiro/liquidación al agrupar inventario.", "float", 0.0, 1.0, 0.01, ""),
    ParameterSpec("inventory_rebalance_buffer", "costs", "Margen de rebalanceo", "Holgura extra que se trae al rebalancear, para evitar transferencias repetidas.", "float", 0.0, 2.0, 0.05, ""),
    # Valor esperado y latencia
    ParameterSpec("ev_latency_cost_weight", "ev", "Peso de latencia en EV", "Cuánto se resta el riesgo de latencia en la puntuación de valor esperado.", "float", 0.0, 2.0, 0.05, ""),
    ParameterSpec("volatility_ev_risk_bps", "ev", "Riesgo de volatilidad en EV", "Riesgo de volatilidad plano cobrado por nocional en la puntuación EV.", "float", 0.0, 5.0, 0.01, "bps"),
    ParameterSpec("inventory_ev_penalty_weight", "ev", "Peso de inventario en EV", "Cuánto se penaliza el costo de rebalanceo de inventario en la puntuación EV.", "float", 0.0, 2.0, 0.05, ""),
    ParameterSpec("latency_half_life_ms", "ev", "Vida media de captura", "Latencia a la que la probabilidad de captura se reduce a la mitad (decaimiento exponencial).", "float", 100.0, 5000.0, 50.0, "ms"),
    ParameterSpec("latency_bps_per_second", "ev", "Tasa de costo de latencia", "Riesgo de latencia acumulado por segundo de latencia ida y vuelta.", "float", 0.0, 10.0, 0.1, "bps/s"),
    ParameterSpec("latency_risk_floor_bps", "ev", "Piso de costo de latencia", "Riesgo de latencia mínimo cobrado a cualquier oportunidad.", "float", 0.0, 5.0, 0.05, "bps"),
    # Riesgo y disyuntor
    ParameterSpec("max_volatility_pct", "risk", "Disparo de volatilidad", "Movimiento de BTC en la ventana que dispara el disyuntor.", "float", 0.1, 10.0, 0.1, "%"),
    ParameterSpec("volatility_window_ms", "risk", "Ventana de volatilidad", "Ventana de análisis retrospectivo para detectar volatilidad.", "int", 2000, 120000, 1000, "ms"),
    ParameterSpec("volatility_min_samples", "risk", "Muestras de volatilidad", "Muestras de precio mínimas antes de que la volatilidad pueda disparar.", "int", 2, 60, 1, ""),
    ParameterSpec("volatility_rearm_ms", "risk", "Rearme de volatilidad", "Enfriamiento antes de que la volatilidad pueda disparar de nuevo.", "int", 0, 180000, 1000, "ms"),
    ParameterSpec("max_book_age_ms", "risk", "Límite de datos viejos", "Edad del libro más allá de la cual los datos se consideran viejos.", "int", 500, 30000, 250, "ms"),
    ParameterSpec("max_loss_streak", "risk", "Límite de racha de pérdidas", "Operaciones perdedoras consecutivas antes de que el disyuntor pause la ejecución.", "int", 1, 20, 1, ""),
    ParameterSpec("pause_after_loss_ms", "risk", "Enfriamiento de pausa", "Cuánto tiempo permanece pausado el disyuntor tras activarse.", "int", 0, 600000, 1000, "ms"),
    ParameterSpec("risk_budget_hour_usd", "risk", "Presupuesto de pérdida por hora", "Pérdida máxima por hora móvil antes de que el disyuntor pause.", "float", 1.0, 10000.0, 5.0, "USD"),
    # Triangular / ciclos
    ParameterSpec("triangular_enabled", "triangular", "Motor triangular", "Habilita la detección triangular y de ciclos dinámicos.", "bool"),
    ParameterSpec("triangular_quote_size", "triangular", "Nocional del ciclo", "Nocional inicial en la moneda de cotización usado para evaluar cada ciclo.", "float", 50.0, 10000.0, 50.0, "USDT"),
    ParameterSpec("triangular_min_net_profit_usd", "triangular", "Ganancia mín. del ciclo", "Ganancia neta mínima para ejecutar un ciclo.", "float", 0.0, 20.0, 0.05, "USD"),
    ParameterSpec("triangular_min_net_bps", "triangular", "Margen mín. del ciclo", "Margen neto mínimo para ejecutar un ciclo.", "float", 0.0, 25.0, 0.05, "bps"),
    ParameterSpec("triangular_max_legs", "triangular", "Máx. tramos del ciclo", "Número máximo de tramos en un ciclo detectado.", "int", 3, 6, 1, ""),
    ParameterSpec("triangular_max_cycles_per_exchange", "triangular", "Ciclos / casa", "Máximo de ciclos evaluados por casa por tick.", "int", 1, 32, 1, ""),
    # Salud de casas
    ParameterSpec("exchange_demotion_ticks", "venue", "Ticks para degradar", "Ticks viejos/con error antes de degradar una casa.", "int", 1, 60, 1, ""),
    ParameterSpec("exchange_recovery_ticks", "venue", "Ticks para recuperar", "Ticks sanos requeridos para que una casa se recupere.", "int", 1, 120, 1, ""),
    ParameterSpec("health_slow_latency_ms", "venue", "Latencia lenta", "Latencia por encima de la cual una casa se marca lenta.", "int", 100, 5000, 50, "ms"),
    ParameterSpec("health_min_score", "venue", "Puntuación mín. de salud", "Piso de puntuación de salud (0-100) bajo el cual se degrada una casa.", "float", 0.0, 100.0, 1.0, ""),
    ParameterSpec("feed_guard_enabled", "venue", "Guardia de datos", "Rechaza libros de órdenes en vivo corruptos (precios no finitos, libros cruzados, saltos de dedo gordo) en la frontera del proveedor.", "bool"),
    ParameterSpec("feed_max_jump_pct", "venue", "Filtro de salto de datos", "Movimiento máximo del precio medio entre actualizaciones consecutivas de un libro antes de rechazar la actualización como dato malo.", "float", 0.5, 50.0, 0.5, "%"),
    # Descubrimiento amplio
    ParameterSpec("discovery_enabled", "discovery", "Carril de descubrimiento", "Explorador en segundo plano que barre todo el universo de casas más pares XRP/LTC/SOL/AVAX fuera del bucle principal.", "bool"),
    ParameterSpec("discovery_interval_ms", "discovery", "Intervalo de barrido", "Con qué frecuencia el carril de descubrimiento barre el universo amplio.", "int", 10000, 600000, 5000, "ms"),
    ParameterSpec("discovery_min_persistence", "discovery", "Racha de promoción", "Barridos consecutivos que una ruta debe superar el umbral de margen antes de marcarse promocionable.", "int", 1, 20, 1, "barridos"),
    # El piso de -30 es deliberado: los márgenes reales en los majors están en
    # -20..-25 bps tras las comisiones de nivel de entrada, así que rastrear CUÁL
    # ruta está persistentemente más cerca requiere un umbral por debajo de ellos.
    ParameterSpec("discovery_min_net_bps", "discovery", "Umbral de margen", "Margen neto que una ruta descubierta debe mostrar para construir una racha de persistencia (pon por debajo de 0 para rastrear casi-aciertos persistentes).", "float", -30.0, 25.0, 0.25, "bps"),
    # Cadencia del motor y demo
    ParameterSpec("evaluation_interval_ms", "cadence", "Intervalo de tick", "Con qué frecuencia el motor evalúa el mercado.", "int", 100, 5000, 50, "ms"),
    ParameterSpec("execution_adverse_bps_per_second", "cadence", "Tasa de movimiento adverso", "Deriva adversa del precio cobrada por segundo de latencia de ejecución.", "float", 0.0, 10.0, 0.1, "bps/s"),
    ParameterSpec("execution_adverse_max_bps", "cadence", "Tope de movimiento adverso", "Techo del costo de ejecución por movimiento adverso.", "float", 0.0, 10.0, 0.1, "bps"),
    ParameterSpec("demo_min_execution_gap_ms", "cadence", "Espaciado de operaciones demo", "Espaciado mínimo entre llenados simulados del demo (realismo de presentación).", "int", 0, 120000, 1000, "ms"),
)

PARAMETER_PRESETS: dict[str, dict[str, float | int | bool]] = {
    "conservative": {
        "min_net_bps": 1.6, "min_net_profit_usd": 0.5, "min_confidence": 0.6,
        "max_trade_btc": 0.008, "max_executions_per_tick": 1, "pair_cooldown_ms": 20000,
        "max_volatility_pct": 1.6, "max_loss_streak": 3, "risk_budget_hour_usd": 40,
        "triangular_min_net_bps": 1.4, "triangular_quote_size": 400,
        "ev_latency_cost_weight": 0.6, "inventory_ev_penalty_weight": 0.6,
        "evaluation_interval_ms": 450,
    },
    "balanced": {
        "min_net_bps": 0.75, "min_net_profit_usd": 0.2, "min_confidence": 0.42,
        "max_trade_btc": 0.015, "max_executions_per_tick": 1, "pair_cooldown_ms": 14000,
        "max_volatility_pct": 2.4, "max_loss_streak": 5, "risk_budget_hour_usd": 75,
        "triangular_min_net_bps": 0.65, "triangular_quote_size": 650,
        "ev_latency_cost_weight": 0.35, "inventory_ev_penalty_weight": 0.35,
        "evaluation_interval_ms": 450,
    },
    "aggressive": {
        "min_net_bps": 0.4, "min_net_profit_usd": 0.1, "min_confidence": 0.3,
        "max_trade_btc": 0.03, "max_executions_per_tick": 3, "pair_cooldown_ms": 7000,
        "max_volatility_pct": 3.5, "max_loss_streak": 8, "risk_budget_hour_usd": 150,
        "triangular_min_net_bps": 0.4, "triangular_quote_size": 900,
        "ev_latency_cost_weight": 0.2, "inventory_ev_penalty_weight": 0.2,
        "evaluation_interval_ms": 350,
    },
    "hft": {
        "min_net_bps": 0.25, "min_net_profit_usd": 0.05, "min_confidence": 0.28,
        "max_trade_btc": 0.02, "max_executions_per_tick": 4, "pair_cooldown_ms": 4000,
        "evaluation_interval_ms": 200, "latency_half_life_ms": 500, "latency_bps_per_second": 1.6,
        "triangular_min_net_bps": 0.3, "triangular_quote_size": 800,
        "max_volatility_pct": 3.0, "risk_budget_hour_usd": 120,
    },
}

_REGISTRY_BY_KEY: dict[str, ParameterSpec] = {spec.key: spec for spec in PARAMETER_REGISTRY}


def coerce_parameter(spec: ParameterSpec, value) -> float | int | bool:
    """Coerce + clamp a raw value to the spec's kind and range. Raises ValueError on bad input."""
    if spec.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if spec.kind == "choice":
        text = str(value)
        if spec.options and text not in spec.options:
            raise ValueError("invalid choice")
        return text
    number = float(value)
    if spec.kind == "int":
        number = int(round(number))
    if spec.minimum is not None:
        number = max(spec.minimum, number)
    if spec.maximum is not None:
        number = min(spec.maximum, number)
    return int(number) if spec.kind == "int" else float(number)


def apply_parameter_updates(target: Settings, updates: dict | None) -> dict:
    """Validate + apply scalar parameter updates onto a Settings instance in place."""
    applied: dict[str, float | int | bool] = {}
    changed: dict[str, dict] = {}
    rejected: list[dict] = []
    for key, value in (updates or {}).items():
        spec = _REGISTRY_BY_KEY.get(key)
        if spec is None:
            rejected.append({"key": key, "reason": "unknown parameter"})
            continue
        try:
            coerced = coerce_parameter(spec, value)
        except (TypeError, ValueError):
            rejected.append({"key": key, "reason": "invalid value"})
            continue
        before = getattr(target, key, None)
        setattr(target, key, coerced)
        applied[key] = coerced
        if before != coerced:
            changed[key] = {"from": before, "to": coerced}
    return {"applied": applied, "changed": changed, "rejected": rejected}


def parameter_values(source: Settings) -> dict[str, float | int | bool]:
    return {spec.key: getattr(source, spec.key) for spec in PARAMETER_REGISTRY}


def parameter_specs_payload() -> list[dict]:
    return [
        {
            "key": spec.key,
            "group": spec.group,
            "label": spec.label,
            "description": spec.description,
            "kind": spec.kind,
            "min": spec.minimum,
            "max": spec.maximum,
            "step": spec.step,
            "unit": spec.unit,
            "options": list(spec.options),
        }
        for spec in PARAMETER_REGISTRY
    ]


settings = Settings()
