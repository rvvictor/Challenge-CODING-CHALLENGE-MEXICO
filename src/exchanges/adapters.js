import { performance } from "node:perf_hooks";
import { sortLevels } from "../engine/fills.js";

function toLevels(rawLevels, side) {
  return sortLevels(
    rawLevels.map((level) => ({
      price: Array.isArray(level) ? level[0] : level.price,
      qty: Array.isArray(level) ? level[1] : level.qty
    })),
    side
  ).slice(0, 20);
}

async function fetchJson(url, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const started = performance.now();

  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        "accept": "application/json",
        "user-agent": "bitcoin-arbitrage-sentinel/1.0"
      }
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();
    return {
      data,
      latencyMs: Math.round(performance.now() - started)
    };
  } finally {
    clearTimeout(timeout);
  }
}

function buildBook(exchange, asks, bids, latencyMs, source = "live") {
  const now = Date.now();
  return {
    key: `${exchange.id}:${exchange.primarySymbol || exchange.product}`,
    exchangeId: exchange.id,
    exchangeName: exchange.name,
    symbol: exchange.primarySymbol || exchange.product,
    product: exchange.primarySymbol || exchange.product,
    primary: true,
    source,
    status: source === "live" ? "live" : "simulated",
    feeBps: exchange.takerFeeBps,
    slippageBps: exchange.slippageBps,
    confidence: exchange.confidence,
    asks: toLevels(asks, "ask"),
    bids: toLevels(bids, "bid"),
    latencyMs,
    timestamp: now,
    error: null
  };
}

const adapterHandlers = {
  async binance(exchange, timeoutMs) {
    const url = "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20";
    const { data, latencyMs } = await fetchJson(url, timeoutMs);
    return buildBook(exchange, data.asks, data.bids, latencyMs);
  },

  async okx(exchange, timeoutMs) {
    const url = "https://www.okx.com/api/v5/market/books?instId=BTC-USDT&sz=20";
    const { data, latencyMs } = await fetchJson(url, timeoutMs);
    const book = data.data?.[0];
    if (!book) throw new Error("OKX empty book");
    return buildBook(exchange, book.asks, book.bids, latencyMs);
  },

  async kraken(exchange, timeoutMs) {
    const url = "https://api.kraken.com/0/public/Depth?pair=XBTUSDT&count=20";
    const { data, latencyMs } = await fetchJson(url, timeoutMs);
    const key = Object.keys(data.result || {})[0];
    const book = key ? data.result[key] : null;
    if (!book) throw new Error(data.error?.join(", ") || "Kraken empty book");
    return buildBook(exchange, book.asks, book.bids, latencyMs);
  },

  async coinbase(exchange, timeoutMs) {
    const url = "https://api.exchange.coinbase.com/products/BTC-USD/book?level=2";
    const { data, latencyMs } = await fetchJson(url, timeoutMs);
    return buildBook(exchange, data.asks.slice(0, 20), data.bids.slice(0, 20), latencyMs);
  },

  async bitstamp(exchange, timeoutMs) {
    const url = "https://www.bitstamp.net/api/v2/order_book/btcusd/";
    const { data, latencyMs } = await fetchJson(url, timeoutMs);
    return buildBook(exchange, data.asks.slice(0, 20), data.bids.slice(0, 20), latencyMs);
  }
};

export function getAdapter(exchange) {
  const handler = adapterHandlers[exchange.adapter];
  if (!handler) {
    throw new Error(`No adapter configured for ${exchange.adapter}`);
  }

  return {
    exchange,
    fetchOrderBook: (timeoutMs) => handler(exchange, timeoutMs)
  };
}
