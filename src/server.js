import http from "node:http";
import { createReadStream, existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CONFIG } from "./config.js";
import { MarketDataEngine } from "./engine/marketDataEngine.js";
import { StreamHub } from "./utils/streamHub.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, "..");
const publicDir = path.join(rootDir, "public");
const engine = new MarketDataEngine(CONFIG);
const streams = new StreamHub();

const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon"
};

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store"
  });
  response.end(JSON.stringify(payload));
}

async function parseBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  if (!chunks.length) return {};
  const text = Buffer.concat(chunks).toString("utf8");
  return text ? JSON.parse(text) : {};
}

async function serveStatic(request, response) {
  const requestUrl = new URL(request.url, `http://${request.headers.host}`);
  const pathname = requestUrl.pathname === "/" ? "/index.html" : requestUrl.pathname;
  const safePath = path.normalize(decodeURIComponent(pathname)).replace(/^(\.\.[/\\])+/, "");
  const filePath = path.join(publicDir, safePath);

  if (!filePath.startsWith(publicDir) || !existsSync(filePath)) {
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("Not found");
    return;
  }

  const extension = path.extname(filePath);
  response.writeHead(200, {
    "content-type": mimeTypes[extension] || "application/octet-stream",
    "cache-control": "no-cache"
  });
  createReadStream(filePath).pipe(response);
}

async function route(request, response) {
  const requestUrl = new URL(request.url, `http://${request.headers.host}`);

  if (request.method === "GET" && requestUrl.pathname === "/api/health") {
    sendJson(response, 200, {
      ok: true,
      mode: engine.mode,
      now: Date.now()
    });
    return;
  }

  if (request.method === "GET" && requestUrl.pathname === "/api/snapshot") {
    sendJson(response, 200, engine.snapshot());
    return;
  }

  if (request.method === "GET" && requestUrl.pathname === "/api/config") {
    sendJson(response, 200, {
      market: CONFIG.market,
      trade: CONFIG.trade,
      risk: CONFIG.risk,
      triangular: CONFIG.triangular,
      redis: {
        enabled: CONFIG.redis.enabled,
        namespace: CONFIG.redis.namespace
      },
      exchanges: CONFIG.exchanges.map((exchange) => ({
        id: exchange.id,
        name: exchange.name,
        product: exchange.product,
        primarySymbol: exchange.primarySymbol,
        triangularSymbols: exchange.triangularSymbols,
        takerFeeBps: exchange.takerFeeBps,
        slippageBps: exchange.slippageBps
      }))
    });
    return;
  }

  if (request.method === "GET" && requestUrl.pathname === "/api/readme") {
    const markdown = await readFile(path.join(rootDir, "README.md"), "utf8").catch(() => "");
    sendJson(response, 200, { markdown });
    return;
  }

  if (request.method === "GET" && requestUrl.pathname === "/events") {
    streams.add(response, engine.snapshot());
    return;
  }

  if (request.method === "POST" && requestUrl.pathname === "/api/control") {
    const body = await parseBody(request);
    if (typeof body.autoExecution === "boolean") engine.setAutoExecution(body.autoExecution);
    if (typeof body.mode === "string") engine.setMode(body.mode);
    sendJson(response, 200, engine.snapshot());
    return;
  }

  if (request.method === "POST" && requestUrl.pathname === "/api/reset") {
    engine.reset();
    sendJson(response, 200, engine.snapshot());
    return;
  }

  if (request.method === "OPTIONS") {
    response.writeHead(204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET,POST,OPTIONS",
      "access-control-allow-headers": "content-type"
    });
    response.end();
    return;
  }

  await serveStatic(request, response);
}

const server = http.createServer((request, response) => {
  route(request, response).catch((error) => {
    sendJson(response, 500, {
      ok: false,
      error: error.message
    });
  });
});

engine.on("snapshot", (snapshot) => streams.broadcast("snapshot", snapshot));
engine.on("error", (error) => {
  console.error("[engine]", error);
});

server.listen(CONFIG.server.port, CONFIG.server.host, () => {
  engine.start();
  console.log(`Bitcoin Arbitrage Sentinel running on http://localhost:${CONFIG.server.port}`);
});
