from __future__ import annotations

import html
import time

# One-click judge report: a single self-contained HTML file (inline CSS, one
# inline SVG, no external assets) built from the live snapshot plus the
# persisted Research Lab artifacts. Spanish, because the evaluating committee
# is Spanish-speaking. Pure function of its inputs so it is trivially testable.


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def _fmt(value, decimals: int = 2) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _pnl_svg(series: list[dict]) -> str:
    points = [(row.get("time", 0), float(row.get("pnl", 0))) for row in (series or [])][-240:]
    if len(points) < 2:
        return "<p class='muted'>Sin operaciones suficientes para la curva de P&amp;L.</p>"
    values = [pnl for _, pnl in points]
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    width, height, pad = 640.0, 150.0, 8.0
    step = (width - 2 * pad) / (len(points) - 1)
    coords = " ".join(
        f"{pad + index * step:.1f},{height - pad - (value - low) / span * (height - 2 * pad):.1f}"
        for index, (_, value) in enumerate(points)
    )
    return (
        f"<svg viewBox='0 0 {width:.0f} {height:.0f}' role='img' aria-label='Curva de P&L'>"
        f"<polyline fill='none' stroke='#0d7d67' stroke-width='2' points='{coords}'/>"
        f"<text x='{pad}' y='14' class='svgLabel'>max {_fmt(high)}</text>"
        f"<text x='{pad}' y='{height - 12:.0f}' class='svgLabel'>min {_fmt(low)}</text>"
        "</svg>"
    )


def _radar_rows(discovery: dict) -> str:
    routes = ((discovery or {}).get("lastSweep") or {}).get("topRoutes") or []
    if not routes:
        return "<tr><td colspan='5' class='muted'>Sin barrido reciente (el radar corre en segundo plano).</td></tr>"
    rows = []
    for route in routes[:6]:
        rows.append(
            "<tr>"
            f"<td>{_esc(route.get('route'))}</td><td>{_esc(route.get('kind'))} · {_esc(route.get('base'))}</td>"
            f"<td>{_fmt(route.get('grossBps'))}</td><td>{_fmt(route.get('costsBps'))}</td>"
            f"<td class='{'good' if (route.get('netBps') or 0) > 0 else 'bad'}'>{_fmt(route.get('netBps'))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _research_section(research: list[dict]) -> str:
    if not research:
        return "<p class='muted'>Aún no hay sesiones de investigación persistidas.</p>"
    items = []
    for entry in research[:6]:
        stamp = entry.get("generatedAt")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp / 1000)) if stamp else "—"
        kind = "Entrenamiento" if entry.get("kind") == "autotune" else "Estudio de spreads"
        items.append(f"<li><b>{_esc(kind)}</b> · {when} — {_esc(entry.get('headline'))}</li>")
    latest_train = next((entry["payload"] for entry in research if entry.get("kind") == "autotune"), None)
    detail = ""
    if latest_train:
        baseline = latest_train.get("baseline") or {}
        best = latest_train.get("best") or {}
        changed = ", ".join(
            f"{key}→{value.get('to')}" for key, value in list((best.get("changedVsCurrent") or {}).items())[:6]
        )
        detail = (
            "<table><tr><th></th><th>Score (train)</th><th>Score (validación)</th><th>Drawdown</th></tr>"
            f"<tr><td>Configuración actual</td><td>{_fmt(baseline.get('score'))}</td><td>{_fmt(baseline.get('validationScore'))}</td><td>{_fmt(baseline.get('maxDrawdown'))}</td></tr>"
            f"<tr><td>Preset aprendido</td><td>{_fmt(best.get('score'))}</td><td class='good'>{_fmt(best.get('validationScore'))}</td><td>{_fmt(best.get('maxDrawdown'))}</td></tr></table>"
            f"<p class='muted'>El ganador se elige por score de <b>validación</b> (realización de mercado independiente). Cambios clave: {_esc(changed) or '—'}.</p>"
        )
    return f"<ul>{''.join(items)}</ul>{detail}"


STAGE_LABELS = {
    "ingest": "Ingesta + salud de venues",
    "riskGate": "Gate de riesgo",
    "scan": "Escaneo de oportunidades",
    "rank": "Ranking + explicabilidad",
    "execute": "Ejecución (paper)",
    "publish": "Snapshot + difusión SSE",
}


def _stages_table(slo: dict) -> str:
    stages = (slo or {}).get("stages") or {}
    if not stages:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(STAGE_LABELS.get(name, name))}</td><td>{_fmt(stat.get('p50'))}</td><td>{_fmt(stat.get('p95'))}</td></tr>"
        for name, stat in stages.items()
    )
    return (
        "<h2>Latencia interna por etapa (ms)</h2>"
        "<table><tr><th>Etapa</th><th>p50</th><th>p95</th></tr>" + rows + "</table>"
        "<p class='muted'>Dónde se va cada milisegundo dentro de un tick — medido en vivo, ventana móvil de 200 muestras.</p>"
    )


def _continuity_line(continuity: dict) -> str:
    prior = continuity.get("priorSessions") or 0
    if not prior:
        return ""
    pnl = continuity.get("lastSessionFinalPnl")
    trades = continuity.get("lastSessionTrades")
    return (
        "<h2>Continuidad entre sesiones</h2>"
        f"<p class='muted'>El almacén durable ({_esc(continuity.get('driver'))}) conserva {prior} sesión(es) previa(s); "
        f"la última cerró con {trades if trades is not None else '—'} operación(es) y P&amp;L final {_fmt(pnl)}. "
        "Un reinicio no borra la sesión auditable.</p>"
    )


def build_report_html(snapshot: dict, research: list[dict]) -> str:
    metrics = snapshot.get("metrics") or {}
    slo = snapshot.get("latencySlo") or {}
    decision = slo.get("decisionMs") or {}
    risk = snapshot.get("risk") or {}
    models = snapshot.get("models") or {}
    coverage = snapshot.get("exchangeCoverage") or {}
    discovery = snapshot.get("discovery") or {}
    sweep = discovery.get("lastSweep") or {}
    generated = time.strftime("%Y-%m-%d %H:%M:%S")
    uptime_min = (snapshot.get("uptimeMs") or 0) / 60000

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Aurelion — Reporte para el jurado</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; background: #eef2ec; color: #17251f; }}
  main {{ max-width: 860px; margin: 0 auto; padding: 28px 20px 60px; }}
  h1 {{ margin: 0; font-size: 26px; }} h2 {{ margin: 26px 0 8px; font-size: 17px; border-bottom: 2px solid #0d7d67; padding-bottom: 4px; }}
  .sub {{ color: #5b6c63; margin: 4px 0 18px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
  .card {{ background: #fff; border: 1px solid #d7ded6; border-radius: 10px; padding: 10px 12px; }}
  .card b {{ display: block; font-size: 18px; }} .card span {{ color: #5b6c63; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; font-size: 13px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #e4e9e2; }}
  th {{ background: #e2eae2; font-size: 12px; }}
  .good {{ color: #0d7d67; font-weight: 700; }} .bad {{ color: #b23c3c; font-weight: 700; }}
  .muted {{ color: #5b6c63; font-size: 12.5px; }}
  svg {{ width: 100%; height: auto; background: #fff; border: 1px solid #d7ded6; border-radius: 10px; }}
  .svgLabel {{ font-size: 10px; fill: #5b6c63; }}
  ul {{ margin: 8px 0; padding-left: 18px; font-size: 13.5px; }} li {{ margin-bottom: 4px; }}
  footer {{ margin-top: 30px; color: #5b6c63; font-size: 11.5px; line-height: 1.6; }}
</style></head><body><main>
<h1>Aurelion — Reporte para el jurado</h1>
<p class="sub">Generado {generated} · modo <b>{_esc(snapshot.get('mode'))}</b> · sesión de {_fmt(uptime_min, 0)} min · paper trading (sin dinero real)</p>

<h2>Resumen de la sesión</h2>
<div class="cards">
  <div class="card"><span>P&amp;L realizado</span><b>{_fmt(metrics.get('cumulativePnl'))}</b></div>
  <div class="card"><span>Operaciones</span><b>{_esc(metrics.get('executedCount'))}</b></div>
  <div class="card"><span>Win rate</span><b>{_fmt((metrics.get('winRate') or 0) * 100, 1)}%</b></div>
  <div class="card"><span>Señales detectadas</span><b>{_esc(metrics.get('detectedCount'))}</b></div>
  <div class="card"><span>Decisión p50 / p95</span><b>{_fmt(decision.get('p50'))} / {_fmt(decision.get('p95'))} ms</b></div>
  <div class="card"><span>Venues activos</span><b>{_esc(coverage.get('activeCount'))} de {_esc(coverage.get('universeCount'))}</b></div>
</div>

<h2>Curva de P&amp;L</h2>
{_pnl_svg(snapshot.get('pnlSeries') or [])}

{_stages_table(slo)}

<h2>Modelos activos</h2>
<table><tr><th>Ciclos</th><th>Slippage</th><th>Sizing</th><th>Volatilidad</th><th>Calibración</th><th>Circuit breaker</th></tr>
<tr><td>{_esc(models.get('cycleAlgo'))}</td><td>{_esc(models.get('slippageModel'))}</td><td>{_esc(models.get('sizingMode'))}</td>
<td>{_esc(models.get('volatilityModel'))}</td><td>{'activa' if models.get('calibrationEnabled') else 'apagada'}</td>
<td>{'PAUSADO: ' + _esc(risk.get('reason')) if risk.get('paused') else 'armado'}</td></tr></table>

<h2>Radar de red amplia (datos reales, read-only)</h2>
<p class="muted">{_esc(discovery.get('universeCount'))} venues + {_esc('/'.join(discovery.get('bases') or []))} · último barrido: {_esc(sweep.get('venuesLive'))} venues vivos, {_esc(sweep.get('seriesCount'))} series, {_esc(sweep.get('routesPriced'))} rutas valoradas.</p>
<table><tr><th>Ruta</th><th>Tipo</th><th>Bruto (bps)</th><th>Costos (bps)</th><th>Neto (bps)</th></tr>
{_radar_rows(discovery)}</table>

<h2>Investigación y entrenamiento (persistido)</h2>
{_research_section(research)}

{_continuity_line(snapshot.get('continuity') or {})}

<footer>
Aurelion es software de análisis, simulación y paper trading; no ejecuta órdenes reales y nunca usa llaves con permisos de retiro.
Comisiones modeladas: taker spot del nivel de entrada publicado de cada venue (julio 2026), sin descuentos.
Referencias: Bertram (2010) umbrales óptimos OU; Makarov &amp; Schoar (2020) arbitraje entre exchanges; Kaiko (2025) ventanas &lt;4 s; freqtrade hyperopt (búsqueda de parámetros vía backtest).
</footer>
</main></body></html>"""
