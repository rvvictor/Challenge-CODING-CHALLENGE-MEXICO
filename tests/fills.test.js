import test from "node:test";
import assert from "node:assert/strict";
import { estimateFill } from "../src/engine/fills.js";

test("estimateFill consumes multiple levels and reports a complete fill", () => {
  const fill = estimateFill([
    { price: 100, qty: 0.5 },
    { price: 101, qty: 0.75 },
    { price: 102, qty: 1 }
  ], 1, "ask");

  assert.equal(fill.filledQty, 1);
  assert.equal(fill.quote, 100 * 0.5 + 101 * 0.5);
  assert.equal(fill.levelCount, 2);
  assert.equal(fill.partial, false);
});

test("estimateFill marks partial fills when depth is not enough", () => {
  const fill = estimateFill([
    { price: 100, qty: 0.2 },
    { price: 101, qty: 0.3 }
  ], 1, "bid");

  assert.ok(Math.abs(fill.filledQty - 0.5) < 0.00000001);
  assert.ok(Math.abs(fill.unfilledQty - 0.5) < 0.00000001);
  assert.equal(fill.partial, true);
});
