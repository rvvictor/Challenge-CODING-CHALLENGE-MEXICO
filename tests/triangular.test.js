import test from "node:test";
import assert from "node:assert/strict";
import { CONFIG } from "../src/config.js";
import { TriangularArbitrageEngine } from "../src/engine/triangularArbitrageEngine.js";
import { WalletLedger } from "../src/engine/walletLedger.js";

function book(exchange, symbol, asks, bids) {
  return {
    key: `${exchange.id}:${symbol}`,
    exchangeId: exchange.id,
    exchangeName: exchange.name,
    symbol,
    primary: symbol === exchange.primarySymbol,
    source: "test",
    feeBps: exchange.takerFeeBps,
    slippageBps: exchange.slippageBps,
    confidence: 1,
    asks,
    bids,
    latencyMs: 20,
    timestamp: Date.now()
  };
}

test("TriangularArbitrageEngine detects profitable USDT BTC ETH USDT cycle", () => {
  const config = structuredClone(CONFIG);
  config.triangular.quoteSize = 2500;
  config.triangular.minNetBps = 0.5;
  config.triangular.minNetProfitUsd = 0.1;
  const exchange = config.exchanges[0];
  const books = new Map();
  books.set(`${exchange.id}:BTC/USDT`, book(exchange, "BTC/USDT", [{ price: 70000, qty: 2 }], [{ price: 69980, qty: 2 }]));
  books.set(`${exchange.id}:ETH/BTC`, book(exchange, "ETH/BTC", [{ price: 0.05, qty: 100 }], [{ price: 0.0499, qty: 100 }]));
  books.set(`${exchange.id}:ETH/USDT`, book(exchange, "ETH/USDT", [{ price: 3570, qty: 100 }], [{ price: 3625, qty: 100 }]));

  const engine = new TriangularArbitrageEngine(config, new WalletLedger(config));
  const opportunities = engine.scan(books);

  assert.equal(opportunities.length, 1);
  assert.equal(opportunities[0].strategy, "triangular");
  assert.equal(opportunities[0].status, "profitable");
  assert.ok(opportunities[0].netProfit > 0);
});
