import test from "node:test";
import assert from "node:assert/strict";
import { CONFIG } from "../src/config.js";
import { RiskManager } from "../src/engine/riskManager.js";

function book(price, now) {
  return {
    exchangeName: "Binance",
    timestamp: now,
    bestAsk: price + 1,
    bestBid: price - 1
  };
}

test("RiskManager activates circuit breaker on fast BTC volatility", () => {
  const config = structuredClone(CONFIG);
  config.risk.maxVolatilityPct = 1.5;
  config.risk.volatilityWindowMs = 30000;
  const risk = new RiskManager(config);
  const now = Date.now();

  risk.evaluateMarket(now, [book(70000, now)]);
  risk.evaluateMarket(now + 15000, [book(71200, now + 15000)]);

  assert.equal(risk.snapshot(now + 15000).paused, true);
  assert.equal(risk.drainEvents()[0].metadata.condition, "volatility");
});

test("RiskManager activates circuit breaker after five losing trades", () => {
  const config = structuredClone(CONFIG);
  config.risk.maxLossStreak = 5;
  const risk = new RiskManager(config);
  const now = Date.now();

  for (let index = 0; index < 5; index += 1) {
    risk.recordTrade({ netProfit: -1 }, now + index);
  }

  assert.equal(risk.snapshot(now + 5).paused, true);
  assert.equal(risk.drainEvents()[0].metadata.condition, "loss-streak");
});
