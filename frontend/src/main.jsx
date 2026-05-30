import React from "react";
import { createRoot } from "react-dom/client";
import { Activity, ArrowRightLeft, ChartNoAxesCombined, CirclePause, Clock3, DatabaseZap, FileDown, Gauge, Globe2, ListChecks, Network, Power, Radar, RefreshCw, ShieldAlert, Sparkles, Triangle, Zap } from "lucide-react";
import "./styles/app.css";

const API_BASE = import.meta.env.VITE_API_BASE || "";

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 });
const btc = new Intl.NumberFormat("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 6 });

function formatMoney(value) {
  return money.format(Number(value) || 0);
}

function formatBtc(value) {
  return `${btc.format(Number(value) || 0)} BTC`;
}

function formatNumber(value, digits = 2) {
  return Number(value || 0).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function formatPercent(value, digits = 0) {
  return `${formatNumber((Number(value) || 0) * 100, digits)}%`;
}

function clampRatio(value) {
  const ratio = Number(value);
  if (!Number.isFinite(ratio)) return 1;
  return Math.max(0, Math.min(1, ratio));
}

function ago(ms) {
  const value = Math.max(0, Number(ms) || 0);
  if (value < 1000) return "now";
  if (value < 60000) return `${Math.round(value / 1000)}s`;
  return `${Math.round(value / 60000)}m`;
}

function signalAge(item, now) {
  return ago((now || Date.now()) - (item?.time || now || Date.now()));
}

function useAurelion() {
  const [snapshot, setSnapshot] = React.useState(null);
  const [connected, setConnected] = React.useState(false);

  React.useEffect(() => {
    let fallback = null;
    let retry = null;
    let events;

    const poll = async () => {
      const response = await fetch(`${API_BASE}/api/snapshot`);
      setSnapshot(await response.json());
    };

    const connect = () => {
      events = new EventSource(`${API_BASE}/events`);
      events.addEventListener("open", () => {
        setConnected(true);
        if (fallback) clearInterval(fallback);
      });
      events.addEventListener("snapshot", (event) => {
        setConnected(true);
        setSnapshot(JSON.parse(event.data));
      });
      events.addEventListener("error", () => {
        setConnected(false);
        events.close();
        fallback = fallback || setInterval(poll, 1600);
        retry = setTimeout(connect, 2500);
      });
    };

    connect();
    return () => {
      if (events) events.close();
      if (fallback) clearInterval(fallback);
      if (retry) clearTimeout(retry);
    };
  }, []);

  const control = React.useCallback(async (payload) => {
    const response = await fetch(`${API_BASE}/api/control`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    setSnapshot(await response.json());
  }, []);

  const reset = React.useCallback(async () => {
    const response = await fetch(`${API_BASE}/api/reset`, { method: "POST" });
    setSnapshot(await response.json());
  }, []);

  const exportSession = React.useCallback(async () => {
    const response = await fetch(`${API_BASE}/api/export/session`);
    const payload = await response.json();
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `aurelion-session-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }, []);

  return { snapshot, connected, control, reset, exportSession };
}

function Metric({ icon: Icon, label, value, note, tone = "neutral" }) {
  return (
    <article className={`metric ${tone}`}>
      <Icon size={18} />
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </article>
  );
}

function Header({ snapshot, connected, control, reset, exportSession }) {
  const risk = snapshot?.risk;
  return (
    <header className="topbar">
      <div className="identity">
        <div className="sigil"><Sparkles size={22} /></div>
        <div>
          <h1>Aurelion</h1>
          <p>Bitcoin Arbitrage Intelligence</p>
        </div>
      </div>
      <div className="controls">
        <span className={`conn ${connected ? "online" : "offline"}`}><i />{connected ? "en vivo" : "sincronizando"}</span>
        <div className="segmented">
          {["auto", "live", "demo"].map((mode) => (
            <button key={mode} className={snapshot?.mode === mode ? "active" : ""} onClick={() => control({ mode })}>{mode}</button>
          ))}
        </div>
        <button className={`toggle ${risk?.autoExecution ? "on" : ""}`} onClick={() => control({ autoExecution: !risk?.autoExecution })}>
          {risk?.autoExecution ? <Power size={16} /> : <CirclePause size={16} />}
          {risk?.autoExecution ? "activo" : "pausado"}
        </button>
        <button className="stressButton" title="Simulate volatility circuit breaker" onClick={() => control({ volatilityShock: true })}>
          <Zap size={16} />
          volatilidad
        </button>
        <button className="iconButton" title="Export audit session" onClick={exportSession}><FileDown size={17} /></button>
        <button className="iconButton" title="Reset session" onClick={reset}><RefreshCw size={17} /></button>
      </div>
    </header>
  );
}

function Overview({ snapshot }) {
  const metrics = snapshot.metrics;
  const risk = snapshot.risk;
  const stateLabel = risk.paused ? "Pausado por riesgo" : risk.autoExecution ? "Operando" : "Pausado manual";
  const condition = risk.condition && risk.condition !== "healthy" ? risk.condition : "healthy";
  const stateNote = risk.paused
    ? `${condition} / ${risk.reason} / resumes in ${ago(risk.cooldownRemainingMs ?? risk.pausedUntil - snapshot.now)}`
    : risk.reason;
  const freshness = Math.max(0, metrics.avgFreshnessMs ?? metrics.avgLatencyMs);
  const bestEdge = metrics.bestNetBps > 0 ? `${formatNumber(metrics.bestNetBps, 2)} bps` : "Sin edge";
  const observed = metrics.bestNetBps > 0
    ? `${snapshot.queue.executable} listas para ejecutar`
    : metrics.bestObservedNetBps < 0
      ? `faltan ${formatNumber(Math.abs(metrics.bestObservedNetBps), 2)} bps`
      : "esperando libros completos";
  return (
    <section className="overview">
      <Metric icon={ChartNoAxesCombined} label="P&L realizado" value={formatMoney(metrics.cumulativePnl)} note={`${metrics.executedCount} trades ejecutados`} tone={metrics.cumulativePnl >= 0 ? "good" : "bad"} />
      <Metric icon={ShieldAlert} label="Estado del bot" value={stateLabel} note={stateNote} tone={risk.paused || !risk.autoExecution ? "bad" : "good"} />
      <Metric icon={Radar} label="Mejor oportunidad" value={bestEdge} note={observed} />
      <Metric icon={ArrowRightLeft} label="Señales detectadas" value={compact.format(metrics.detectedCount)} note={`${metrics.liveSignalCount || 0} ocurriendo ahora`} />
      <Metric icon={Gauge} label="Velocidad" value={`${Math.round(freshness)} ms`} note={`p95 ${Math.max(0, Math.round(metrics.p95FreshnessMs || freshness))} ms`} tone={(metrics.staleBooks || 0) > 0 ? "bad" : "neutral"} />
    </section>
  );
}

function Books({ books }) {
  return (
    <section className="surface books">
      <PanelTitle icon={Activity} title="Mercado en Vivo" pill={`${books.length} exchanges`} />
      <div className="bookGrid">
        {books.map((book) => (
          <article className={`book ${book.source}`} key={book.exchangeId}>
            <div className="bookHead">
              <div><strong>{book.exchangeName}</strong><span>{book.symbol}</span></div>
              <em>{book.source}</em>
            </div>
            <div className="quote">
              <span>Bid</span><b className="green">{formatMoney(book.bestBid)}</b>
            </div>
            <div className="quote">
              <span>Ask</span><b className="red">{formatMoney(book.bestAsk)}</b>
            </div>
            <div className="micro">
              <span>{formatBtc(book.depthBid)}</span>
              <span>{Math.round(book.ageMs)} ms age</span>
              <span>{Math.round(book.latencyMs)} ms upd</span>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function PanelTitle({ icon: Icon, title, pill }) {
  return (
    <div className="panelTitle">
      <h2><Icon size={17} />{title}</h2>
      <span>{pill}</span>
    </div>
  );
}

function RouteLabel({ item }) {
  if (item.strategy === "triangular") {
    const path = item.cyclePath || ["USDT", "BTC", "ETH", "USDT"];
    return (
      <span className="routeStack">
        <b>{item.exchange} triangular</b>
        <span className="cyclePath">{path.map((node, index) => <React.Fragment key={`${node}-${index}`}><i>{node}</i>{index < path.length - 1 && <ArrowRightLeft size={12} />}</React.Fragment>)}</span>
        <small>{item.legs?.map((leg) => leg.symbol).join(" / ") || item.product}</small>
      </span>
    );
  }
  return (
    <span className="routeStack">
      <b>{item.buyExchange} {"->"} {item.sellExchange}</b>
      <small>{formatMoney(item.buyPrice)} compra / {formatMoney(item.sellPrice)} venta</small>
    </span>
  );
}

function opportunitySize(item) {
  if (item.strategy === "triangular") return formatMoney(item.quoteIn);
  return formatBtc(item.qtyBtc);
}

function opportunityTarget(item) {
  if (item.strategy === "triangular") return `target ${formatMoney(item.targetQuote || item.quoteIn)}`;
  return `target ${formatBtc(item.targetQtyBtc || item.qtyBtc)}`;
}

function statusClass(item) {
  if (item.status === "profitable" && item.partial) return "profitable-partial";
  if (item.status === "profitable") return "profitable";
  return item.status;
}

function statusLabel(item) {
  if (item.status === "profitable" && item.partial) return "rentable parcial";
  if (item.status === "profitable") return "rentable";
  if (item.status === "blocked" && `${item.reason}`.toLowerCase().includes("wallet")) return "inventario";
  if (item.status === "blocked") return "liquidez";
  if (item.status === "rejected") return "descartada";
  return item.status;
}

function statusHelp(item) {
  if (item.status === "profitable" && item.partial) return `${formatPercent(clampRatio(item.filledRatio))} de liquidez`;
  if (item.status === "profitable") return "lista para ejecutar";
  if (item.status === "blocked") return item.reason || "inventario o profundidad insuficiente";
  return item.reason;
}

function decisionActionLabel(item) {
  const action = item?.decision?.action;
  if (action === "execute-partial") return "Ejecutar parcial";
  if (action === "execute-full") return "Ejecutar completa";
  if (action === "inventory-gate") return "Esperar inventario";
  if (action === "liquidity-gate") return "Esperar liquidez";
  if (action === "skip-costs") return "Descartar";
  return statusLabel(item || {});
}

function decisionCaption(item) {
  if (!item) return "";
  if (item.status === "profitable" && item.partial) return "Rentable, pero con liquidez limitada.";
  if (item.status === "profitable") return "Rentable después de fees, slippage y latencia.";
  if (item.status === "blocked") return "No pasa una compuerta dura de inventario o profundidad.";
  return "El costo total consume el spread observado.";
}

function OpportunityTable({ opportunities, queue = {}, now }) {
  const visible = opportunities.filter((item) => item.status !== "blocked");
  const fallback = visible.length ? visible : opportunities;
  const rows = fallback.slice(0, 7);
  return (
    <section className="surface queue">
      <PanelTitle icon={Triangle} title="Oportunidades Priorizadas" pill={`${queue.executable || 0} ejecutables`} />
      <div className="queueStats">
        <span><b>{queue.received || 0}</b> analizadas</span>
        <span><b>{queue.deduped || 0}</b> duplicadas fuera</span>
        <span><b>{queue.executable || 0}</b> listas</span>
        <span><b>{queue.queued || 0}</b> en ranking</span>
      </div>
      <div className="table">
        <div className="thead"><span>Ruta</span><span>Tamaño</span><span>Ganancia neta</span><span>Score</span><span>Estado</span></div>
        {rows.map((opportunity) => (
          <div className="tr" key={opportunity.id}>
            <span className="routeStack">
              <RouteLabel item={opportunity} />
              <small className={now - opportunity.time <= 1500 ? "liveStamp on" : "liveStamp"}><Clock3 size={12} /> hace {signalAge(opportunity, now)}</small>
            </span>
            <span>
              <b>{opportunitySize(opportunity)}</b>
              <small>{opportunity.partial ? `${formatPercent(clampRatio(opportunity.filledRatio))} of target` : opportunityTarget(opportunity)}</small>
            </span>
            <span className={opportunity.netProfit >= 0 ? "green" : "red"}>
              {formatMoney(opportunity.netProfit)}
              <small>{formatNumber(opportunity.netBps, 2)} bps / costs {formatMoney(opportunity.costs?.totalCosts)}</small>
            </span>
            <span>
              <b>{formatNumber(opportunity.score, 3)}</b>
              <small>conf {formatNumber(opportunity.confidence, 2)}</small>
            </span>
            <span>
              <em className={`badge ${statusClass(opportunity)}`}>{statusLabel(opportunity)}</em>
              <small>{statusHelp(opportunity)}</small>
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function topDecision(opportunities = []) {
  return opportunities.find((item) => item.status === "profitable") || opportunities.find((item) => item.decision) || opportunities[0];
}

function scoreTone(score) {
  if (score >= 82) return "good";
  if (score >= 58) return "watch";
  return "bad";
}

function EdgeExplainability({ opportunities = [] }) {
  const item = topDecision(opportunities);
  if (!item) {
    return (
      <section className="surface edgePanel">
        <PanelTitle icon={Radar} title="Decisión Actual" pill="esperando" />
        <div className="empty">Aún no hay rutas rankeadas</div>
      </section>
    );
  }
  const decision = item.decision || {};
  const breakdown = item.edgeBreakdown || {};
  return (
    <section className="surface edgePanel">
      <PanelTitle icon={Radar} title="Decisión Actual" pill={`calidad ${decision.scoreGrade || "D"}`} />
      <div className="edgeBody">
        <div className={`decisionStamp ${statusClass(item)}`}>
          <b>{decisionActionLabel(item)}</b>
          <span>{decisionCaption(item)}</span>
        </div>
        <div className="edgeRoute">
          <RouteLabel item={item} />
          <small>{formatNumber(breakdown.netBps, 2)} bps net / {formatNumber(breakdown.costDragPct, 1)}% cost drag / {formatNumber(breakdown.latencyMs, 0)} ms</small>
        </div>
        <div className="scoreBars">
          {(breakdown.components || []).map((component) => (
            <div className="scoreBar" key={component.label}>
              <span>{component.label}</span>
              <i style={{ "--fill": `${(Number(component.value || 0) / Number(component.max || 1)) * 100}%` }} />
              <b>{formatNumber(component.value, 1)}</b>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function RealityCheck({ opportunities = [] }) {
  const item = topDecision(opportunities);
  const reality = item?.paperVsSettlement;
  if (!item || !reality) {
    return (
      <section className="surface realityPanel">
        <PanelTitle icon={ArrowRightLeft} title="Costos Reales" pill="sin ruta" />
        <div className="empty">No hay ruta para revisar</div>
      </section>
    );
  }
  return (
    <section className="surface realityPanel">
      <PanelTitle icon={ArrowRightLeft} title="Costos Reales" pill={reality.verdict} />
      <div className="realityGrid">
        <article>
          <span>Sin rebalanceo</span>
          <b className={reality.prefundedNetProfit >= 0 ? "green" : "red"}>{formatMoney(reality.prefundedNetProfit)}</b>
        </article>
        <article>
          <span>Neto realista</span>
          <b className={reality.settlementNetProfit >= 0 ? "green" : "red"}>{formatMoney(reality.settlementNetProfit)}</b>
        </article>
        <article>
          <span>Costo extra</span>
          <b>{formatMoney(reality.settlementDrag)}</b>
          <small>{formatNumber(reality.settlementDragBps, 2)} bps</small>
        </article>
      </div>
    </section>
  );
}

function OpportunityHistory({ opportunities = [], metrics = {}, now }) {
  const [filter, setFilter] = React.useState("all");
  const filters = [
    ["all", "Todas"],
    ["live", "Ahora"],
    ["profitable", "Rentables"],
    ["rejected", "Descartadas"],
    ["partial", "Parciales"],
    ["triangular", "Triangular"],
  ];
  const filtered = opportunities.filter((item) => {
    if (filter === "all") return true;
    if (filter === "live") return now - item.time <= 2500;
    if (filter === "partial") return item.partial;
    return item.status === filter || item.strategy === filter;
  });
  const rows = filtered.slice(0, 18);
  return (
    <section className="surface history">
      <PanelTitle icon={ListChecks} title="Historial de Señales" pill={`${rows.length} recientes`} />
      <div className="historyToolbar">
        {filters.map(([id, label]) => (
          <button className={filter === id ? "active" : ""} key={id} onClick={() => setFilter(id)} type="button">{label}</button>
        ))}
      </div>
      <div className="historyList">
        {rows.map((item) => (
          <article className={`historyItem ${statusClass(item)}`} key={`hist-${item.id}`}>
            <RouteLabel item={item} />
            <span className="historyEdge">
              <b className={item.netProfit >= 0 ? "green" : "red"}>{formatNumber(item.netBps, 2)} bps</b>
              <small>{formatMoney(item.netProfit)} neto / score {formatNumber(item.score, 2)}</small>
            </span>
            <span className="historyMeta">
              <em className={`badge ${statusClass(item)}`}>{statusLabel(item)}</em>
              <small className={now - item.time <= 1500 ? "liveStamp on" : "liveStamp"}><Clock3 size={12} /> hace {signalAge(item, now)}</small>
            </span>
          </article>
        ))}
        {!rows.length && <div className="empty">No hay señales con este filtro</div>}
      </div>
    </section>
  );
}

function Streams({ streams, redis }) {
  const rows = streams.streams || [];
  const redisLabel = redis.enabled ? redis.status : "optional off";
  const streamTone = (stream) => stream.disabled ? "disabled" : stream.restFallback ? "rest" : "ws";
  return (
    <section className="surface streams">
      <PanelTitle icon={DatabaseZap} title="Infraestructura" pill={redisLabel} />
      <div className="streamList">
        {rows.slice(0, 12).map((stream) => (
          <article className="stream" key={stream.key}>
            <b>{stream.exchangeName}</b>
            <span>{stream.symbol}</span>
            <em className={streamTone(stream)}>{stream.mode}</em>
            <small>{stream.disabledReason || `${stream.updates} updates / ${stream.failures} failures`}</small>
          </article>
        ))}
        {!rows.length && <div className="empty">{streams.unavailableReason || "No stream telemetry"}</div>}
      </div>
    </section>
  );
}

function GlobalMarket({ globalMarket }) {
  return (
    <section className="surface">
      <PanelTitle icon={Globe2} title="Contexto Global" pill={globalMarket.status || "cargando"} />
      <div className="globalGrid">
        <article>
          <span>BTC reference</span>
          <b>{formatMoney(globalMarket.btcUsd)}</b>
          <small className={globalMarket.btcChange24h >= 0 ? "green" : "red"}>{formatNumber(globalMarket.btcChange24h, 2)}% 24h</small>
        </article>
        <article>
          <span>ETH reference</span>
          <b>{formatMoney(globalMarket.ethUsd)}</b>
          <small className={globalMarket.ethChange24h >= 0 ? "green" : "red"}>{formatNumber(globalMarket.ethChange24h, 2)}% 24h</small>
        </article>
        <article>
          <span>BTC market cap</span>
          <b>{compact.format(globalMarket.btcMarketCap || 0)}</b>
          <small>{globalMarket.source || "CoinGecko"}</small>
        </article>
      </div>
    </section>
  );
}

function LatencySloPanel({ slo = {} }) {
  const age = slo.bookAgeMs || {};
  const update = slo.updateLatencyMs || {};
  return (
    <section className={`surface sloPanel ${slo.status || "green"}`}>
      <PanelTitle icon={Gauge} title="Velocidad" pill={slo.summary || "cargando"} />
      <div className="sloGrid">
        <article>
          <span>Libros p95</span>
          <b>{Math.round(age.p95 || 0)} ms</b>
          <small>target {Math.round(age.targetP95 || 0)} ms</small>
        </article>
        <article>
          <span>Update p95</span>
          <b>{Math.round(update.p95 || 0)} ms</b>
          <small>target {Math.round(update.targetP95 || 0)} ms</small>
        </article>
      </div>
      <div className="sloStrip">
        <span>p50 age {Math.round(age.p50 || 0)} ms</span>
        <span>p99 age {Math.round(age.p99 || 0)} ms</span>
        <span>p99 upd {Math.round(update.p99 || 0)} ms</span>
      </div>
    </section>
  );
}

function DemoQualityPanel({ quality = {}, mode }) {
  const tone = scoreTone(Number(quality.score || 0));
  return (
    <section className={`surface qualityPanel ${tone}`}>
      <PanelTitle icon={Sparkles} title="Calidad Demo" pill={mode === "demo" ? quality.label || "cargando" : "observando"} />
      <div className="qualityDial">
        <b>{Math.round(quality.score || 0)}</b>
        <span>calidad</span>
      </div>
      <div className="qualityStats">
        <span>{formatMoney(quality.pnlPerMinute || 0)} / min</span>
        <span>{formatNumber(quality.fillsPerMinute || 0, 2)} fills / min</span>
        <span>{formatPercent(quality.partialRate || 0)} partial</span>
      </div>
    </section>
  );
}

function ExchangeCoverage({ coverage = {}, quality = [], control }) {
  const active = new Set((coverage.active || []).map((exchange) => exchange.id));
  const universe = coverage.universe || coverage.active || [];
  const qualityById = new Map((quality || []).map((venue) => [venue.exchangeId, venue]));
  const toggle = (exchange) => {
    const next = active.has(exchange.id)
      ? [...active].filter((id) => id !== exchange.id)
      : [...active, exchange.id];
    if (next.length < 2 || next.length > 5) return;
    control({ activeExchanges: next });
  };
  return (
    <section className="surface">
      <PanelTitle icon={Network} title="Exchanges" pill={`${coverage.activeCount || active.size} activos`} />
      <div className="coverageGrid">
        {universe.map((exchange) => (
          <button className={active.has(exchange.id) ? "active" : ""} disabled={!active.has(exchange.id) && active.size >= 5} key={exchange.id} onClick={() => toggle(exchange)} type="button">
            <b>{exchange.name}</b>
            <span>{qualityById.has(exchange.id) ? `${qualityById.get(exchange.id).score} quality / ${qualityById.get(exchange.id).latencyMs} ms` : active.has(exchange.id) ? "speed profile" : "coverage catalog"}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function PnlChart({ series }) {
  const ref = React.useRef(null);
  React.useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = rect.width * ratio;
    canvas.height = rect.height * ratio;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    ctx.fillStyle = "#fbfcf8";
    ctx.fillRect(0, 0, rect.width, rect.height);
    ctx.strokeStyle = "#dfe6da";
    for (let y = 28; y < rect.height; y += 42) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(rect.width, y);
      ctx.stroke();
    }
    const points = series.length ? series : [{ pnl: 0 }, { pnl: 0 }];
    const min = Math.min(0, ...points.map((point) => point.pnl));
    const max = Math.max(1, ...points.map((point) => point.pnl));
    const range = Math.max(1, max - min);
    ctx.strokeStyle = "#0d7d67";
    ctx.lineWidth = 3;
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = points.length === 1 ? 0 : (index / (points.length - 1)) * rect.width;
      const y = rect.height - 24 - ((point.pnl - min) / range) * (rect.height - 48);
      index === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
  }, [series]);
  return <canvas className="chart" ref={ref} />;
}

function SideRail({ snapshot, control }) {
  return (
    <aside className="sideRail">
      <section className="surface">
        <PanelTitle icon={ChartNoAxesCombined} title="P&L" pill={formatMoney(snapshot.metrics.cumulativePnl)} />
        <PnlChart series={snapshot.pnlSeries} />
      </section>
      <LatencySloPanel slo={snapshot.latencySlo} />
      <DemoQualityPanel quality={snapshot.demoQuality} mode={snapshot.mode} />
      <ExchangeCoverage coverage={snapshot.exchangeCoverage} quality={snapshot.venueQuality} control={control} />
      <GlobalMarket globalMarket={snapshot.globalMarket || {}} />
      <section className="surface">
        <PanelTitle icon={DatabaseZap} title="Wallets" pill={formatMoney(snapshot.totals.markToMarket)} />
        <div className="wallets">
          {snapshot.wallets.map((wallet) => (
            <article key={wallet.exchangeId}>
              <b>{wallet.exchangeName}</b>
              <span>{formatMoney(wallet.USDT)}</span>
              <small>{formatBtc(wallet.BTC)} / {formatNumber(wallet.ETH, 3)} ETH</small>
            </article>
          ))}
        </div>
      </section>
      <section className="surface">
        <PanelTitle icon={ShieldAlert} title="Riesgo" pill={`${snapshot.riskEvents.length} eventos`} />
        <div className="events">
          {snapshot.riskEvents.slice(0, 8).map((event) => (
            <article className="event" key={event.id || `${event.type}-${event.time}`}>
              <b>{event.condition || event.type}</b>
              <span>{event.reason || "market event"}</span>
              <small>{new Date(event.time).toLocaleTimeString()}</small>
            </article>
          ))}
          {!snapshot.riskEvents.length && <div className="empty">Sin eventos de riesgo</div>}
        </div>
      </section>
    </aside>
  );
}

function fillTitle(item) {
  if (item.strategy === "triangular") return `${item.exchange} triangular cycle`;
  return `${item.buyExchange} -> ${item.sellExchange}`;
}

function executionKind(item) {
  if (item.strategy === "triangular" && item.partial) return "triangular parcial";
  if (item.strategy === "triangular") return "triangular";
  if (item.partial) return "parcial";
  return "completa";
}

function executionKindClass(item) {
  if (item.strategy === "triangular" && item.partial) return "triangular-partial";
  if (item.strategy === "triangular") return "triangular";
  if (item.partial) return "partial-fill";
  return "filled";
}

function Trades({ trades, metrics = {} }) {
  return (
    <section className="surface trades">
      <PanelTitle icon={ArrowRightLeft} title="Trades Ejecutados" pill={`${trades.length} recientes`} />
      <div className="tradeList">
        {trades.map((trade) => (
          <article className={trade.partial ? "partialTrade" : ""} key={trade.id}>
            <div className="tradeTop">
              <b>{fillTitle(trade)}</b>
              <em className={`badge ${executionKindClass(trade)}`}>{executionKind(trade)}</em>
            </div>
            <span>{trade.strategy === "triangular" ? `${trade.cyclePath?.join(" -> ")} / ${formatMoney(trade.quoteIn)}` : formatBtc(trade.qtyBtc)}</span>
            <em className={trade.netProfit >= 0 ? "green" : "red"}>{formatMoney(trade.netProfit)}</em>
            <div className="tradeDetails">
              <small>{new Date(trade.time).toLocaleTimeString()}</small>
              <small>{formatNumber(trade.executionQuality?.edgeCaptureBps || trade.netBps, 2)} bps capturados</small>
              {trade.strategy === "triangular" && <small>{trade.legs?.map((leg) => `${leg.from}->${leg.to}`).join(" / ")}</small>}
              {trade.partial && <small>{formatPercent(clampRatio(trade.filledRatio))} del objetivo</small>}
              {!trade.partial && <small>100% del objetivo</small>}
            </div>
          </article>
        ))}
        {!trades.length && <div className="empty">Aún no hay trades ejecutados</div>}
      </div>
    </section>
  );
}

function App() {
  const { snapshot, connected, control, reset, exportSession } = useAurelion();
  if (!snapshot) {
    return <main className="loading"><div className="sigil"><Sparkles size={24} /></div><span>Starting Aurelion</span></main>;
  }
  return (
    <>
      <Header snapshot={snapshot} connected={connected} control={control} reset={reset} exportSession={exportSession} />
      <main className="layout">
        <Overview snapshot={snapshot} />
        <section className="mainGrid">
          <div className="primary">
            <Books books={snapshot.books} />
            <div className="decisionDeck">
              <EdgeExplainability opportunities={snapshot.queuedOpportunities} />
              <RealityCheck opportunities={snapshot.queuedOpportunities} />
            </div>
            <OpportunityTable opportunities={snapshot.queuedOpportunities} queue={snapshot.queue} now={snapshot.now} />
            <Trades trades={snapshot.trades} metrics={snapshot.metrics} />
            <OpportunityHistory opportunities={snapshot.opportunityHistory || snapshot.opportunities} metrics={snapshot.metrics} now={snapshot.now} />
          </div>
          <SideRail snapshot={snapshot} control={control} />
        </section>
        <Streams streams={snapshot.streams} redis={snapshot.redis} />
      </main>
    </>
  );
}

createRoot(document.getElementById("root")).render(<App />);
