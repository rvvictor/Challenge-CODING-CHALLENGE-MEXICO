from __future__ import annotations

import json
import os
import time

from backend.app.core.config import Settings

SYSTEM_PROMPT = (
    "You are Aurelion's trading co-pilot. You ONLY explain the system's current "
    "decisions in plain, concise language for a non-expert evaluator. You never give "
    "financial advice, never predict prices, and never decide trades — the engine "
    "decides; you describe. Answer in 2-4 short sentences, grounded only in the "
    "facts provided. Do not invent numbers."
)


def now_ms() -> int:
    return int(time.time() * 1000)


class DecisionNarrator:
    """Advisory, explanation-only co-pilot.

    Reads a dashboard snapshot and returns a plain-language read on why the top
    opportunity is taken or skipped. It is strictly out of the execution path. When
    ANTHROPIC_API_KEY is set it uses Claude; otherwise (or on any error) it falls
    back to a deterministic template built from the same facts, so the dashboard
    works offline and during evaluation without a key. Results are briefly cached
    to rate-limit calls.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.model = os.getenv("NARRATOR_MODEL", "claude-haiku-4-5-20251001")
        self._client = None
        self._cache_key = None
        self._cache_text = ""
        self._cache_source = ""
        self._cache_at = 0
        self._cache_ttl_ms = 8000
        self.last_error = ""

    def available(self) -> bool:
        return bool(self.api_key)

    def _build_context(self, snapshot: dict) -> dict:
        risk = snapshot.get("risk", {})
        metrics = snapshot.get("metrics", {})
        scenarios = (snapshot.get("scenarios") or {}).get("active", [])
        queued = snapshot.get("queuedOpportunities") or snapshot.get("opportunities") or []
        top = queued[0] if queued else None
        decision = None
        if top:
            if top.get("strategy") == "triangular":
                route = " -> ".join(top.get("cyclePath") or [])
            else:
                route = f"{top.get('buyExchange')} -> {top.get('sellExchange')}"
            decision = {
                "route": route,
                "strategy": top.get("strategy"),
                "status": top.get("status"),
                "netBps": top.get("netBps"),
                "evBps": top.get("evBps"),
                "confidence": top.get("confidence"),
                "reason": top.get("reason"),
            }
        return {
            "mode": snapshot.get("mode"),
            "paused": risk.get("paused"),
            "riskReason": risk.get("reason"),
            "models": snapshot.get("models", {}),
            "scenarios": scenarios,
            "realizedPnl": metrics.get("cumulativePnl"),
            "executed": metrics.get("executedCount"),
            "detected": metrics.get("detectedCount"),
            "bestNetBps": metrics.get("bestNetBps"),
            "autonomy": (snapshot.get("inventoryAutonomy") or {}).get("sessionAutonomy"),
            "decision": decision,
        }

    def _context_key(self, ctx: dict) -> str:
        decision = ctx.get("decision") or {}
        return "|".join(str(part) for part in (
            ctx.get("paused"),
            decision.get("route"),
            decision.get("status"),
            round(decision.get("netBps") or 0, 1),
            ",".join(ctx.get("scenarios") or []),
        ))

    def _fallback(self, ctx: dict) -> str:
        parts: list[str] = []
        if ctx.get("paused"):
            parts.append(
                f"Execution is paused by the circuit breaker ({ctx.get('riskReason')}); the bot keeps "
                "watching the market but opens no new trades until risk clears."
            )
        decision = ctx.get("decision")
        if decision:
            confidence_pct = round((decision.get("confidence") or 0) * 100)
            if decision.get("status") == "profitable":
                parts.append(
                    f"The top route {decision['route']} clears the gates with a net edge of "
                    f"{decision.get('netBps')} bps after fees, slippage and latency, at {confidence_pct}% confidence."
                )
            elif decision.get("status") == "blocked":
                parts.append(f"The top candidate {decision['route']} is blocked: {decision.get('reason')}.")
            else:
                parts.append(
                    f"The top candidate {decision['route']} is skipped because costs or risk removed the edge "
                    f"({decision.get('netBps')} bps net): {decision.get('reason')}."
                )
        else:
            parts.append("No actionable opportunity right now — visible spreads do not survive costs.")
        if ctx.get("scenarios"):
            parts.append(f"Active stress scenarios: {', '.join(ctx['scenarios'])}.")
        models = ctx.get("models", {})
        parts.append(
            f"Models in use: {models.get('cycleAlgo')} cycle detection, {models.get('slippageModel')} slippage, "
            f"{models.get('sizingMode')} sizing."
        )
        return " ".join(part for part in parts if part)

    def _call_llm(self, ctx: dict) -> str:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        prompt = (
            "Explain Aurelion's current state to a non-expert hackathon judge. Focus on WHY the top "
            "opportunity is taken or skipped, and mention the circuit breaker or active stress scenarios "
            "if relevant. Facts:\n\n" + json.dumps(ctx, default=str)
        )
        message = self._client.messages.create(
            model=self.model,
            max_tokens=320,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        return text.strip()

    def _store(self, key: str, text: str, when: int, source: str) -> None:
        self._cache_key = key
        self._cache_text = text
        self._cache_source = source
        self._cache_at = when

    def narrate(self, snapshot: dict) -> dict:
        ctx = self._build_context(snapshot)
        key = self._context_key(ctx)
        current = now_ms()
        if key == self._cache_key and self._cache_text and current - self._cache_at < self._cache_ttl_ms:
            return {"source": self._cache_source, "text": self._cache_text, "cached": True, "model": self.model if self.available() else None}
        if not self.available():
            text = self._fallback(ctx)
            self._store(key, text, current, "deterministic")
            return {"source": "deterministic", "text": text, "cached": False, "model": None}
        try:
            text = self._call_llm(ctx)
            if not text:
                raise ValueError("empty narration")
            self._store(key, text, current, "claude")
            return {"source": "claude", "text": text, "cached": False, "model": self.model}
        except Exception as exc:  # pragma: no cover - network/optional dependency path
            self.last_error = str(exc)
            text = self._fallback(ctx)
            self._store(key, text, current, "deterministic-fallback")
            return {"source": "deterministic-fallback", "text": text, "cached": False, "model": None, "error": self.last_error}
