import test from "node:test";
import assert from "node:assert/strict";
import { CONFIG } from "../src/config.js";
import { ArbitrageEngine } from "../src/engine/arbitrageEngine.js";
import { WalletLedger } from "../src/engine/walletLedger.js";

function book(exchange, ask, bid) {
  return {
    exchangeId: exchange.id,
    exchangeName: exchange.name,
    product: exchange.product,
    source: "test",
    status: "live",
    feeBps: exchange.takerFeeBps,
    slippageBps: exchange.slippageBps,
    confidence: 1,
    asks: [{ price: ask, qty: 1 }],
    bids: [{ price: bid, qty: 1 }],
    latencyMs: 20,
    timestamp: Date.now(),
    error: null
  };
}

test("ArbitrageEngine returns profitable opportunity after real costs", () => {
  const config = structuredClone(CONFIG);
  config.trade.maxTradeBtc = 0.1;
  config.trade.minTradeBtc = 0.01;
  config.trade.minNetBps = 0.5;
  config.trade.withdrawalFeeImpact = 0;
  const ledger = new WalletLedger(config);
  const engine = new ArbitrageEngine(config, ledger);
  const buyExchange = config.exchanges[0];
  const sellExchange = config.exchanges[1];

  const opportunity = engine.evaluatePair(
    book(buyExchange, 70000, 69990),
    book(sellExchange, 70300, 70280),
    Date.now()
  );

  assert.equal(opportunity.status, "profitable");
  assert.ok(opportunity.netProfit > 0);
  assert.ok(opportunity.costs.totalCosts > 0);
});

test("ArbitrageEngine rejects gross edge when costs dominate", () => {
  const config = structuredClone(CONFIG);
  config.trade.maxTradeBtc = 0.1;
  config.trade.minTradeBtc = 0.01;
  const ledger = new WalletLedger(config);
  const engine = new ArbitrageEngine(config, ledger);
  const buyExchange = config.exchanges[2];
  const sellExchange = config.exchanges[3];

  const opportunity = engine.evaluatePair(
    book(buyExchange, 70000, 69990),
    book(sellExchange, 70020, 70015),
    Date.now()
  );

  assert.equal(opportunity.status, "rejected");
  assert.ok(opportunity.grossProfit > 0);
  assert.ok(opportunity.netProfit < opportunity.grossProfit);
});
