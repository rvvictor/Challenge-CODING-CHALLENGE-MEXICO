from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Level:
    price: float
    qty: float


@dataclass
class OrderBook:
    key: str
    exchange_id: str
    exchange_name: str
    symbol: str
    primary: bool
    source: str
    status: str
    fee_bps: float
    slippage_bps: float
    confidence: float
    asks: list[Level]
    bids: list[Level]
    latency_ms: float
    timestamp: int
    error: str | None = None


@dataclass
class Opportunity:
    id: str
    strategy: str
    time: int
    score: float
    status: str
    net_profit: float
    net_bps: float
    gross_profit: float
    gross_bps: float
    confidence: float
    partial: bool
    source: str
    reason: str
    product: str
    costs: dict[str, float] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "strategy": self.strategy,
            "time": self.time,
            "score": self.score,
            "status": self.status,
            "netProfit": self.net_profit,
            "netBps": self.net_bps,
            "grossProfit": self.gross_profit,
            "grossBps": self.gross_bps,
            "confidence": self.confidence,
            "partial": self.partial,
            "source": self.source,
            "reason": self.reason,
            "product": self.product,
            "costs": self.costs,
            "dedupeKey": self.dedupe_key,
        }
        payload.update(self.data)
        return payload
