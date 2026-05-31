from __future__ import annotations

import site
import sys
from typing import Any

USER_SITE = site.getusersitepackages()
if USER_SITE not in sys.path:
    sys.path.append(USER_SITE)

try:
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover - fallback for minimal local test environments
    class _Field:
        def __init__(self, default=None, default_factory=None):
            self.default = default
            self.default_factory = default_factory

        def value(self):
            if self.default_factory:
                return self.default_factory()
            return self.default

    def Field(default=None, default_factory=None):
        return _Field(default=default, default_factory=default_factory)

    class BaseModel:
        def __init__(self, **data):
            annotations: dict[str, object] = {}
            for klass in reversed(self.__class__.mro()):
                annotations.update(getattr(klass, "__annotations__", {}))
            for name in annotations:
                if name in data:
                    value = data.pop(name)
                else:
                    sentinel = object()
                    default = sentinel
                    for klass in self.__class__.mro():
                        if name in klass.__dict__:
                            default = klass.__dict__[name]
                            break
                    if isinstance(default, _Field):
                        value = default.value()
                    elif default is not sentinel:
                        value = default
                    else:
                        raise TypeError(f"Missing required field: {name}")
                setattr(self, name, value)
            for name, value in data.items():
                setattr(self, name, value)

        def copy(self, update=None):
            payload = dict(self.__dict__)
            payload.update(update or {})
            return self.__class__(**payload)

        def model_copy(self, update=None):
            return self.copy(update=update)


class AurelionModel(BaseModel):
    class Config:
        arbitrary_types_allowed = True

    def clone_with(self, **updates):
        copier = getattr(self, "model_copy", None)
        if copier:
            return copier(update=updates)
        return self.copy(update=updates)


class Level(AurelionModel):
    price: float
    qty: float

    def __init__(self, *args, **data):
        if args:
            if len(args) != 2 or data:
                raise TypeError("Level accepts price and qty")
            data = {"price": args[0], "qty": args[1]}
        super().__init__(**data)


class OrderBook(AurelionModel):
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


class Opportunity(AurelionModel):
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
    costs: dict[str, float] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
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
