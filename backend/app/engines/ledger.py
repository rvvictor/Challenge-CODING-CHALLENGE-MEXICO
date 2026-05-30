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

    def route_capacity_btc(self, buy_exchange_id: str, sell_exchange_id: str, ask_price: float, exchanges) -> dict[str, float | str]:
        buy = self.get(buy_exchange_id)
        sell = self.get(sell_exchange_id)
        local_qty = min(float(sell["BTC"]), (float(buy["USDT"]) * 0.985) / ask_price)
        if not self.settings.inventory_rebalance_enabled:
            return {"qty": local_qty, "mode": "local"}

        active_ids = {exchange.id for exchange in exchanges}
        active_wallets = [wallet for exchange_id, wallet in self.balances.items() if exchange_id in active_ids]
        reserve_btc = self.settings.min_trade_btc * max(1, len(active_wallets))
        total_btc = max(0, sum(float(wallet["BTC"]) for wallet in active_wallets) - reserve_btc)
        total_usdt = sum(float(wallet["USDT"]) for wallet in active_wallets) * 0.985 / ask_price
        pooled_qty = min(total_btc, total_usdt)
        if pooled_qty > local_qty:
            return {"qty": pooled_qty, "mode": "rebalanced"}
        return {"qty": local_qty, "mode": "local"}

    def prepare_inventory_for_trade(self, trade: dict) -> list[dict]:
        if not self.settings.inventory_rebalance_enabled or trade["strategy"] == "triangular":
            return []

        transfers: list[dict] = []
        buy = self.get(trade["buyExchangeId"])
        sell = self.get(trade["sellExchangeId"])
        buy_debit = trade["buyQuote"] + trade["buyFee"] + trade["slippageCostBuy"] + trade["rebalanceCost"]
        if self._available_asset("BTC", trade["sellExchangeId"]) < trade["qtyBtc"]:
            return []
        if self._available_asset("USDT", trade["buyExchangeId"]) < buy_debit:
            return []
        self._rebalance_asset("BTC", trade["sellExchangeId"], trade["qtyBtc"], transfers)
        self._rebalance_asset("USDT", trade["buyExchangeId"], buy_debit, transfers)
        buy["exchangeName"] = trade["buyExchange"]
        sell["exchangeName"] = trade["sellExchange"]
        return transfers

    def _available_asset(self, asset: str, target_exchange_id: str) -> float:
        floor = self.settings.min_trade_btc if asset == "BTC" else self.settings.min_trade_btc * 70000
        target = self.get(target_exchange_id)
        donor_capacity = sum(
            max(0, float(wallet[asset]) - floor)
            for exchange_id, wallet in self.balances.items()
            if exchange_id != target_exchange_id
        )
        return float(target[asset]) + donor_capacity

    def _rebalance_asset(self, asset: str, target_exchange_id: str, required: float, transfers: list[dict]) -> None:
        target = self.get(target_exchange_id)
        current = float(target[asset])
        if current >= required:
            return

        needed = (required - current) * (1 + self.settings.inventory_rebalance_buffer)
        floor = self.settings.min_trade_btc if asset == "BTC" else self.settings.min_trade_btc * 70000
        donors = sorted(
            (
                (exchange_id, wallet)
                for exchange_id, wallet in self.balances.items()
                if exchange_id != target_exchange_id and float(wallet[asset]) > floor
            ),
            key=lambda item: float(item[1][asset]),
            reverse=True,
        )
        for source_id, source in donors:
            available = max(0, float(source[asset]) - floor)
            moved = min(available, needed)
            if moved <= 0:
                continue
            source[asset] = float(source[asset]) - moved
            target[asset] = float(target[asset]) + moved
            needed -= moved
            transfers.append({
                "asset": asset,
                "from": source_id,
                "to": target_exchange_id,
                "amount": round(moved, 8 if asset == "BTC" else 4),
            })
            if needed <= 0:
                break

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
