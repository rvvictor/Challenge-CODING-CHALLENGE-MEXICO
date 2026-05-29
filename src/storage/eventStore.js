export class EventStore {
  constructor({ opportunitiesLimit = 800, tradesLimit = 300, pnlLimit = 400, eventLimit = 200 } = {}) {
    this.opportunitiesLimit = opportunitiesLimit;
    this.tradesLimit = tradesLimit;
    this.pnlLimit = pnlLimit;
    this.eventLimit = eventLimit;
    this.reset();
  }

  reset() {
    this.opportunities = [];
    this.trades = [];
    this.pnlSeries = [];
    this.events = [];
    this.detectedCount = 0;
    this.rejectedCount = 0;
    this.executedCount = 0;
    this.triangularCount = 0;
    this.simpleCount = 0;
  }

  addOpportunities(opportunities) {
    for (const opportunity of opportunities) {
      this.detectedCount += 1;
      if (opportunity.strategy === "triangular") this.triangularCount += 1;
      else this.simpleCount += 1;
      if (opportunity.status !== "profitable") this.rejectedCount += 1;
      this.opportunities.unshift(opportunity);
    }
    this.opportunities = this.opportunities.slice(0, this.opportunitiesLimit);
  }

  addTrade(trade, cumulativePnl) {
    this.executedCount += 1;
    this.trades.unshift(trade);
    this.trades = this.trades.slice(0, this.tradesLimit);
    this.pnlSeries.push({
      time: trade.time,
      pnl: cumulativePnl
    });
    this.pnlSeries = this.pnlSeries.slice(-this.pnlLimit);
  }

  addEvent(event) {
    this.events.unshift(event);
    this.events = this.events.slice(0, this.eventLimit);
  }

  latestOpportunities(limit = 80) {
    return this.opportunities.slice(0, limit);
  }

  latestTrades(limit = 80) {
    return this.trades.slice(0, limit);
  }

  latestEvents(limit = 60) {
    return this.events.slice(0, limit);
  }
}
