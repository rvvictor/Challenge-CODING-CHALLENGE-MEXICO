export function normalizeLevel(level) {
  return {
    price: Number(level.price),
    qty: Number(level.qty)
  };
}

export function sortLevels(levels, side) {
  const normalized = levels
    .map(normalizeLevel)
    .filter((level) => Number.isFinite(level.price) && Number.isFinite(level.qty) && level.price > 0 && level.qty > 0);

  return normalized.sort((a, b) => (side === "ask" ? a.price - b.price : b.price - a.price));
}

export function depthQty(levels) {
  return levels.reduce((sum, level) => sum + Number(level.qty || 0), 0);
}

export function estimateFill(levels, requestedQty, side) {
  const sortedLevels = sortLevels(levels, side);
  let remaining = Math.max(0, Number(requestedQty) || 0);
  let filledQty = 0;
  let quote = 0;
  const consumed = [];

  for (const level of sortedLevels) {
    if (remaining <= 0) break;
    const qty = Math.min(remaining, level.qty);
    if (qty <= 0) continue;

    consumed.push({
      price: level.price,
      qty
    });

    filledQty += qty;
    quote += qty * level.price;
    remaining -= qty;
  }

  return {
    requestedQty,
    filledQty,
    unfilledQty: Math.max(0, remaining),
    quote,
    avgPrice: filledQty > 0 ? quote / filledQty : 0,
    levels: consumed,
    levelCount: consumed.length,
    partial: remaining > 0.00000001
  };
}

export function best(levels, side) {
  const sorted = sortLevels(levels, side);
  return sorted[0] || null;
}
