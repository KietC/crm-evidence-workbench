/** Optional opt-in model reviewer sees counters/error classes only, never evidence bodies.
 * 可选模型复验器仅接收计数与错误分类，禁止读取或上传证据正文。 */
import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { codexResumeArgs, reviewerDisposition } from "./codex-wake-policy.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, "..");
const RUNTIME_ROOT = path.resolve(process.env.OKKI_RUNTIME_ROOT ?? path.join(APP_ROOT, "runtime"));
const CONFIG_FILE = path.join(RUNTIME_ROOT, "codex-monitor.json");
const EVENT_FILE = path.join(RUNTIME_ROOT, "codex-review-request.json");
const STATUS_FILE = path.join(RUNTIME_ROOT, "codex-monitor-status.json");
const LOCK_FILE = path.join(RUNTIME_ROOT, "codex-monitor.lock");
const LOG_FILE = path.join(RUNTIME_ROOT, "codex-monitor.log");

interface MonitorConfig {
  schema: 1;
  enabled: boolean;
  standalone_enabled?: boolean;
  watcher_mode?: "electron" | "standalone";
  thread_id: string;
  workspace: string;
  codex_executable: string;
  poll_interval_ms: number;
  max_wake_retries: number;
  reviewer_timeout_ms?: number;
}

interface ReviewEvent {
  schema: 1;
  event_id: string;
  kind: "capture_terminal" | "queue_exhausted";
  status: "pending" | "claimed" | "acknowledged" | "manual_cancelled" | "wake_failed";
  retry_count: number;
  next_attempt_at?: string | null;
  claimed_at?: string;
  [key: string]: unknown;
}

let stopping = false;
let wakeChild: ChildProcess | null = null;
let wakeEventId: string | null = null;
let wakeStartedAtMs: number | null = null;
let lockHeld = false;

async function readJson<T>(filePath: string): Promise<T | null> {
  try {
    return JSON.parse(await fsp.readFile(filePath, "utf8")) as T;
  } catch {
    return null;
  }
}

async function writeJsonAtomic(filePath: string, value: unknown): Promise<void> {
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.tmp`;
  await fsp.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await fsp.rm(filePath, { force: true }).catch(() => undefined);
  await fsp.rename(temporary, filePath);
}

async function appendLog(message: string): Promise<void> {
  await fsp.appendFile(LOG_FILE, `[${new Date().toISOString()}] ${message}\n`, "utf8").catch(() => undefined);
}

function pidIsAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function acquireLock(): Promise<boolean> {
  await fsp.mkdir(RUNTIME_ROOT, { recursive: true });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const handle = await fsp.open(LOCK_FILE, "wx");
      await handle.writeFile(`${process.pid}\n`, "utf8");
      await handle.close();
      lockHeld = true;
      return true;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "EEXIST") throw error;
      const oldPid = Number((await fsp.readFile(LOCK_FILE, "utf8").catch(() => "0")).trim());
      if (pidIsAlive(oldPid)) return false;
      await fsp.rm(LOCK_FILE, { force: true });
    }
  }
  return false;
}

async function releaseLock(): Promise<void> {
  if (!lockHeld) return;
  const owner = Number((await fsp.readFile(LOCK_FILE, "utf8").catch(() => "0")).trim());
  if (owner === process.pid) await fsp.rm(LOCK_FILE, { force: true }).catch(() => undefined);
  lockHeld = false;
}

async function writeStatus(state: string, extra: Record<string, unknown> = {}): Promise<void> {
  await writeJsonAtomic(STATUS_FILE, {
    schema: 1,
    pid: process.pid,
    state,
    updated_at: new Date().toISOString(),
    token_policy: "no_model_call_without_pending_terminal_event",
    privacy_policy: "anonymous_counts_hashes_and_error_classes_only",
    ...extra
  });
}

function wakePrompt(event: ReviewEvent): string {
  const action = event.kind === "queue_exhausted"
    ? "The main customer queue is exhausted. Read GET /api/deferred-repairs/status. Process every pending_error through POST /api/deferred-repairs/start-next, repair collector defects when needed, privately audit results, and resolve only safe pending_warning entries through POST /api/deferred-repairs/resolve-warning. ACK the queue event and complete the goal only after deferred pending is zero."
    : "Run the fixed anonymous audit. Repair and regression-test collector defects, re-capture only what is necessary, and ACK only after zero errors and zero reconciliation failures.";
  return [
    `OKKI local terminal event: ${event.event_id}; kind: ${event.kind}.`,
    "Continue the operator-confirmed active goal.",
    "Read only runtime/codex-review-request.json, anonymous audit reports, counts, hashes, and error classifications.",
    "Never output customer names, customer IDs, message bodies, or business content. Never upload local evidence.",
    action,
    "Use existing private audit scripts first. Stay token-efficient and do not poll chat while capture is running.",
    "A manual_cancelled event must never be resumed."
  ].join("\n");
}

async function handleWakeExit(event: ReviewEvent, config: MonitorConfig, code: number | null, signal: NodeJS.Signals | null): Promise<void> {
  if (wakeEventId === event.event_id) {
    wakeChild = null;
    wakeEventId = null;
    wakeStartedAtMs = null;
  }
  await appendLog(`exit event=${event.event_id} code=${String(code)} signal=${String(signal)}`);
  const current = await readJson<ReviewEvent>(EVENT_FILE);
  if (!current || current.event_id !== event.event_id || current.status !== "claimed") {
    await writeStatus("waiting");
    return;
  }

  current.retry_count = Number(current.retry_count ?? 0) + 1;
  if (current.retry_count >= Math.max(1, Number(config.max_wake_retries) || 1)) {
    current.status = "wake_failed";
    current.next_attempt_at = null;
    await writeStatus("wake_failed", { event_id: current.event_id, kind: current.kind, retry_count: current.retry_count });
  } else {
    current.status = "pending";
    current.next_attempt_at = new Date(Date.now() + 60_000 * current.retry_count).toISOString();
    await writeStatus("retry_wait", { event_id: current.event_id, kind: current.kind, retry_count: current.retry_count });
  }
  await writeJsonAtomic(EVENT_FILE, current);
}

async function wakeCodex(event: ReviewEvent, config: MonitorConfig): Promise<void> {
  event.status = "claimed";
  event.claimed_at = new Date().toISOString();
  await writeJsonAtomic(EVENT_FILE, event);
  await writeStatus("codex_running", { event_id: event.event_id, kind: event.kind, retry_count: event.retry_count ?? 0 });
  await appendLog(`wake event=${event.event_id} kind=${event.kind}`);

  const child = spawn(config.codex_executable, codexResumeArgs(event.kind, config.thread_id), {
    cwd: config.workspace,
    windowsHide: true,
    env: { ...process.env },
    stdio: ["pipe", "ignore", "ignore"]
  });
  wakeChild = child;
  wakeEventId = event.event_id;
  wakeStartedAtMs = Date.now();
  child.stdin?.end(wakePrompt(event), "utf8");
  child.once("error", (error) => {
    void appendLog(`spawn_error event=${event.event_id} code=${(error as NodeJS.ErrnoException).code ?? "unknown"}`);
  });
  child.once("exit", (code, signal) => {
    void handleWakeExit(event, config, code, signal);
  });
}

async function recoverStaleClaim(event: ReviewEvent): Promise<ReviewEvent> {
  if (event.status !== "claimed") return event;
  const claimedAt = Date.parse(event.claimed_at ?? "");
  if (!Number.isFinite(claimedAt) || Date.now() - claimedAt < 30 * 60_000) return event;
  event.status = "pending";
  event.retry_count = Number(event.retry_count ?? 0) + 1;
  event.next_attempt_at = null;
  await writeJsonAtomic(EVENT_FILE, event);
  await appendLog(`recovered_stale_claim event=${event.event_id}`);
  return event;
}

async function tick(): Promise<number> {
  if (stopping) return 2_000;
  const config = await readJson<MonitorConfig>(CONFIG_FILE);
  const interval = Math.max(1_000, Math.min(60_000, Number(config?.poll_interval_ms ?? 2_000)));
  if (config?.standalone_enabled !== true || config.watcher_mode !== "standalone") {
    await writeStatus("disabled");
    return interval;
  }
  if (!/^[0-9a-f-]{36}$/i.test(config.thread_id)) {
    await writeStatus("invalid_config", { reason: "thread_id" });
    return interval;
  }
  if (!fs.existsSync(config.codex_executable)) {
    await writeStatus("invalid_config", { reason: "codex_executable" });
    return interval;
  }

  let event = await readJson<ReviewEvent>(EVENT_FILE);
  if (wakeChild && wakeEventId && wakeStartedAtMs !== null) {
    const disposition = reviewerDisposition(
      wakeEventId,
      wakeStartedAtMs,
      event,
      Date.now(),
      Number(config.reviewer_timeout_ms ?? 15 * 60_000)
    );
    if (disposition !== "keep") {
      await appendLog(`${disposition} event=${wakeEventId} current=${event?.event_id ?? "none"}`);
      await writeStatus("stale_reviewer_stopping", {
        event_id: wakeEventId,
        current_event_id: event?.event_id ?? null,
        reason: disposition
      });
      wakeChild.kill();
    }
    return interval;
  }
  if (!event) {
    await writeStatus("waiting");
    return interval;
  }
  event = await recoverStaleClaim(event);
  if (event.status === "manual_cancelled" || event.status === "acknowledged" || event.status === "wake_failed") {
    await writeStatus("waiting", { last_event_id: event.event_id, last_event_status: event.status });
    return interval;
  }
  if (event.status !== "pending") {
    await writeStatus("claimed_external", { event_id: event.event_id, kind: event.kind });
    return interval;
  }
  if (event.next_attempt_at && Date.parse(event.next_attempt_at) > Date.now()) {
    await writeStatus("retry_wait", { event_id: event.event_id, kind: event.kind, retry_count: event.retry_count ?? 0 });
    return interval;
  }
  await wakeCodex(event, config);
  return interval;
}

async function shutdown(signal: string): Promise<void> {
  if (stopping) return;
  stopping = true;
  await appendLog(`stop pid=${process.pid} signal=${signal}`);
  await writeStatus("stopped", { signal });
  await releaseLock();
  process.exit(0);
}

async function main(): Promise<void> {
  if (!(await acquireLock())) process.exit(0);
  await appendLog(`start pid=${process.pid}`);
  await writeStatus("starting");
  process.once("SIGINT", () => { void shutdown("SIGINT"); });
  process.once("SIGTERM", () => { void shutdown("SIGTERM"); });
  process.once("exit", () => {
    if (lockHeld) fs.rmSync(LOCK_FILE, { force: true });
  });

  while (!stopping) {
    let interval = 2_000;
    try {
      interval = await tick();
    } catch (error) {
      await appendLog(`tick_error class=${error instanceof Error ? error.name : "unknown"}`);
      await writeStatus("monitor_error", { error_class: error instanceof Error ? error.name : "unknown" }).catch(() => undefined);
    }
    await new Promise<void>((resolve) => setTimeout(resolve, interval));
  }
}

void main().catch(async (error) => {
  await appendLog(`fatal class=${error instanceof Error ? error.name : "unknown"}`);
  await releaseLock();
  process.exit(1);
});
