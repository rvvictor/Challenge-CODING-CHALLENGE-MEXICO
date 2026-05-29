export class RiskManager {
  constructor(config) {
    this.config = config;
    this.autoExecution = config.trade.autoExecution;
    this.reset();
  }

  reset() {
    this.lossStreak = 0;
    this.pausedUntil = 0;
    this.lastReason = "Ready";
    this.priceWindow = [];
    this.pendingEvents = [];
  }

  setAutoExecution(enabled) {
    this.autoExecution = Boolean(enabled);
  }

  canExecute(now, books) {
    if (!this.autoExecution) {
      return { allowed: false, reason: "Auto execution disabled" };
    }

    if (now < this.pausedUntil) {
      return { allowed: false, reason: "Circuit breaker cooling down" };
    }

    const staleBooks = books.filter((book) => now - book.timestamp > this.config.risk.maxBookAgeMs);
    if (staleBooks.length > 0) {
      this.activate(now, `Stale order book >${this.config.risk.maxBookAgeMs}ms`, {
        books: staleBooks.map((book) => book.exchangeName)
      });
      return {
        allowed: false,
        reason: `Stale market data: ${staleBooks.map((book) => book.exchangeName).join(", ")}`
      };
    }

    return { allowed: true, reason: "Risk checks passed" };
  }

  evaluateMarket(now, books) {
    const freshBooks = books.filter((book) => book.bestAsk > 0 && book.bestBid > 0);
    if (freshBooks.length === 0) return;

    const staleBooks = freshBooks.filter((book) => now - book.timestamp > this.config.risk.maxBookAgeMs);
    if (staleBooks.length > 0) {
      this.activate(now, `Stale data: ${staleBooks.map((book) => book.exchangeName).join(", ")}`, {
        condition: "stale-data",
        maxAgeMs: this.config.risk.maxBookAgeMs
      });
      return;
    }

    const mid = freshBooks.reduce((sum, book) => sum + (book.bestAsk + book.bestBid) / 2, 0) / freshBooks.length;
    this.priceWindow.push({ time: now, price: mid });
    this.priceWindow = this.priceWindow.filter((point) => now - point.time <= this.config.risk.volatilityWindowMs);
    const oldest = this.priceWindow[0];
    if (!oldest || oldest.price <= 0) return;

    const changePct = Math.abs((mid - oldest.price) / oldest.price) * 100;
    if (changePct > this.config.risk.maxVolatilityPct) {
      this.activate(now, `BTC volatility ${changePct.toFixed(2)}% in ${Math.round((now - oldest.time) / 1000)}s`, {
        condition: "volatility",
        changePct
      });
    }
  }

  recordTrade(trade, now) {
    if (trade.netProfit < 0) {
      this.lossStreak += 1;
      if (this.lossStreak >= this.config.risk.maxLossStreak) {
        this.activate(now, `${this.lossStreak} consecutive losing trades`, {
          condition: "loss-streak",
          lossStreak: this.lossStreak
        });
      }
      return;
    }

    this.lossStreak = 0;
    this.lastReason = "Healthy";
  }

  activate(now, reason, metadata = {}) {
    if (now < this.pausedUntil && this.lastReason === reason) return null;
    this.pausedUntil = now + this.config.risk.pauseAfterLossMs;
    this.lastReason = reason;
    const event = {
      id: `CB-${now.toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
      type: "circuit-breaker",
      time: now,
      reason,
      cooldownMs: this.config.risk.pauseAfterLossMs,
      pausedUntil: this.pausedUntil,
      metadata
    };
    this.pendingEvents.push(event);
    return event;
  }

  drainEvents() {
    const events = this.pendingEvents;
    this.pendingEvents = [];
    return events;
  }

  snapshot(now = Date.now()) {
    return {
      autoExecution: this.autoExecution,
      lossStreak: this.lossStreak,
      paused: now < this.pausedUntil,
      pausedUntil: this.pausedUntil,
      reason: now < this.pausedUntil ? this.lastReason : "Healthy",
      volatilityWindowPoints: this.priceWindow.length
    };
  }
}
