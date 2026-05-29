import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const rootDir = path.resolve(path.dirname(__filename), "..");

function cleanEnv(extra) {
  const env = { ...process.env, ...extra };
  if (env.PATH && env.Path) delete env.PATH;
  return env;
}

async function waitForJson(url, attempts = 30) {
  let lastError = null;
  for (let index = 0; index < attempts; index += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return response.json();
      lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw lastError;
}

test("server exposes live dashboard snapshot", async (t) => {
  const port = 3137;
  const child = spawn(process.execPath, ["src/server.js"], {
    cwd: rootDir,
    env: cleanEnv({
      PORT: String(port),
      MARKET_MODE: "demo",
      POLL_INTERVAL_MS: "350",
      REQUEST_TIMEOUT_MS: "200"
    }),
    stdio: ["ignore", "pipe", "pipe"]
  });

  let logs = "";
  child.stdout.on("data", (chunk) => {
    logs += chunk.toString();
  });
  child.stderr.on("data", (chunk) => {
    logs += chunk.toString();
  });
  t.after(() => child.kill());

  const health = await waitForJson(`http://localhost:${port}/api/health`);
  assert.equal(health.ok, true);
  assert.equal(health.mode, "demo");

  await new Promise((resolve) => setTimeout(resolve, 2500));
  const snapshot = await waitForJson(`http://localhost:${port}/api/snapshot`);
  assert.equal(snapshot.mode, "demo");
  assert.ok(snapshot.books.length >= 3, logs);
  assert.ok(snapshot.wallets.length >= 3);
  assert.ok(Array.isArray(snapshot.opportunities));
  assert.ok(snapshot.metrics.detectedCount > 0);
});
