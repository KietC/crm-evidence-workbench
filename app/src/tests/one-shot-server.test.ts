import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const SERVER_ENTRY = fileURLToPath(new URL("../server.js", import.meta.url));
const APP_ROOT = path.resolve(path.dirname(SERVER_ENTRY), "..");

async function unusedPort(): Promise<number> {
  const server = net.createServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return port;
}

async function stop(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  await new Promise<void>(resolve => {
    const timer = setTimeout(() => { child.kill("SIGKILL"); resolve(); }, 3_000);
    child.once("exit", () => { clearTimeout(timer); resolve(); });
  });
}

async function waitForHealth(port: number, child: ChildProcess): Promise<void> {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`one-shot test server exited: ${child.exitCode}`);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/healthz`, { signal: AbortSignal.timeout(500) });
      if (response.ok) return;
    } catch { /* retry */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("one-shot test server health timeout");
}

test("one-shot server isolates runtime and rejects queue/deferred endpoints", async () => {
  const runtime = await fs.mkdtemp(path.join(os.tmpdir(), "okki-one-shot-runtime-"));
  const port = await unusedPort();
  const cdpPort = await unusedPort();
  const child = spawn(process.execPath, [SERVER_ENTRY], {
    cwd: APP_ROOT,
    windowsHide: true,
    stdio: "ignore",
    env: {
      ...process.env,
      OKKI_ONE_SHOT_COMPANY_ID: "123456789",
      OKKI_RUNTIME_ROOT: runtime,
      OKKI_SHARED_RUNTIME_ROOT: path.join(runtime, "must-not-be-used"),
      OKKI_CAPTURE_PORT: String(port),
      OKKI_CDP_PORT: String(cdpPort),
      OKKI_QUEUE_COORDINATOR: "1"
    }
  });
  try {
    await waitForHealth(port, child);
    const status = await fetch(`http://127.0.0.1:${port}/api/status`).then(response => response.json()) as {
      app: { oneShot: boolean; queueCoordinator: boolean };
      deferredRepairs: { disabled: boolean; total: number };
    };
    assert.equal(status.app.oneShot, true);
    assert.equal(status.app.queueCoordinator, false);
    assert.deepEqual(status.deferredRepairs, {
      total: 0, pending: 0, pending_error: 0, pending_warning: 0, resolved: 0, disabled: true
    });

    const unavailableBrowserRevisit = await fetch(`http://127.0.0.1:${port}/api/capture/start`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ revisit_ui_gaps: true })
    });
    assert.equal(unavailableBrowserRevisit.status, 409);
    const unavailableBrowserBody = await unavailableBrowserRevisit.json() as { error: string };
    assert.match(unavailableBrowserBody.error, /one-shot/);

    for (const endpoint of ["/api/next-customer", "/api/deferred-repairs/status"]) {
      const response = await fetch(`http://127.0.0.1:${port}${endpoint}`);
      assert.equal(response.status, 409);
    }
    await new Promise(resolve => setTimeout(resolve, 1_700));
    for (const forbidden of [
      "deferred-repair-queue.json",
      "customer-queue-cursor-v1.json",
      "auto-next-schedule.json"
    ]) {
      await assert.rejects(fs.stat(path.join(runtime, forbidden)), { code: "ENOENT" });
    }
    await assert.rejects(fs.stat(path.join(runtime, "must-not-be-used")), { code: "ENOENT" });
  } finally {
    await stop(child);
    await fs.rm(runtime, { recursive: true, force: true });
  }
});

test("capture-one exposes an explicit no-auto-next UI-gap switch", async () => {
  const script = await fs.readFile(path.join(APP_ROOT, "scripts", "capture-one.ps1"), "utf8");
  assert.match(script, /Alias\('revisit-ui-gaps'\)/);
  assert.doesNotMatch(script, /\$UiGapCompanyId/);
  assert.match(script, /ValueFromRemainingArguments = \$true/);
  assert.match(script, /\^--company-id/);
  assert.match(script, /\^--revisit-ui-gaps\$/);
  assert.match(script, /--revisit-ui-gaps requires --no-auto-next/);
  assert.match(script, /revisit_ui_gaps = \[bool\]\$RevisitUiGaps/);
});

test("invalid one-shot identity fails before the server listens", async () => {
  const port = await unusedPort();
  const child = spawn(process.execPath, [SERVER_ENTRY], {
    cwd: APP_ROOT,
    windowsHide: true,
    stdio: "ignore",
    env: { ...process.env, OKKI_ONE_SHOT_COMPANY_ID: "invalid", OKKI_CAPTURE_PORT: String(port) }
  });
  const exitCode = await new Promise<number | null>((resolve, reject) => {
    const timer = setTimeout(() => { child.kill(); reject(new Error("invalid one-shot server did not exit")); }, 5_000);
    child.once("exit", code => { clearTimeout(timer); resolve(code); });
  });
  assert.notEqual(exitCode, 0);
});

