function routeKey(opportunity) {
  if (opportunity.strategy === "triangular") {
    return `triangular:${opportunity.exchangeId}:${opportunity.cycleId}`;
  }

  const pair = [opportunity.buyExchangeId, opportunity.sellExchangeId].sort().join("<>");
  return `simple:${opportunity.product}:${pair}`;
}

export class OpportunityQueue {
  constructor({ maxSize = 100 } = {}) {
    this.maxSize = maxSize;
    this.lastStats = {
      received: 0,
      deduped: 0,
      queued: 0,
      executable: 0
    };
  }

  rank(opportunities) {
    const byKey = new Map();
    for (const opportunity of opportunities) {
      const key = routeKey(opportunity);
      const existing = byKey.get(key);
      if (!existing || opportunity.score > existing.score) {
        byKey.set(key, {
          ...opportunity,
          dedupeKey: key
        });
      }
    }

    const queued = [...byKey.values()]
      .sort((a, b) => b.score - a.score)
      .slice(0, this.maxSize);

    this.lastStats = {
      received: opportunities.length,
      deduped: opportunities.length - queued.length,
      queued: queued.length,
      executable: queued.filter((opportunity) => opportunity.status === "profitable").length
    };

    return queued;
  }

  snapshot() {
    return { ...this.lastStats };
  }
}
