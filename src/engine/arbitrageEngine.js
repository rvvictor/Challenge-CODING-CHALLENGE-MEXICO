import { best, depthQty, estimateFill } from "./fills.js";

function round(value, decimals = 6) {
  const factor = 10 ** decimals;
  return Math.round((Number(value) || 0) * factor) / factor;
}

function getExchangeConfig(config, exchangeId) {
  return config.exchanges.find((exchange) => exchange.id === exchangeId);
}

function ageConfidence(ageMs, maxAgeMs) {
  if (ageMs <= maxAgeMs * 0.35) return 1;
  if (ageMs >= maxAgeMs) return 0.2;
  return Math.max(0.2, 1 - ageMs / maxAgeMs);
}

export class ArbitrageEngine {
  constructor(config, ledger) {
    this.config = config;
    this.ledger = ledger;
  }

  scan(booksByExchange) {
    const now = Date.now();
    const books = [...booksByExchange.values()].filter((book) => book.asks.length && book.bids.length);
    const opportunities = [];

    for (const buyBook of books) {
      for (const sellBook of books) {
        if (buyBook.exchangeId === sellBook.exchangeId) continue;
        const opportunity = this.evaluatePair(buyBook, sellBook, now);
        if (opportunity) opportunities.push(opportunity);
      }
    }

    return opportunities.sort((a, b) => b.score - a.score);
  }

  evaluatePair(buyBook, sellBook, now) {
    const bestAsk = best(buyBook.asks, "ask");
    const bestBid = best(sellBook.bids, "bid");
    if (!bestAsk || !bestBid || bestAsk.price >= bestBid.price) return null;

    const buyExchange = getExchangeConfig(this.config, buyBook.exchangeId);
    const sellExchange = getExchangeConfig(this.config, sellBook.exchangeId);
    const buyWallet = this.ledger.get(buyBook.exchangeId);
    const sellWallet = this.ledger.get(sellBook.exchangeId);
    const walletLimitedQty = Math.min(
      sellWallet?.BTC || 0,
      ((buyWallet?.USDT || 0) * 0.985) / bestAsk.price
    );
    const maxDepthQty = Math.min(depthQty(buyBook.asks), depthQty(sellBook.bids));
    const targetQty = Math.min(this.config.trade.maxTradeBtc, maxDepthQty, walletLimitedQty);

    if (targetQty < this.config.trade.minTradeBtc) {
      return {
        id: `${now}-${buyBook.exchangeId}-${sellBook.exchangeId}-blocked`,
        strategy: "simple",
        time: now,
        buyExchangeId: buyBook.exchangeId,
        sellExchangeId: sellBook.exchangeId,
        buyExchange: buyBook.exchangeName,
        sellExchange: sellBook.exchangeName,
        product: buyBook.product,
        qtyBtc: round(targetQty, 8),
        buyPrice: bestAsk.price,
        sellPrice: bestBid.price,
        grossSpread: round(bestBid.price - bestAsk.price, 2),
        grossBps: round(((bestBid.price - bestAsk.price) / bestAsk.price) * 10000, 3),
        netProfit: 0,
        netBps: 0,
        score: -1,
        status: "blocked",
        reason: "Insufficient wallet balance or depth"
      };
    }

    const buyFill = estimateFill(buyBook.asks, targetQty, "ask");
    const sellFill = estimateFill(sellBook.bids, targetQty, "bid");
    const qty = Math.min(buyFill.filledQty, sellFill.filledQty);
    const adjustedBuyFill = qty < buyFill.filledQty ? estimateFill(buyBook.asks, qty, "ask") : buyFill;
    const adjustedSellFill = qty < sellFill.filledQty ? estimateFill(sellBook.bids, qty, "bid") : sellFill;

    const buyFee = adjustedBuyFill.quote * (buyExchange.takerFeeBps / 10000);
    const sellFee = adjustedSellFill.quote * (sellExchange.takerFeeBps / 10000);
    const slippageCostBuy = adjustedBuyFill.quote * (buyExchange.slippageBps / 10000);
    const slippageCostSell = adjustedSellFill.quote * (sellExchange.slippageBps / 10000);
    const latencySeconds = (buyBook.latencyMs + sellBook.latencyMs) / 2000;
    const latencyRiskBps = Math.max(
      this.config.risk.latencyRiskFloorBps,
      latencySeconds * this.config.risk.latencyBpsPerSecond
    );
    const latencyRiskCost = adjustedBuyFill.quote * (latencyRiskBps / 10000);
    const rebalanceCost = (
      buyExchange.withdrawalFeeBtc * adjustedSellFill.avgPrice +
      sellExchange.withdrawalFeeQuote
    ) * this.config.trade.withdrawalFeeImpact;
    const grossProfit = adjustedSellFill.quote - adjustedBuyFill.quote;
    const totalCosts = buyFee + sellFee + slippageCostBuy + slippageCostSell + latencyRiskCost + rebalanceCost;
    const netProfit = grossProfit - totalCosts;
    const netBps = adjustedBuyFill.quote > 0 ? (netProfit / adjustedBuyFill.quote) * 10000 : 0;
    const grossBps = adjustedBuyFill.quote > 0 ? (grossProfit / adjustedBuyFill.quote) * 10000 : 0;
    const buyAge = now - buyBook.timestamp;
    const sellAge = now - sellBook.timestamp;
    const confidence = Math.min(
      buyBook.confidence,
      sellBook.confidence,
      ageConfidence(buyAge, this.config.risk.maxBookAgeMs),
      ageConfidence(sellAge, this.config.risk.maxBookAgeMs)
    );
    const profitable = netProfit >= this.config.trade.minNetProfitUsd &&
      netBps >= this.config.trade.minNetBps &&
      confidence >= this.config.risk.minConfidence;
    const partial = qty < this.config.trade.maxTradeBtc || adjustedBuyFill.partial || adjustedSellFill.partial;
    const latencyPenalty = 1 + (buyBook.latencyMs + sellBook.latencyMs) / 800;
    const score = profitable
      ? (netBps * confidence * Math.sqrt(Math.max(qty, 0.000001))) / latencyPenalty
      : netBps * confidence;

    return {
      id: `${now}-${buyBook.exchangeId}-${sellBook.exchangeId}-${Math.round(adjustedBuyFill.avgPrice)}-${Math.round(adjustedSellFill.avgPrice)}`,
      strategy: "simple",
      time: now,
      buyExchangeId: buyBook.exchangeId,
      sellExchangeId: sellBook.exchangeId,
      buyExchange: buyBook.exchangeName,
      sellExchange: sellBook.exchangeName,
      product: buyBook.product,
      qtyBtc: round(qty, 8),
      buyPrice: round(adjustedBuyFill.avgPrice, 2),
      sellPrice: round(adjustedSellFill.avgPrice, 2),
      bestAsk: bestAsk.price,
      bestBid: bestBid.price,
      grossSpread: round(bestBid.price - bestAsk.price, 2),
      grossProfit: round(grossProfit, 4),
      grossBps: round(grossBps, 3),
      netProfit: round(netProfit, 4),
      netBps: round(netBps, 3),
      score: round(score, 5),
      confidence: round(confidence, 3),
      status: profitable ? "profitable" : "rejected",
      reason: profitable ? "Net edge cleared risk gates" : "Costs or risk removed the edge",
      partial,
      costs: {
        buyFee: round(buyFee, 4),
        sellFee: round(sellFee, 4),
        slippageCostBuy: round(slippageCostBuy, 4),
        slippageCostSell: round(slippageCostSell, 4),
        latencyRiskCost: round(latencyRiskCost, 4),
        latencyRiskBps: round(latencyRiskBps, 3),
        rebalanceCost: round(rebalanceCost, 4),
        totalCosts: round(totalCosts, 4)
      },
      fills: {
        buyLevels: adjustedBuyFill.levelCount,
        sellLevels: adjustedSellFill.levelCount
      },
      latencies: {
        buyMs: buyBook.latencyMs,
        sellMs: sellBook.latencyMs
      },
      source: buyBook.source === sellBook.source ? buyBook.source : "mixed"
    };
  }
}
