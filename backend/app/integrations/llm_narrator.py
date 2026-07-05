from __future__ import annotations

import asyncio
import json
import os
import random
import time

from backend.app.core.config import Settings

SYSTEM_PROMPT = (
    "Eres el copiloto de trading de Aurelion. SOLO explicas la decisión actual del "
    "sistema en español claro y muy breve para un evaluador no experto. Nunca das "
    "consejos financieros, nunca predices precios y nunca decides operaciones — el "
    "motor decide; tú describes. RESPONDE EN ESPAÑOL, en 1-2 frases cortas (máximo ~30 "
    "palabras), basándote solo en los hechos dados; no inventes números. Como se "
    "actualiza muy rápido, sé conciso. Varía la redacción entre actualizaciones. Si "
    "facts.mode es 'demo', es una muestra simulada. Si es 'auto'/'live' son mercados "
    "REALES: si operan pocas o ninguna, es el resultado correcto y medido (las "
    "comisiones superan al margen), no una falla. Si facts.degraded es verdadero, "
    "advierte que se pidió modo en vivo pero los datos son el respaldo simulado."
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
        observation_raw = snapshot.get("observation") or {}
        observation = None
        if observation_raw.get("recording"):
            observation = {
                "samples": observation_raw.get("samples"),
                "routes": observation_raw.get("routesObserved"),
                "capturable": observation_raw.get("capturableRoutes"),
            }
        return {
            "mode": snapshot.get("mode"),
            "degraded": bool(snapshot.get("degradedDemo")),
            "paused": risk.get("paused"),
            "riskReason": risk.get("reason"),
            "models": snapshot.get("models", {}),
            "scenarios": scenarios,
            "realizedPnl": metrics.get("cumulativePnl"),
            "executed": metrics.get("executedCount"),
            "detected": metrics.get("detectedCount"),
            "bestNetBps": metrics.get("bestNetBps"),
            "bestObservedNetBps": metrics.get("bestObservedNetBps"),
            "autonomy": (snapshot.get("inventoryAutonomy") or {}).get("sessionAutonomy"),
            "radar": radar,
            "observation": observation,
            "engineFaultsContained": (snapshot.get("engineHealth") or {}).get("tickErrors") or 0,
            "decision": decision,
            "focusTrade": self._focus_trade(snapshot, trade_id),
        }

    def _context_key(self, ctx: dict) -> str:
        decision = ctx.get("decision") or {}
        focus = ctx.get("focusTrade") or {}
        return "|".join(str(part) for part in (
            ctx.get("mode"),
            ctx.get("degraded"),
            ctx.get("paused"),
            decision.get("route"),
            decision.get("status"),
            round(decision.get("netBps") or 0, 1),
            ",".join(ctx.get("scenarios") or []),
            focus.get("id"),
        ))

    def _fallback_focus_trade(self, focus: dict) -> str:
        """Short Spanish explanation of one executed trade. On-demand (the user
        clicks 'explicar'), so a little more detail than the live stream is fine."""
        parts: list[str] = [f"Operación {focus['route']}: {focus.get('status')}."]
        net = focus.get("netProfit")
        if net is not None:
            parts.append(f"P&L neto {net:+.4f} tras todos los costos modelados.")
        if focus.get("partial"):
            ratio = round((focus.get("filledRatio") or 0) * 100)
            parts.append(f"Solo se llenó el {ratio}% del tamaño objetivo.")
        if focus.get("legExposureBtc"):
            parts.append(f"Un tramo se llenó de menos; el bot cubrió el residual (costo {focus.get('coverCost')}).")
        return " ".join(parts)

    def _short_mode_tag(self, ctx: dict) -> str:
        """A brief Spanish mode note so demo and live never read the same. Kept
        to a few words — the exception is the live 'fee wall' case, which is the
        honest measured finding worth stating in full."""
        if ctx.get("degraded"):
            return "Respaldo simulado (no en vivo)."
        mode = ctx.get("mode")
        if mode in ("auto", "live"):
            obs = ctx.get("observation")
            if obs and obs.get("samples") and (obs.get("capturable") or 0) == 0:
                return (
                    f"En vivo: 0/{obs.get('routes')} rutas superan el muro de comisiones en "
                    f"{obs.get('samples')} muestras — hallazgo real, no falla."
                )
            return "En vivo (mercados reales)."
        if mode == "demo":
            return "Demo simulado."
        return ""

    def _fallback(self, ctx: dict) -> str:
        # Deliberately SHORT (1-2 sentences): the panel refreshes fast, so long
        # text is unreadable. Spanish, grounded only in the facts, phrasing rotated.
        if ctx.get("focusTrade"):
            return self._fallback_focus_trade(ctx["focusTrade"])
        tag = self._short_mode_tag(ctx)
        decision = ctx.get("decision")
        if ctx.get("paused"):
            reason = ctx.get("riskReason")
            line = self._pick([
                f"Ejecución en pausa por el disyuntor ({reason}); el bot sigue observando el mercado.",
                f"El disyuntor detuvo la ejecución ({reason}); el monitoreo continúa y se rearma solo.",
            ])
        elif decision:
            route = decision["route"]
            net = decision.get("netBps")
            conf = round((decision.get("confidence") or 0) * 100)
            if decision.get("status") == "profitable":
                line = self._pick([
                    f"{route} pasa los filtros: {net} bps netos tras costos, {conf}% de confianza.",
                    f"Mejor ruta: {route} — {net} bps netos tras comisiones y latencia ({conf}% conf).",
                ])
            elif decision.get("status") == "blocked":
                line = f"{route} bloqueada: {decision.get('reason')}."
            else:
                line = self._pick([
                    f"Se omite {route}: con costos reales el margen es {net} bps netos, insuficiente.",
                    f"{route} no sobrevive los costos ({net} bps netos), así que se descarta.",
                ])
        else:
            line = self._pick([
                "Sin oportunidad accionable: los spreads visibles no sobreviven los costos.",
                "Nada accionable ahora: cada spread visible muere al aplicar los costos reales.",
            ])
        if ctx.get("scenarios"):
            line = f"{line} Estrés: {', '.join(ctx['scenarios'])}."
        return f"{tag} {line}".strip() if tag else line

    def _answer_or_fallback(self, ctx: dict, question: str | None) -> str:
        text = self._fallback(ctx)
        if question:
            text = f"{text} (Conecta una llave API de Anthropic para preguntas y respuestas detalladas.)"
        return text

    def _prompt(self, ctx: dict, question: str | None) -> str:
        facts = json.dumps(ctx, default=str)
        if ctx.get("focusTrade"):
            base = (
                "Explica en español y en 1-2 frases cortas esta operación ejecutada (ctx.focusTrade) a un "
                "evaluador no experto: qué pasó, si se llenó limpiamente o tuvo un problema (llenado parcial / "
                "falla de tramo / movimiento adverso) y cómo afectó su P&L."
            )
            if question:
                base += f" El evaluador también preguntó: {question}"
            return f"{base}\n\nHechos:\n{facts}"
        if question:
            return (
                "Responde en español, breve, la pregunta del evaluador sobre el estado actual de Aurelion, "
                f"basándote SOLO en estos hechos. Pregunta: {question}\n\nHechos:\n{facts}"
            )
        return (
            "Explica en español y en 1-2 frases cortas el estado actual de Aurelion a un juez no experto. "
            "Céntrate en POR QUÉ la mejor oportunidad se toma o se omite, y menciona el disyuntor o los "
            f"escenarios de estrés activos solo si son relevantes. Hechos:\n\n{facts}"
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
