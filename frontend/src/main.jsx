import React from "react";
import { createRoot } from "react-dom/client";
import { Activity, ArrowRightLeft, ChartNoAxesCombined, CirclePause, DatabaseZap, Gauge, Globe2, Network, Power, Radar, RefreshCw, ShieldAlert, Sparkles, Split, Triangle } from "lucide-react";
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
  if (ms < 1000) return "now";
  if (ms < 60000) return `${Math.round(ms / 1000)}s`;
  return `${Math.round(ms / 60000)}m`;
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
        <button className="iconButton" title="Reset session" onClick={reset}><RefreshCw size={17} /></button>
      </div>
    </header>
  );
}

function Overview({ snapshot }) {
  const metrics = snapshot.metrics;
  const risk = snapshot.risk;
  const stateLabel = risk.paused ? "cooldown" : risk.autoExecution ? "armed" : "manual";
  const stateNote = risk.paused ? `${risk.reason} / ${ago(Math.max(0, risk.pausedUntil - snapshot.now))} left` : risk.reason;
  const freshness = metrics.avgFreshnessMs ?? metrics.avgLatencyMs;
  return (
    <section className="overview">
      <Metric icon={ChartNoAxesCombined} label="Realized P&L" value={formatMoney(metrics.cumulativePnl)} note={`${metrics.executedCount} fills`} tone={metrics.cumulativePnl >= 0 ? "good" : "bad"} />
      <Metric icon={ShieldAlert} label="State" value={stateLabel} note={stateNote} tone={risk.paused || !risk.autoExecution ? "bad" : "good"} />
      <Metric icon={Radar} label="Best Edge" value={`${formatNumber(metrics.bestNetBps, 2)} bps`} note={`${snapshot.queue.executable} executable after costs`} />
      <Metric icon={ArrowRightLeft} label="Detected" value={compact.format(metrics.detectedCount)} note={`${compact.format(metrics.triangularCount)} triangular`} />
      <Metric icon={Network} label="Books" value={`${metrics.liveBooks} WS / ${metrics.restBooks} REST`} note={`${metrics.simulatedBooks} demo`} />
      <Metric icon={Split} label="Partial Fills" value={compact.format(metrics.partialCount || 0)} note={`${metrics.blockedCount || 0} blocked`} />
      <Metric icon={Gauge} label="Book Age" value={`${Math.round(freshness)} ms`} note={`p95 ${Math.round(metrics.p95FreshnessMs || freshness)} ms / ${metrics.fastBooks || 0} fast`} tone={(metrics.staleBooks || 0) > 0 ? "bad" : "neutral"} />
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

function OpportunityTable({ opportunities, queue = {} }) {
  const rows = opportunities.slice(0, 18);
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
            <RouteLabel item={opportunity} />
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
              <em className={`badge ${opportunity.status}`}>{opportunity.status}</em>
              <small>{opportunity.partial ? "partial liquidity" : opportunity.status === "blocked" ? "size/depth gate" : opportunity.reason}</small>
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function Streams({ streams, redis }) {
  const rows = streams.streams || [];
  const redisLabel = redis.enabled ? redis.status : "optional off";
  return (
    <section className="surface streams">
      <PanelTitle icon={DatabaseZap} title="Infrastructure" pill={redisLabel} />
      <div className="streamList">
        {rows.slice(0, 12).map((stream) => (
          <article className="stream" key={stream.key}>
            <b>{stream.exchangeName}</b>
            <span>{stream.symbol}</span>
            <em className={stream.restFallback ? "rest" : "ws"}>{stream.mode}</em>
            <small>{stream.updates} updates / {stream.failures} failures</small>
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

function SideRail({ snapshot }) {
  return (
    <aside className="sideRail">
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
      <section className="surface">
        <PanelTitle icon={ChartNoAxesCombined} title="P&L" pill={formatMoney(snapshot.metrics.cumulativePnl)} />
        <PnlChart series={snapshot.pnlSeries} />
      </section>
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

function PartialFills({ trades, opportunities }) {
  const executed = trades.filter((trade) => trade.partial).slice(0, 4).map((trade) => ({ ...trade, visualType: "executed" }));
  const queued = opportunities.filter((opportunity) => opportunity.partial).slice(0, 4).map((opportunity) => ({ ...opportunity, visualType: "queued" }));
  const items = [...executed, ...queued].slice(0, 6);
  return (
    <section className="surface partials">
      <PanelTitle icon={Split} title="Partial Execution Watch" pill={`${executed.length} fills / ${queued.length} queued`} />
      <div className="partialList">
        {items.map((item) => {
          const ratio = clampRatio(item.filledRatio ?? (item.partial ? 0.72 : 1));
          return (
            <article key={`${item.visualType}-${item.id}`}>
              <div className="partialHead">
                <b>{fillTitle(item)}</b>
                <em className={`badge ${item.status}`}>{item.visualType}</em>
              </div>
              <span>{fillSubtitle(item)}</span>
              <div className="fillMeter" style={{ "--fill": `${ratio * 100}%` }}><i /></div>
              <small>{formatPercent(ratio)} of target liquidity captured</small>
            </article>
          );
        })}
        {!items.length && <div className="empty">No partial fills yet</div>}
      </div>
    </section>
  );
}

function Trades({ trades }) {
  return (
    <section className="surface trades">
      <PanelTitle icon={ArrowRightLeft} title="Executed Fills" pill={`${trades.length} recent`} />
      <div className="tradeList">
        {trades.slice(0, 8).map((trade) => (
          <article className={trade.partial ? "partialTrade" : ""} key={trade.id}>
            <b>{fillTitle(trade)}</b>
            <span>{trade.strategy === "triangular" ? `${trade.cyclePath?.join(" -> ")} / ${formatMoney(trade.quoteIn)}` : formatBtc(trade.qtyBtc)}</span>
            <em className={trade.netProfit >= 0 ? "green" : "red"}>{formatMoney(trade.netProfit)}</em>
            <div className="tradeDetails">
              <small>{trade.status} / {formatNumber(trade.executionQuality?.edgeCaptureBps || trade.netBps, 2)} bps captured</small>
              {trade.strategy === "triangular" && <small>{trade.legs?.map((leg) => `${leg.from}->${leg.to}`).join(" / ")}</small>}
              {trade.partial && <small>{formatPercent(clampRatio(trade.filledRatio))} target fill</small>}
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
            <OpportunityTable opportunities={snapshot.queuedOpportunities} queue={snapshot.queue} />
            <PartialFills trades={snapshot.trades} opportunities={snapshot.queuedOpportunities} />
            <Trades trades={snapshot.trades} />
          </div>
          <SideRail snapshot={snapshot} />
        </section>
        <Streams streams={snapshot.streams} redis={snapshot.redis} />
      </main>
    </>
  );
}

createRoot(document.getElementById("root")).render(<App />);
