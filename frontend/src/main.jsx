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

function seenAge(item, now) {
  const value = signalAge(item, now);
  return value === "now" ? "now" : `${value} ago`;
}

function streamCounts(streams = {}) {
  const rows = streams.streams || [];
  return {
    ws: rows.filter((stream) => stream.mode === "websocket").length,
    rest: rows.filter((stream) => stream.restFallback || stream.mode === "rest").length,
    disabled: rows.filter((stream) => stream.disabled || stream.mode === "disabled").length,
    total: rows.length,
  };
}

function dataFeedLabel(streams = {}, books = [], coverage = {}) {
  const counts = streamCounts(streams);
  const venues = coverage.activeCount || books.length || 0;
  if (!counts.total && books.some((book) => book.source === "simulated")) return `${venues} demo venues`;
  if (counts.disabled) return `${counts.disabled} disabled`;
  if (counts.rest) return `${venues} venues / ${counts.ws} WS / ${counts.rest} REST`;
  return `${venues} venues / ${counts.ws || books.length} WS streams`;
}

function auditLabel(database = {}, redis = {}) {
  if (database?.postgresReady) return "Postgres audit";
  if (database?.status === "connected") return `${database.driver || "local"} audit`;
  if (redis?.enabled) return `Redis ${redis.status}`;
  return "local audit";
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
  const metrics = snapshot?.metrics || {};
  const counts = streamCounts(snapshot?.streams);
  const dataTone = counts.disabled ? "bad" : counts.rest ? "watch" : "good";
  return (
    <header className="topbar">
      <div className="identityCluster">
        <div className="identity">
          <div className="sigil"><Sparkles size={22} /></div>
          <div>
            <h1>Aurelion</h1>
            <p>Bitcoin Arbitrage Intelligence</p>
          </div>
        </div>
        <span className={`conn ${connected ? "online" : "offline"}`}><i />{connected ? "live" : "syncing"}</span>
      </div>
      <div className="modeDock">
        <div className="segmented">
          {["auto", "live", "demo"].map((mode) => (
            <button key={mode} className={snapshot?.mode === mode ? "active" : ""} onClick={() => control({ mode })}>{mode[0].toUpperCase() + mode.slice(1)}</button>
          ))}
        </div>
      </div>
      <div className="topPulse">
        <span className="pulseItem good"><b>{formatMoney(metrics.cumulativePnl)}</b><small>P&L</small></span>
        <span className={`pulseItem ${dataTone}`}><b>{dataFeedLabel(snapshot?.streams, snapshot?.books, snapshot?.exchangeCoverage)}</b><small>data feed</small></span>
        <span className="pulseItem"><b>{snapshot?.venueHealth?.demotedCount || metrics.demotedVenues || 0}</b><small>demoted</small></span>
        <span className="pulseItem"><b>{auditLabel(snapshot?.database, snapshot?.redis)}</b><small>audit</small></span>
      </div>
      <div className="controls">
        <button className={`toggle ${risk?.autoExecution ? "on" : ""}`} onClick={() => control({ autoExecution: !risk?.autoExecution })}>
          {risk?.autoExecution ? <Power size={16} /> : <CirclePause size={16} />}
          {risk?.autoExecution ? "running" : "paused"}
        </button>
        <button className={`stressButton ${risk?.paused ? "active" : ""}`} title="Simulate volatility circuit breaker" onClick={() => control({ volatilityShock: true })}>
          <Zap size={16} />
          {risk?.paused ? "risk active" : "volatility"}
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
  const best = topDecision(snapshot.queuedOpportunities || []);
  const stateLabel = risk.paused ? "Risk Paused" : risk.autoExecution ? "Running" : "Manual Pause";
  const condition = risk.condition && risk.condition !== "healthy" ? risk.condition : "healthy";
  const stateNote = risk.paused
    ? `${condition} / ${risk.reason} / resumes in ${ago(risk.cooldownRemainingMs ?? risk.pausedUntil - snapshot.now)}`
    : `risk ${formatMoney(risk.riskBudgetUsedUsd || 0)} / ${formatMoney(risk.riskBudgetHourUsd || 0)}`;
  const freshness = Math.max(0, metrics.avgFreshnessMs ?? metrics.avgLatencyMs);
  const bestEdge = metrics.bestNetBps > 0 ? `${formatNumber(metrics.bestNetBps, 2)} bps` : "No edge";
  const observed = metrics.bestNetBps > 0
    ? `EV ${formatMoney(best?.expectedValue || best?.netProfit || 0)} / capture ${formatPercent(best?.latencyCaptureProbability || best?.edgeBreakdown?.latencyCaptureProbability || 0)}`
    : metrics.bestObservedNetBps < 0
      ? `${formatNumber(Math.abs(metrics.bestObservedNetBps), 2)} bps short`
      : "waiting for complete books";
  return (
    <section className="overview">
      <Metric icon={ChartNoAxesCombined} label="Realized P&L" value={formatMoney(metrics.cumulativePnl)} note={`${metrics.executedCount} executed trades`} tone={metrics.cumulativePnl >= 0 ? "good" : "bad"} />
      <Metric icon={ShieldAlert} label="Bot Status" value={stateLabel} note={stateNote} tone={risk.paused || !risk.autoExecution ? "bad" : "good"} />
      <Metric icon={Radar} label="Best Opportunity" value={bestEdge} note={observed} />
      <Metric icon={ArrowRightLeft} label="Detected Signals" value={compact.format(metrics.detectedCount)} note={`${metrics.liveSignalCount || 0} happening now`} />
      <Metric icon={Gauge} label="Speed" value={`${Math.round(freshness)} ms`} note={`freshness p95 ${Math.max(0, Math.round(metrics.p95FreshnessMs || freshness))} ms`} tone={(metrics.staleBooks || 0) > 0 ? "bad" : "neutral"} />
      <Metric icon={DatabaseZap} label="Data Health" value={dataFeedLabel(snapshot.streams, snapshot.books, snapshot.exchangeCoverage)} note={`${metrics.demotedVenues || 0} venues demoted`} tone={(metrics.staleBooks || 0) > 0 || (metrics.demotedVenues || 0) > 0 ? "bad" : "neutral"} />
    </section>
  );
}

function Books({ books }) {
  return (
    <section className="surface books">
      <PanelTitle icon={Activity} title="Live Market" pill={`${books.length} venues`} />
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
        <small>{item.dynamicCycle || path.length > 4 ? `dynamic ${path.length - 1}-leg cycle / ` : ""}{item.legs?.map((leg) => leg.symbol).join(" / ") || item.product}</small>
      </span>
    );
  }
  return (
    <span className="routeStack">
      <b>{item.buyExchange} {"->"} {item.sellExchange}</b>
      <small>{formatMoney(item.buyPrice)} buy / {formatMoney(item.sellPrice)} sell</small>
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
  if (item.status === "profitable" && item.partial) return "profitable partial";
  if (item.status === "profitable") return "profitable";
  if (item.status === "blocked" && `${item.reason}`.toLowerCase().includes("wallet")) return "inventory";
  if (item.status === "blocked") return "liquidity";
  if (item.status === "rejected") return "rejected";
  return item.status;
}

function statusHelp(item) {
  if (item.status === "profitable" && item.partial) return `${formatPercent(clampRatio(item.filledRatio))} liquidity`;
  if (item.status === "profitable") return "ready to execute";
  if (item.status === "blocked") return item.reason || "insufficient inventory or depth";
  return item.reason;
}

function decisionActionLabel(item) {
  const action = item?.decision?.action;
  if (action === "execute-partial") return "Execute partial";
  if (action === "execute-full") return "Execute full";
  if (action === "inventory-gate") return "Wait inventory";
  if (action === "liquidity-gate") return "Wait liquidity";
  if (action === "skip-costs") return "Skip";
  return statusLabel(item || {});
}

function decisionCaption(item) {
  if (!item) return "";
  if (item.status === "profitable" && item.partial) return "Profitable, with limited executable liquidity.";
  if (item.status === "profitable") return "Profitable after fees, slippage and latency.";
  if (item.status === "blocked") return "A hard inventory or depth gate blocked execution.";
  return "Total cost consumes the observed spread.";
}

function OpportunityTable({ opportunities, queue = {}, now }) {
  const visible = opportunities.filter((item) => item.status !== "blocked");
  const fallback = visible.length ? visible : opportunities;
  const rows = fallback.slice(0, 7);
  return (
    <section className="surface queue">
      <PanelTitle icon={Triangle} title="Priority Queue" pill={queue.paused ? "risk paused" : `${queue.executable || 0} executable`} />
      <div className="queueStats">
        <span><b>{queue.received || 0}</b> analyzed</span>
        <span><b>{queue.deduped || 0}</b> deduped</span>
        <span><b>{queue.executable || 0}</b> ready</span>
        <span><b>{queue.queued || 0}</b> ranked</span>
      </div>
      <div className="table">
        <div className="thead"><span>Route</span><span>Size</span><span>Net Profit</span><span>EV</span><span>Status</span></div>
        {rows.map((opportunity) => (
          <div className="tr" key={opportunity.id}>
            <span className="routeStack">
              <RouteLabel item={opportunity} />
              <small className={now - opportunity.time <= 1500 ? "liveStamp on" : "liveStamp"}><Clock3 size={12} /> seen {seenAge(opportunity, now)}</small>
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
              <b>{formatMoney(opportunity.expectedValue ?? opportunity.netProfit)}</b>
              <small>{formatNumber(opportunity.evBps ?? opportunity.netBps, 2)} bps / conf {formatNumber(opportunity.confidence, 2)}</small>
            </span>
            <span>
              <em className={`badge ${statusClass(opportunity)}`}>{statusLabel(opportunity)}</em>
              <small>{statusHelp(opportunity)}</small>
            </span>
          </div>
        ))}
        {!rows.length && <div className="tableEmpty">{queue.paused ? "Execution paused: Aurelion keeps reading the market, but does not generate new signals until risk clears." : "No ranked opportunities yet."}</div>}
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
        <PanelTitle icon={Radar} title="Current Decision" pill="waiting" />
        <div className="empty">No ranked routes yet</div>
      </section>
    );
  }
  const decision = item.decision || {};
  const breakdown = item.edgeBreakdown || {};
  return (
    <section className="surface edgePanel">
      <PanelTitle icon={Radar} title="Current Decision" pill={`grade ${decision.scoreGrade || "D"}`} />
      <div className="edgeBody">
        <div className={`decisionStamp ${statusClass(item)}`}>
          <b>{decisionActionLabel(item)}</b>
          <span>{decisionCaption(item)}</span>
        </div>
        <div className="edgeRoute">
          <RouteLabel item={item} />
          <small>{formatNumber(breakdown.netBps, 2)} bps net / {formatNumber(breakdown.costDragPct, 1)}% cost drag / {formatNumber(breakdown.latencyMs, 0)} ms</small>
          <div className="evStrip">
            <span>EV <b>{formatMoney(item.expectedValue ?? breakdown.expectedValue ?? item.netProfit)}</b></span>
            <span>capture <b>{formatPercent(item.latencyCaptureProbability ?? breakdown.latencyCaptureProbability ?? 0)}</b></span>
            <span>{formatNumber(item.evBps ?? breakdown.evBps ?? item.netBps, 2)} EV bps</span>
          </div>
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
        <PanelTitle icon={ArrowRightLeft} title="Real Costs" pill="no route" />
        <div className="empty">No route to review</div>
      </section>
    );
  }
  return (
    <section className="surface realityPanel">
      <PanelTitle icon={ArrowRightLeft} title="Real Costs" pill={reality.verdict} />
      <div className="realityGrid">
        <article>
          <span>Prefunded</span>
          <b className={reality.prefundedNetProfit >= 0 ? "green" : "red"}>{formatMoney(reality.prefundedNetProfit)}</b>
        </article>
        <article>
          <span>Settlement Net</span>
          <b className={reality.settlementNetProfit >= 0 ? "green" : "red"}>{formatMoney(reality.settlementNetProfit)}</b>
        </article>
        <article>
          <span>Extra Cost</span>
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
    ["all", "All"],
    ["live", "Now"],
    ["profitable", "Profitable"],
    ["rejected", "Rejected"],
    ["partial", "Partials"],
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
      <PanelTitle icon={ListChecks} title="Signal History" pill={`${rows.length} recent`} />
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
              <small>{formatMoney(item.netProfit)} net / EV {formatMoney(item.expectedValue ?? item.netProfit)}</small>
            </span>
            <span className="historyMeta">
              <em className={`badge ${statusClass(item)}`}>{statusLabel(item)}</em>
              <small className={now - item.time <= 1500 ? "liveStamp on" : "liveStamp"}><Clock3 size={12} /> seen {seenAge(item, now)}</small>
            </span>
          </article>
        ))}
        {!rows.length && <div className="empty">No signals match this filter</div>}
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
      <PanelTitle icon={DatabaseZap} title="Infrastructure" pill={redisLabel} />
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
      <PanelTitle icon={Globe2} title="Global Context" pill={globalMarket.status || "loading"} />
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
      <PanelTitle icon={Gauge} title="Speed" pill={slo.summary || "loading"} />
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
      <PanelTitle icon={Sparkles} title="Demo Quality" pill={mode === "demo" ? quality.label || "loading" : "observing"} />
      <div className="qualityDial">
        <b>{Math.round(quality.score || 0)}</b>
        <span>quality</span>
      </div>
      <div className="qualityStats">
        <span>{formatMoney(quality.pnlPerMinute || 0)} / min</span>
        <span>{formatNumber(quality.fillsPerMinute || 0, 2)} fills / min</span>
        <span>{formatPercent(quality.partialRate || 0)} partial</span>
      </div>
    </section>
  );
}

function ExchangeCoverage({ coverage = {}, quality = [], health = {}, control }) {
  const active = new Set((coverage.active || []).map((exchange) => exchange.id));
  const universe = coverage.universe || coverage.active || [];
  const qualityById = new Map((quality || []).map((venue) => [venue.exchangeId, venue]));
  const healthById = new Map((health.venues || []).map((venue) => [venue.exchangeId, venue]));
  const toggle = (exchange) => {
    const next = active.has(exchange.id)
      ? [...active].filter((id) => id !== exchange.id)
      : [...active, exchange.id];
    if (next.length < 2 || next.length > 5) return;
    control({ activeExchanges: next });
  };
  return (
    <section className="surface">
      <PanelTitle icon={Network} title="Exchanges" pill={`${coverage.activeCount || active.size} active / ${health.demotedCount || 0} demoted`} />
      <div className="coverageGrid">
        {universe.map((exchange) => {
          const venue = qualityById.get(exchange.id);
          const healthRow = healthById.get(exchange.id);
          const healthStatus = healthRow?.status || venue?.healthStatus || (active.has(exchange.id) ? "healthy" : "catalog");
          return (
            <button className={`${active.has(exchange.id) ? "active" : ""} ${healthStatus}`} disabled={!active.has(exchange.id) && active.size >= 5} key={exchange.id} onClick={() => toggle(exchange)} type="button">
              <b>{exchange.name}</b>
              <span>{venue ? `${healthStatus} / ${venue.latencyMs} ms / q ${venue.score}` : active.has(exchange.id) ? "speed profile" : "coverage catalog"}</span>
            </button>
          );
        })}
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
    const source = series.length ? series : [{ pnl: 0 }];
    const points = source.length === 1 ? [{ pnl: 0 }, source[0]] : source;
    const rawMin = Math.min(0, ...points.map((point) => point.pnl));
    const rawMax = Math.max(0, ...points.map((point) => point.pnl));
    const padding = Math.max(0.25, (rawMax - rawMin) * 0.2);
    const min = rawMin - padding;
    const max = rawMax + padding;
    const range = Math.max(0.5, max - min);
    const chartLeft = 46;
    const chartRight = rect.width - 12;
    const chartTop = 16;
    const chartBottom = rect.height - 24;
    const chartWidth = Math.max(1, chartRight - chartLeft);
    const chartHeight = Math.max(1, chartBottom - chartTop);
    const mapY = (pnl) => chartBottom - ((pnl - min) / range) * chartHeight;
    ctx.strokeStyle = "#dfe6da";
    ctx.fillStyle = "#66736d";
    ctx.font = "800 10px Aptos, Segoe UI, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = min + (range * tick) / 4;
      const y = mapY(value);
      ctx.beginPath();
      ctx.moveTo(chartLeft, y);
      ctx.lineTo(chartRight, y);
      ctx.stroke();
      ctx.fillText(formatMoney(value).replace(".00", ""), chartLeft - 8, y);
    }
    const zeroY = mapY(0);
    ctx.strokeStyle = "#c8d4c5";
    ctx.setLineDash([4, 5]);
    ctx.beginPath();
    ctx.moveTo(chartLeft, zeroY);
    ctx.lineTo(chartRight, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);
    const coords = points.map((point, index) => {
      const x = points.length === 1 ? chartRight : chartLeft + (index / (points.length - 1)) * chartWidth;
      return [x, mapY(point.pnl)];
    });
    ctx.beginPath();
    coords.forEach(([x, y], index) => {
      index === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.lineTo(chartRight, zeroY);
    ctx.lineTo(chartLeft, zeroY);
    ctx.closePath();
    ctx.fillStyle = "rgba(13, 125, 103, 0.12)";
    ctx.fill();
    ctx.strokeStyle = "#0d7d67";
    ctx.lineWidth = 3;
    ctx.beginPath();
    coords.forEach(([x, y], index) => {
      index === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    const [lastX, lastY] = coords[coords.length - 1];
    ctx.fillStyle = "#fbfcf8";
    ctx.strokeStyle = "#0d7d67";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = "#0d7d67";
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3.5, 0, Math.PI * 2);
    ctx.fill();
    const label = formatMoney(points[points.length - 1]?.pnl || 0);
    ctx.font = "900 11px Aptos, Segoe UI, sans-serif";
    ctx.textAlign = "left";
    ctx.fillStyle = "#12332c";
    const labelX = Math.min(lastX + 9, rect.width - 72);
    ctx.fillText(label, labelX, Math.max(17, lastY - 12));
    if (series.length === 0) {
      ctx.fillStyle = "#66736d";
      ctx.font = "700 12px Aptos, Segoe UI, sans-serif";
      ctx.textAlign = "left";
      ctx.fillText("Esperando el primer trade", chartLeft, rect.height - 13);
    }
  }, [series]);
  return <canvas className="chart" ref={ref} />;
}

function SystemStatus({ snapshot }) {
  const counts = streamCounts(snapshot.streams);
  const risk = snapshot.risk || {};
  const database = snapshot.database || {};
  const used = Number(risk.riskBudgetUsedUsd || 0);
  const limit = Number(risk.riskBudgetHourUsd || 0);
  const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
  return (
    <section className="surface systemStatus">
      <PanelTitle icon={DatabaseZap} title="System" pill={risk.paused ? "halted" : "armed"} />
      <div className="systemGrid">
        <article>
          <span>Market data</span>
          <b>{dataFeedLabel(snapshot.streams, snapshot.books, snapshot.exchangeCoverage)}</b>
          <small>{counts.total || snapshot.books.length} streams watched</small>
        </article>
        <article>
          <span>Audit trail</span>
          <b>{auditLabel(database, snapshot.redis)}</b>
          <small>{database.status || "local"} / {snapshot.redis?.enabled ? snapshot.redis.status : "SSE"}</small>
        </article>
        <article className="riskBudget">
          <span>Risk budget</span>
          <b>{formatMoney(used)} / {formatMoney(limit)}</b>
          <i style={{ "--fill": `${ratio * 100}%` }} />
        </article>
      </div>
    </section>
  );
}

function PnlBreakdown({ totals = {} }) {
  const exposure = totals.exposure || {};
  return (
    <div className="pnlBreakdown">
      <article>
        <span>Realized</span>
        <b className={(totals.realizedPnl || 0) >= 0 ? "green" : "red"}>{formatMoney(totals.realizedPnl)}</b>
      </article>
      <article>
        <span>Unrealized</span>
        <b className={(totals.unrealizedPnl || 0) >= 0 ? "green" : "red"}>{formatMoney(totals.unrealizedPnl)}</b>
      </article>
      <article>
        <span>BTC exposure</span>
        <b>{formatMoney(exposure.BTC?.usd || 0)}</b>
      </article>
      <article>
        <span>ETH exposure</span>
        <b>{formatMoney(exposure.ETH?.usd || 0)}</b>
      </article>
    </div>
  );
}

function SideRail({ snapshot, control }) {
  return (
    <aside className="sideRail">
      <section className="surface pnlCard">
        <PanelTitle icon={ChartNoAxesCombined} title="P&L" pill={formatMoney(snapshot.metrics.cumulativePnl)} />
        <PnlChart series={snapshot.pnlSeries} />
        <PnlBreakdown totals={snapshot.totals} />
      </section>
      <ExchangeCoverage coverage={snapshot.exchangeCoverage} quality={snapshot.venueQuality} health={snapshot.venueHealth} control={control} />
      <SystemStatus snapshot={snapshot} />
      <LatencySloPanel slo={snapshot.latencySlo} />
      <DemoQualityPanel quality={snapshot.demoQuality} mode={snapshot.mode} />
      <GlobalMarket globalMarket={snapshot.globalMarket || {}} />
    </aside>
  );
}

function fillTitle(item) {
  if (item.strategy === "triangular") return `${item.exchange} triangular cycle`;
  return `${item.buyExchange} -> ${item.sellExchange}`;
}

function executionKind(item) {
  if (item.strategy === "triangular" && item.partial) return "triangular partial";
  if (item.strategy === "triangular") return "triangular";
  if (item.partial) return "partial";
  return "complete";
}

function executionKindClass(item) {
  if (item.strategy === "triangular" && item.partial) return "triangular-partial";
  if (item.strategy === "triangular") return "triangular";
  if (item.partial) return "partial-fill";
  return "filled";
}

function Trades({ trades, metrics = {} }) {
  const [filter, setFilter] = React.useState("all");
  const filters = [
    ["all", "All"],
    ["partial", "Partials"],
    ["complete", "Complete"],
    ["triangular", "Triangular"],
  ];
  const visibleTrades = trades.filter((trade) => {
    if (filter === "partial") return trade.partial;
    if (filter === "complete") return !trade.partial;
    if (filter === "triangular") return trade.strategy === "triangular";
    return true;
  });
  return (
    <section className="surface trades">
      <PanelTitle icon={ArrowRightLeft} title="Executed Trades" pill={`${visibleTrades.length}/${trades.length} visible`} />
      <div className="tradeToolbar">
        {filters.map(([id, label]) => (
          <button className={filter === id ? "active" : ""} key={id} onClick={() => setFilter(id)} type="button">{label}</button>
        ))}
      </div>
      <div className="tradeList">
        {visibleTrades.map((trade) => (
          <article className={trade.partial ? "partialTrade" : ""} key={trade.id}>
            <div className="tradeTop">
              <b>{fillTitle(trade)}</b>
              <em className={`badge ${executionKindClass(trade)}`}>{executionKind(trade)}</em>
            </div>
            <span>{trade.strategy === "triangular" ? `${trade.cyclePath?.join(" -> ")} / ${formatMoney(trade.quoteIn)}` : formatBtc(trade.qtyBtc)}</span>
            <em className={trade.netProfit >= 0 ? "green" : "red"}>{formatMoney(trade.netProfit)}</em>
            <div className="tradeDetails">
              <small>{new Date(trade.time).toLocaleTimeString()}</small>
              <small>{formatNumber(trade.executionQuality?.edgeCaptureBps || trade.netBps, 2)} bps captured</small>
              <small>EV {formatMoney(trade.expectedValue ?? trade.netProfit)}</small>
              {trade.executionQuality?.adverseMoveBps > 0 && <small>latency move {formatNumber(trade.executionQuality.adverseMoveBps, 2)} bps</small>}
              {trade.strategy === "triangular" && <small>{trade.legs?.map((leg) => `${leg.from}->${leg.to}`).join(" / ")}</small>}
              {trade.partial && <small>{formatPercent(clampRatio(trade.filledRatio))} of target</small>}
              {!trade.partial && <small>100% of target</small>}
            </div>
          </article>
        ))}
        {!visibleTrades.length && <div className="empty">{trades.length ? "No trades match this filter" : "No executed trades yet"}</div>}
      </div>
    </section>
  );
}

function InfrastructurePanel({ snapshot }) {
  return (
    <div className="infraDeck">
      <Streams streams={snapshot.streams} redis={snapshot.redis} />
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
        <PanelTitle icon={ShieldAlert} title="Risk Timeline" pill={`${snapshot.riskEvents.length} events`} />
        <div className="events compactEvents">
          {snapshot.riskEvents.slice(0, 10).map((event) => (
            <article className="event" key={event.id || `${event.type}-${event.time}`}>
              <b>{event.condition || event.type}</b>
              <span>{event.reason || "market event"}</span>
              <small>{new Date(event.time).toLocaleTimeString()}</small>
            </article>
          ))}
          {!snapshot.riskEvents.length && <div className="empty">No risk events</div>}
        </div>
      </section>
    </div>
  );
}

function ResultsWorkbench({ snapshot }) {
  const [tab, setTab] = React.useState("opportunities");
  const tabs = [
    ["opportunities", "Opportunities", snapshot.queue?.queued || 0],
    ["trades", "Trades", snapshot.trades?.length || 0],
    ["signals", "Signals", snapshot.opportunityHistory?.length || snapshot.opportunities?.length || 0],
    ["infra", "Infra", snapshot.streams?.streams?.length || snapshot.books?.length || 0],
  ];
  return (
    <section className="workbench">
      <div className="workbenchTabs">
        {tabs.map(([id, label, count]) => (
          <button className={tab === id ? "active" : ""} key={id} onClick={() => setTab(id)} type="button">
            {label}<span>{count}</span>
          </button>
        ))}
      </div>
      {tab === "opportunities" && <OpportunityTable opportunities={snapshot.queuedOpportunities} queue={snapshot.queue} now={snapshot.now} />}
      {tab === "trades" && <Trades trades={snapshot.trades} metrics={snapshot.metrics} />}
      {tab === "signals" && <OpportunityHistory opportunities={snapshot.opportunityHistory || snapshot.opportunities} metrics={snapshot.metrics} now={snapshot.now} />}
      {tab === "infra" && <InfrastructurePanel snapshot={snapshot} />}
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
            <ResultsWorkbench snapshot={snapshot} />
          </div>
          <SideRail snapshot={snapshot} control={control} />
        </section>
      </main>
    </>
  );
}

createRoot(document.getElementById("root")).render(<App />);
