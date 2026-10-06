/** Loopback-only control plane; one-shot mode disables queue traversal and shared repairs.
 * 仅本机可访问的控制层；单客户模式禁止跳下一家和共享延期修复。 */
import { spawn } from "node:child_process";
import { CaptureStartGate, CaptureBusyError, ensureCustomerIdentity, reservationExpired, reservationOwnerMatches } from "./capture-start-gate.js";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

try { os.setPriority(0, os.constants.priority.PRIORITY_ABOVE_NORMAL); } catch { /* best effort */ }
import { loadAdapter, DEFAULT_ADAPTER_PATH } from "./adapter.js";
import { BrowserManager, type CustomerQueueCursor } from "./browser-manager.js";
import { CaptureEngine } from "./capture-engine.js";
import { sha256 } from "./evidence-store.js";
import { AUTO_NEXT_DELAY_MS, autoNextDecision } from "./auto-next.js";
import { loadCaseInventory, refreshCompletedCase } from "./case-inventory.js";
import {
  eligibleDeferredErrors,
  mergeInventoryRepairCandidates,
  type DeferredInventoryMergeReport,
  type DeferredRepairEntry,
  type DeferredRepairQueue,
  type DeferredRepairStatus
} from "./deferred-repair-queue.js";
import { removeStaleDirectoryLock, removeStalePidFileLock, withDirectoryLock } from "./runtime-lock.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, "..");
const UI_ROOT = path.join(APP_ROOT, "ui");
const INSTANCE_ID = /^\d+$/.test(process.env.OKKI_INSTANCE_ID ?? "") ? process.env.OKKI_INSTANCE_ID! : "1";
const ONE_SHOT_RAW = process.env.OKKI_ONE_SHOT_COMPANY_ID?.trim() ?? "";
if (ONE_SHOT_RAW && !/^\d+$/.test(ONE_SHOT_RAW)) throw new Error("OKKI_ONE_SHOT_COMPANY_ID must contain digits only");
const ONE_SHOT_COMPANY_ID = ONE_SHOT_RAW || null;
const ONE_SHOT_MODE = ONE_SHOT_COMPANY_ID !== null;
// Resolve only an explicitly configured workspace or this repository's parent of app/.
// 仅使用明确配置的工作区或本仓库根目录；不依赖原开发机目录布局。
const WORKSPACE_ROOT = path.resolve(process.env.OKKI_WORKSPACE_ROOT ?? path.join(APP_ROOT, ".."));
const DEFAULT_RUNTIME_ROOT = ONE_SHOT_COMPANY_ID
  ? path.join(APP_ROOT, "runtime", "one-shot", `company_${ONE_SHOT_COMPANY_ID}`)
  : path.join(APP_ROOT, "runtime");
const RUNTIME_ROOT = path.resolve(process.env.OKKI_RUNTIME_ROOT ?? DEFAULT_RUNTIME_ROOT);
const SHARED_RUNTIME_ROOT = ONE_SHOT_MODE
  ? RUNTIME_ROOT
  : path.resolve(process.env.OKKI_SHARED_RUNTIME_ROOT ?? path.join(APP_ROOT, "runtime"));
const IS_QUEUE_COORDINATOR = !ONE_SHOT_MODE && process.env.OKKI_QUEUE_COORDINATOR !== "0";
const ACTIVE_CAPTURE_MARKER = path.join(RUNTIME_ROOT, "active-capture.json");
const AUTO_NEXT_SCHEDULE = path.join(RUNTIME_ROOT, "auto-next-schedule.json");
const CODEX_REVIEW_EVENT = path.join(RUNTIME_ROOT, "codex-review-request.json");
const DEFERRED_REPAIR_QUEUE = path.join(SHARED_RUNTIME_ROOT, "deferred-repair-queue.json");
const DEFERRED_REPAIR_LOCK = path.join(SHARED_RUNTIME_ROOT, "deferred-repair-queue.lock");
const QUEUE_RESERVATION_ROOT = path.join(SHARED_RUNTIME_ROOT, "queue-reservations");
const QUEUE_CURSOR_FILE = path.join(SHARED_RUNTIME_ROOT, "customer-queue-cursor-v1.json");
const QUEUE_CURSOR_LOCK = path.join(SHARED_RUNTIME_ROOT, "customer-queue-cursor-v1.lock");
const configuredCaseScanConcurrency = Number(process.env.OKKI_CASE_SCAN_CONCURRENCY);
const CASE_SCAN_CONCURRENCY = Math.min(128, Math.max(
  8,
  Number.isFinite(configuredCaseScanConcurrency) ? configuredCaseScanConcurrency : 32
));
const HOST = "127.0.0.1";
const PORT = Number(process.env.OKKI_CAPTURE_PORT ?? 3210);
const CDP_PORT = Number(process.env.OKKI_CDP_PORT ?? 9333);
const PROFILE_DIR = process.env.OKKI_BROWSER_PROFILE ?? path.join(RUNTIME_ROOT, "browser-profile");
const ONE_SHOT_CASE_LOCK = ONE_SHOT_COMPANY_ID
  ? path.join(RUNTIME_ROOT, `case-exclusive-${sha256(ONE_SHOT_COMPANY_ID).slice(0, 32)}.lock`)
  : null;

const adapter = await loadAdapter();
const browserManager = new BrowserManager(CDP_PORT, PROFILE_DIR, adapter);
const engine = new CaptureEngine(WORKSPACE_ROOT, adapter, browserManager);
const captureStartGate = new CaptureStartGate();
let nextCandidateCache: { at: number; value: Awaited<ReturnType<BrowserManager["nextCustomerCandidate"]>> } | null = null;

type AutoNextMode = "idle" | "capturing" | "review_required" | "countdown" | "opening" | "stopped_error" | "stopped_user" | "no_candidate";
interface AutoNextState {
  mode: AutoNextMode;
  dueAt: string | null;
  reason: string;
  lastJobId: string | null;
}
let autoNextState: AutoNextState = { mode: "idle", dueAt: null, reason: "", lastJobId: null };
let autoNextTimer: NodeJS.Timeout | null = null;
let activeDeferredRepairId: string | null = null;
let activeQueueReservation: { companyId: string; filePath: string; claimedAt: string } | null = null;
let lastDeferredInventorySync: (DeferredInventoryMergeReport & { completed_at: string; error_class: null }) | {
  completed_at: string;
  error_class: string;
} | null = null;

interface CodexReviewEvent {
  schema: 1;
  event_id: string;
  kind: "capture_terminal" | "queue_exhausted";
  status: "pending" | "claimed" | "acknowledged" | "manual_cancelled" | "wake_failed";
  generated_at: string;
  case_anon: string | null;
  job_id: string | null;
  terminal_phase: string;
  error_count: number;
  warning_count: number;
  reconciliation_failures: number | null;
  metrics: Record<string, number | boolean | null>;
  retry_count: number;
  next_attempt_at?: string | null;
  claimed_at?: string;
  acknowledged_at?: string;
}

async function writeJsonAtomic(filePath: string, value: unknown): Promise<void> {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.tmp`;
  const encoded = JSON.stringify(value, null, 2)
    .replaceAll("\u0085", "\\u0085")
    .replaceAll("\u2028", "\\u2028")
    .replaceAll("\u2029", "\\u2029");
  await fs.writeFile(temporary, `${encoded}\n`, "utf8");
  await fs.rm(filePath, { force: true }).catch(() => undefined);
  await fs.rename(temporary, filePath);
}

async function requireOneShotCustomer(openWhenMissing: boolean): Promise<void> {
  if (!ONE_SHOT_COMPANY_ID) return;
  let status = await browserManager.status();
  if (status.companyId === ONE_SHOT_COMPANY_ID) return;
  if (openWhenMissing) {
    await browserManager.openCustomer(ONE_SHOT_COMPANY_ID);
    status = await browserManager.status();
  }
  if (status.companyId !== ONE_SHOT_COMPANY_ID) {
    throw new Error("内置浏览器未停留在 one-shot 绑定的客户详情页");
  }
}

function oneShotDeferredSummary(): {
  total: number; pending: number; pending_error: number; pending_warning: number; resolved: number; disabled: true;
} {
  return { total: 0, pending: 0, pending_error: 0, pending_warning: 0, resolved: 0, disabled: true };
}

async function readCodexReviewEvent(): Promise<CodexReviewEvent | null> {
  try { return JSON.parse(await fs.readFile(CODEX_REVIEW_EVENT, "utf8")) as CodexReviewEvent; }
  catch { return null; }
}

async function readDeferredRepairQueue(): Promise<DeferredRepairQueue> {
  try {
    const parsed = JSON.parse(await fs.readFile(DEFERRED_REPAIR_QUEUE, "utf8")) as DeferredRepairQueue;
    if (parsed.schema === 1 && Array.isArray(parsed.entries)) return parsed;
  } catch { /* create an empty queue */ }
  return { schema: 1, updated_at: new Date().toISOString(), entries: [] };
}

async function writeDeferredRepairQueue(queue: DeferredRepairQueue): Promise<void> {
  queue.updated_at = new Date().toISOString();
  await writeJsonAtomic(DEFERRED_REPAIR_QUEUE, queue);
}

async function withDeferredRepairQueueLock<T>(operation: (queue: DeferredRepairQueue) => Promise<T>): Promise<T> {
  return withDirectoryLock(DEFERRED_REPAIR_LOCK, async () => {
    const queue = await readDeferredRepairQueue();
    const result = await operation(queue);
    await writeDeferredRepairQueue(queue);
    return result;
  }, { timeoutMs: 10_000, retryMs: 50 });
}

function deferredEntrySeverity(phase: string, errors: string[], warnings: string[], reconciliationFailures: number): DeferredRepairStatus | null {
  if (phase !== "complete" || errors.length > 0 || reconciliationFailures > 0) return "pending_error";
  if (warnings.length > 0) return "pending_warning";
  return null;
}

async function recordDeferredCaptureOutcome(job: ReturnType<CaptureEngine["status"]>): Promise<void> {
  const reconciliationFailures = Number(job.metrics.reconciliation_failures ?? 0);
  const status = deferredEntrySeverity(job.phase, job.errors, job.warnings, reconciliationFailures);
  if (!status) return;
  const now = new Date().toISOString();
  const currentPage = job.companyId ? null : await browserManager.customerPage().catch(() => null);
  let pageCompanyId: string | null = null;
  if (currentPage) {
    try {
      const value = new URL(currentPage.url()).searchParams.get("company_id");
      if (value && /^\d+$/.test(value)) pageCompanyId = value;
    } catch { /* already validated browser page changed during terminal handling */ }
  }
  const companyId = job.companyId ?? pageCompanyId;
  const stableIdentity = companyId ?? job.caseRoot ?? job.id ?? `${now}|unknown`;
  const entryId = sha256(stableIdentity).slice(0, 24);
  await withDeferredRepairQueueLock(async queue => {
    const existing = queue.entries.find((entry) => entry.entry_id === entryId);
    const next: DeferredRepairEntry = {
      entry_id: entryId,
      status,
      company_id: companyId,
      case_root: job.caseRoot,
      session_root: job.sessionRoot,
      job_id: job.id,
      terminal_phase: job.phase,
      error_count: job.errors.length,
      warning_count: job.warnings.length,
      reconciliation_failures: reconciliationFailures,
      errors: [...job.errors],
      warnings: [...job.warnings],
      attempts: existing?.attempts ?? 0,
      first_recorded_at: existing?.first_recorded_at ?? now,
      updated_at: now
    };
    if (existing) Object.assign(existing, next);
    else queue.entries.push(next);
  });
}

async function recordDeferredOpenFailure(companyId: string, error: unknown): Promise<void> {
  const now = new Date().toISOString();
  const entryId = sha256(companyId).slice(0, 24);
  const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  await withDeferredRepairQueueLock(async queue => {
    const existing = queue.entries.find((entry) => entry.entry_id === entryId);
    const next: DeferredRepairEntry = {
      entry_id: entryId,
      status: "pending_error",
      company_id: companyId,
      case_root: existing?.case_root ?? null,
      session_root: existing?.session_root ?? null,
      job_id: existing?.job_id ?? null,
      terminal_phase: "open_failed",
      error_count: 1,
      warning_count: 0,
      reconciliation_failures: 0,
      errors: [message],
      warnings: [],
      attempts: existing?.attempts ?? 0,
      first_recorded_at: existing?.first_recorded_at ?? now,
      updated_at: now
    };
    if (existing) Object.assign(existing, next);
    else queue.entries.push(next);
  });
}

async function deferredRepairSummary(): Promise<{
  total: number; pending: number; pending_error: number; pending_warning: number; resolved: number;
}> {
  const queue = await readDeferredRepairQueue();
  const pendingError = queue.entries.filter((entry) => entry.status === "pending_error").length;
  const pendingWarning = queue.entries.filter((entry) => entry.status === "pending_warning").length;
  const resolved = queue.entries.filter((entry) => entry.status === "resolved").length;
  return {
    total: queue.entries.length,
    pending: pendingError + pendingWarning,
    pending_error: pendingError,
    pending_warning: pendingWarning,
    resolved
  };
}

async function synchronizeDeferredRepairsFromInventory(): Promise<void> {
  try {
    const inventory = await loadCaseInventory(
      WORKSPACE_ROOT,
      SHARED_RUNTIME_ROOT,
      CASE_SCAN_CONCURRENCY,
      { forceRefresh: IS_QUEUE_COORDINATOR }
    );
    const completedAt = new Date().toISOString();
    const report = await withDeferredRepairQueueLock(async queue => {
      const merged = mergeInventoryRepairCandidates(queue.entries, inventory.repairCandidates, completedAt);
      queue.entries = merged.entries;
      return merged.report;
    });
    lastDeferredInventorySync = { ...report, completed_at: completedAt, error_class: null };
  } catch (error) {
    lastDeferredInventorySync = {
      completed_at: new Date().toISOString(),
      error_class: error instanceof Error ? error.name : "unknown"
    };
    throw error;
  }
}

async function deferredRepairPublicStatus(): Promise<Record<string, unknown>> {
  const queue = await readDeferredRepairQueue();
  const summary = await deferredRepairSummary();
  return {
    ...summary,
    active: activeDeferredRepairId,
    inventory_sync: lastDeferredInventorySync,
    entries: queue.entries
      .filter((entry) => entry.status !== "resolved")
      .map((entry) => ({
        entry_id: entry.entry_id,
        status: entry.status,
        terminal_phase: entry.terminal_phase,
        error_count: entry.error_count,
        warning_count: entry.warning_count,
        reconciliation_failures: entry.reconciliation_failures,
        attempts: entry.attempts,
        error_classes: entry.errors.map((value) => value.split(":", 1)[0]?.slice(0, 120) ?? "error"),
        warning_classes: entry.warnings.map((value) => value.split(":", 1)[0]?.slice(0, 120) ?? "warning")
      }))
  };
}

async function updateDeferredRepairOutcome(entryId: string, job: ReturnType<CaptureEngine["status"]>): Promise<void> {
  await withDeferredRepairQueueLock(async queue => {
    const entry = queue.entries.find((item) => item.entry_id === entryId);
    if (!entry) throw new Error("deferred repair entry missing");
    const reconciliationFailures = Number(job.metrics.reconciliation_failures ?? 0);
    const nextStatus = deferredEntrySeverity(job.phase, job.errors, job.warnings, reconciliationFailures);
    entry.status = nextStatus ?? "resolved";
    entry.company_id = job.companyId ?? entry.company_id;
    entry.case_root = job.caseRoot ?? entry.case_root;
    entry.session_root = job.sessionRoot ?? entry.session_root;
    entry.job_id = job.id;
    entry.terminal_phase = job.phase;
    entry.error_count = job.errors.length;
    entry.warning_count = job.warnings.length;
    entry.reconciliation_failures = reconciliationFailures;
    entry.errors = [...job.errors];
    entry.warnings = [...job.warnings];
    entry.attempts += 1;
    entry.updated_at = new Date().toISOString();
    if (entry.status === "resolved") entry.resolved_at = entry.updated_at;
    else delete entry.resolved_at;
  });
}

async function requestCodexReview(kind: CodexReviewEvent["kind"], job = engine.status()): Promise<void> {
  clearAutoNextTimer();
  await fs.rm(AUTO_NEXT_SCHEDULE, { force: true }).catch(() => undefined);
  const deferred = await deferredRepairSummary();
  const event: CodexReviewEvent = {
    schema: 1,
    event_id: sha256(`${Date.now()}|${job.id ?? "none"}|${kind}`).slice(0, 24),
    kind,
    status: "pending",
    generated_at: new Date().toISOString(),
    case_anon: job.caseRoot ? sha256(job.caseRoot).slice(0, 12) : null,
    job_id: job.id,
    terminal_phase: job.phase,
    error_count: job.errors.length,
    warning_count: job.warnings.length,
    reconciliation_failures: typeof job.metrics.reconciliation_failures === "number" ? job.metrics.reconciliation_failures : null,
    metrics: {
      api_rows_dynamic: Number(job.metrics.api_rows_dynamic ?? 0),
      api_rows_dynamic_mail: Number(job.metrics.api_rows_dynamic_mail ?? 0),
      api_rows_documents: Number(job.metrics.api_rows_documents ?? 0),
      mail_details_attempted: Number(job.metrics.mail_details_attempted ?? 0),
      discovered_resources: Number(job.metrics.discovered_resources ?? 0),
      resources_failed: Number(job.metrics.resources_failed ?? 0),
      deferred_total: deferred.total,
      deferred_pending: deferred.pending,
      deferred_pending_error: deferred.pending_error,
      deferred_pending_warning: deferred.pending_warning,
      deferred_resolved: deferred.resolved
    },
    retry_count: 0,
    next_attempt_at: null
  };
  await writeJsonAtomic(CODEX_REVIEW_EVENT, event);
  autoNextState = kind === "queue_exhausted"
    ? { mode: "no_candidate", dueAt: null, reason: "队列已耗尽，等待 Codex 最终汇总", lastJobId: job.id }
    : { mode: "review_required", dueAt: null, reason: "当前客户已终止，等待 Codex 匿名审计后确认继续", lastJobId: job.id };
}

async function cancelPendingCodexReview(): Promise<void> {
  const event = await readCodexReviewEvent();
  if (!event || !["pending", "claimed"].includes(event.status)) return;
  event.status = "manual_cancelled";
  await writeJsonAtomic(CODEX_REVIEW_EVENT, event);
}

function autoNextSnapshot(): AutoNextState & { delayMs: number; remainingMs: number } {
  const due = autoNextState.dueAt ? Date.parse(autoNextState.dueAt) : NaN;
  return {
    ...autoNextState,
    delayMs: AUTO_NEXT_DELAY_MS,
    remainingMs: Number.isFinite(due) ? Math.max(0, due - Date.now()) : 0
  };
}

function clearAutoNextTimer(): void {
  if (autoNextTimer) clearTimeout(autoNextTimer);
  autoNextTimer = null;
}

function stopAutoNext(mode: "stopped_error" | "stopped_user" | "no_candidate", reason: string): void {
  clearAutoNextTimer();
  void fs.rm(AUTO_NEXT_SCHEDULE, { force: true }).catch(() => undefined);
  autoNextState = { mode, dueAt: null, reason, lastJobId: engine.status().id };
}

async function scheduleAutoNext(jobId: string | null, warningCount: number, errorCount = 0, phase = "complete"): Promise<void> {
  clearAutoNextTimer();
  const dueAt = new Date(Date.now() + AUTO_NEXT_DELAY_MS).toISOString();
  autoNextState = {
    mode: "countdown",
    dueAt,
    reason: errorCount > 0 || phase !== "complete"
      ? `本次 ${errorCount} 个 error 已写入延期修复队列，15 秒后继续`
      : warningCount > 0
        ? `本次 ${warningCount} 条 warning 已写入延期复核队列，15 秒后继续`
        : "本次采集完成，15 秒后继续",
    lastJobId: jobId
  };
  await writeJsonAtomic(AUTO_NEXT_SCHEDULE, {
    schema: 2,
    mode: "countdown",
    due_at: dueAt,
    delay_ms: AUTO_NEXT_DELAY_MS,
    last_job_id: jobId,
    outcome_recorded: true,
    terminal_phase: phase,
    error_count: errorCount,
    warning_count: warningCount
  });
  autoNextTimer = setTimeout(() => {
    autoNextTimer = null;
    void openAndCaptureNext();
  }, AUTO_NEXT_DELAY_MS);
}

interface ActiveCaptureMarker {
  schema: 1 | 2;
  active: true;
  started_at?: string;
  company_id?: string | null;
}

async function readActiveCaptureMarker(): Promise<ActiveCaptureMarker | null> {
  try {
    const value = JSON.parse(await fs.readFile(ACTIVE_CAPTURE_MARKER, "utf8")) as ActiveCaptureMarker;
    return value.active === true ? value : null;
  } catch { return null; }
}

async function writeActiveCaptureMarker(): Promise<void> {
  const companyId = await browserManager.status().then(value => value.companyId).catch(() => null);
  if (ONE_SHOT_COMPANY_ID && companyId !== ONE_SHOT_COMPANY_ID) {
    throw new Error("拒绝为非 one-shot 绑定客户创建采集标记");
  }
  await fs.mkdir(RUNTIME_ROOT, { recursive: true });
  const temporary = `${ACTIVE_CAPTURE_MARKER}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify({
    schema: 2,
    active: true,
    started_at: new Date().toISOString(),
    company_id: companyId
  })}\n`, "utf8");
  await fs.rm(ACTIVE_CAPTURE_MARKER, { force: true }).catch(() => undefined);
  await fs.rename(temporary, ACTIVE_CAPTURE_MARKER);
}

async function clearActiveCaptureMarker(): Promise<void> {
  await fs.rm(ACTIVE_CAPTURE_MARKER, { force: true }).catch(() => undefined);
}

async function interruptedCaptureMarkerExists(): Promise<boolean> {
  return (await readActiveCaptureMarker()) !== null;
}

async function runCaptureChain(
  resumeExisting = false,
  flow: { kind: "collect" } | { kind: "deferred_repair"; entryId: string } = { kind: "collect" },
  revisitUiGaps = false,
  expectedCompanyId = ONE_SHOT_COMPANY_ID ?? undefined
): Promise<void> {
  return captureStartGate.run(async () => {
    try { await runCaptureChainOwned(resumeExisting, flow, revisitUiGaps, expectedCompanyId); }
    catch { stopAutoNext("stopped_error", "采集预检启动失败，已停止"); }
  });
}

async function runCaptureChainOwned(
  resumeExisting: boolean,
  flow: { kind: "collect" } | { kind: "deferred_repair"; entryId: string },
  revisitUiGaps: boolean,
  expectedCompanyId?: string
): Promise<void> {
  if (ONE_SHOT_MODE) {
    if (flow.kind !== "collect" || !ONE_SHOT_CASE_LOCK) throw new Error("one-shot mode rejects deferred and queue flows");
    try {
      await withDirectoryLock(ONE_SHOT_CASE_LOCK, () => runCaptureChainLocked(resumeExisting, flow, revisitUiGaps, expectedCompanyId), {
        timeoutMs: 1_000,
        staleAfterMs: 60_000,
        retryMs: 50
      });
    } catch (error) {
      stopAutoNext("stopped_error", "one-shot 客户已被其他进程独占或启动失败");
    }
    return;
  }
  await runCaptureChainLocked(resumeExisting, flow, revisitUiGaps, expectedCompanyId);
}

async function runCaptureChainLocked(
  resumeExisting: boolean,
  flow: { kind: "collect" } | { kind: "deferred_repair"; entryId: string },
  revisitUiGaps = false,
  expectedCompanyId?: string
): Promise<void> {
  clearAutoNextTimer();
  await requireOneShotCustomer(false);
  await writeActiveCaptureMarker();
  await fs.rm(AUTO_NEXT_SCHEDULE, { force: true }).catch(() => undefined);
  autoNextState = {
    mode: "capturing", dueAt: null,
    reason: flow.kind === "deferred_repair"
      ? "统一延期修复中；已落盘对象按哈希索引复用"
      : revisitUiGaps ? "补采当前绑定客户的 UI 标签和末页；不进入贸易数据或客户队列"
      : resumeExisting ? "进程中断续采中；已落盘分页和详情按哈希索引复用" : "采集中；warning 和 error 都会本地记录后继续",
    lastJobId: engine.status().id
  };
  try {
    const job = await engine.start({
      resumeExisting, revisitUiGaps,
      ...((ONE_SHOT_COMPANY_ID ?? expectedCompanyId) === undefined ? {} : {
        expectedCompanyId: (ONE_SHOT_COMPANY_ID ?? expectedCompanyId)!
      })
    });
    if (ONE_SHOT_COMPANY_ID && job.companyId !== ONE_SHOT_COMPANY_ID) {
      throw new Error("采集结果客户身份与 one-shot 绑定不一致");
    }
    if (!ONE_SHOT_MODE) await refreshCompletedCase(WORKSPACE_ROOT, SHARED_RUNTIME_ROOT, job.caseRoot).catch(() => undefined);
    const decision = autoNextDecision(job);
    if (ONE_SHOT_MODE) {
      const reconciliationFailures = Number(job.metrics.reconciliation_failures ?? 0);
      autoNextState = job.phase === "complete" && job.errors.length === 0 && reconciliationFailures === 0
        ? { mode: "idle", dueAt: null, reason: "one-shot 当前客户采集已结束；不会进入队列", lastJobId: job.id }
        : { mode: "stopped_error", dueAt: null, reason: "one-shot 当前客户保留了未完成状态；不会写入共享延期队列", lastJobId: job.id };
    } else if (decision === "stop_cancelled") {
      if (flow.kind === "deferred_repair") activeDeferredRepairId = null;
      stopAutoNext("stopped_user", "当前采集已取消，自动队列已停止");
    } else if (flow.kind === "deferred_repair") {
      await updateDeferredRepairOutcome(flow.entryId, job);
      activeDeferredRepairId = null;
      autoNextState = { mode: "idle", dueAt: null, reason: "本项统一延期修复已完成，等待处理下一项", lastJobId: job.id };
    } else if (decision === "advance" && IS_QUEUE_COORDINATOR) {
      await recordDeferredCaptureOutcome(job);
      await scheduleAutoNext(job.id, job.warnings.length, job.errors.length, job.phase);
    } else {
      autoNextState = {
        mode: "idle",
        dueAt: null,
        reason: IS_QUEUE_COORDINATOR ? "" : "非队列专用实例已完成当前客户，等待显式启动下一客户",
        lastJobId: job.id
      };
    }
  } catch (error) {
    const job = engine.status();
    try {
      if (ONE_SHOT_MODE) {
        stopAutoNext("stopped_error", "one-shot 当前客户采集异常；已停止且不会写入共享队列");
      } else if (flow.kind === "deferred_repair") {
        await updateDeferredRepairOutcome(flow.entryId, job);
        activeDeferredRepairId = null;
        autoNextState = { mode: "idle", dueAt: null, reason: "本项统一延期修复仍有 error，已保留待继续修复", lastJobId: job.id };
      } else {
        await recordDeferredCaptureOutcome(job);
        await scheduleAutoNext(job.id, job.warnings.length, Math.max(1, job.errors.length), job.phase);
      }
    } catch {
      stopAutoNext("stopped_error", "本地延期修复队列写入失败，为防止静默遗漏已停止");
    }
  } finally {
    await clearActiveCaptureMarker();
    await releaseQueueReservation();
  }
}

async function resumeInterruptedCapture(): Promise<void> {
  try { await captureStartGate.run(resumeInterruptedCaptureOwned, false); }
  catch (error) { if (!(error instanceof CaptureBusyError)) throw error; }
}

async function resumeInterruptedCaptureOwned(): Promise<void> {
  const marker = await readActiveCaptureMarker();
  if (!marker || engine.status().running) return;
  try {
    if (ONE_SHOT_COMPANY_ID && marker.company_id !== ONE_SHOT_COMPANY_ID) {
      await clearActiveCaptureMarker();
      stopAutoNext("stopped_error", "one-shot 中断标记身份不一致，已拒绝恢复");
      return;
    }
    if (!marker.company_id) throw new Error("resume marker has no customer identity");
    await ensureCustomerIdentity(marker.company_id,
      async () => (await browserManager.status()).companyId ?? null,
      id => browserManager.openCustomer(id));
    if (!(await browserManager.customerPage())) {
      // Legacy markers had no company id. Remove an unrecoverable marker so it
      // cannot force the same false stop on every later application launch.
      await clearActiveCaptureMarker();
      stopAutoNext("stopped_error", "检测到上次进程中断，但当前不在客户详情页；请手动打开后重新开始");
      return;
    }
    autoNextState = { mode: "opening", dueAt: null, reason: "检测到进程中断，正在按本地索引增量续采", lastJobId: null };
    void runCaptureChain(true, { kind: "collect" }, false, marker.company_id);
  } catch {
    stopAutoNext("stopped_error", "检测到上次进程中断，但自动续采启动失败");
  }
}

async function handleNoQueueCandidate(): Promise<"review" | "waiting" | "secondary_done"> {
  if (!IS_QUEUE_COORDINATOR) {
    stopAutoNext("no_candidate", "当前无未预留客户；由主实例统一完成队列审核");
    return "secondary_done";
  }
  const activeReservations = await activeQueueReservationIds();
  if (activeReservations.size > 0) {
    const dueAt = new Date(Date.now() + AUTO_NEXT_DELAY_MS).toISOString();
    autoNextState = {
      mode: "countdown",
      dueAt,
      reason: `其他采集器仍有 ${activeReservations.size} 个正在处理的客户，15 秒后重试`,
      lastJobId: engine.status().id
    };
    clearAutoNextTimer();
    autoNextTimer = setTimeout(() => {
      autoNextTimer = null;
      void openAndCaptureNext();
    }, AUTO_NEXT_DELAY_MS);
    return "waiting";
  }
  await requestCodexReview("queue_exhausted", engine.status());
  return "review";
}

async function openAndCaptureNext(): Promise<void> {
  try { await captureStartGate.run(openAndCaptureNextOwned, false); }
  catch (error) { if (!(error instanceof CaptureBusyError)) throw error; }
}

async function openAndCaptureNextOwned(): Promise<void> {
  clearAutoNextTimer();
  if (engine.status().running) {
    stopAutoNext("stopped_error", "检测到并发采集，自动队列已停止");
    return;
  }
  autoNextState = { mode: "opening", dueAt: null, reason: "正在重新计算并打开下一个客户", lastJobId: engine.status().id };
  let candidate: Awaited<ReturnType<BrowserManager["nextCustomerCandidate"]>> = null;
  try {
    candidate = await claimNextCandidate();
    if (!candidate) {
      await handleNoQueueCandidate();
      return;
    }
    nextCandidateCache = null;
    await browserManager.openCustomer(candidate.companyId);
    void runCaptureChain();
  } catch (error) {
    try {
      if (candidate) await recordDeferredOpenFailure(candidate.companyId, error);
      await releaseQueueReservation();
      await scheduleAutoNext(engine.status().id, 0, 1, "open_failed");
    } catch {
      stopAutoNext("stopped_error", "打开失败且本地延期修复队列写入失败，为防止静默遗漏已停止");
    }
  }
}

async function resumePersistedAutoNext(): Promise<void> {
  if (engine.status().running || await interruptedCaptureMarkerExists()) return;
  let saved: {
    schema?: unknown; mode?: unknown; due_at?: unknown; last_job_id?: unknown;
    outcome_recorded?: unknown; terminal_phase?: unknown; error_count?: unknown; warning_count?: unknown;
  };
  try { saved = JSON.parse(await fs.readFile(AUTO_NEXT_SCHEDULE, "utf8")) as typeof saved; }
  catch { return; }
  const event = await readCodexReviewEvent();
  const dueAt = typeof saved.due_at === "string" ? saved.due_at : "";
  const dueMs = Date.parse(dueAt);
  const safeEvent = event?.kind === "capture_terminal"
    && event.status === "acknowledged"
    && event.terminal_phase === "complete"
    && event.error_count === 0
    && Number(event.reconciliation_failures ?? 0) === 0;
  const deferredSchedule = saved.schema === 2
    && saved.outcome_recorded === true
    && typeof saved.terminal_phase === "string"
    && Number.isFinite(Number(saved.error_count ?? 0))
    && Number.isFinite(Number(saved.warning_count ?? 0));
  const legacySchedule = saved.schema === 1 && safeEvent;
  if (saved.mode !== "countdown" || !Number.isFinite(dueMs) || (!deferredSchedule && !legacySchedule)) {
    await fs.rm(AUTO_NEXT_SCHEDULE, { force: true }).catch(() => undefined);
    return;
  }
  autoNextState = {
    mode: "countdown",
    dueAt,
    reason: "服务重启后恢复15秒自动下一个倒计时",
    lastJobId: typeof saved.last_job_id === "string" ? saved.last_job_id : null
  };
  const remaining = Math.max(0, dueMs - Date.now());
  autoNextTimer = setTimeout(() => {
    autoNextTimer = null;
    void openAndCaptureNext();
  }, remaining);
}

async function resumeRuntimeState(): Promise<void> {
  if (ONE_SHOT_MODE) {
    if (await interruptedCaptureMarkerExists()) await resumeInterruptedCapture();
    return;
  }
  await removeStalePidFileLock(path.join(SHARED_RUNTIME_ROOT, "codex-monitor.lock")).catch(() => false);
  await removeStaleDirectoryLock(QUEUE_CURSOR_LOCK).catch(() => false);
  await removeStaleDirectoryLock(DEFERRED_REPAIR_LOCK).catch(() => false);
  await removeStaleDirectoryLock(path.join(SHARED_RUNTIME_ROOT, "case-inventory-v1.lock")).catch(() => false);
  await synchronizeDeferredRepairsFromInventory().catch(() => undefined);
  // Electron starts the server before the CRM WebContentsView has necessarily
  // reached its initial list page. Do not turn that normal race into an error.
  const browserDeadline = Date.now() + 30_000;
  while (Date.now() < browserDeadline) {
    if (await browserManager.customerPage() || await browserManager.customerListPage()) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  if (await interruptedCaptureMarkerExists()) {
    await resumeInterruptedCapture();
    return;
  }
  await resumePersistedAutoNext();
}

interface CaseInventory {
  completedCompanyIds: Set<string>;
  directoryNameKeys: Set<string>;
}

interface QueueReservationRecord {
  schema: 1;
  company_id: string;
  instance_id: string;
  pid: number;
  claimed_at: string;
}

function reservationPidState(pid: number): "alive" | "dead" | "unknown" {
  if (!Number.isInteger(pid) || pid <= 0) return "unknown";
  try { process.kill(pid, 0); return "alive"; }
  catch (error) { return (error as NodeJS.ErrnoException).code === "ESRCH" ? "dead" : "unknown"; }
}

async function activeQueueReservationIds(): Promise<Set<string>> {
  return withDirectoryLock(`${QUEUE_RESERVATION_ROOT}.lock`, activeQueueReservationIdsOwned);
}

async function activeQueueReservationIdsOwned(): Promise<Set<string>> {
  const ids = new Set<string>();
  let files: string[] = [];
  try { files = await fs.readdir(QUEUE_RESERVATION_ROOT); }
  catch { return ids; }
  for (const file of files.filter(value => value.endsWith(".json"))) {
    const filePath = path.join(QUEUE_RESERVATION_ROOT, file);
    try {
      const record = JSON.parse(await fs.readFile(filePath, "utf8")) as QueueReservationRecord;
      const stale = !/^\d+$/.test(record.company_id)
        || reservationExpired(reservationPidState(record.pid), record.claimed_at);
      if (stale) {
        await fs.rm(filePath, { force: true });
        continue;
      }
      ids.add(record.company_id);
    } catch {
      try {
        const stat = await fs.stat(filePath);
        if (Date.now() - stat.mtimeMs > 60_000) await fs.rm(filePath, { force: true });
      } catch { /* already removed */ }
    }
  }
  return ids;
}

async function tryClaimQueueCandidate(companyId: string): Promise<boolean> {
  return withDirectoryLock(`${QUEUE_RESERVATION_ROOT}.lock`, () => tryClaimQueueCandidateOwned(companyId));
}

async function tryClaimQueueCandidateOwned(companyId: string): Promise<boolean> {
  await fs.mkdir(QUEUE_RESERVATION_ROOT, { recursive: true });
  const filePath = path.join(QUEUE_RESERVATION_ROOT, `${sha256(companyId).slice(0, 32)}.json`);
  const record: QueueReservationRecord = {
    schema: 1,
    company_id: companyId,
    instance_id: INSTANCE_ID,
    pid: process.pid,
    claimed_at: new Date().toISOString()
  };
  try {
    const handle = await fs.open(filePath, "wx");
    try { await handle.writeFile(`${JSON.stringify(record)}\n`, "utf8"); }
    finally { await handle.close(); }
    activeQueueReservation = { companyId, filePath, claimedAt: record.claimed_at };
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    return false;
  }
}

async function releaseQueueReservation(): Promise<void> {
  await withDirectoryLock(`${QUEUE_RESERVATION_ROOT}.lock`, releaseQueueReservationOwned);
}

async function releaseQueueReservationOwned(): Promise<void> {
  const current = activeQueueReservation;
  activeQueueReservation = null;
  if (!current) return;
  try {
    const record = JSON.parse(await fs.readFile(current.filePath, "utf8")) as QueueReservationRecord;
    if (reservationOwnerMatches(record, { instanceId: INSTANCE_ID, pid: process.pid,
      companyId: current.companyId, claimedAt: current.claimedAt })) {
      await fs.rm(current.filePath, { force: true });
    }
  } catch { /* Missing or replaced owner is never ours to remove. */ }
}

interface PersistedQueueCursor {
  schema: 1;
  stage_id: string;
  page: number;
  row: number;
  updated_at: string;
}

async function readQueueCursor(): Promise<CustomerQueueCursor | null> {
  try {
    const value = JSON.parse(await fs.readFile(QUEUE_CURSOR_FILE, "utf8")) as PersistedQueueCursor;
    if (value.schema !== 1 || !/^\d+$/.test(value.stage_id)) return null;
    if (!Number.isInteger(value.page) || value.page < 1 || !Number.isInteger(value.row) || value.row < 1) return null;
    return { stageId: value.stage_id, page: value.page, row: value.row };
  } catch { return null; }
}

async function writeQueueCursor(cursor: CustomerQueueCursor | null): Promise<void> {
  if (!cursor) {
    await fs.rm(QUEUE_CURSOR_FILE, { force: true }).catch(() => undefined);
    return;
  }
  await writeJsonAtomic(QUEUE_CURSOR_FILE, {
    schema: 1,
    stage_id: cursor.stageId,
    page: cursor.page,
    row: cursor.row,
    updated_at: new Date().toISOString()
  } satisfies PersistedQueueCursor);
}

async function withQueueCursorLock<T>(action: () => Promise<T>): Promise<T> {
  return withDirectoryLock(QUEUE_CURSOR_LOCK, action, { timeoutMs: 120_000 });
}

async function caseInventory(): Promise<CaseInventory> {
  const cached = await loadCaseInventory(WORKSPACE_ROOT, SHARED_RUNTIME_ROOT, CASE_SCAN_CONCURRENCY);
  const completedCompanyIds = cached.completedCompanyIds;
  const directoryNameKeys = cached.directoryNameKeys;
  const deferred = await readDeferredRepairQueue();
  for (const entry of deferred.entries) {
    if (entry.company_id && /^\d+$/.test(entry.company_id)) completedCompanyIds.add(entry.company_id);
  }
  for (const companyId of await activeQueueReservationIds()) completedCompanyIds.add(companyId);
  return { completedCompanyIds, directoryNameKeys };
}

async function nextCandidate(force = false): Promise<Awaited<ReturnType<BrowserManager["nextCustomerCandidate"]>>> {
  if (!force && nextCandidateCache && Date.now() - nextCandidateCache.at < 15_000) return nextCandidateCache.value;
  const inventory = await caseInventory();
  const value = await browserManager.nextCustomerCandidate(
    inventory.completedCompanyIds,
    inventory.directoryNameKeys,
    await readQueueCursor()
  );
  nextCandidateCache = { at: Date.now(), value };
  return value;
}

async function claimNextCandidate(): Promise<Awaited<ReturnType<BrowserManager["nextCustomerCandidate"]>>> {
  return withQueueCursorLock(async () => {
    const inventory = await caseInventory();
    let cursor = await readQueueCursor();
    let wrapped = false;
    for (let attempt = 0; attempt < 10_000; attempt += 1) {
      const candidate = await browserManager.nextCustomerCandidate(
        inventory.completedCompanyIds,
        inventory.directoryNameKeys,
        cursor
      );
      if (!candidate) {
        if (cursor && !wrapped) {
          cursor = null;
          wrapped = true;
          continue;
        }
        await writeQueueCursor(null);
        return null;
      }
      const nextCursor: CustomerQueueCursor = {
        stageId: candidate.stageId,
        page: candidate.page,
        row: candidate.row
      };
      if (await tryClaimQueueCandidate(candidate.companyId)) {
        await writeQueueCursor(nextCursor);
        nextCandidateCache = null;
        return candidate;
      }
      inventory.completedCompanyIds.add(candidate.companyId);
      cursor = nextCursor;
      await writeQueueCursor(cursor);
    }
    throw new Error("queue reservation safety limit exceeded");
  });
}

function jsonResponse(response: http.ServerResponse, status: number, value: unknown): void {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff"
  });
  response.end(body);
}

async function readBody(request: http.IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  if (!chunks.length) return null;
  const body = Buffer.concat(chunks);
  if (body.byteLength > 64 * 1024) throw new Error("request body too large");
  return JSON.parse(body.toString("utf8"));
}

function contentType(filePath: string): string {
  const extension = path.extname(filePath).toLowerCase();
  return ({
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon"
  } as Record<string, string>)[extension] ?? "application/octet-stream";
}

async function serveStatic(requestPath: string, response: http.ServerResponse): Promise<void> {
  const requested = requestPath === "/" ? "index.html" : requestPath.replace(/^\/+/, "");
  const resolved = path.resolve(UI_ROOT, requested);
  if (!resolved.startsWith(`${path.resolve(UI_ROOT)}${path.sep}`) && resolved !== path.join(path.resolve(UI_ROOT), "index.html")) {
    jsonResponse(response, 403, { error: "forbidden" });
    return;
  }
  try {
    const data = await fs.readFile(resolved);
    response.writeHead(200, {
      "content-type": contentType(resolved),
      "content-length": data.byteLength,
      "cache-control": "no-cache",
      "x-content-type-options": "nosniff",
      "content-security-policy": "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    });
    response.end(data);
  } catch {
    jsonResponse(response, 404, { error: "not found" });
  }
}

const server = http.createServer(async (request, response) => {
  const pathname = new URL(request.url ?? "/", `http://${HOST}:${PORT}`).pathname;
  const startsCapture = request.method === "POST" && (
    pathname === "/api/capture/start" || pathname === "/api/next-customer/start"
    || pathname === "/api/deferred-repairs/start-next");
  try {
    if (startsCapture) await captureStartGate.run(() => handleRequest(request, response), false);
    else await handleRequest(request, response);
  } catch (error) {
    jsonResponse(response, error instanceof CaptureBusyError ? 409 : 500, {
      error: error instanceof Error ? error.message : String(error)
    });
  }
});

async function handleRequest(request: http.IncomingMessage, response: http.ServerResponse): Promise<void> {
  const url = new URL(request.url ?? "/", `http://${HOST}:${PORT}`);
  try {
    if (request.method === "GET" && url.pathname === "/api/status") {
      const browser = await browserManager.status();
      jsonResponse(response, 200, {
        app: {
          version: "1.0.0", node: process.version, pid: process.pid,
          instanceId: INSTANCE_ID,
          queueCoordinator: IS_QUEUE_COORDINATOR,
          oneShot: ONE_SHOT_MODE,
          workspaceRoot: WORKSPACE_ROOT, adapterId: adapter.adapter_id,
          adapterPath: DEFAULT_ADAPTER_PATH,
          localOnly: true,
          embeddedMode: process.env.OKKI_EMBEDDED_MODE === "1",
          externalAiUpload: "forbidden"
        },
        browser,
        job: engine.status(),
        autoNext: autoNextSnapshot(),
        deferredRepairs: ONE_SHOT_MODE ? oneShotDeferredSummary() : await deferredRepairSummary()
      });
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/browser/launch") {
      await readBody(request);
      jsonResponse(response, 200, { browser: await browserManager.launch() });
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/capture/start") {
      const body = await readBody(request) as { resume_existing?: unknown; revisit_ui_gaps?: unknown } | null;
      const resumeExisting = body?.resume_existing === true;
      const revisitUiGaps = body?.revisit_ui_gaps === true;
      if (engine.status().running) {
        jsonResponse(response, 409, { error: "capture already running", job: engine.status() });
        return;
      }
      if (revisitUiGaps && !ONE_SHOT_MODE) {
        jsonResponse(response, 409, { error: "UI gap revisit requires an explicit one-shot company binding" });
        return;
      }
      if (revisitUiGaps && resumeExisting) {
        jsonResponse(response, 409, { error: "UI gap revisit always creates a new independent session" });
        return;
      }
      if (ONE_SHOT_MODE) {
        try { await requireOneShotCustomer(true); }
        catch {
          jsonResponse(response, 409, { error: "无法打开或确认 one-shot 绑定的客户详情页；请先在内置浏览器完成登录" });
          return;
        }
      } else if (!(await browserManager.customerPage())) {
        jsonResponse(response, 409, { error: "内置浏览器中没有打开合法的 OKKI 单一客户详情页" });
        return;
      }
      clearAutoNextTimer();
      await fs.rm(AUTO_NEXT_SCHEDULE, { force: true }).catch(() => undefined);
      void runCaptureChain(resumeExisting, { kind: "collect" }, revisitUiGaps);
      jsonResponse(response, 202, {
        accepted: true,
        resume_existing: resumeExisting,
        revisit_ui_gaps: revisitUiGaps,
        no_auto_next: ONE_SHOT_MODE
      });
      return;
    }
    if (ONE_SHOT_MODE && (
      url.pathname.startsWith("/api/deferred-repairs/")
      || url.pathname.startsWith("/api/next-customer")
      || url.pathname === "/api/codex-review/ack"
    )) {
      jsonResponse(response, 409, { error: "one-shot mode disables deferred repair, customer queue and auto-next operations" });
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/capture/cancel") {
      await readBody(request);
      engine.cancel();
      stopAutoNext("stopped_user", "用户停止了当前采集，自动队列已停止");
      jsonResponse(response, 200, { job: engine.status() });
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/auto-next/cancel") {
      await readBody(request);
      if (autoNextState.mode === "review_required" || autoNextState.mode === "countdown") {
        await cancelPendingCodexReview();
        stopAutoNext("stopped_user", "用户取消了自动继续，不会打开下一客户");
      }
      jsonResponse(response, 200, { autoNext: autoNextSnapshot() });
      return;
    }
    if (request.method === "GET" && url.pathname === "/api/codex-review/status") {
      const event = await readCodexReviewEvent();
      jsonResponse(response, 200, {
        event: event ? {
          event_id: event.event_id,
          kind: event.kind,
          status: event.status,
          terminal_phase: event.terminal_phase,
          error_count: event.error_count,
          warning_count: event.warning_count,
          reconciliation_failures: event.reconciliation_failures,
          metrics: event.metrics,
          retry_count: event.retry_count
        } : null
      });
      return;
    }
    if (request.method === "GET" && url.pathname === "/api/deferred-repairs/status") {
      jsonResponse(response, 200, await deferredRepairPublicStatus());
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/deferred-repairs/start-next") {
      const body = await readBody(request) as { max_attempts?: unknown } | null;
      const parsedMaxAttempts = Number(body?.max_attempts ?? 3);
      const maxAttempts = Number.isFinite(parsedMaxAttempts) ? Math.min(10, Math.max(1, Math.trunc(parsedMaxAttempts))) : 3;
      if (engine.status().running || activeDeferredRepairId) {
        jsonResponse(response, 409, { error: "capture or deferred repair already running" });
        return;
      }
      const queue = await readDeferredRepairQueue();
      const pending = queue.entries
        .filter((item) => item.status === "pending_error")
        .sort((left, right) => left.attempts - right.attempts || Date.parse(left.updated_at) - Date.parse(right.updated_at));
      if (!pending.length) {
        jsonResponse(response, 404, { error: "no pending error repair", deferred: await deferredRepairSummary() });
        return;
      }
      const eligible = eligibleDeferredErrors(pending, maxAttempts);
      if (!eligible.length) {
        jsonResponse(response, 422, { error: "all pending deferred repairs reached the attempt limit", pending: pending.length, max_attempts: maxAttempts });
        return;
      }
      let entry: DeferredRepairEntry | undefined;
      for (const candidate of eligible) {
        if (!candidate.company_id || !/^\d+$/.test(candidate.company_id)) continue;
        if (await tryClaimQueueCandidate(candidate.company_id)) {
          entry = candidate;
          break;
        }
      }
      if (!entry) {
        const navigable = eligible.filter((item) => item.company_id && /^\d+$/.test(item.company_id)).length;
        jsonResponse(response, 409, {
          error: navigable > 0 ? "all deferred repairs are currently reserved" : "pending deferred repairs have no navigable customer identity",
          pending: eligible.length,
          navigable
        });
        return;
      }
      activeDeferredRepairId = entry.entry_id;
      try {
        await browserManager.openCustomer(entry.company_id!);
        void runCaptureChain(true, { kind: "deferred_repair", entryId: entry.entry_id });
        jsonResponse(response, 202, { accepted: true, entry_id: entry.entry_id });
      } catch (error) {
        activeDeferredRepairId = null;
        await releaseQueueReservation();
        await withDeferredRepairQueueLock(async currentQueue => {
          const current = currentQueue.entries.find(item => item.entry_id === entry.entry_id);
          if (!current) return;
          current.attempts += 1;
          current.updated_at = new Date().toISOString();
          current.errors = [error instanceof Error ? `${error.name}: ${error.message}` : String(error)];
          current.error_count = current.errors.length;
        });
        jsonResponse(response, 502, { error: "deferred repair navigation failed", entry_id: entry.entry_id });
      }
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/deferred-repairs/resolve-warning") {
      const body = await readBody(request) as { entry_id?: unknown } | null;
      const queue = await readDeferredRepairQueue();
      const entry = typeof body?.entry_id === "string" ? queue.entries.find((item) => item.entry_id === body.entry_id) : undefined;
      if (!entry || entry.status !== "pending_warning") {
        jsonResponse(response, 404, { error: "pending warning entry not found" });
        return;
      }
      if (entry.terminal_phase !== "complete" || entry.error_count > 0 || entry.reconciliation_failures > 0) {
        jsonResponse(response, 409, { error: "warning entry does not satisfy the safe resolution gate" });
        return;
      }
      await withDeferredRepairQueueLock(async currentQueue => {
        const current = currentQueue.entries.find(item => item.entry_id === entry.entry_id);
        if (!current || current.status !== "pending_warning") throw new Error("pending warning changed concurrently");
        current.status = "resolved";
        current.resolved_at = new Date().toISOString();
        current.updated_at = current.resolved_at;
      });
      jsonResponse(response, 200, { resolved: true, entry_id: entry.entry_id, deferred: await deferredRepairSummary() });
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/codex-review/ack") {
      const body = await readBody(request) as { event_id?: unknown } | null;
      const event = await readCodexReviewEvent();
      if (!event || !["pending", "claimed"].includes(event.status)) {
        jsonResponse(response, 409, { error: "no pending Codex review event" });
        return;
      }
      if (typeof body?.event_id === "string" && body.event_id !== event.event_id) {
        jsonResponse(response, 409, { error: "stale Codex review event" });
        return;
      }
      if (event.kind === "queue_exhausted") {
        const deferred = await deferredRepairSummary();
        if (deferred.pending > 0) {
          jsonResponse(response, 409, { error: "deferred repair queue is not empty", deferred });
          return;
        }
        event.status = "acknowledged";
        event.acknowledged_at = new Date().toISOString();
        await writeJsonAtomic(CODEX_REVIEW_EVENT, event);
        stopAutoNext("no_candidate", "队列已经完成并由 Codex 确认");
        jsonResponse(response, 200, { acknowledged: true, queue_exhausted: true });
        return;
      }
      const job = engine.status();
      const reconciliationFailures = Number(job.metrics.reconciliation_failures ?? 0);
      const liveGatePassed = !job.running && job.phase === "complete" && job.errors.length === 0 && reconciliationFailures === 0;
      const restartGatePassed = !job.running
        && job.phase === "idle"
        && event.terminal_phase === "complete"
        && event.error_count === 0
        && Number(event.reconciliation_failures ?? 0) === 0;
      if (!liveGatePassed && !restartGatePassed) {
        jsonResponse(response, 409, {
          error: "latest capture has not passed the completeness gate",
          phase: job.phase,
          error_count: job.errors.length,
          reconciliation_failures: reconciliationFailures
        });
        return;
      }
      event.status = "acknowledged";
      event.acknowledged_at = new Date().toISOString();
      await writeJsonAtomic(CODEX_REVIEW_EVENT, event);
      await scheduleAutoNext(job.id ?? event.job_id, liveGatePassed ? job.warnings.length : event.warning_count);
      jsonResponse(response, 202, { acknowledged: true, continuing: true, delay_ms: AUTO_NEXT_DELAY_MS, due_at: autoNextState.dueAt });
      return;
    }
    if (request.method === "GET" && url.pathname === "/api/next-customer") {
      if (engine.status().running) {
        jsonResponse(response, 409, { error: "capture running" });
        return;
      }
      const candidate = await nextCandidate();
      jsonResponse(response, 200, { candidate });
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/next-customer/start") {
      await readBody(request);
      clearAutoNextTimer();
      await fs.rm(AUTO_NEXT_SCHEDULE, { force: true }).catch(() => undefined);
      if (engine.status().running) {
        jsonResponse(response, 409, { error: "capture already running" });
        return;
      }
      autoNextState = { mode: "opening", dueAt: null, reason: "用户立即开始下一个客户", lastJobId: engine.status().id };
      try {
        const candidate = await claimNextCandidate();
        if (!candidate) {
          const emptyState = await handleNoQueueCandidate();
          jsonResponse(response, emptyState === "waiting" ? 202 : 404, {
            error: emptyState === "waiting" ? "waiting for other collector instances" : "没有待处理客户",
            queue_state: emptyState
          });
          return;
        }
        nextCandidateCache = null;
        await browserManager.openCustomer(candidate.companyId);
        void runCaptureChain();
        jsonResponse(response, 202, { accepted: true });
      } catch (error) {
        await releaseQueueReservation();
        stopAutoNext("stopped_error", "手动打开下一个客户失败，自动队列已停止");
        throw error;
      }
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/open-case") {
      await readBody(request);
      const caseRoot = engine.status().caseRoot;
      if (!caseRoot) {
        jsonResponse(response, 409, { error: "no case selected" });
        return;
      }
      spawn("explorer.exe", [caseRoot], { detached: true, stdio: "ignore", windowsHide: false }).unref();
      jsonResponse(response, 200, { opened: caseRoot });
      return;
    }
    if (request.method === "GET" && url.pathname === "/api/workflows") {
      const indexPath = path.join(WORKSPACE_ROOT, "CODEX_LOCAL_WORKFLOW_INDEX.json");
      try {
        jsonResponse(response, 200, JSON.parse(await fs.readFile(indexPath, "utf8")));
      } catch {
        jsonResponse(response, 404, { error: "workflow index not generated" });
      }
      return;
    }
    if (request.method === "GET" && url.pathname === "/healthz") {
      jsonResponse(response, 200, { status: "ok", localOnly: true });
      return;
    }
    await serveStatic(url.pathname, response);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    jsonResponse(response, 500, { error: message });
  }
}

server.listen(PORT, HOST, () => {
  process.stdout.write(`OKKI local capture UI: http://${HOST}:${PORT}\n`);
  setTimeout(() => { void resumeRuntimeState(); }, 1_500);
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    void releaseQueueReservation().finally(() => server.close(() => process.exit(0)));
  });
}
