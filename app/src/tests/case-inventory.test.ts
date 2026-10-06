import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { loadCaseInventory, refreshCompletedCase } from "../case-inventory.js";

async function writeCase(root: string, directory: string, companyId: string, status: "pass" | "incomplete"): Promise<string> {
  const caseRoot = path.join(root, "cases", directory);
  await fs.mkdir(path.join(caseRoot, "manifests"), { recursive: true });
  const sessionRoot = path.join(caseRoot, "raw", "sessions", "session-1");
  await fs.mkdir(sessionRoot, { recursive: true });
  await fs.writeFile(path.join(caseRoot, "case_identity.json"), JSON.stringify({ company_id: companyId }), "utf8");
  await fs.writeFile(path.join(caseRoot, "manifests", "processing_scope_latest.json"), JSON.stringify({ capture_status: status }), "utf8");
  await fs.writeFile(path.join(sessionRoot, "final_status.json"), JSON.stringify({
    id: "session-1",
    phase: status === "pass" ? "complete" : "incomplete",
    errors: status === "pass" ? [] : ["structural reconciliation failed"],
    warnings: [],
    metrics: { reconciliation_failures: status === "pass" ? 0 : 1 }
  }), "utf8");
  return caseRoot;
}

test("case inventory is shared, incremental, and refreshes an existing completed case", async () => {
  const workspace = await fs.mkdtemp(path.join(os.tmpdir(), "okki-case-inventory-"));
  const runtime = path.join(workspace, "runtime");
  try {
    await writeCase(workspace, "case-a", "1001", "pass");
    const incompleteRoot = await writeCase(workspace, "case-b", "1002", "incomplete");

    const first = await loadCaseInventory(workspace, runtime, 8);
    assert.equal(first.scannedDirectories, 2);
    assert.equal(first.completedCompanyIds.has("1001"), true);
    assert.equal(first.completedCompanyIds.has("1002"), false);

    const second = await loadCaseInventory(workspace, runtime, 8);
    assert.equal(second.cacheHit, true);
    assert.equal(second.scannedDirectories, 0);

    await writeCase(workspace, "case-c", "1003", "pass");
    const third = await loadCaseInventory(workspace, runtime, 8);
    assert.equal(third.scannedDirectories, 1);
    assert.equal(third.completedCompanyIds.has("1003"), true);

    const repairedSession = path.join(incompleteRoot, "raw", "sessions", "session-2");
    await fs.mkdir(repairedSession, { recursive: true });
    await fs.writeFile(path.join(repairedSession, "final_status.json"), JSON.stringify({
      id: "session-2", phase: "complete", errors: [], warnings: [], metrics: { reconciliation_failures: 0 }
    }), "utf8");
    const future = new Date(Date.now() + 2_000);
    await fs.utimes(path.join(repairedSession, "final_status.json"), future, future);
    await refreshCompletedCase(workspace, runtime, incompleteRoot);
    const refreshed = await loadCaseInventory(workspace, runtime, 8);
    assert.equal(refreshed.completedCompanyIds.has("1002"), true);
    assert.equal(refreshed.scannedDirectories, 0);
  } finally {
    await fs.rm(workspace, { recursive: true, force: true });
  }
});

test("latest failed terminal overrides a stale pass scope and missing terminal is repairable", async () => {
  const workspace = await fs.mkdtemp(path.join(os.tmpdir(), "okki-case-terminal-"));
  const runtime = path.join(workspace, "runtime");
  try {
    const stalePass = await writeCase(workspace, "stale-pass", "2001", "pass");
    const failedSession = path.join(stalePass, "raw", "sessions", "session-2");
    await fs.mkdir(failedSession, { recursive: true });
    await fs.writeFile(path.join(failedSession, "final_status.json"), JSON.stringify({
      id: "session-2", phase: "incomplete", errors: ["latest failed"], warnings: [], metrics: { reconciliation_failures: 1 }
    }), "utf8");
    const future = new Date(Date.now() + 2_000);
    await fs.utimes(path.join(failedSession, "final_status.json"), future, future);

    const missingRoot = path.join(workspace, "cases", "missing-terminal");
    await fs.mkdir(path.join(missingRoot, "raw", "sessions", "session-1"), { recursive: true });
    await fs.mkdir(path.join(missingRoot, "manifests"), { recursive: true });
    await fs.writeFile(path.join(missingRoot, "case_identity.json"), JSON.stringify({ company_id: "2002" }), "utf8");
    await fs.writeFile(path.join(missingRoot, "manifests", "processing_scope_latest.json"), JSON.stringify({ capture_status: "pass" }), "utf8");

    const inventory = await loadCaseInventory(workspace, runtime, 4);
    assert.equal(inventory.completedCompanyIds.has("2001"), false);
    assert.equal(inventory.completedCompanyIds.has("2002"), false);
    assert.equal(inventory.repairCandidates.length, 2);
    assert.deepEqual(
      inventory.repairCandidates.map(item => item.terminalState).sort(),
      ["failed", "missing"]
    );
  } finally {
    await fs.rm(workspace, { recursive: true, force: true });
  }
});

test("coordinator force refresh detects a terminal written after the directory-name cache", async () => {
  const workspace = await fs.mkdtemp(path.join(os.tmpdir(), "okki-case-force-refresh-"));
  const runtime = path.join(workspace, "runtime");
  try {
    const caseRoot = await writeCase(workspace, "case-a", "3001", "pass");
    const initial = await loadCaseInventory(workspace, runtime, 4);
    assert.equal(initial.completedCompanyIds.has("3001"), true);

    const failedSession = path.join(caseRoot, "raw", "sessions", "session-after-cache");
    await fs.mkdir(failedSession, { recursive: true });
    await fs.writeFile(path.join(failedSession, "final_status.json"), JSON.stringify({
      id: "session-after-cache", phase: "incomplete", errors: ["late failure"], warnings: [],
      metrics: { reconciliation_failures: 1 }
    }), "utf8");
    const future = new Date(Date.now() + 2_000);
    await fs.utimes(path.join(failedSession, "final_status.json"), future, future);

    const cached = await loadCaseInventory(workspace, runtime, 4);
    assert.equal(cached.cacheHit, true);
    assert.equal(cached.completedCompanyIds.has("3001"), true);
    const forced = await loadCaseInventory(workspace, runtime, 4, { forceRefresh: true });
    assert.equal(forced.cacheHit, false);
    assert.equal(forced.completedCompanyIds.has("3001"), false);
    assert.equal(forced.repairCandidates.length, 1);
  } finally {
    await fs.rm(workspace, { recursive: true, force: true });
  }
});
