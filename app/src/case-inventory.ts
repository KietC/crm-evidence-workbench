/** Case completion inventory is a cache, not proof; verify identities before reuse.
 * 案例完成索引仅是缓存而非事实证明；复用前必须核实身份。 */
import fs from "node:fs/promises";
import path from "node:path";

import { caseDirectoryNameKey, sha256 } from "./evidence-store.js";
import { withDirectoryLock } from "./runtime-lock.js";

type TerminalState = "complete" | "failed" | "missing";

interface CachedCaseRecord {
  directory_key: string;
  company_id: string | null;
  completed: boolean;
  case_root: string;
  session_root: string | null;
  job_id: string | null;
  terminal_state: TerminalState;
  terminal_phase: string;
  error_count: number;
  warning_count: number;
  reconciliation_failures: number;
  errors: string[];
  warnings: string[];
  terminal_observed_at: string;
}

interface CaseInventorySnapshot {
  schema: 2;
  generated_at: string;
  directory_fingerprint: string;
  records: Record<string, CachedCaseRecord>;
}

export interface CaseInventoryResult {
  completedCompanyIds: Set<string>;
  directoryNameKeys: Set<string>;
  directoryCount: number;
  scannedDirectories: number;
  cacheHit: boolean;
  repairCandidates: CaseRepairCandidate[];
}

export interface CaseRepairCandidate {
  companyId: string;
  caseRoot: string;
  sessionRoot: string | null;
  jobId: string | null;
  terminalState: Exclude<TerminalState, "complete">;
  terminalPhase: string;
  errors: string[];
  warnings: string[];
  reconciliationFailures: number;
  observedAt: string;
}

function inventoryPaths(sharedRuntimeRoot: string): { file: string; lock: string } {
  return {
    file: path.join(sharedRuntimeRoot, "case-inventory-v1.json"),
    lock: path.join(sharedRuntimeRoot, "case-inventory-v1.lock")
  };
}

function fingerprint(names: string[]): string {
  return sha256([...names].sort((a, b) => a.localeCompare(b, "en-US")).join("\n"));
}

async function readSnapshot(file: string): Promise<CaseInventorySnapshot | null> {
  try {
    const value = JSON.parse(await fs.readFile(file, "utf8")) as CaseInventorySnapshot;
    if (value.schema === 2 && value.records && typeof value.records === "object") return value;
  } catch { /* cache is optional and rebuildable */ }
  return null;
}

async function writeSnapshot(file: string, value: CaseInventorySnapshot): Promise<void> {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.${Date.now()}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(value)}\n`, "utf8");
  await fs.rm(file, { force: true }).catch(() => undefined);
  await fs.rename(temporary, file);
}

async function withInventoryLock<T>(sharedRuntimeRoot: string, action: () => Promise<T>): Promise<T> {
  const { lock } = inventoryPaths(sharedRuntimeRoot);
  return withDirectoryLock(lock, action, { timeoutMs: 120_000 });
}

interface LatestTerminal {
  sessionRoot: string | null;
  jobId: string | null;
  state: TerminalState;
  phase: string;
  errors: string[];
  warnings: string[];
  reconciliationFailures: number;
  observedAt: string;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

async function latestTerminal(caseRoot: string): Promise<LatestTerminal> {
  const sessionsRoot = path.join(caseRoot, "raw", "sessions");
  let sessions: string[] = [];
  try {
    sessions = (await fs.readdir(sessionsRoot, { withFileTypes: true }))
      .filter(entry => entry.isDirectory())
      .map(entry => path.join(sessionsRoot, entry.name));
  } catch { /* represented as a missing terminal below */ }

  const sessionStats = await Promise.all(sessions.map(async sessionRoot => {
    const finalPath = path.join(sessionRoot, "final_status.json");
    try {
      const stat = await fs.stat(finalPath);
      return { sessionRoot, finalPath, mtimeMs: stat.mtimeMs, hasFinal: true };
    } catch {
      const stat = await fs.stat(sessionRoot).catch(() => null);
      return { sessionRoot, finalPath, mtimeMs: stat?.mtimeMs ?? 0, hasFinal: false };
    }
  }));
  sessionStats.sort((left, right) => right.mtimeMs - left.mtimeMs);
  const latestWithFinal = sessionStats[0];

  if (!latestWithFinal?.hasFinal) {
    const newestSession = latestWithFinal;
    const caseStat = newestSession ? null : await fs.stat(caseRoot).catch(() => null);
    const observedMs = newestSession?.mtimeMs ?? caseStat?.mtimeMs ?? Date.now();
    return {
      sessionRoot: newestSession?.sessionRoot ?? null,
      jobId: newestSession ? path.basename(newestSession.sessionRoot) : null,
      state: "missing",
      phase: "missing_terminal_status",
      errors: ["本地终态完整性: missing final_status.json"],
      warnings: [],
      reconciliationFailures: 1,
      observedAt: new Date(observedMs).toISOString()
    };
  }

  try {
    const value = JSON.parse(await fs.readFile(latestWithFinal.finalPath, "utf8")) as Record<string, unknown>;
    const phase = typeof value.phase === "string" ? value.phase : "missing_terminal_phase";
    const errors = stringArray(value.errors);
    const warnings = stringArray(value.warnings);
    const metrics = value.metrics && typeof value.metrics === "object" ? value.metrics as Record<string, unknown> : {};
    const parsedReconciliation = Number(metrics.reconciliation_failures ?? 0);
    const reconciliationFailures = Number.isFinite(parsedReconciliation) ? Math.max(0, parsedReconciliation) : 1;
    const state: TerminalState = phase === "complete" && errors.length === 0 && reconciliationFailures === 0
      ? "complete"
      : "failed";
    return {
      sessionRoot: latestWithFinal.sessionRoot,
      jobId: typeof value.id === "string" ? value.id : path.basename(latestWithFinal.sessionRoot),
      state,
      phase,
      errors,
      warnings,
      reconciliationFailures,
      observedAt: new Date(latestWithFinal.mtimeMs).toISOString()
    };
  } catch {
    return {
      sessionRoot: latestWithFinal.sessionRoot,
      jobId: path.basename(latestWithFinal.sessionRoot),
      state: "failed",
      phase: "unreadable_terminal_status",
      errors: ["本地终态完整性: unreadable final_status.json"],
      warnings: [],
      reconciliationFailures: 1,
      observedAt: new Date(latestWithFinal.mtimeMs).toISOString()
    };
  }
}

async function readCaseRecord(casesRoot: string, directoryName: string): Promise<CachedCaseRecord> {
  const root = path.join(casesRoot, directoryName);
  let companyId: string | null = null;
  try {
    const identity = JSON.parse(await fs.readFile(path.join(root, "case_identity.json"), "utf8")) as Record<string, unknown>;
    const value = String(identity.company_id ?? "");
    if (/^\d+$/.test(value)) companyId = value;
  } catch { /* legacy/incomplete directory */ }
  const terminal = await latestTerminal(root);
  return {
    directory_key: caseDirectoryNameKey(directoryName),
    company_id: companyId,
    completed: Boolean(companyId && terminal.state === "complete"),
    case_root: root,
    session_root: terminal.sessionRoot,
    job_id: terminal.jobId,
    terminal_state: terminal.state,
    terminal_phase: terminal.phase,
    error_count: terminal.errors.length,
    warning_count: terminal.warnings.length,
    reconciliation_failures: terminal.reconciliationFailures,
    errors: terminal.errors,
    warnings: terminal.warnings,
    terminal_observed_at: terminal.observedAt
  };
}

function resultFromSnapshot(snapshot: CaseInventorySnapshot, scannedDirectories: number, cacheHit: boolean): CaseInventoryResult {
  const completedCompanyIds = new Set<string>();
  const directoryNameKeys = new Set<string>();
  const repairCandidates: CaseRepairCandidate[] = [];
  for (const record of Object.values(snapshot.records)) {
    if (record.directory_key) directoryNameKeys.add(record.directory_key);
    if (record.completed && record.company_id) completedCompanyIds.add(record.company_id);
    if (record.company_id && record.terminal_state !== "complete") {
      repairCandidates.push({
        companyId: record.company_id,
        caseRoot: record.case_root,
        sessionRoot: record.session_root,
        jobId: record.job_id,
        terminalState: record.terminal_state,
        terminalPhase: record.terminal_phase,
        errors: [...record.errors],
        warnings: [...record.warnings],
        reconciliationFailures: record.reconciliation_failures,
        observedAt: record.terminal_observed_at
      });
    }
  }
  return {
    completedCompanyIds,
    directoryNameKeys,
    directoryCount: Object.keys(snapshot.records).length,
    scannedDirectories,
    cacheHit,
    repairCandidates
  };
}

export async function loadCaseInventory(
  workspaceRoot: string,
  sharedRuntimeRoot: string,
  concurrency: number,
  options: { forceRefresh?: boolean } = {}
): Promise<CaseInventoryResult> {
  const casesRoot = path.join(workspaceRoot, "cases");
  await fs.mkdir(casesRoot, { recursive: true });
  const names = (await fs.readdir(casesRoot, { withFileTypes: true }))
    .filter(entry => entry.isDirectory())
    .map(entry => entry.name);
  const currentFingerprint = fingerprint(names);
  const { file } = inventoryPaths(sharedRuntimeRoot);
  const initial = await readSnapshot(file);
  if (!options.forceRefresh && initial?.directory_fingerprint === currentFingerprint) {
    return resultFromSnapshot(initial, 0, true);
  }

  return withInventoryLock(sharedRuntimeRoot, async () => {
    const lockedNames = (await fs.readdir(casesRoot, { withFileTypes: true }))
      .filter(entry => entry.isDirectory())
      .map(entry => entry.name);
    const lockedFingerprint = fingerprint(lockedNames);
    const existing = await readSnapshot(file);
    if (!options.forceRefresh && existing?.directory_fingerprint === lockedFingerprint) {
      return resultFromSnapshot(existing, 0, true);
    }

    const namesSet = new Set(lockedNames);
    const records: Record<string, CachedCaseRecord> = {};
    if (existing && !options.forceRefresh) {
      for (const [name, record] of Object.entries(existing.records)) {
        if (namesSet.has(name)) records[name] = record;
      }
    }
    const pending = lockedNames.filter(name => !records[name]);
    let cursor = 0;
    const workers = Math.min(Math.max(1, concurrency), Math.max(1, pending.length));
    await Promise.all(Array.from({ length: workers }, async () => {
      for (;;) {
        const index = cursor++;
        if (index >= pending.length) return;
        const name = pending[index]!;
        records[name] = await readCaseRecord(casesRoot, name);
      }
    }));
    const snapshot: CaseInventorySnapshot = {
      schema: 2,
      generated_at: new Date().toISOString(),
      directory_fingerprint: lockedFingerprint,
      records
    };
    await writeSnapshot(file, snapshot);
    return resultFromSnapshot(snapshot, pending.length, false);
  });
}

export async function refreshCompletedCase(
  workspaceRoot: string,
  sharedRuntimeRoot: string,
  caseRoot: string | null
): Promise<void> {
  if (!caseRoot) return;
  const casesRoot = path.resolve(workspaceRoot, "cases");
  const resolvedCaseRoot = path.resolve(caseRoot);
  if (!resolvedCaseRoot.startsWith(`${casesRoot}${path.sep}`)) return;
  const directoryName = path.basename(resolvedCaseRoot);
  await withInventoryLock(sharedRuntimeRoot, async () => {
    const names = (await fs.readdir(casesRoot, { withFileTypes: true }))
      .filter(entry => entry.isDirectory())
      .map(entry => entry.name);
    const { file } = inventoryPaths(sharedRuntimeRoot);
    const existing = await readSnapshot(file);
    const namesSet = new Set(names);
    const records: Record<string, CachedCaseRecord> = {};
    if (existing) {
      for (const [name, record] of Object.entries(existing.records)) {
        if (namesSet.has(name)) records[name] = record;
      }
    }
    records[directoryName] = await readCaseRecord(casesRoot, directoryName);
    await writeSnapshot(file, {
      schema: 2,
      generated_at: new Date().toISOString(),
      directory_fingerprint: fingerprint(names),
      records
    });
  });
}

export async function indexedCaseDirectoryForCompany(
  workspaceRoot: string,
  companyId: string
): Promise<string | null> {
  const file = path.join(workspaceRoot, "crm_single_customer_capture", "app", "runtime", "case-inventory-v1.json");
  const snapshot = await readSnapshot(file);
  if (!snapshot) return null;
  for (const [directoryName, record] of Object.entries(snapshot.records)) {
    if (record.company_id === companyId) return directoryName;
  }
  return null;
}
