import test from "node:test";
import assert from "node:assert/strict";
import { OpportunityQueue } from "../src/engine/opportunityQueue.js";

test("OpportunityQueue keeps only the best direction for the same venue pair", () => {
  const queue = new OpportunityQueue();
  const ranked = queue.rank([
    {
      strategy: "simple",
      product: "BTC/USDT",
      buyExchangeId: "binance",
      sellExchangeId: "kraken",
      score: 1,
      status: "profitable"
    },
    {
      strategy: "simple",
      product: "BTC/USDT",
      buyExchangeId: "kraken",
      sellExchangeId: "binance",
      score: 3,
      status: "profitable"
    }
  ]);

  assert.equal(ranked.length, 1);
  assert.equal(ranked[0].buyExchangeId, "kraken");
  assert.equal(queue.snapshot().deduped, 1);
});

test("OpportunityQueue dedupes repeated triangular cycles by exchange and cycle", () => {
  const queue = new OpportunityQueue();
  const ranked = queue.rank([
    {
      strategy: "triangular",
      exchangeId: "binance",
      cycleId: "USDT-BTC-ETH-USDT",
      score: 2,
      status: "rejected"
    },
    {
      strategy: "triangular",
      exchangeId: "binance",
      cycleId: "USDT-BTC-ETH-USDT",
      score: 5,
      status: "profitable"
    }
  ]);

  assert.equal(ranked.length, 1);
  assert.equal(ranked[0].score, 5);
  assert.equal(ranked[0].status, "profitable");
});
