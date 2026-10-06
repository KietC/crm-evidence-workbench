/** Shared repair ordering uses explicit identity and terminal outcomes, not folder names.
 * 共享修复队列以明确身份和终态排序，不能仅依赖文件夹名称。 */
import { createHash } from "node:crypto";

import type { CaseRepairCandidate } from "./case-inventory.js";

export type DeferredRepairStatus = "pending_error" | "pending_warning" | "resolved";

export interface DeferredRepairEntry {
  entry_id: string;
  status: DeferredRepairStatus;
  company_id: string | null;
  case_root: string | null;
  session_root: string | null;
  job_id: string | null;
  terminal_phase: string;
  error_count: number;
  warning_count: number;
  reconciliation_failures: number;
  errors: string[];
  warnings: string[];
  attempts: number;
  attempt_reset_versions?: string[];
  first_recorded_at: string;
  updated_at: string;
  resolved_at?: string;
  source?: "capture" | "inventory_reconciliation";
}

export interface DeferredRepairQueue {
  schema: 1;
  updated_at: string;
  entries: DeferredRepairEntry[];
}

export interface DeferredInventoryMergeReport {
  scanned_candidates: number;
  added: number;
  updated: number;
  reopened: number;
  deduplicated: number;
  unchanged: number;
}

export function eligibleDeferredErrors(entries: DeferredRepairEntry[], maxAttempts: number): DeferredRepairEntry[] {
  const boundedAttempts = Math.min(10, Math.max(1, Math.trunc(maxAttempts)));
  return entries
    .filter(entry => entry.status === "pending_error" && entry.attempts < boundedAttempts)
    .sort((left, right) => left.attempts - right.attempts || Date.parse(left.updated_at) - Date.parse(right.updated_at));
}

function stableEntryId(companyId: string): string {
  return createHash("sha256").update(companyId).digest("hex").slice(0, 24);
}

function severity(status: DeferredRepairStatus): number {
  return status === "pending_error" ? 2 : status === "pending_warning" ? 1 : 0;
}

function deduplicate(entries: DeferredRepairEntry[]): { entries: DeferredRepairEntry[]; removed: number } {
  const byIdentity = new Map<string, DeferredRepairEntry>();
  let removed = 0;
  for (const entry of entries) {
    const identity = entry.company_id && /^\d+$/.test(entry.company_id)
      ? `company:${entry.company_id}`
      : `entry:${entry.entry_id}`;
    const existing = byIdentity.get(identity);
    if (!existing) {
      byIdentity.set(identity, { ...entry, errors: [...entry.errors], warnings: [...entry.warnings] });
      continue;
    }
    removed += 1;
    const preferred = severity(entry.status) > severity(existing.status)
      || (severity(entry.status) === severity(existing.status) && Date.parse(entry.updated_at) > Date.parse(existing.updated_at))
      ? entry
      : existing;
    const merged: DeferredRepairEntry = {
      ...preferred,
      entry_id: preferred.company_id && /^\d+$/.test(preferred.company_id) ? stableEntryId(preferred.company_id) : preferred.entry_id,
      attempts: Math.max(existing.attempts, entry.attempts),
      first_recorded_at: Date.parse(existing.first_recorded_at) <= Date.parse(entry.first_recorded_at)
        ? existing.first_recorded_at
        : entry.first_recorded_at,
      errors: [...preferred.errors],
      warnings: [...preferred.warnings]
    };
    byIdentity.set(identity, merged);
  }
  return { entries: [...byIdentity.values()], removed };
}

export function mergeInventoryRepairCandidates(
  entries: DeferredRepairEntry[],
  candidates: CaseRepairCandidate[],
  now = new Date().toISOString()
): { entries: DeferredRepairEntry[]; report: DeferredInventoryMergeReport } {
  const deduplicated = deduplicate(entries);
  const nextEntries = deduplicated.entries;
  const byCompany = new Map(nextEntries
    .filter(entry => entry.company_id && /^\d+$/.test(entry.company_id))
    .map(entry => [entry.company_id!, entry]));
  const report: DeferredInventoryMergeReport = {
    scanned_candidates: candidates.length,
    added: 0,
    updated: 0,
    reopened: 0,
    deduplicated: deduplicated.removed,
    unchanged: 0
  };

  const candidateByCompany = new Map<string, CaseRepairCandidate>();
  for (const candidate of candidates) {
    const prior = candidateByCompany.get(candidate.companyId);
    if (!prior || Date.parse(candidate.observedAt) >= Date.parse(prior.observedAt)) {
      candidateByCompany.set(candidate.companyId, candidate);
    }
  }

  for (const candidate of candidateByCompany.values()) {
    const existing = byCompany.get(candidate.companyId);
    const observedMs = Date.parse(candidate.observedAt);
    const resolvedMs = existing?.resolved_at ? Date.parse(existing.resolved_at) : Number.NaN;
    if (existing?.status === "resolved" && Number.isFinite(resolvedMs) && resolvedMs >= observedMs) {
      report.unchanged += 1;
      continue;
    }

    const errors = candidate.errors.length > 0
      ? [...candidate.errors]
      : [`本地终态完整性: ${candidate.terminalPhase}`];
    const alreadyCurrent = existing?.status === "pending_error"
      && existing.case_root === candidate.caseRoot
      && existing.session_root === candidate.sessionRoot
      && existing.job_id === candidate.jobId
      && existing.terminal_phase === candidate.terminalPhase
      && existing.reconciliation_failures === Math.max(1, candidate.reconciliationFailures)
      && JSON.stringify(existing.errors) === JSON.stringify(errors)
      && JSON.stringify(existing.warnings) === JSON.stringify(candidate.warnings);
    if (alreadyCurrent) {
      report.unchanged += 1;
      continue;
    }
    const replacement: DeferredRepairEntry = {
      entry_id: stableEntryId(candidate.companyId),
      status: "pending_error",
      company_id: candidate.companyId,
      case_root: candidate.caseRoot,
      session_root: candidate.sessionRoot,
      job_id: candidate.jobId,
      terminal_phase: candidate.terminalPhase,
      error_count: errors.length,
      warning_count: candidate.warnings.length,
      reconciliation_failures: Math.max(1, candidate.reconciliationFailures),
      errors,
      warnings: [...candidate.warnings],
      attempts: existing?.attempts ?? 0,
      first_recorded_at: existing?.first_recorded_at ?? now,
      updated_at: now,
      source: "inventory_reconciliation"
    };
    if (!existing) {
      nextEntries.push(replacement);
      byCompany.set(candidate.companyId, replacement);
      report.added += 1;
    } else {
      if (existing.status === "resolved") report.reopened += 1;
      else report.updated += 1;
      Object.assign(existing, replacement);
      delete existing.resolved_at;
    }
  }

  report.unchanged += candidates.length - candidateByCompany.size;
  return { entries: nextEntries, report };
}
