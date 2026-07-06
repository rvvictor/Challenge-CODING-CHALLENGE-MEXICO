from __future__ import annotations

import time

from backend.app.core.config import Settings

# Observation & research recorder — the committee's stated #1 real-world step:
# "usarlo en modo de observación, recolectando datos reales durante varios días
# ... qué pares generan señales repetibles, cuánto duran las oportunidades, qué
# porcentaje desaparece antes de poder ejecutarse."
#
# This ingests the LIVE opportunity scan every tick (real books, real costs — not
# tickers like the wide-net radar) and accumulates per-route statistics:
# frequency, capturable-after-fees rate, average/best net edge, and how long a
# profitable episode persists (in consecutive samples). It records only in live
# modes; demo is excluded so the demo path is untouched. Read-only and off the
# hot decision path (it consumes the already-ranked opportunities).


def now_ms() -> int:
    return int(time.time() * 1000)


class ObservationRecorder:
    MAX_ROUTES = 400
    TOP_N = 12

    def __init__(self, settings: Settings):
        self.settings = settings
        self.reset()

    def reset(self) -> None:
        self.routes: dict[str, dict] = {}
        self.samples = 0
        self.started_at = now_ms()
        self.recording = False

    @staticmethod
    def _route_id(opportunity: dict) -> tuple[str, str]:
        if opportunity.get("strategy") == "triangular":
            path = " -> ".join(opportunity.get("cyclePath") or [])
            return (f"tri:{opportunity.get('exchange')}:{path}", "triangular")
        base = opportunity.get("baseAsset", "BTC")
        return (f"x:{base}:{opportunity.get('buyExchange')}>{opportunity.get('sellExchange')}", "cross")

    def observe(self, ranked: list[dict], mode: str, degraded: bool) -> None:
        """Record one sample of the live scan. No-op in demo/degraded so demo and
        offline runs never accumulate synthetic observation data."""
        if mode == "demo" or degraded:
            self.recording = False
            return
        self.recording = True
        self.samples += 1
        current = now_ms()
        seen_now: set[str] = set()
        for opportunity in ranked:
            status = opportunity.get("status")
            if status == "blocked":
                continue
            net = opportunity.get("netBps")
            if net is None:
                continue
            route_id, kind = self._route_id(opportunity)
            seen_now.add(route_id)
            stats = self.routes.get(route_id)
            if stats is None:
                if len(self.routes) >= self.MAX_ROUTES:
                    continue
                stats = {
                    "id": route_id, "kind": kind, "base": opportunity.get("baseAsset", ""),
                    "route": self._label(opportunity), "seen": 0, "capturable": 0,
                    "sumNetBps": 0.0, "bestNetBps": net, "streak": 0, "maxStreak": 0,
                    "lastSeenSample": 0, "firstSeen": current,
                }
                self.routes[route_id] = stats
            stats["seen"] += 1
            stats["sumNetBps"] += net
            stats["bestNetBps"] = max(stats["bestNetBps"], net)
            stats["lastSeen"] = current
            capturable = status == "profitable"
            if capturable:
                stats["capturable"] += 1
                # A profitable *episode* is consecutive samples clearing the fee
                # wall; the streak measures how long the opportunity persists.
                if stats["lastSeenSample"] == self.samples - 1:
                    stats["streak"] += 1
                else:
                    stats["streak"] = 1
                stats["maxStreak"] = max(stats["maxStreak"], stats["streak"])
            else:
                stats["streak"] = 0
            stats["lastSeenSample"] = self.samples

    def _label(self, opportunity: dict) -> str:
        if opportunity.get("strategy") == "triangular":
            return f"{opportunity.get('exchange')} {' -> '.join(opportunity.get('cyclePath') or [])}"
        return f"{opportunity.get('buyExchange')} -> {opportunity.get('sellExchange')} ({opportunity.get('baseAsset', 'BTC')})"

    def snapshot(self) -> dict:
        duration_ms = max(1, now_ms() - self.started_at)
        hours = duration_ms / 3_600_000
        rows = []
        for stats in self.routes.values():
            seen = stats["seen"]
            rows.append({
                "id": stats["id"],
                "kind": stats["kind"],
                "base": stats["base"],
                "route": stats["route"],
                "seen": seen,
                "frequencyPerHour": round(seen / hours, 1) if hours else 0.0,
                "capturable": stats["capturable"],
                "capturableRate": round(stats["capturable"] / seen, 3) if seen else 0.0,
                "avgNetBps": round(stats["sumNetBps"] / seen, 2) if seen else 0.0,
                "bestNetBps": round(stats["bestNetBps"], 2),
                "maxEpisodeSamples": stats["maxStreak"],
            })
        # Rank by capturable count then frequency: the routes that most often
        # produce a real, after-fees edge come first.
        rows.sort(key=lambda row: (row["capturable"], row["seen"]), reverse=True)
        capturable_routes = sum(1 for row in rows if row["capturable"] > 0)
        return {
            "recording": self.recording,
            "samples": self.samples,
            "durationMs": duration_ms,
            "routesObserved": len(rows),
            "capturableRoutes": capturable_routes,
            "topRoutes": rows[: self.TOP_N],
            "note": (
                "Live observation on real books and real costs (not tickers). Per route: how often "
                "it appears, what fraction clears the fee wall, its average/best net edge, and the "
                "longest run of consecutive samples it stayed profitable. Records only in live modes."
            ),
        }
