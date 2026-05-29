const state = {
  snapshot: null,
  connected: false,
  priceHistory: new Map(),
  spreadHistory: [],
  pollTimer: null
};

const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2
});

const number = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 2
});

const btc = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 4,
  maximumFractionDigits: 6
});

const $ = (selector) => document.querySelector(selector);

function formatCurrency(value) {
  return currency.format(Number(value) || 0);
}

function formatNumber(value, digits = 2) {
  return Number(value || 0).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

function formatBtc(value) {
  return `${btc.format(Number(value) || 0)} BTC`;
}

function compactMoney(value) {
  const amount = Number(value) || 0;
  if (Math.abs(amount) >= 1000000) return `$${formatNumber(amount / 1000000, 2)}M`;
  if (Math.abs(amount) >= 1000) return `$${formatNumber(amount / 1000, 1)}K`;
  return formatCurrency(amount);
}

function ago(ms) {
  if (ms < 1000) return "now";
  if (ms < 60000) return `${Math.round(ms / 1000)}s`;
  return `${Math.round(ms / 60000)}m`;
}

function setConnection(status) {
  state.connected = status === "online";
  const node = $("#connectionStatus");
  node.className = `connection ${status}`;
  node.querySelector("span:last-child").textContent =
    status === "online" ? "Live stream" : status === "offline" ? "Reconnecting" : "Connecting";
}

function rememberHistory(snapshot) {
  for (const book of snapshot.books) {
    const mid = (book.bestAsk + book.bestBid) / 2;
    if (!state.priceHistory.has(book.exchangeId)) state.priceHistory.set(book.exchangeId, []);
    const history = state.priceHistory.get(book.exchangeId);
    history.push({ time: snapshot.now, mid });
    if (history.length > 80) history.shift();
  }

  const bestNet = snapshot.opportunities.reduce((max, opportunity) => {
    return Math.max(max, Number(opportunity.netBps) || 0);
  }, 0);
  state.spreadHistory.push({ time: snapshot.now, bestNet });
  if (state.spreadHistory.length > 80) state.spreadHistory.shift();
}

function render(snapshot) {
  state.snapshot = snapshot;
  rememberHistory(snapshot);
  renderControls(snapshot);
  renderMetrics(snapshot);
  renderBooks(snapshot);
  renderOpportunities(snapshot);
  renderStreams(snapshot);
  renderWallets(snapshot);
  renderTrades(snapshot);
  renderEvents(snapshot);
  renderCharts(snapshot);
}

function renderControls(snapshot) {
  $("#modePill").textContent = snapshot.mode.toUpperCase();
  $("#executionToggle").checked = snapshot.risk.autoExecution;
  $("#riskPill").textContent = snapshot.risk.paused ? "Paused" : snapshot.risk.reason;
  $("#riskPill").className = `pill ${snapshot.risk.paused ? "negative" : "positive"}`;
  $("#latencyPill").textContent = `${Math.round(snapshot.metrics.avgLatencyMs)} ms avg`;

  document.querySelectorAll(".mode-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === snapshot.mode);
  });
}

function renderMetrics(snapshot) {
  const metrics = [
    ["Realized P&L", formatCurrency(snapshot.metrics.cumulativePnl), `${snapshot.metrics.executedCount} executed`],
    ["Detected", number.format(snapshot.metrics.detectedCount), `${snapshot.metrics.rejectedCount} rejected`],
    ["Win Rate", `${Math.round(snapshot.metrics.winRate * 100)}%`, "simulation fills"],
    ["Best Net Edge", `${formatNumber(snapshot.metrics.bestNetBps, 2)} bps`, "after all costs"],
    ["Queue", `${snapshot.queue?.queued || 0} ranked`, `${snapshot.queue?.deduped || 0} deduped`],
    ["Books", `${snapshot.metrics.liveBooks} WS / ${snapshot.metrics.restBooks || 0} REST`, `${snapshot.metrics.simulatedBooks} demo`],
    ["Strategies", `${snapshot.metrics.triangularCount} triangular`, `${snapshot.metrics.simpleCount} cross-exchange`]
  ];

  $("#metrics").innerHTML = metrics.map(([label, value, note]) => `
    <article class="metric-card">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${value}</div>
      <div class="metric-note">${note}</div>
    </article>
  `).join("");
}

function renderBooks(snapshot) {
  $("#marketSubhead").textContent = `${snapshot.books.length} venues updated ${new Date(snapshot.now).toLocaleTimeString()}`;
  $("#exchangeGrid").innerHTML = snapshot.books.map((book) => `
    <article class="exchange-card ${book.source}">
      <div class="exchange-head">
        <div>
          <div class="exchange-name">${book.exchangeName}</div>
          <div class="exchange-product">${book.product}</div>
        </div>
        <span class="source-tag">${book.source}</span>
      </div>
      <div class="book-values">
        <div class="book-row">
          <span>Bid</span>
          <strong class="bid">${formatCurrency(book.bestBid)}</strong>
        </div>
        <div class="book-row">
          <span>Ask</span>
          <strong class="ask">${formatCurrency(book.bestAsk)}</strong>
        </div>
        <div class="book-row">
          <span>Spread</span>
          <strong>${formatCurrency(Math.abs(book.spread))}</strong>
        </div>
      </div>
      <div class="mini-grid">
        <div class="mini-box">
          <div class="mini-label">Depth bid</div>
          <div class="mini-value">${formatBtc(book.depthBid)}</div>
        </div>
        <div class="mini-box">
          <div class="mini-label">Latency</div>
          <div class="mini-value">${Math.round(book.latencyMs)} ms</div>
        </div>
        <div class="mini-box">
          <div class="mini-label">Fee</div>
          <div class="mini-value">${formatNumber(book.feeBps, 1)} bps</div>
        </div>
        <div class="mini-box">
          <div class="mini-label">Age</div>
          <div class="mini-value">${ago(book.ageMs)}</div>
        </div>
      </div>
    </article>
  `).join("");
}

function renderOpportunities(snapshot) {
  const opportunities = snapshot.opportunities.slice(0, 30);
  $("#opportunityCount").textContent = `${snapshot.metrics.detectedCount} detected`;

  if (!opportunities.length) {
    $("#opportunityTable").innerHTML = `<tr><td colspan="7"><div class="empty">Waiting for a cross-exchange spread</div></td></tr>`;
    return;
  }

  $("#opportunityTable").innerHTML = opportunities.map((opportunity) => `
    <tr>
      <td>
        <div class="route">
          ${routeTitle(opportunity)}
          <small>${routeSubtitle(opportunity)}</small>
        </div>
      </td>
      <td>${opportunity.strategy === "triangular" ? formatCurrency(opportunity.quoteIn) : formatBtc(opportunity.qtyBtc)}<br><span class="subtle">${opportunity.partial ? "partial depth" : opportunity.strategy || "simple"}</span></td>
      <td>${formatCurrency(opportunity.grossProfit)}<br><span class="subtle">${formatNumber(opportunity.grossBps, 2)} bps</span></td>
      <td>${formatCurrency(opportunity.costs?.totalCosts || 0)}<br><span class="subtle">fee/slip/latency</span></td>
      <td class="${opportunity.netProfit >= 0 ? "positive-text" : "negative-text"}">
        ${formatCurrency(opportunity.netProfit)}<br><span class="subtle">${formatNumber(opportunity.netBps, 2)} bps</span>
      </td>
      <td>${formatNumber(opportunity.score, 3)}<br><span class="subtle">${Math.round((opportunity.confidence || 0) * 100)}% conf</span></td>
      <td><span class="status ${opportunity.status}">${opportunity.status}</span></td>
    </tr>
  `).join("");
}

function routeTitle(opportunity) {
  if (opportunity.strategy === "triangular") {
    return `${opportunity.exchange} triangle`;
  }
  return `${opportunity.buyExchange} -> ${opportunity.sellExchange}`;
}

function routeSubtitle(opportunity) {
  if (opportunity.strategy === "triangular") {
    return opportunity.cyclePath?.join(" -> ") || opportunity.product;
  }
  return `Buy ${formatCurrency(opportunity.buyPrice)} / sell ${formatCurrency(opportunity.sellPrice)}`;
}

function renderStreams(snapshot) {
  const streams = snapshot.streams?.streams || [];
  $("#streamPill").textContent = `${streams.length} streams`;
  if (!streams.length) {
    $("#streamGrid").innerHTML = `<div class="empty">${snapshot.streams?.unavailableReason || "No streams yet"}</div>`;
    return;
  }

  $("#streamGrid").innerHTML = streams.slice(0, 24).map((stream) => `
    <article class="stream-item">
      <div class="stream-top">
        <div>
          <div class="stream-name">${stream.exchangeName}</div>
          <div class="stream-meta">${stream.symbol}</div>
        </div>
        <span class="status ${stream.restFallback ? "rejected" : "profitable"}">${stream.mode}</span>
      </div>
      <div class="stream-meta">${stream.updates} updates / ${stream.failures} failures / ${stream.lastUpdate ? ago(Date.now() - stream.lastUpdate) : "no data"}</div>
      ${stream.lastError ? `<div class="stream-meta">${stream.lastError}</div>` : ""}
    </article>
  `).join("");
}

function renderWallets(snapshot) {
  const totals = snapshot.totals;
  $("#walletTotal").textContent = compactMoney(totals.markToMarket);
  const maxQuote = Math.max(...snapshot.wallets.map((wallet) => wallet.USDT), 1);
  const maxBase = Math.max(...snapshot.wallets.map((wallet) => wallet.BTC), 1);
  const maxEth = Math.max(...snapshot.wallets.map((wallet) => wallet.ETH || 0), 1);

  $("#walletList").innerHTML = snapshot.wallets.map((wallet) => `
    <div class="wallet-row">
      <div>
        <div class="wallet-title">${wallet.exchangeName}</div>
        <div class="subtle">${formatCurrency(wallet.USDT + wallet.BTC * averageMid(snapshot))}</div>
      </div>
      <div class="wallet-bars">
        <div class="bar">
          <span>USDT</span>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, (wallet.USDT / maxQuote) * 100)}%"></div></div>
          <span>${compactMoney(wallet.USDT)}</span>
        </div>
        <div class="bar">
          <span>BTC</span>
          <div class="bar-track"><div class="bar-fill btc" style="width:${Math.max(2, (wallet.BTC / maxBase) * 100)}%"></div></div>
          <span>${formatBtc(wallet.BTC)}</span>
        </div>
        <div class="bar">
          <span>ETH</span>
          <div class="bar-track"><div class="bar-fill btc" style="width:${Math.max(2, ((wallet.ETH || 0) / maxEth) * 100)}%"></div></div>
          <span>${formatNumber(wallet.ETH || 0, 4)} ETH</span>
        </div>
      </div>
    </div>
  `).join("");
}

function renderTrades(snapshot) {
  $("#tradeCount").textContent = `${snapshot.metrics.executedCount} trades`;
  const trades = snapshot.trades.slice(0, 18);
  if (!trades.length) {
    $("#tradeFeed").innerHTML = `<div class="empty">No simulated execution yet</div>`;
    return;
  }

  $("#tradeFeed").innerHTML = trades.map((trade) => `
    <article class="trade-item">
      <div class="trade-top">
        <div>
          <div class="trade-route">${tradeTitle(trade)}</div>
          <div class="subtle">${new Date(trade.time).toLocaleTimeString()} / ${trade.source}</div>
        </div>
        <span class="status ${trade.status}">${trade.status}</span>
      </div>
      <div class="trade-meta">
        <div class="trade-stat"><span>Qty</span><strong>${trade.strategy === "triangular" ? formatCurrency(trade.quoteIn) : formatBtc(trade.qtyBtc)}</strong></div>
        <div class="trade-stat"><span>Costs</span><strong>${formatCurrency(trade.totalCosts)}</strong></div>
        <div class="trade-stat"><span>Net</span><strong class="${trade.netProfit >= 0 ? "positive-text" : "negative-text"}">${formatCurrency(trade.netProfit)}</strong></div>
      </div>
    </article>
  `).join("");
}

function tradeTitle(trade) {
  if (trade.strategy === "triangular") {
    return `${trade.exchange} ${trade.cyclePath?.join(" -> ") || trade.product}`;
  }
  return `${trade.buyExchange} -> ${trade.sellExchange}`;
}

function renderEvents(snapshot) {
  const events = snapshot.riskEvents || [];
  $("#eventCount").textContent = `${events.length} events`;
  if (!events.length) {
    $("#eventFeed").innerHTML = `<div class="empty">No risk events</div>`;
    return;
  }

  $("#eventFeed").innerHTML = events.slice(0, 20).map((event) => `
    <article class="event-item">
      <div class="event-top">
        <div class="event-title">${event.type}</div>
        <span class="status ${event.type === "circuit-breaker" ? "blocked" : "rejected"}">${new Date(event.time).toLocaleTimeString()}</span>
      </div>
      <div class="event-meta">${event.reason || event.exchange || "Market event"}</div>
    </article>
  `).join("");
}

function averageMid(snapshot) {
  if (!snapshot.books.length) return 0;
  return snapshot.books.reduce((sum, book) => sum + (book.bestAsk + book.bestBid) / 2, 0) / snapshot.books.length;
}

function setupCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width: rect.width, height: rect.height };
}

function drawGrid(ctx, width, height) {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#fbfcfa";
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "#e3e8df";
  ctx.lineWidth = 1;
  for (let y = 28; y < height; y += 42) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
}

function renderCharts(snapshot) {
  drawPnl(snapshot);
  drawSpread(snapshot);
}

function drawPnl(snapshot) {
  const canvas = $("#pnlCanvas");
  const { ctx, width, height } = setupCanvas(canvas);
  drawGrid(ctx, width, height);
  const series = snapshot.pnlSeries.length
    ? snapshot.pnlSeries
    : [{ time: snapshot.now, pnl: 0 }, { time: snapshot.now, pnl: snapshot.metrics.cumulativePnl }];
  const values = series.map((point) => point.pnl);
  const min = Math.min(0, ...values);
  const max = Math.max(1, ...values);
  const range = Math.max(1, max - min);

  ctx.strokeStyle = "#08795f";
  ctx.lineWidth = 3;
  ctx.beginPath();
  series.forEach((point, index) => {
    const x = series.length === 1 ? 0 : (index / (series.length - 1)) * width;
    const y = height - 24 - ((point.pnl - min) / range) * (height - 48);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  const zeroY = height - 24 - ((0 - min) / range) * (height - 48);
  ctx.strokeStyle = "#bb4a3f";
  ctx.setLineDash([5, 6]);
  ctx.beginPath();
  ctx.moveTo(0, zeroY);
  ctx.lineTo(width, zeroY);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = "#121816";
  ctx.font = "700 13px system-ui";
  ctx.fillText(`Realized: ${formatCurrency(snapshot.metrics.cumulativePnl)}`, 14, 22);
}

function drawSpread(snapshot) {
  const canvas = $("#spreadCanvas");
  const { ctx, width, height } = setupCanvas(canvas);
  drawGrid(ctx, width, height);
  const books = snapshot.books;
  if (!books.length) return;
  const cellW = width / books.length;
  const maxSpread = Math.max(1, ...snapshot.opportunities.slice(0, 20).map((opp) => Math.max(0, opp.grossBps || 0)));

  books.forEach((buyBook, column) => {
    books.forEach((sellBook, row) => {
      const x = column * cellW + 5;
      const rowH = (height - 36) / books.length;
      const y = 30 + row * rowH;
      const rawBps = buyBook.exchangeId === sellBook.exchangeId
        ? 0
        : ((sellBook.bestBid - buyBook.bestAsk) / buyBook.bestAsk) * 10000;
      const intensity = Math.max(0, Math.min(1, rawBps / maxSpread));
      const hue = rawBps > 0 ? "8, 121, 95" : "187, 74, 63";
      ctx.fillStyle = `rgba(${hue}, ${0.1 + intensity * 0.72})`;
      ctx.fillRect(x, y, cellW - 10, rowH - 6);
      ctx.fillStyle = rawBps > 0 ? "#063f34" : "#6d2c27";
      ctx.font = "700 11px system-ui";
      ctx.fillText(`${formatNumber(rawBps, 1)} bps`, x + 7, y + 18);
    });
    ctx.fillStyle = "#65716c";
    ctx.font = "700 11px system-ui";
    ctx.fillText(buyBook.exchangeName.slice(0, 9), column * cellW + 8, 18);
  });
}

async function control(payload) {
  const response = await fetch("/api/control", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload)
  });
  render(await response.json());
}

async function reset() {
  const response = await fetch("/api/reset", { method: "POST" });
  state.priceHistory.clear();
  state.spreadHistory = [];
  render(await response.json());
}

function connectStream() {
  setConnection("connecting");
  const events = new EventSource("/events");

  events.addEventListener("open", () => {
    setConnection("online");
    if (state.pollTimer) clearInterval(state.pollTimer);
  });

  events.addEventListener("snapshot", (event) => {
    setConnection("online");
    render(JSON.parse(event.data));
  });

  events.addEventListener("error", () => {
    setConnection("offline");
    events.close();
    startPollingFallback();
    setTimeout(connectStream, 2500);
  });
}

function startPollingFallback() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(async () => {
    const response = await fetch("/api/snapshot");
    render(await response.json());
  }, 1800);
}

function bindControls() {
  document.querySelectorAll(".mode-button").forEach((button) => {
    button.addEventListener("click", () => control({ mode: button.dataset.mode }));
  });

  $("#executionToggle").addEventListener("change", (event) => {
    control({ autoExecution: event.target.checked });
  });

  $("#resetButton").addEventListener("click", reset);
  window.addEventListener("resize", () => {
    if (state.snapshot) renderCharts(state.snapshot);
  });
}

bindControls();
connectStream();
