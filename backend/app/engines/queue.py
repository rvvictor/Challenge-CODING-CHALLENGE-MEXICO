from __future__ import annotations


def route_key(opportunity: dict) -> str:
    if opportunity.get("strategy") == "triangular":
        return f"triangular:{opportunity.get('exchangeId')}:{opportunity.get('cycleId')}"
    pair = sorted([opportunity.get("buyExchangeId"), opportunity.get("sellExchangeId")])
    return f"simple:{opportunity.get('product')}:{pair[0]}<>{pair[1]}"


def queue_value(opportunity: dict) -> tuple[int, float, float]:
    status = opportunity.get("status")
    status_rank = {"profitable": 3, "rejected": 2, "blocked": 1}.get(status, 0)
    liquidity = opportunity.get("filledRatio", 0) or 0
    return (status_rank, opportunity.get("score", 0), liquidity)


class OpportunityQueue:
    def __init__(self, max_size: int = 140):
        self.max_size = max_size
        self.last_stats = {"received": 0, "deduped": 0, "queued": 0, "executable": 0}

    def rank(self, opportunities: list[dict]) -> list[dict]:
        by_key: dict[str, dict] = {}
        for opportunity in opportunities:
            key = route_key(opportunity)
            existing = by_key.get(key)
            if existing is None or queue_value(opportunity) > queue_value(existing):
                candidate = dict(opportunity)
                candidate["dedupeKey"] = key
                by_key[key] = candidate
        queued = sorted(by_key.values(), key=queue_value, reverse=True)[: self.max_size]
        self.last_stats = {
            "received": len(opportunities),
            "deduped": len(opportunities) - len(queued),
            "queued": len(queued),
            "executable": sum(1 for item in queued if item.get("status") == "profitable"),
            "blocked": sum(1 for item in queued if item.get("status") == "blocked"),
            "nearMiss": sum(1 for item in queued if item.get("status") == "rejected" and item.get("netBps", 0) > -10),
        }
        return queued

    def pause(self, reason: str) -> None:
        self.last_stats = {
            "received": 0,
            "deduped": 0,
            "queued": 0,
            "executable": 0,
            "blocked": 0,
            "nearMiss": 0,
            "paused": True,
            "reason": reason,
        }

    def snapshot(self) -> dict:
        return dict(self.last_stats)
