function makeTradeId() {
  return `T-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export class ExecutionSimulator {
  constructor(config, ledger, store, riskManager) {
    this.config = config;
    this.ledger = ledger;
    this.store = store;
    this.riskManager = riskManager;
    this.cooldowns = new Map();
  }

  reset() {
    this.cooldowns.clear();
  }

  tryExecute(opportunities, books) {
    const now = Date.now();
    const executions = [];
    const risk = this.riskManager.canExecute(now, books);
    if (!risk.allowed) return executions;

    for (const opportunity of opportunities) {
      if (executions.length >= this.config.trade.maxExecutionsPerTick) break;
      if (opportunity.status !== "profitable") continue;

      const key = opportunity.dedupeKey || `${opportunity.buyExchangeId}->${opportunity.sellExchangeId}`;
      const blockedUntil = this.cooldowns.get(key) || 0;
      if (blockedUntil > now) continue;

      const trade = this.buildTrade(opportunity);
      this.ledger.applyTrade(trade);
      this.cooldowns.set(key, now + this.config.trade.pairCooldownMs);
      this.riskManager.recordTrade(trade, now);
      this.store.addTrade(trade, this.ledger.realizedPnl);
      executions.push(trade);
    }

    return executions;
  }

  buildTrade(opportunity) {
    if (opportunity.strategy === "triangular") {
      return {
        id: makeTradeId(),
        strategy: "triangular",
        time: Date.now(),
        opportunityId: opportunity.id,
        exchangeId: opportunity.exchangeId,
        exchange: opportunity.exchange,
        product: opportunity.product,
        cycleId: opportunity.cycleId,
        cyclePath: opportunity.cyclePath,
        quoteIn: opportunity.quoteIn,
        quoteOut: opportunity.quoteOut,
        qtyBtc: opportunity.qtyBtc,
        qtyEth: opportunity.qtyEth,
        grossProfit: opportunity.grossProfit,
        netProfit: opportunity.netProfit,
        netBps: opportunity.netBps,
        confidence: opportunity.confidence,
        partial: opportunity.partial,
        totalCosts: opportunity.costs.totalCosts,
        legs: opportunity.legs,
        source: opportunity.source,
        status: opportunity.partial ? "partial-cycle" : "filled"
      };
    }

    return {
      id: makeTradeId(),
      strategy: "simple",
      time: Date.now(),
      opportunityId: opportunity.id,
      buyExchangeId: opportunity.buyExchangeId,
      sellExchangeId: opportunity.sellExchangeId,
      buyExchange: opportunity.buyExchange,
      sellExchange: opportunity.sellExchange,
      product: opportunity.product,
      qtyBtc: opportunity.qtyBtc,
      buyPrice: opportunity.buyPrice,
      sellPrice: opportunity.sellPrice,
      buyQuote: opportunity.buyPrice * opportunity.qtyBtc,
      sellQuote: opportunity.sellPrice * opportunity.qtyBtc,
      buyFee: opportunity.costs.buyFee,
      sellFee: opportunity.costs.sellFee,
      slippageCostBuy: opportunity.costs.slippageCostBuy,
      slippageCostSell: opportunity.costs.slippageCostSell,
      latencyRiskCost: opportunity.costs.latencyRiskCost,
      rebalanceCost: opportunity.costs.rebalanceCost,
      totalCosts: opportunity.costs.totalCosts,
      grossProfit: opportunity.grossProfit,
      netProfit: opportunity.netProfit,
      netBps: opportunity.netBps,
      confidence: opportunity.confidence,
      partial: opportunity.partial,
      source: opportunity.source,
      status: opportunity.partial ? "partial-fill" : "filled"
    };
  }
}
