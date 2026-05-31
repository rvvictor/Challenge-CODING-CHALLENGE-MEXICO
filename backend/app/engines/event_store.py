from __future__ import annotations


class EventStore:
    def __init__(self, opportunities_limit: int = 5000, trades_limit: int = 1000, pnl_limit: int = 1500, event_limit: int = 500, persistence=None):
        self.opportunities_limit = opportunities_limit
        self.trades_limit = trades_limit
        self.pnl_limit = pnl_limit
        self.event_limit = event_limit
        self.persistence = persistence
        self.reset()

    def reset(self) -> None:
        self.opportunities: list[dict] = []
        self.trades: list[dict] = []
        self.pnl_series: list[dict] = []
        self.events: list[dict] = []
        self.detected_count = 0
        self.rejected_count = 0
        self.executed_count = 0
        self.simple_count = 0
        self.triangular_count = 0
        self.profitable_count = 0
        self.blocked_count = 0
        self.partial_count = 0
        self.executed_simple_count = 0
        self.executed_triangular_count = 0
        if self.persistence:
            self.persistence.append("session-reset", {"reason": "runtime reset"})

    def add_opportunities(self, opportunities: list[dict]) -> None:
        for opportunity in opportunities:
            self.detected_count += 1
            if opportunity.get("strategy") == "triangular":
                self.triangular_count += 1
            else:
                self.simple_count += 1
            if opportunity.get("status") == "profitable":
                self.profitable_count += 1
            if opportunity.get("status") == "blocked":
                self.blocked_count += 1
            if opportunity.get("status") != "profitable":
                self.rejected_count += 1
            self.opportunities.insert(0, opportunity)
        self.opportunities = self.opportunities[: self.opportunities_limit]
        if self.persistence:
            self.persistence.append_many("opportunity", opportunities)

    def add_trade(self, trade: dict, cumulative_pnl: float) -> None:
        self.executed_count += 1
        if trade.get("partial"):
            self.partial_count += 1
        if trade.get("strategy") == "triangular":
            self.executed_triangular_count += 1
        else:
            self.executed_simple_count += 1
        self.trades.insert(0, trade)
        self.trades = self.trades[: self.trades_limit]
        self.pnl_series.append({"time": trade["time"], "pnl": cumulative_pnl})
        self.pnl_series = self.pnl_series[-self.pnl_limit :]
        if self.persistence:
            self.persistence.append("trade", {"trade": trade, "cumulativePnl": cumulative_pnl})

    def add_event(self, event: dict) -> None:
        self.events.insert(0, event)
        self.events = self.events[: self.event_limit]
        if self.persistence:
            self.persistence.append("event", event)

    def latest_opportunities(self, limit: int = 90) -> list[dict]:
        return self.opportunities[:limit]

    def latest_trades(self, limit: int = 90) -> list[dict]:
        return self.trades[:limit]

    def latest_events(self, limit: int = 80) -> list[dict]:
        return self.events[:limit]
