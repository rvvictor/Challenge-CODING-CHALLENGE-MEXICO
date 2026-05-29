import { EventEmitter } from "node:events";
import { CcxtStreamProvider } from "../exchanges/ccxtStreamProvider.js";
import { SimulatedMarket } from "../exchanges/simulator.js";
import { RedisBus } from "../integrations/redisBus.js";
import { ArbitrageEngine } from "./arbitrageEngine.js";
import { ExecutionSimulator } from "./executionSimulator.js";
import { OpportunityQueue } from "./opportunityQueue.js";
import { RiskManager } from "./riskManager.js";
import { TriangularArbitrageEngine } from "./triangularArbitrageEngine.js";
import { WalletLedger } from "./walletLedger.js";
import { EventStore } from "../storage/eventStore.js";
import { best } from "./fills.js";

function midFromBook(book) {
  const ask = best(book.asks, "ask");
  const bid = best(book.bids, "bid");
  return ask && bid ? (ask.price + bid.price) / 2 : null;
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function primaryBookMap(books) {
  const map = new Map();
  for (const book of books) {
    if (book.primary) map.set(book.exchangeId, book);
  }
  return map;
}

export class MarketDataEngine extends EventEmitter {
  constructor(config) {
    super();
    this.config = structuredClone(config);
    this.mode = this.config.market.mode;
    this.simulator = new SimulatedMarket(this.config.exchanges);
    this.books = new Map();
    this.store = new EventStore();
    this.ledger = new WalletLedger(this.config);
    this.riskManager = new RiskManager(this.config);
    this.arbitrageEngine = new ArbitrageEngine(this.config, this.ledger);
    this.triangularEngine = new TriangularArbitrageEngine(this.config, this.ledger);
    this.opportunityQueue = new OpportunityQueue({ maxSize: 120 });
    this.executor = new ExecutionSimulator(this.config, this.ledger, this.store, this.riskManager);
    this.redisBus = new RedisBus(this.config);
    this.streamProvider = null;
    this.startedAt = Date.now();
    this.timer = null;
    this.lastExecutions = [];
    this.lastScan = [];
    this.degradedDemo = false;
  }

  start() {
    if (this.timer) return;
    void this.redisBus.start();
    if (this.mode !== "demo") void this.startStreams();
    this.tick();
    this.timer = setInterval(() => this.tick(), this.config.market.evaluationIntervalMs);
  }

  async startStreams() {
    if (this.streamProvider) return;
    this.streamProvider = new CcxtStreamProvider(this.config, {
      onBook: (book) => this.handleBook(book),
      onEvent: (event) => this.handleProviderEvent(event)
    });
    await this.streamProvider.start();
    this.degradedDemo = this.mode === "auto" && !this.streamProvider.available;
  }

  async stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.streamProvider) {
      await this.streamProvider.stop();
      this.streamProvider = null;
    }
  }

  setMode(mode) {
    if (!["auto", "live", "demo"].includes(mode)) return this.mode;
    if (mode === this.mode) return this.mode;
    this.mode = mode;
    this.degradedDemo = false;
    this.books.clear();

    if (mode === "demo") {
      if (this.streamProvider) void this.streamProvider.stop();
      this.streamProvider = null;
    } else {
      void this.startStreams();
    }

    return this.mode;
  }

  setAutoExecution(enabled) {
    this.riskManager.setAutoExecution(enabled);
  }

  reset() {
    this.store.reset();
    this.ledger.reset();
    this.riskManager.reset();
    this.executor.reset();
    this.startedAt = Date.now();
  }

  handleBook(book) {
    this.books.set(book.key || `${book.exchangeId}:${book.symbol || book.product}`, book);
  }

  handleProviderEvent(event) {
    this.store.addEvent(event);
    void this.redisBus.publish("market-events", event);
  }

  tick() {
    try {
      if (this.mode === "demo" || this.degradedDemo) {
        this.generateDemoBooks();
      }

      const primaryBooks = this.primaryBooks();
      const primarySnapshots = this.bookSummaries(primaryBooks);
      this.riskManager.evaluateMarket(Date.now(), primarySnapshots);
      this.flushRiskEvents();

      const crossExchange = this.arbitrageEngine.scan(primaryBookMap(primaryBooks));
      const triangular = this.triangularEngine.scan(this.books);
      const ranked = this.opportunityQueue.rank([...crossExchange, ...triangular]);
      this.lastScan = ranked;
      if (ranked.length) this.store.addOpportunities(ranked.slice(0, 30));

      this.lastExecutions = this.executor.tryExecute(ranked, primaryBooks);
      for (const trade of this.lastExecutions) {
        void this.redisBus.publish("trades", trade);
      }
      this.flushRiskEvents();

      const snapshot = this.snapshot();
      void this.redisBus.publish("snapshots", snapshot);
      this.emit("snapshot", snapshot);
    } catch (error) {
      this.emit("error", error);
    }
  }

  flushRiskEvents() {
    for (const event of this.riskManager.drainEvents()) {
      this.store.addEvent(event);
      void this.redisBus.publish("risk", event);
    }
  }

  generateDemoBooks() {
    for (const exchange of this.config.exchanges) {
      const symbols = unique([exchange.primarySymbol, ...(exchange.triangularSymbols || [])]);
      for (const symbol of symbols) {
        const previous = this.books.get(`${exchange.id}:${symbol}`);
        const previousMid = previous ? midFromBook(previous) : null;
        const book = this.simulator.generate(exchange, this.config.exchanges, previousMid, symbol);
        this.books.set(book.key, book);
      }
    }
  }

  primaryBooks() {
    return [...this.books.values()].filter((book) => book.primary && book.asks.length && book.bids.length);
  }

  bookSummaries(books) {
    const now = Date.now();
    return books.map((book) => {
      const ask = best(book.asks, "ask");
      const bid = best(book.bids, "bid");
      return {
        ...book,
        bestAsk: ask?.price || 0,
        bestBid: bid?.price || 0,
        ageMs: now - book.timestamp
      };
    });
  }

  snapshot() {
    const now = Date.now();
    const books = this.bookSummaries(this.primaryBooks()).map((book) => {
      const depthAsk = book.asks.reduce((sum, level) => sum + level.qty, 0);
      const depthBid = book.bids.reduce((sum, level) => sum + level.qty, 0);
      return {
        exchangeId: book.exchangeId,
        exchangeName: book.exchangeName,
        symbol: book.symbol,
        product: book.product,
        source: book.source,
        status: book.status,
        bestAsk: book.bestAsk,
        bestBid: book.bestBid,
        spread: book.bestAsk && book.bestBid ? book.bestAsk - book.bestBid : 0,
        depthAsk,
        depthBid,
        feeBps: book.feeBps,
        slippageBps: book.slippageBps,
        confidence: book.confidence,
        latencyMs: book.latencyMs,
        ageMs: now - book.timestamp,
        timestamp: book.timestamp,
        error: book.error
      };
    });
    const triangularBooks = [...this.books.values()]
      .filter((book) => !book.primary)
      .map((book) => ({
        exchangeId: book.exchangeId,
        exchangeName: book.exchangeName,
        symbol: book.symbol,
        source: book.source,
        timestamp: book.timestamp,
        ageMs: now - book.timestamp
      }));
    const markPrice = books.length
      ? books.reduce((sum, book) => sum + (book.bestAsk + book.bestBid) / 2, 0) / books.length
      : 0;
    const trades = this.store.latestTrades();
    const wins = trades.filter((trade) => trade.netProfit >= 0).length;
    const avgLatency = books.length
      ? books.reduce((sum, book) => sum + book.latencyMs, 0) / books.length
      : 0;
    const latestOpportunities = this.store.latestOpportunities();
    const triangularCount = latestOpportunities.filter((opportunity) => opportunity.strategy === "triangular").length;

    return {
      now,
      mode: this.mode,
      degradedDemo: this.degradedDemo,
      uptimeMs: now - this.startedAt,
      books,
      triangularBooks,
      opportunities: latestOpportunities,
      queuedOpportunities: this.lastScan.slice(0, 40),
      trades,
      wallets: this.ledger.all(),
      totals: this.ledger.totals(markPrice),
      pnlSeries: this.store.pnlSeries,
      risk: this.riskManager.snapshot(now),
      riskEvents: this.store.latestEvents(),
      redis: this.redisBus.snapshot(),
      streams: this.streamProvider?.snapshot() || {
        available: false,
        unavailableReason: this.mode === "demo" ? "Demo mode" : "Not started",
        streams: []
      },
      queue: this.opportunityQueue.snapshot(),
      metrics: {
        detectedCount: this.store.detectedCount,
        rejectedCount: this.store.rejectedCount,
        executedCount: this.store.executedCount,
        simpleCount: this.store.simpleCount,
        triangularCount: this.store.triangularCount,
        queuedTriangular: triangularCount,
        cumulativePnl: this.ledger.realizedPnl,
        winRate: trades.length ? wins / trades.length : 0,
        avgLatencyMs: avgLatency,
        liveBooks: books.filter((book) => book.source === "websocket").length,
        restBooks: books.filter((book) => book.source === "rest").length,
        simulatedBooks: books.filter((book) => book.source === "simulated").length,
        bestNetBps: latestOpportunities.slice(0, 20).reduce((bestScore, opp) => Math.max(bestScore, opp.netBps || 0), 0)
      }
    };
  }
}
