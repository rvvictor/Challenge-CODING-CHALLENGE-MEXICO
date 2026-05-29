export class WalletLedger {
  constructor(config) {
    this.config = config;
    this.reset();
  }

  reset() {
    this.balances = {};
    for (const exchange of this.config.exchanges) {
      this.balances[exchange.id] = {
        exchangeId: exchange.id,
        exchangeName: exchange.name,
        BTC: this.config.wallets.startingBase,
        ETH: this.config.wallets.startingEth,
        USDT: this.config.wallets.startingQuote
      };
    }
    this.realizedPnl = 0;
  }

  get(exchangeId) {
    return this.balances[exchangeId];
  }

  all() {
    return Object.values(this.balances).map((wallet) => ({ ...wallet }));
  }

  applyTrade(trade) {
    if (trade.strategy === "triangular") {
      const wallet = this.get(trade.exchangeId);
      if (!wallet) return;
      wallet.USDT += trade.netProfit;
      this.realizedPnl += trade.netProfit;
      return;
    }

    const buyWallet = this.get(trade.buyExchangeId);
    const sellWallet = this.get(trade.sellExchangeId);
    if (!buyWallet || !sellWallet) return;

    const buyDebit = trade.buyQuote + trade.buyFee + trade.slippageCostBuy + trade.rebalanceCost;
    const sellCredit = trade.sellQuote - trade.sellFee - trade.slippageCostSell - trade.latencyRiskCost;

    buyWallet.USDT -= buyDebit;
    buyWallet.BTC += trade.qtyBtc;
    sellWallet.BTC -= trade.qtyBtc;
    sellWallet.USDT += sellCredit;
    this.realizedPnl += trade.netProfit;
  }

  totals(markPrice = 0) {
    const wallets = this.all();
    const quote = wallets.reduce((sum, wallet) => sum + wallet.USDT, 0);
    const base = wallets.reduce((sum, wallet) => sum + wallet.BTC, 0);
    const eth = wallets.reduce((sum, wallet) => sum + (wallet.ETH || 0), 0);
    return {
      USDT: quote,
      BTC: base,
      ETH: eth,
      markToMarket: quote + base * markPrice,
      realizedPnl: this.realizedPnl
    };
  }
}
