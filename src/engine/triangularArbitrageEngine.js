import { best, estimateFill } from "./fills.js";

function round(value, decimals = 6) {
  const factor = 10 ** decimals;
  return Math.round((Number(value) || 0) * factor) / factor;
}

function bookKey(exchangeId, symbol) {
  return `${exchangeId}:${symbol}`;
}

function estimateBuyWithQuote(asks, quoteBudget) {
  let remainingQuote = quoteBudget;
  let baseReceived = 0;
  let quoteSpent = 0;
  let levelCount = 0;

  for (const level of asks) {
    if (remainingQuote <= 0) break;
    const maxQuoteAtLevel = level.price * level.qty;
    const quoteAtLevel = Math.min(remainingQuote, maxQuoteAtLevel);
    const baseAtLevel = quoteAtLevel / level.price;
    baseReceived += baseAtLevel;
    quoteSpent += quoteAtLevel;
    remainingQuote -= quoteAtLevel;
    levelCount += 1;
  }

  return {
    quoteSpent,
    baseReceived,
    avgPrice: baseReceived > 0 ? quoteSpent / baseReceived : 0,
    unspentQuote: remainingQuote,
    levelCount,
    partial: remainingQuote > quoteBudget * 0.001
  };
}

function feeRate(exchange) {
  return (exchange.takerFeeBps + exchange.slippageBps) / 10000;
}

export class TriangularArbitrageEngine {
  constructor(config, ledger) {
    this.config = config;
    this.ledger = ledger;
  }

  scan(booksByKey) {
    if (!this.config.triangular.enabled) return [];
    const now = Date.now();
    const opportunities = [];

    for (const exchange of this.config.exchanges) {
      const cycles = (exchange.triangularCycles || []).slice(0, this.config.triangular.maxCyclesPerExchange);
      for (const cycle of cycles) {
        const opportunity = this.evaluateCycle(exchange, cycle, booksByKey, now);
        if (opportunity) opportunities.push(opportunity);
      }
    }

    return opportunities.sort((a, b) => b.score - a.score);
  }

  evaluateCycle(exchange, cycle, booksByKey, now) {
    const [btcQuoteSymbol, ethBtcSymbol, ethQuoteSymbol] = cycle.symbols;
    const btcQuoteBook = booksByKey.get(bookKey(exchange.id, btcQuoteSymbol));
    const ethBtcBook = booksByKey.get(bookKey(exchange.id, ethBtcSymbol));
    const ethQuoteBook = booksByKey.get(bookKey(exchange.id, ethQuoteSymbol));

    if (!btcQuoteBook?.asks?.length || !ethBtcBook?.asks?.length || !ethQuoteBook?.bids?.length) return null;

    const wallet = this.ledger.get(exchange.id);
    const quoteIn = Math.min(this.config.triangular.quoteSize, (wallet?.USDT || 0) * 0.12);
    if (quoteIn < 100) return null;

    const combinedFeeRate = feeRate(exchange);
    const step1 = estimateBuyWithQuote(btcQuoteBook.asks, quoteIn);
    if (step1.baseReceived <= 0) return null;
    const btcAfterCosts = step1.baseReceived * (1 - combinedFeeRate);

    const step2 = estimateBuyWithQuote(ethBtcBook.asks, btcAfterCosts);
    if (step2.baseReceived <= 0) return null;
    const ethAfterCosts = step2.baseReceived * (1 - combinedFeeRate);

    const step3 = estimateFill(ethQuoteBook.bids, ethAfterCosts, "bid");
    if (step3.filledQty <= 0) return null;
    const grossQuoteOut = step3.quote;
    const finalQuoteOut = grossQuoteOut * (1 - combinedFeeRate);
    const latencyMs = btcQuoteBook.latencyMs + ethBtcBook.latencyMs + ethQuoteBook.latencyMs;
    const latencyRiskBps = Math.max(
      this.config.risk.latencyRiskFloorBps,
      (latencyMs / 3000) * this.config.risk.latencyBpsPerSecond
    );
    const latencyRiskCost = quoteIn * (latencyRiskBps / 10000);
    const netProfit = finalQuoteOut - quoteIn - latencyRiskCost;
    const grossProfit = grossQuoteOut - quoteIn;
    const netBps = (netProfit / quoteIn) * 10000;
    const grossBps = (grossProfit / quoteIn) * 10000;
    const ageMs = Math.max(
      now - btcQuoteBook.timestamp,
      now - ethBtcBook.timestamp,
      now - ethQuoteBook.timestamp
    );
    const confidence = Math.min(
      exchange.confidence,
      btcQuoteBook.confidence,
      ethBtcBook.confidence,
      ethQuoteBook.confidence,
      Math.max(0.2, 1 - ageMs / this.config.risk.maxBookAgeMs)
    );
    const totalCosts = (quoteIn * combinedFeeRate) +
      (step2.quoteSpent * combinedFeeRate * step1.avgPrice) +
      (grossQuoteOut * combinedFeeRate) +
      latencyRiskCost;
    const profitable = netProfit >= this.config.triangular.minNetProfitUsd &&
      netBps >= this.config.triangular.minNetBps &&
      confidence >= this.config.risk.minConfidence;
    const partial = step1.partial || step2.partial || step3.partial;
    const score = profitable
      ? (netBps * confidence * Math.log10(Math.max(quoteIn, 10))) / (1 + latencyMs / 1200)
      : netBps * confidence;

    return {
      id: `${now}-${exchange.id}-${cycle.id}`,
      strategy: "triangular",
      time: now,
      exchangeId: exchange.id,
      exchange: exchange.name,
      cycleId: cycle.id,
      cyclePath: cycle.path,
      product: cycle.path.join(" -> "),
      quoteIn: round(quoteIn, 4),
      quoteOut: round(finalQuoteOut, 4),
      qtyBtc: round(step1.baseReceived, 8),
      qtyEth: round(step2.baseReceived, 8),
      buyPrice: round(step1.avgPrice, 8),
      sellPrice: round(step3.avgPrice, 8),
      grossProfit: round(grossProfit, 4),
      grossBps: round(grossBps, 3),
      netProfit: round(netProfit, 4),
      netBps: round(netBps, 3),
      score: round(score, 5),
      confidence: round(confidence, 3),
      status: profitable ? "profitable" : "rejected",
      reason: profitable ? "Triangular cycle cleared risk gates" : "Triangular costs or risk removed the edge",
      partial,
      costs: {
        totalCosts: round(totalCosts, 4),
        latencyRiskCost: round(latencyRiskCost, 4),
        latencyRiskBps: round(latencyRiskBps, 3)
      },
      legs: [
        {
          action: "buy",
          symbol: btcQuoteSymbol,
          from: cycle.path[0],
          to: cycle.path[1],
          avgPrice: round(step1.avgPrice, 8),
          levels: step1.levelCount
        },
        {
          action: "buy",
          symbol: ethBtcSymbol,
          from: cycle.path[1],
          to: cycle.path[2],
          avgPrice: round(step2.avgPrice, 8),
          levels: step2.levelCount
        },
        {
          action: "sell",
          symbol: ethQuoteSymbol,
          from: cycle.path[2],
          to: cycle.path[3],
          avgPrice: round(step3.avgPrice, 8),
          levels: step3.levelCount
        }
      ],
      latencies: {
        totalMs: latencyMs
      },
      source: [btcQuoteBook.source, ethBtcBook.source, ethQuoteBook.source].every((source) => source === "websocket")
        ? "websocket"
        : "mixed"
    };
  }
}
