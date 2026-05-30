import React from "react";
import { createRoot } from "react-dom/client";
import { Activity, ArrowRightLeft, ChartNoAxesCombined, CirclePause, Clock3, DatabaseZap, Gauge, Globe2, ListChecks, Network, Power, Radar, RefreshCw, ShieldAlert, Sparkles, Split, Triangle, Zap } from "lucide-react";
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

  return { snapshot, connected, control, reset };
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

function Header({ snapshot, connected, control, reset }) {
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
        <span className={`conn ${connected ? "online" : "offline"}`}><i />{connected ? "streaming" : "syncing"}</span>
        <div className="segmented">
          {["auto", "live", "demo"].map((mode) => (
            <button key={mode} className={snapshot?.mode === mode ? "active" : ""} onClick={() => control({ mode })}>{mode}</button>
          ))}
        </div>
        <button className={`toggle ${risk?.autoExecution ? "on" : ""}`} onClick={() => control({ autoExecution: !risk?.autoExecution })}>
          {risk?.autoExecution ? <Power size={16} /> : <CirclePause size={16} />}
          {risk?.autoExecution ? "armed" : "paused"}
        </button>
        <button className="stressButton" title="Simulate volatility circuit breaker" onClick={() => control({ volatilityShock: true })}>
          <Zap size={16} />
          stress
        </button>
        <button className="iconButton" title="Reset session" onClick={reset}><RefreshCw size={17} /></button>
      </div>
    </header>
  );
}

function Overview({ snapshot }) {
  const metrics = snapshot.metrics;
  const risk = snapshot.risk;
  const stateLabel = risk.paused ? "circuit breaker" : risk.autoExecution ? "armed" : "manual stop";
  const condition = risk.condition && risk.condition !== "healthy" ? risk.condition : "healthy";
  const stateNote = risk.paused
    ? `${condition} / ${risk.reason} / resumes in ${ago(risk.cooldownRemainingMs ?? risk.pausedUntil - snapshot.now)}`
    : risk.reason;
  const freshness = Math.max(0, metrics.avgFreshnessMs ?? metrics.avgLatencyMs);
  const bestEdge = metrics.bestNetBps > 0 ? `${formatNumber(metrics.bestNetBps, 2)} bps` : "No edge";
  const observed = metrics.bestNetBps > 0
    ? `${snapshot.queue.executable} executable`
    : metrics.bestObservedNetBps < 0
      ? `closest miss ${formatNumber(Math.abs(metrics.bestObservedNetBps), 2)} bps short`
      : "waiting for complete books";
  return (
    <section className="overview">
      <Metric icon={ChartNoAxesCombined} label="Realized P&L" value={formatMoney(metrics.cumulativePnl)} note={`${metrics.executedCount} fills`} tone={metrics.cumulativePnl >= 0 ? "good" : "bad"} />
      <Metric icon={ShieldAlert} label="State" value={stateLabel} note={stateNote} tone={risk.paused || !risk.autoExecution ? "bad" : "good"} />
      <Metric icon={Radar} label="Best Edge" value={bestEdge} note={observed} />
      <Metric icon={ArrowRightLeft} label="Detected" value={compact.format(metrics.detectedCount)} note={`${metrics.liveSignalCount || 0} live now / ${compact.format(metrics.triangularCount)} triangular`} />
      <Metric icon={Network} label="Books" value={`${metrics.liveBooks} WS / ${metrics.restBooks} REST`} note={`${metrics.simulatedBooks} demo`} />
      <Metric icon={Split} label="Partial Fills" value={compact.format(metrics.partialCount || 0)} note={`${metrics.partialQueuedCount || 0} queued candidates`} />
      <Metric icon={Gauge} label="Book Age" value={`${Math.round(freshness)} ms`} note={`p95 ${Math.max(0, Math.round(metrics.p95FreshnessMs || freshness))} ms / ${metrics.fastBooks || 0} fast`} tone={(metrics.staleBooks || 0) > 0 ? "bad" : "neutral"} />
    </section>
  );
}

function Books({ books }) {
  return (
    <section className="surface books">
      <PanelTitle icon={Activity} title="Market Books" pill={`${books.length} venues`} />
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
  if (item.status === "profitable") return "profitable full";
  if (item.status === "blocked" && `${item.reason}`.toLowerCase().includes("wallet")) return "inventory gate";
  if (item.status === "blocked") return "liquidity gate";
  return item.status;
}

function statusHelp(item) {
  if (item.status === "profitable" && item.partial) return `${formatPercent(clampRatio(item.filledRatio))} target liquidity`;
  if (item.status === "profitable") return "full target executable";
  if (item.status === "blocked") return item.reason || "insufficient inventory/depth";
  return item.reason;
}

function OpportunityTable({ opportunities, queue = {}, now }) {
  const visible = opportunities.filter((item) => item.status !== "blocked");
  const fallback = visible.length ? visible : opportunities;
  const rows = fallback.slice(0, 10);
  return (
    <section className="surface queue">
      <PanelTitle icon={Triangle} title="Priority Queue" pill={`${rows.length} routes`} />
      <div className="queueStats">
        <span><b>{queue.received || 0}</b> scanned</span>
        <span><b>{queue.deduped || 0}</b> deduped</span>
        <span><b>{queue.executable || 0}</b> executable</span>
        <span><b>{queue.queued || 0}</b> ranked</span>
      </div>
      <div className="table">
        <div className="thead"><span>Route</span><span>Size</span><span>Net</span><span>Score</span><span>Status</span></div>
        {rows.map((opportunity) => (
          <div className="tr" key={opportunity.id}>
            <span className="routeStack">
              <RouteLabel item={opportunity} />
              <small className={now - opportunity.time <= 1500 ? "liveStamp on" : "liveStamp"}><Clock3 size={12} /> detected {signalAge(opportunity, now)} ago</small>
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

function OpportunityHistory({ opportunities = [], metrics = {}, now }) {
  const [filter, setFilter] = React.useState("all");
  const filtered = opportunities.filter((item) => {
    if (filter === "all") return true;
    if (filter === "live") return now - item.time <= 2500;
    if (filter === "partial") return item.partial;
    return item.status === filter || item.strategy === filter;
  });
  const rows = filtered.slice(0, 32);
  return (
    <section className="surface history">
      <PanelTitle icon={ListChecks} title="Detection Tape" pill={`${rows.length} shown / ${compact.format(metrics.historyRetainedCount || opportunities.length)} retained`} />
      <div className="historyToolbar">
        {["all", "live", "profitable", "rejected", "partial", "triangular"].map((item) => (
          <button className={filter === item ? "active" : ""} key={item} onClick={() => setFilter(item)} type="button">{item}</button>
        ))}
      </div>
      <div className="historyList">
        {rows.map((item) => (
          <article className={`historyItem ${statusClass(item)}`} key={`hist-${item.id}`}>
            <RouteLabel item={item} />
            <span className="historyEdge">
              <b className={item.netProfit >= 0 ? "green" : "red"}>{formatNumber(item.netBps, 2)} bps</b>
              <small>{formatMoney(item.netProfit)} net / score {formatNumber(item.score, 2)}</small>
            </span>
            <span className="historyMeta">
              <em className={`badge ${statusClass(item)}`}>{statusLabel(item)}</em>
              <small className={now - item.time <= 1500 ? "liveStamp on" : "liveStamp"}><Clock3 size={12} /> {signalAge(item, now)} ago</small>
            </span>
          </article>
        ))}
        {!rows.length && <div className="empty">No matching signals</div>}
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
      <PanelTitle icon={Globe2} title="Global Market" pill={globalMarket.status || "warming"} />
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

function ExchangeCoverage({ coverage = {}, control }) {
  const active = new Set((coverage.active || []).map((exchange) => exchange.id));
  const universe = coverage.universe || coverage.active || [];
  const toggle = (exchange) => {
    const next = active.has(exchange.id)
      ? [...active].filter((id) => id !== exchange.id)
      : [...active, exchange.id];
    if (next.length < 2 || next.length > 5) return;
    control({ activeExchanges: next });
  };
  return (
    <section className="surface">
      <PanelTitle icon={Network} title="Exchange Coverage" pill={`${coverage.activeCount || active.size}/${coverage.universeCount || universe.length} active`} />
      <div className="coverageGrid">
        {universe.map((exchange) => (
          <button className={active.has(exchange.id) ? "active" : ""} disabled={!active.has(exchange.id) && active.size >= 5} key={exchange.id} onClick={() => toggle(exchange)} type="button">
            <b>{exchange.name}</b>
            <span>{active.has(exchange.id) ? "speed profile" : "coverage catalog"}</span>
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
      <ExchangeCoverage coverage={snapshot.exchangeCoverage} control={control} />
      <GlobalMarket globalMarket={snapshot.globalMarket || {}} />
      <section className="surface">
        <PanelTitle icon={DatabaseZap} title="Inventory" pill={formatMoney(snapshot.totals.markToMarket)} />
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
        <div className="events">
          {snapshot.riskEvents.slice(0, 8).map((event) => (
            <article className="event" key={event.id || `${event.type}-${event.time}`}>
              <b>{event.condition || event.type}</b>
              <span>{event.reason || "market event"}</span>
              <small>{new Date(event.time).toLocaleTimeString()}</small>
            </article>
          ))}
          {!snapshot.riskEvents.length && <div className="empty">No risk events</div>}
        </div>
      </section>
    </aside>
  );
}

function fillTitle(item) {
  if (item.strategy === "triangular") return `${item.exchange} triangular cycle`;
  return `${item.buyExchange} -> ${item.sellExchange}`;
}

function fillSubtitle(item) {
  if (item.strategy === "triangular") {
    const path = item.cyclePath?.join(" -> ") || item.product;
    return `${path} / ${formatMoney(item.quoteIn || 0)} in`;
  }
  return `${formatBtc(item.qtyBtc)} filled / ${formatMoney(item.buyPrice)} to ${formatMoney(item.sellPrice)}`;
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

function PartialFills({ trades, opportunities }) {
  const executed = trades.filter((trade) => trade.partial).slice(0, 5).map((trade) => ({ ...trade, visualType: "executed" }));
  const queued = opportunities.filter((opportunity) => opportunity.partial).slice(0, 5).map((opportunity) => ({ ...opportunity, visualType: "candidate" }));
  const items = [...executed, ...queued].slice(0, 6);
  return (
    <section className="surface partials">
      <PanelTitle icon={Split} title="Partial Execution Watch" pill={`${executed.length} fills / ${queued.length} candidates`} />
      <div className="partialList">
        {items.map((item) => {
          const ratio = clampRatio(item.filledRatio ?? (item.partial ? 0.72 : 1));
          return (
            <article key={`${item.visualType}-${item.id}`}>
              <div className="partialHead">
                <b>{fillTitle(item)}</b>
                <em className={`badge ${item.visualType === "executed" ? executionKindClass(item) : statusClass(item)}`}>{item.visualType === "executed" ? executionKind(item) : "candidate"}</em>
              </div>
              <span>{fillSubtitle(item)}</span>
              <div className="fillMeter" style={{ "--fill": `${ratio * 100}%` }}><i /></div>
              <small>{formatPercent(ratio)} captured / target {item.strategy === "triangular" ? formatMoney(item.targetQuote || item.quoteIn) : formatBtc(item.targetQtyBtc || item.qtyBtc)}</small>
            </article>
          );
        })}
        {!items.length && <div className="empty">No partial fills yet</div>}
      </div>
    </section>
  );
}

function Trades({ trades, metrics = {} }) {
  return (
    <section className="surface trades">
      <PanelTitle icon={ArrowRightLeft} title="Executed Fills" pill={`${trades.length}/${metrics.tradeRetainedCount || trades.length} recent`} />
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
              <small>{formatNumber(trade.executionQuality?.edgeCaptureBps || trade.netBps, 2)} bps captured</small>
              {trade.strategy === "triangular" && <small>{trade.legs?.map((leg) => `${leg.from}->${leg.to}`).join(" / ")}</small>}
              {trade.partial && <small>{formatPercent(clampRatio(trade.filledRatio))} target fill</small>}
              {!trade.partial && <small>100% target fill</small>}
            </div>
          </article>
        ))}
        {!trades.length && <div className="empty">No fills yet</div>}
      </div>
    </section>
  );
}

function App() {
  const { snapshot, connected, control, reset } = useAurelion();
  if (!snapshot) {
    return <main className="loading"><div className="sigil"><Sparkles size={24} /></div><span>Starting Aurelion</span></main>;
  }
  return (
    <>
      <Header snapshot={snapshot} connected={connected} control={control} reset={reset} />
      <main className="layout">
        <Overview snapshot={snapshot} />
        <section className="mainGrid">
          <div className="primary">
            <Books books={snapshot.books} />
            <OpportunityTable opportunities={snapshot.queuedOpportunities} queue={snapshot.queue} now={snapshot.now} />
            <Trades trades={snapshot.trades} metrics={snapshot.metrics} />
            <PartialFills trades={snapshot.trades} opportunities={snapshot.queuedOpportunities} />
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
