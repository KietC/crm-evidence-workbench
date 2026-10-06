import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import type { CaseRepairCandidate } from "../case-inventory.js";
import { eligibleDeferredErrors, mergeInventoryRepairCandidates, type DeferredRepairEntry } from "../deferred-repair-queue.js";
import { removeStaleDirectoryLock, removeStalePidFileLock, withDirectoryLock } from "../runtime-lock.js";

function candidate(companyId: string, observedAt: string, phase = "incomplete"): CaseRepairCandidate {
  return {
    companyId,
    caseRoot: path.join("C:\\cases", companyId),
    sessionRoot: path.join("C:\\cases", companyId, "raw", "sessions", "latest"),
    jobId: "latest",
    terminalState: phase === "missing_terminal_status" ? "missing" : "failed",
    terminalPhase: phase,
    errors: phase === "missing_terminal_status" ? [] : ["structural failure"],
    warnings: [],
    reconciliationFailures: 1,
    observedAt
  };
}

test("inventory repair merge adds missing failures, reopens stale resolutions, and deduplicates", () => {
  const old = "2026-01-01T00:00:00.000Z";
  const recent = "2026-01-02T00:00:00.000Z";
  const duplicate: DeferredRepairEntry = {
    entry_id: "legacy-a", status: "pending_warning", company_id: "3001", case_root: null,
    session_root: null, job_id: null, terminal_phase: "complete", error_count: 0,
    warning_count: 1, reconciliation_failures: 0, errors: [], warnings: ["warning"], attempts: 0,
    first_recorded_at: old, updated_at: old
  };
  const resolved: DeferredRepairEntry = {
    ...duplicate, entry_id: "legacy-b", status: "resolved", company_id: "3001", resolved_at: old
  };

  const merged = mergeInventoryRepairCandidates(
    [duplicate, resolved],
    [candidate("3001", recent), candidate("3002", recent, "missing_terminal_status")],
    "2026-01-03T00:00:00.000Z"
  );
  assert.equal(merged.entries.length, 2);
  assert.equal(merged.report.deduplicated, 1);
  assert.equal(merged.report.updated + merged.report.reopened, 1);
  assert.equal(merged.report.added, 1);
  assert.equal(merged.entries.every(entry => entry.status === "pending_error"), true);
  assert.equal(new Set(merged.entries.map(entry => entry.company_id)).size, 2);
});

test("a resolution newer than the observed failed terminal is not reopened", () => {
  const entry: DeferredRepairEntry = {
    entry_id: "resolved", status: "resolved", company_id: "4001", case_root: null,
    session_root: null, job_id: null, terminal_phase: "complete", error_count: 0,
    warning_count: 0, reconciliation_failures: 0, errors: [], warnings: [], attempts: 1,
    first_recorded_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-03T00:00:00.000Z",
    resolved_at: "2026-01-03T00:00:00.000Z"
  };
  const merged = mergeInventoryRepairCandidates([entry], [candidate("4001", "2026-01-02T00:00:00.000Z")]);
  assert.equal(merged.entries[0]?.status, "resolved");
  assert.equal(merged.report.unchanged, 1);
});

test("deferred error selection rotates lower attempts first and enforces the retry ceiling", () => {
  const base: DeferredRepairEntry = {
    entry_id: "base", status: "pending_error", company_id: "5001", case_root: null,
    session_root: null, job_id: null, terminal_phase: "incomplete", error_count: 1,
    warning_count: 0, reconciliation_failures: 1, errors: ["failure"], warnings: [], attempts: 0,
    first_recorded_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z"
  };
  const entries: DeferredRepairEntry[] = [
    { ...base, entry_id: "two", company_id: "5002", attempts: 2 },
    { ...base, entry_id: "zero", attempts: 0, updated_at: "2026-01-02T00:00:00.000Z" },
    { ...base, entry_id: "limit", company_id: "5003", attempts: 3 },
    { ...base, entry_id: "warning", company_id: "5004", status: "pending_warning" }
  ];
  assert.deepEqual(eligibleDeferredErrors(entries, 3).map(entry => entry.entry_id), ["zero", "two"]);
  assert.deepEqual(eligibleDeferredErrors(entries, 1).map(entry => entry.entry_id), ["zero"]);
});

test("runtime locks preserve live owners and self-heal dead or legacy stale owners", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "okki-locks-"));
  try {
    const pidLock = path.join(root, "monitor.lock");
    await fs.writeFile(pidLock, "2147483647\n", "utf8");
    assert.equal(await removeStalePidFileLock(pidLock), true);

    const liveDirectory = path.join(root, "live.lock");
    let removedWhileHeld = true;
    await withDirectoryLock(liveDirectory, async () => {
      removedWhileHeld = await removeStaleDirectoryLock(liveDirectory, 1, Date.now() + 60_000);
    });
    assert.equal(removedWhileHeld, false);
    await assert.rejects(fs.stat(liveDirectory));

    const legacyDirectory = path.join(root, "legacy.lock");
    await fs.mkdir(legacyDirectory);
    const old = new Date(Date.now() - 10 * 60_000);
    await fs.utimes(legacyDirectory, old, old);
    assert.equal(await removeStaleDirectoryLock(legacyDirectory, 5 * 60_000), true);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
