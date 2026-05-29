import { performance } from "node:perf_hooks";
import { sortLevels } from "../engine/fills.js";

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function normalizeCcxtLevels(levels, side) {
  return sortLevels(
    (levels || []).map((level) => ({
      price: Array.isArray(level) ? level[0] : level.price,
      qty: Array.isArray(level) ? level[1] : level.qty
    })),
    side
  );
}

function makeBook(exchange, symbol, orderbook, source, latencyMs, error = null) {
  const now = Date.now();
  const asks = normalizeCcxtLevels(orderbook.asks, "ask").slice(0, 30);
  const bids = normalizeCcxtLevels(orderbook.bids, "bid").slice(0, 30);

  return {
    key: `${exchange.id}:${symbol}`,
    exchangeId: exchange.id,
    exchangeName: exchange.name,
    symbol,
    product: symbol,
    primary: symbol === exchange.primarySymbol,
    source,
    status: error ? "degraded" : source,
    feeBps: exchange.takerFeeBps,
    slippageBps: exchange.slippageBps,
    confidence: error ? Math.max(0.35, exchange.confidence - 0.2) : exchange.confidence,
    asks,
    bids,
    latencyMs,
    timestamp: orderbook.timestamp || now,
    receivedAt: now,
    error
  };
}

export class CcxtStreamProvider {
  constructor(config, { onBook, onEvent }) {
    this.config = config;
    this.onBook = onBook;
    this.onEvent = onEvent;
    this.clients = new Map();
    this.states = new Map();
    this.active = false;
    this.ccxt = null;
    this.available = false;
    this.unavailableReason = "";
  }

  async start() {
    if (this.active) return;
    this.active = true;
    await this.loadCcxt();

    if (!this.available) {
      this.emitEvent("provider-unavailable", {
        severity: "warning",
        reason: this.unavailableReason
      });
      return;
    }

    for (const exchange of this.config.exchanges) {
      const symbols = unique([exchange.primarySymbol, ...(exchange.triangularSymbols || [])]);
      for (const symbol of symbols) {
        const state = this.getState(exchange, symbol);
        this.runWebSocketLoop(state);
      }
    }
  }

  async stop() {
    this.active = false;
    for (const state of this.states.values()) {
      state.active = false;
      if (state.restTimer) clearInterval(state.restTimer);
    }
    for (const client of this.clients.values()) {
      if (typeof client.close === "function") {
        await client.close().catch(() => undefined);
      }
    }
    this.clients.clear();
  }

  snapshot() {
    return {
      available: this.available,
      unavailableReason: this.unavailableReason,
      streams: [...this.states.values()].map((state) => ({
        key: state.key,
        exchangeId: state.exchange.id,
        exchangeName: state.exchange.name,
        symbol: state.symbol,
        mode: state.mode,
        failures: state.failures,
        reconnects: state.reconnects,
        updates: state.updates,
        lastUpdate: state.lastUpdate,
        lastError: state.lastError,
        restFallback: state.mode === "rest"
      }))
    };
  }

  async loadCcxt() {
    if (this.ccxt) return;
    try {
      const module = await import("ccxt");
      this.ccxt = module.default || module;
      this.available = true;
    } catch (error) {
      this.available = false;
      this.unavailableReason = `ccxt package unavailable: ${error.message}`;
    }
  }

  getState(exchange, symbol) {
    const key = `${exchange.id}:${symbol}`;
    if (!this.states.has(key)) {
      this.states.set(key, {
        key,
        exchange,
        symbol,
        active: true,
        mode: "connecting",
        failures: 0,
        reconnects: 0,
        updates: 0,
        lastUpdate: 0,
        lastError: "",
        restTimer: null,
        restStartedAt: 0
      });
    }
    return this.states.get(key);
  }

  getClient(exchange) {
    if (this.clients.has(exchange.id)) return this.clients.get(exchange.id);
    const exchangeNamespace = this.ccxt.pro || this.ccxt;
    const ExchangeClass = exchangeNamespace[exchange.ccxtId || exchange.id] || this.ccxt[exchange.ccxtId || exchange.id];
    if (!ExchangeClass) {
      throw new Error(`CCXT exchange not found: ${exchange.ccxtId || exchange.id}`);
    }

    const client = new ExchangeClass({
      enableRateLimit: true,
      timeout: this.config.market.requestTimeoutMs,
      options: {
        defaultType: "spot",
        adjustForTimeDifference: true
      }
    });
    this.clients.set(exchange.id, client);
    return client;
  }

  runWebSocketLoop(state) {
    state.active = true;
    state.mode = "websocket";
    void this.watchLoop(state);
  }

  async watchLoop(state) {
    while (this.active && state.active && state.mode === "websocket") {
      const { exchange, symbol } = state;
      try {
        const client = this.getClient(exchange);
        if (!client.has?.watchOrderBook || typeof client.watchOrderBook !== "function") {
          throw new Error(`${exchange.name} does not expose watchOrderBook in this CCXT build`);
        }

        const started = performance.now();
        const orderbook = await client.watchOrderBook(symbol, this.config.market.orderBookLimit);
        const latencyMs = Math.max(1, Math.round(performance.now() - started));
        state.failures = 0;
        state.updates += 1;
        state.lastUpdate = Date.now();
        state.lastError = "";
        this.onBook(makeBook(exchange, symbol, orderbook, "websocket", latencyMs));
      } catch (error) {
        state.failures += 1;
        state.reconnects += 1;
        state.lastError = error.message;
        this.emitEvent("websocket-error", {
          exchange: exchange.name,
          symbol,
          failures: state.failures,
          reason: error.message
        });

        if (state.failures >= this.config.market.wsFailureThreshold) {
          this.activateRestFallback(state, error);
          return;
        }

        await delay(this.config.market.reconnectDelayMs);
      }
    }
  }

  activateRestFallback(state, error) {
    state.mode = "rest";
    state.restStartedAt = Date.now();
    state.lastError = error.message;
    this.emitEvent("rest-fallback", {
      severity: "warning",
      exchange: state.exchange.name,
      symbol: state.symbol,
      failures: state.failures,
      reason: `WebSocket failed ${state.failures} times; REST polling activated`
    });

    if (state.restTimer) clearInterval(state.restTimer);
    const poll = () => void this.fetchRestBook(state);
    poll();
    state.restTimer = setInterval(poll, this.config.market.pollIntervalMs);
  }

  async fetchRestBook(state) {
    if (!this.active || state.mode !== "rest") return;
    try {
      const client = this.getClient(state.exchange);
      const started = performance.now();
      const orderbook = await client.fetchOrderBook(state.symbol, this.config.market.orderBookLimit);
      const latencyMs = Math.max(1, Math.round(performance.now() - started));
      state.updates += 1;
      state.lastUpdate = Date.now();
      this.onBook(makeBook(state.exchange, state.symbol, orderbook, "rest", latencyMs));
    } catch (error) {
      state.lastError = error.message;
      this.onBook(makeBook(state.exchange, state.symbol, { asks: [], bids: [], timestamp: Date.now() }, "rest", 0, error.message));
      this.emitEvent("rest-error", {
        exchange: state.exchange.name,
        symbol: state.symbol,
        reason: error.message
      });
    }

    const age = Date.now() - state.restStartedAt;
    if (age >= this.config.market.restRecoveryAttemptMs) {
      clearInterval(state.restTimer);
      state.restTimer = null;
      state.failures = 0;
      state.mode = "websocket";
      this.emitEvent("websocket-retry", {
        exchange: state.exchange.name,
        symbol: state.symbol,
        reason: "Trying WebSocket again after REST cooldown"
      });
      this.runWebSocketLoop(state);
    }
  }

  emitEvent(type, payload) {
    this.onEvent({
      id: `MD-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
      time: Date.now(),
      type,
      ...payload
    });
  }
}
