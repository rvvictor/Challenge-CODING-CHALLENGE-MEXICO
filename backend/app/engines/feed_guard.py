from __future__ import annotations

import math
import time

from backend.app.core.config import Settings
from backend.app.core.models import OrderBook

# Market-data sanitizer for the LIVE feed path. A real exchange (or a flaky
# proxy) can deliver poisoned books: NaN/zero prices, a garbled crossed book,
# or a fat-finger print that jumps the mid by double digits. Any of those
# reaching the engines would poison mids, P&L and the risk window. The guard
# rejects the update at the boundary, counts it per venue and reason, and
# rate-limits its own audit entries so a flapping feed cannot flood the ledger.
# Demo books are generated internally and bypass this path by design.

LOG_COOLDOWN_MS = 30_000
CROSSED_TOLERANCE = 1.02  # bid may exceed ask by at most 2% before it reads as garbage


def now_ms() -> int:
    return int(time.time() * 1000)


class FeedGuard:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_mid: dict[str, float] = {}
        self.rejected_count = 0
        self.by_reason: dict[str, int] = {}
        self.by_venue: dict[str, int] = {}
        self.last_rejection: dict | None = None
        self._last_logged: dict[tuple[str, str], int] = {}

    def inspect(self, book: OrderBook) -> str | None:
        """Returns a rejection reason, or None when the book is safe to use."""
        if not self.settings.feed_guard_enabled:
            return None
        for side in (book.asks, book.bids):
            for level in side:
                if not math.isfinite(level.price) or level.price <= 0:
                    return "non-finite or non-positive price"
                if not math.isfinite(level.qty) or level.qty < 0:
                    return "invalid quantity"
        if book.asks and book.bids:
            best_ask = min(level.price for level in book.asks)
            best_bid = max(level.price for level in book.bids)
            if best_bid > best_ask * CROSSED_TOLERANCE:
                return "crossed book beyond tolerance"
            mid = (best_ask + best_bid) / 2
            previous = self.last_mid.get(book.key)
            max_jump = max(0.5, float(self.settings.feed_max_jump_pct))
            if previous and abs(mid / previous - 1) * 100 > max_jump:
                return f"mid jumped more than {max_jump:g}% in one update"
            self.last_mid[book.key] = mid
        return None

    def record_rejection(self, book: OrderBook, reason: str) -> bool:
        """Counts the rejection; returns True when it should also be logged
        (rate-limited per venue+reason so a flapping feed cannot flood)."""
        self.rejected_count += 1
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
        self.by_venue[book.exchange_id] = self.by_venue.get(book.exchange_id, 0) + 1
        self.last_rejection = {
            "exchangeId": book.exchange_id,
            "symbol": book.symbol,
            "reason": reason,
            "at": now_ms(),
        }
        key = (book.exchange_id, reason)
        current = now_ms()
        if current - self._last_logged.get(key, 0) >= LOG_COOLDOWN_MS:
            self._last_logged[key] = current
            return True
        return False

    def reset(self) -> None:
        self.last_mid.clear()
        self.rejected_count = 0
        self.by_reason = {}
        self.by_venue = {}
        self.last_rejection = None
        self._last_logged = {}

    def snapshot(self) -> dict:
        return {
            "enabled": bool(self.settings.feed_guard_enabled),
            "maxJumpPct": self.settings.feed_max_jump_pct,
            "rejectedCount": self.rejected_count,
            "byReason": dict(self.by_reason),
            "byVenue": dict(self.by_venue),
            "lastRejection": self.last_rejection,
        }
