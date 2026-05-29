import { sortLevels } from "../engine/fills.js";

class DeterministicRandom {
  constructor(seed = 42) {
    this.seed = seed;
  }

  next() {
    this.seed = (1664525 * this.seed + 1013904223) % 4294967296;
    return this.seed / 4294967296;
  }

  between(min, max) {
    return min + (max - min) * this.next();
  }
}

export class SimulatedMarket {
  constructor(exchanges) {
    this.random = new DeterministicRandom(71021);
    this.tick = 0;
    this.state = new Map();
    this.shock = null;

    exchanges.forEach((exchange, index) => {
      this.state.set(`${exchange.id}:BTC`, {
        mid: 70000 + index * 22,
        drift: this.random.between(-3, 3),
        liquidity: this.random.between(0.7, 1.35)
      });
      this.state.set(`${exchange.id}:ETHBTC`, {
        mid: 0.052 + index * 0.00003,
        drift: this.random.between(-0.00002, 0.00002),
        liquidity: this.random.between(8, 26)
      });
    });
  }

  maybeShock(exchanges) {
    if (this.shock && this.shock.until > this.tick) return;

    if (this.tick % 9 === 0) {
      const cheapIndex = Math.floor(this.random.between(0, exchanges.length));
      let richIndex = Math.floor(this.random.between(0, exchanges.length));
      if (richIndex === cheapIndex) richIndex = (richIndex + 1) % exchanges.length;

      this.shock = {
        cheap: exchanges[cheapIndex].id,
        rich: exchanges[richIndex].id,
        cheapBps: this.random.between(-18, -7),
        richBps: this.random.between(7, 20),
        until: this.tick + 4
      };
    }
  }

  generate(exchange, exchanges, anchorMid = null, symbol = exchange.primarySymbol || exchange.product) {
    this.tick += 1;
    this.maybeShock(exchanges);

    const marketKind = symbol.includes("ETH/BTC") ? "ETHBTC" : symbol.includes("ETH/") ? "ETHQUOTE" : "BTC";
    const stateKey = marketKind === "ETHBTC" ? `${exchange.id}:ETHBTC` : `${exchange.id}:BTC`;
    const state = this.state.get(stateKey);
    const randomWalk = this.random.between(-18, 18) + state.drift;
    const btcState = this.state.get(`${exchange.id}:BTC`);
    const ethBtcState = this.state.get(`${exchange.id}:ETHBTC`);
    const anchor = anchorMid && Number.isFinite(anchorMid) && marketKind !== "ETHQUOTE" ? anchorMid : state.mid;

    if (marketKind === "ETHBTC") {
      state.mid = Math.max(0.035, state.mid + this.random.between(-0.00008, 0.00008) + state.drift);
    } else {
      state.mid = anchor * 0.985 + (state.mid + randomWalk) * 0.015;
    }

    let mid = state.mid;
    if (marketKind === "ETHQUOTE") {
      const basisBps = this.random.between(-8, 8);
      mid = btcState.mid * ethBtcState.mid * (1 + basisBps / 10000);
    }

    if (this.shock?.cheap === exchange.id) mid *= 1 + this.shock.cheapBps / 10000;
    if (this.shock?.rich === exchange.id) mid *= 1 + this.shock.richBps / 10000;

    const spreadBps = marketKind === "ETHBTC" ? this.random.between(4, 12) : this.random.between(2.5, 8.5);
    const halfSpread = (mid * spreadBps) / 20000;
    const asks = [];
    const bids = [];

    for (let i = 0; i < 20; i += 1) {
      const levelGap = i * this.random.between(marketKind === "ETHBTC" ? 0.000004 : 2, marketKind === "ETHBTC" ? 0.000018 : 8);
      const askPrice = mid + halfSpread + levelGap;
      const bidPrice = mid - halfSpread - levelGap;
      const qty = marketKind === "ETHBTC" || marketKind === "ETHQUOTE"
        ? this.random.between(0.4, 8) * (ethBtcState.liquidity / 12) * (1 + i / 12)
        : this.random.between(0.012, 0.42) * state.liquidity * (1 + i / 12);

      const decimals = marketKind === "ETHBTC" ? 8 : 2;
      asks.push({ price: Number(askPrice.toFixed(decimals)), qty: Number(qty.toFixed(6)) });
      bids.push({ price: Number(bidPrice.toFixed(decimals)), qty: Number((qty * this.random.between(0.85, 1.18)).toFixed(6)) });
    }

    const latencyMs = Math.round(this.random.between(15, 95));
    return {
      key: `${exchange.id}:${symbol}`,
      exchangeId: exchange.id,
      exchangeName: exchange.name,
      symbol,
      product: symbol,
      primary: symbol === exchange.primarySymbol,
      source: "simulated",
      status: "simulated",
      feeBps: exchange.takerFeeBps,
      slippageBps: exchange.slippageBps,
      confidence: Math.max(0.5, exchange.confidence - 0.12),
      asks: sortLevels(asks, "ask"),
      bids: sortLevels(bids, "bid"),
      latencyMs,
      timestamp: Date.now(),
      error: null
    };
  }
}
