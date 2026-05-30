from __future__ import annotations

from backend.app.core.config import Settings


class WalletLedger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.reset()

    def reset(self) -> None:
        self.balances: dict[str, dict[str, float | str]] = {}
        for exchange in self.settings.exchanges:
            self._add_wallet(exchange)
        self.realized_pnl = 0.0

    def _add_wallet(self, exchange) -> None:
        self.balances[exchange.id] = {
            "exchangeId": exchange.id,
            "exchangeName": exchange.name,
            "USDT": self.settings.starting_usdt,
            "BTC": self.settings.starting_btc,
            "ETH": self.settings.starting_eth,
        }

    def sync_exchanges(self, exchanges) -> None:
        for exchange in exchanges:
            if exchange.id not in self.balances:
                self._add_wallet(exchange)
            else:
                self.balances[exchange.id]["exchangeName"] = exchange.name

    def get(self, exchange_id: str) -> dict[str, float | str]:
        return self.balances[exchange_id]

    def all(self) -> list[dict[str, float | str]]:
        return [dict(wallet) for wallet in self.balances.values()]

    def active(self, exchanges) -> list[dict[str, float | str]]:
        self.sync_exchanges(exchanges)
        return [dict(self.balances[exchange.id]) for exchange in exchanges]

    def apply_trade(self, trade: dict) -> None:
        if trade["strategy"] == "triangular":
            wallet = self.get(trade["exchangeId"])
            wallet["USDT"] = float(wallet["USDT"]) + trade["netProfit"]
            self.realized_pnl += trade["netProfit"]
            return

        buy = self.get(trade["buyExchangeId"])
        sell = self.get(trade["sellExchangeId"])
        buy_debit = trade["buyQuote"] + trade["buyFee"] + trade["slippageCostBuy"] + trade["rebalanceCost"]
        sell_credit = trade["sellQuote"] - trade["sellFee"] - trade["slippageCostSell"] - trade["latencyRiskCost"]
        buy["USDT"] = float(buy["USDT"]) - buy_debit
        buy["BTC"] = float(buy["BTC"]) + trade["qtyBtc"]
        sell["BTC"] = float(sell["BTC"]) - trade["qtyBtc"]
        sell["USDT"] = float(sell["USDT"]) + sell_credit
        self.realized_pnl += trade["netProfit"]

    def totals(self, mark_price: float, exchanges=None) -> dict[str, float]:
        wallets = self.active(exchanges) if exchanges is not None else self.all()
        usdt = sum(float(wallet["USDT"]) for wallet in wallets)
        btc = sum(float(wallet["BTC"]) for wallet in wallets)
        eth = sum(float(wallet["ETH"]) for wallet in wallets)
        return {
            "USDT": usdt,
            "BTC": btc,
            "ETH": eth,
            "markToMarket": usdt + btc * mark_price,
            "realizedPnl": self.realized_pnl,
        }
