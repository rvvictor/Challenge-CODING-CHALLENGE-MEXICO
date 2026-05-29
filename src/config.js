const numberFromEnv = (name, fallback) => {
  const value = Number(process.env[name]);
  return Number.isFinite(value) ? value : fallback;
};

const boolFromEnv = (name, fallback) => {
  const value = process.env[name];
  if (value === undefined) return fallback;
  return ["1", "true", "yes", "on"].includes(value.toLowerCase());
};

export const CONFIG = {
  server: {
    host: process.env.HOST || "0.0.0.0",
    port: numberFromEnv("PORT", 3000)
  },
  market: {
    mode: process.env.MARKET_MODE || "auto",
    evaluationIntervalMs: numberFromEnv("EVALUATION_INTERVAL_MS", 450),
    pollIntervalMs: numberFromEnv("POLL_INTERVAL_MS", 1200),
    requestTimeoutMs: numberFromEnv("REQUEST_TIMEOUT_MS", 2500),
    staleAfterMs: numberFromEnv("STALE_AFTER_MS", 5000),
    orderBookLimit: numberFromEnv("ORDER_BOOK_LIMIT", 20),
    reconnectDelayMs: numberFromEnv("WS_RECONNECT_DELAY_MS", 2000),
    wsFailureThreshold: numberFromEnv("WS_FAILURE_THRESHOLD", 5),
    restRecoveryAttemptMs: numberFromEnv("REST_RECOVERY_ATTEMPT_MS", 60000)
  },
  trade: {
    autoExecution: boolFromEnv("AUTO_EXECUTION", true),
    minTradeBtc: numberFromEnv("MIN_TRADE_BTC", 0.004),
    maxTradeBtc: numberFromEnv("MAX_TRADE_BTC", 0.09),
    minNetProfitUsd: numberFromEnv("MIN_NET_PROFIT_USD", 0.75),
    minNetBps: numberFromEnv("MIN_NET_BPS", 1.25),
    withdrawalFeeImpact: numberFromEnv("WITHDRAWAL_FEE_IMPACT", 0.18),
    pairCooldownMs: numberFromEnv("PAIR_COOLDOWN_MS", 7000),
    maxExecutionsPerTick: numberFromEnv("MAX_EXECUTIONS_PER_TICK", 2)
  },
  risk: {
    maxBookAgeMs: numberFromEnv("MAX_BOOK_AGE_MS", 5000),
    maxLossStreak: numberFromEnv("MAX_LOSS_STREAK", 5),
    pauseAfterLossMs: numberFromEnv("PAUSE_AFTER_LOSS_MS", 60000),
    volatilityWindowMs: numberFromEnv("VOLATILITY_WINDOW_MS", 30000),
    maxVolatilityPct: numberFromEnv("MAX_VOLATILITY_PCT", 1.5),
    latencyBpsPerSecond: numberFromEnv("LATENCY_BPS_PER_SECOND", 1.1),
    latencyRiskFloorBps: numberFromEnv("LATENCY_RISK_FLOOR_BPS", 0.15),
    minConfidence: numberFromEnv("MIN_CONFIDENCE", 0.42)
  },
  redis: {
    url: process.env.REDIS_URL || "",
    namespace: process.env.REDIS_NAMESPACE || "btc-arb",
    enabled: boolFromEnv("REDIS_ENABLED", Boolean(process.env.REDIS_URL))
  },
  triangular: {
    enabled: boolFromEnv("TRIANGULAR_ENABLED", true),
    quoteSize: numberFromEnv("TRIANGULAR_QUOTE_SIZE", 2500),
    minNetProfitUsd: numberFromEnv("TRIANGULAR_MIN_NET_PROFIT_USD", 0.35),
    minNetBps: numberFromEnv("TRIANGULAR_MIN_NET_BPS", 0.9),
    maxCyclesPerExchange: numberFromEnv("TRIANGULAR_MAX_CYCLES_PER_EXCHANGE", 4)
  },
  wallets: {
    quoteSymbol: "USDT",
    baseSymbol: "BTC",
    startingQuote: numberFromEnv("STARTING_USDT_PER_EXCHANGE", 120000),
    startingBase: numberFromEnv("STARTING_BTC_PER_EXCHANGE", 0.75),
    startingEth: numberFromEnv("STARTING_ETH_PER_EXCHANGE", 18)
  },
  exchanges: [
    {
      id: "binance",
      name: "Binance",
      ccxtId: "binance",
      adapter: "binance",
      product: "BTC/USDT",
      primarySymbol: "BTC/USDT",
      triangularSymbols: ["BTC/USDT", "ETH/BTC", "ETH/USDT"],
      triangularCycles: [
        {
          id: "USDT-BTC-ETH-USDT",
          path: ["USDT", "BTC", "ETH", "USDT"],
          symbols: ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
        }
      ],
      takerFeeBps: 10,
      slippageBps: 1.4,
      withdrawalFeeBtc: 0.0002,
      withdrawalFeeQuote: 1.5,
      confidence: 0.98
    },
    {
      id: "okx",
      name: "OKX",
      ccxtId: "okx",
      adapter: "okx",
      product: "BTC/USDT",
      primarySymbol: "BTC/USDT",
      triangularSymbols: ["BTC/USDT", "ETH/BTC", "ETH/USDT"],
      triangularCycles: [
        {
          id: "USDT-BTC-ETH-USDT",
          path: ["USDT", "BTC", "ETH", "USDT"],
          symbols: ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
        }
      ],
      takerFeeBps: 8,
      slippageBps: 1.7,
      withdrawalFeeBtc: 0.0001,
      withdrawalFeeQuote: 1,
      confidence: 0.96
    },
    {
      id: "kraken",
      name: "Kraken",
      ccxtId: "kraken",
      adapter: "kraken",
      product: "BTC/USDT",
      primarySymbol: "BTC/USDT",
      triangularSymbols: ["BTC/USDT", "ETH/BTC", "ETH/USDT"],
      triangularCycles: [
        {
          id: "USDT-BTC-ETH-USDT",
          path: ["USDT", "BTC", "ETH", "USDT"],
          symbols: ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
        }
      ],
      takerFeeBps: 26,
      slippageBps: 2.2,
      withdrawalFeeBtc: 0.00015,
      withdrawalFeeQuote: 2,
      confidence: 0.94
    },
    {
      id: "coinbase",
      name: "Coinbase",
      ccxtId: "coinbase",
      adapter: "coinbase",
      product: "BTC/USD",
      primarySymbol: "BTC/USD",
      triangularSymbols: ["BTC/USD", "ETH/BTC", "ETH/USD"],
      triangularCycles: [
        {
          id: "USD-BTC-ETH-USD",
          path: ["USD", "BTC", "ETH", "USD"],
          symbols: ["BTC/USD", "ETH/BTC", "ETH/USD"]
        }
      ],
      takerFeeBps: 40,
      slippageBps: 2.5,
      withdrawalFeeBtc: 0.00012,
      withdrawalFeeQuote: 3,
      confidence: 0.92
    },
    {
      id: "bitstamp",
      name: "Bitstamp",
      ccxtId: "bitstamp",
      adapter: "bitstamp",
      product: "BTC/USD",
      primarySymbol: "BTC/USD",
      triangularSymbols: ["BTC/USD", "ETH/BTC", "ETH/USD"],
      triangularCycles: [
        {
          id: "USD-BTC-ETH-USD",
          path: ["USD", "BTC", "ETH", "USD"],
          symbols: ["BTC/USD", "ETH/BTC", "ETH/USD"]
        }
      ],
      takerFeeBps: 30,
      slippageBps: 2.0,
      withdrawalFeeBtc: 0.00018,
      withdrawalFeeQuote: 2.5,
      confidence: 0.91
    }
  ]
};

export function exchangeById(id) {
  return CONFIG.exchanges.find((exchange) => exchange.id === id);
}
