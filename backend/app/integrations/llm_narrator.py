from __future__ import annotations

import asyncio
import json
import os
import random
import time

from backend.app.core.config import Settings

SYSTEM_PROMPT = (
    "You are Aurelion's trading co-pilot. You ONLY explain the system's current "
    "decisions in plain, concise language for a non-expert evaluator, and answer "
    "their questions about the live session. You never give financial advice, never "
    "predict prices, and never decide trades — the engine decides; you describe. "
    "Keep answers to 2-4 short sentences, grounded only in the facts provided. Do "
    "not invent numbers. Vary your phrasing naturally between updates so consecutive "
    "explanations do not sound identical."
)

ALLOWED_MODELS = (
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-8",
)


def now_ms() -> int:
    return int(time.time() * 1000)


class DecisionNarrator:
    """Advisory, explanation-only co-pilot.

    Reads a dashboard snapshot and explains why the top opportunity is taken or
    skipped, and answers free-text questions about the live session. Strictly out
    of the execution path. Uses Claude (streaming) when ANTHROPIC_API_KEY is set;
    otherwise a deterministic, change-aware template built from the same facts, so
    the dashboard works offline and key-free during evaluation.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.model = os.getenv("NARRATOR_MODEL", "claude-haiku-4-5-20251001")
        self.allowed_models = ALLOWED_MODELS
        self._client = None
        self._async_client = None
        self._cache_key = None
        self._cache_text = ""
        self._cache_source = ""
        self._cache_at = 0
        self._cache_ttl_ms = 8000
        self._prev_decision: dict | None = None
        self._variety = random.Random()
        self.last_error = ""

    def _pick(self, variants: list[str]) -> str:
        return variants[self._variety.randrange(len(variants))]

    def available(self) -> bool:
        return bool(self.api_key)

    def _resolve_model(self, model: str | None) -> str:
        return model if model in ALLOWED_MODELS else self.model

    def _trade_route(self, trade: dict) -> str:
        if trade.get("strategy") == "triangular":
            return " -> ".join(trade.get("cyclePath") or [])
        return f"{trade.get('buyExchange')} -> {trade.get('sellExchange')}"

    def _focus_trade(self, snapshot: dict, trade_id: str | None) -> dict | None:
        if not trade_id:
            return None
        for trade in snapshot.get("trades") or []:
            if trade.get("id") == trade_id:
                reconciliation = trade.get("reconciliation") or {}
                return {
                    "id": trade.get("id"),
                    "route": self._trade_route(trade),
                    "strategy": trade.get("strategy"),
                    "status": trade.get("status"),
                    "partial": trade.get("partial"),
                    "filledRatio": trade.get("filledRatio"),
                    "netProfit": trade.get("netProfit"),
                    "netBps": trade.get("netBps"),
                    "edgeCaptureBps": (trade.get("executionQuality") or {}).get("edgeCaptureBps"),
                    "adverseMoveCost": (trade.get("executionQuality") or {}).get("adverseMoveCost"),
                    "legExposureBtc": reconciliation.get("netExposureBtc"),
                    "coverCost": reconciliation.get("coverCost") or trade.get("coverCost"),
                }
        return None

    def _build_context(self, snapshot: dict, trade_id: str | None = None) -> dict:
        risk = snapshot.get("risk", {})
        metrics = snapshot.get("metrics", {})
        scenarios = (snapshot.get("scenarios") or {}).get("active", [])
        queued = snapshot.get("queuedOpportunities") or snapshot.get("opportunities") or []
        top = queued[0] if queued else None
        decision = None
        if top:
            decision = {
                "route": self._trade_route(top),
                "strategy": top.get("strategy"),
                "status": top.get("status"),
                "netBps": top.get("netBps"),
                "evBps": top.get("evBps"),
                "confidence": top.get("confidence"),
                "reason": top.get("reason"),
            }
        last_sweep = (snapshot.get("discovery") or {}).get("lastSweep") or {}
        radar_top = (last_sweep.get("topRoutes") or [None])[0]
        radar = None
        if radar_top:
            radar = {
                "venuesLive": last_sweep.get("venuesLive"),
                "bestRoute": radar_top.get("route"),
                "bestNetBps": radar_top.get("netBps"),
                "promotableCount": sum(1 for route in last_sweep.get("topRoutes") or [] if route.get("promotable")),
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
            "radar": radar,
            "decision": decision,
            "focusTrade": self._focus_trade(snapshot, trade_id),
        }

    def _context_key(self, ctx: dict) -> str:
        decision = ctx.get("decision") or {}
        focus = ctx.get("focusTrade") or {}
        return "|".join(str(part) for part in (
            ctx.get("paused"),
            decision.get("route"),
            decision.get("status"),
            round(decision.get("netBps") or 0, 1),
            ",".join(ctx.get("scenarios") or []),
            focus.get("id"),
        ))

    def _change_note(self, decision: dict | None) -> str:
        """Narrate the delta versus the previously narrated decision."""
        previous = self._prev_decision
        if not decision or not previous:
            return ""
        if previous.get("route") != decision.get("route"):
            return f"Focus shifted to {decision['route']} from {previous.get('route')}."
        before = previous.get("netBps")
        after = decision.get("netBps")
        if before is None or after is None or abs(after - before) < 0.05:
            return ""
        direction = "improved" if after > before else "weakened"
        return f"Its net edge {direction} from {before} to {after} bps since the last read."

    def _fallback_focus_trade(self, focus: dict) -> str:
        parts: list[str] = [f"Trade {focus['route']} ({focus.get('strategy')}) is recorded as {focus.get('status')}."]
        net = focus.get("netProfit")
        if net is not None:
            parts.append(f"Net P&L: {net:+.4f} after all modeled costs.")
        if focus.get("partial"):
            ratio = round((focus.get("filledRatio") or 0) * 100)
            parts.append(f"It only filled {ratio}% of the target size.")
        capture = focus.get("edgeCaptureBps")
        if capture is not None:
            parts.append(f"It captured {capture} bps of edge after the adverse price move during execution.")
        exposure = focus.get("legExposureBtc")
        if exposure:
            parts.append(
                f"One leg under-filled, leaving {exposure} BTC of open exposure; the bot covered the residual "
                f"at a worse price, costing {focus.get('coverCost')} — that correction is included in the net P&L above."
            )
        return " ".join(parts)

    def _fallback(self, ctx: dict) -> str:
        # Phrasing rotates between grounded variants so consecutive template
        # narrations don't read identically; every variant carries the same facts.
        if ctx.get("focusTrade"):
            return self._fallback_focus_trade(ctx["focusTrade"])
        parts: list[str] = []
        if ctx.get("paused"):
            reason = ctx.get("riskReason")
            parts.append(self._pick([
                f"Execution is paused by the circuit breaker ({reason}); the bot keeps watching the market but opens no new trades until risk clears.",
                f"The circuit breaker has execution on hold ({reason}). Monitoring continues, and trading re-arms automatically once conditions clear.",
                f"Trading is paused — the circuit breaker tripped ({reason}). Every book is still being watched while the engine waits it out.",
            ]))
        decision = ctx.get("decision")
        if decision:
            route = decision["route"]
            net = decision.get("netBps")
            reason = decision.get("reason")
            confidence_pct = round((decision.get("confidence") or 0) * 100)
            if decision.get("status") == "profitable":
                parts.append(self._pick([
                    f"The top route {route} clears the gates with a net edge of {net} bps after fees, slippage and latency, at {confidence_pct}% confidence.",
                    f"Best opportunity right now: {route}. After fees, slippage and latency it keeps {net} bps of net edge at {confidence_pct}% confidence, so the engine will take it.",
                    f"{route} leads the queue — {net} bps of edge survives all modeled costs at {confidence_pct}% confidence.",
                ]))
            elif decision.get("status") == "blocked":
                parts.append(self._pick([
                    f"The top candidate {route} is blocked: {reason}.",
                    f"{route} can't run right now — blocked by {reason}.",
                ]))
            else:
                parts.append(self._pick([
                    f"The top candidate {route} is skipped because costs or risk removed the edge ({net} bps net): {reason}.",
                    f"The engine is passing on {route}: once real costs are charged the edge is {net} bps net ({reason}).",
                    f"{route} looked interesting but doesn't survive the math — {net} bps net after costs, so it stays rejected ({reason}).",
                ]))
            change = self._change_note(decision)
            if change:
                parts.append(change)
        else:
            parts.append(self._pick([
                "No actionable opportunity right now — visible spreads do not survive costs.",
                "Nothing actionable at the moment: every visible spread dies once real costs are applied.",
                "The scanners are running, but no route currently beats its own costs — standing by.",
            ]))
        if ctx.get("scenarios"):
            names = ", ".join(ctx["scenarios"])
            parts.append(self._pick([
                f"Active stress scenarios: {names}.",
                f"Stress injected right now ({names}) — watch the defenses react.",
            ]))
        # Occasional grounded color so repeated updates feel alive, never invented.
        pnl = ctx.get("realizedPnl")
        autonomy = ctx.get("autonomy")
        if pnl and self._variety.random() < 0.35:
            parts.append(f"Session P&L stands at {round(float(pnl), 2)}.")
        elif autonomy and self._variety.random() < 0.3:
            parts.append(f"Inventory can still fund roughly {autonomy} trades before a rebalance matters.")
        radar = ctx.get("radar")
        if radar and radar.get("bestRoute") and self._variety.random() < 0.3:
            parts.append(self._pick([
                f"On the wide net, the best find across {radar.get('venuesLive')} venues is {radar['bestRoute']} at {radar['bestNetBps']} bps net.",
                f"The discovery radar is sweeping {radar.get('venuesLive')} venues in the background; {radar['bestRoute']} currently leads at {radar['bestNetBps']} bps net.",
            ]))
        models = ctx.get("models", {})
        parts.append(self._pick([
            f"Models in use: {models.get('cycleAlgo')} cycle detection, {models.get('slippageModel')} slippage, {models.get('sizingMode')} sizing.",
            f"Strategy stack: {models.get('cycleAlgo')} cycles, {models.get('slippageModel')} slippage, {models.get('sizingMode')} sizing.",
        ]))
        return " ".join(part for part in parts if part)

    def _answer_or_fallback(self, ctx: dict, question: str | None) -> str:
        text = self._fallback(ctx)
        if question:
            text = f"{text} (Connect an Anthropic API key for detailed question-and-answer.)"
        return text

    def _prompt(self, ctx: dict, question: str | None) -> str:
        facts = json.dumps(ctx, default=str)
        if ctx.get("focusTrade"):
            base = (
                "Explain this specific executed trade (ctx.focusTrade) to a non-expert evaluator: what "
                "happened, whether it filled cleanly or had a problem (partial fill / leg failure / "
                "adverse price move), and how that affected its P&L."
            )
            if question:
                base += f" The evaluator also asked: {question}"
            return f"{base}\n\nFacts:\n{facts}"
        if question:
            return (
                "Answer the evaluator's question about Aurelion's current state, grounded ONLY in these "
                f"facts. Question: {question}\n\nFacts:\n{facts}"
            )
        return (
            "Explain Aurelion's current state to a non-expert hackathon judge. Focus on WHY the top "
            "opportunity is taken or skipped, and mention the circuit breaker or active stress scenarios "
            f"if relevant. Facts:\n\n{facts}"
        )

    def _call_llm(self, ctx: dict, question: str | None, model: str | None) -> str:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        message = self._client.messages.create(
            model=self._resolve_model(model),
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": self._prompt(ctx, question)}],
        )
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        return text.strip()

    def _store(self, key: str, text: str, when: int, source: str, decision: dict | None) -> None:
        self._cache_key = key
        self._cache_text = text
        self._cache_source = source
        self._cache_at = when
        self._prev_decision = decision

    def narrate(self, snapshot: dict, question: str | None = None, model: str | None = None, trade_id: str | None = None) -> dict:
        ctx = self._build_context(snapshot, trade_id)
        key = self._context_key(ctx)
        current = now_ms()
        if not question and not trade_id and key == self._cache_key and self._cache_text and current - self._cache_at < self._cache_ttl_ms:
            return {"source": self._cache_source, "text": self._cache_text, "cached": True, "model": self.model if self.available() else None}
        if not self.available():
            text = self._answer_or_fallback(ctx, question)
            self._store(key, text, current, "deterministic", ctx.get("decision"))
            return {"source": "deterministic", "text": text, "cached": False, "model": None}
        try:
            text = self._call_llm(ctx, question, model)
            if not text:
                raise ValueError("empty narration")
            self._store(key, text, current, "claude", ctx.get("decision"))
            return {"source": "claude", "text": text, "cached": False, "model": self._resolve_model(model)}
        except Exception as exc:  # pragma: no cover - network/optional dependency path
            self.last_error = str(exc)
            text = self._answer_or_fallback(ctx, question)
            self._store(key, text, current, "deterministic-fallback", ctx.get("decision"))
            return {"source": "deterministic-fallback", "text": text, "cached": False, "model": None, "error": self.last_error}

    async def _chunk(self, text: str):
        for index, word in enumerate(text.split(" ")):
            yield {"type": "delta", "text": (word if index == 0 else " " + word)}
            await asyncio.sleep(0.012)

    async def stream_async(self, snapshot: dict, question: str | None = None, model: str | None = None, trade_id: str | None = None):
        """Async generator of {'type': 'delta'|'done', ...} events for SSE."""
        ctx = self._build_context(snapshot, trade_id)
        key = self._context_key(ctx)
        if not self.available():
            text = self._answer_or_fallback(ctx, question)
            async for event in self._chunk(text):
                yield event
            self._store(key, text, now_ms(), "deterministic", ctx.get("decision"))
            yield {"type": "done", "source": "deterministic", "model": None}
            return
        try:
            from anthropic import AsyncAnthropic

            if self._async_client is None:
                self._async_client = AsyncAnthropic(api_key=self.api_key)
            resolved = self._resolve_model(model)
            collected: list[str] = []
            async with self._async_client.messages.stream(
                model=resolved,
                max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._prompt(ctx, question)}],
            ) as stream:
                async for chunk in stream.text_stream:
                    collected.append(chunk)
                    yield {"type": "delta", "text": chunk}
            full = "".join(collected).strip()
            self._store(key, full, now_ms(), "claude", ctx.get("decision"))
            yield {"type": "done", "source": "claude", "model": resolved}
        except Exception as exc:  # pragma: no cover - network/optional dependency path
            self.last_error = str(exc)
            text = self._answer_or_fallback(ctx, question)
            async for event in self._chunk(text):
                yield event
            self._store(key, text, now_ms(), "deterministic-fallback", ctx.get("decision"))
            yield {"type": "done", "source": "deterministic-fallback", "model": None}
