import type { JobStatus } from "./types.js";

export const AUTO_NEXT_DELAY_MS = 15_000;

export type AutoNextDecision = "advance" | "stop_cancelled" | "ignore";

/**
 * Every non-cancelled terminal outcome advances after it has been durably
 * recorded. Deferred warnings/errors are repaired after the main queue ends.
 * 非人工取消的终态先耐久记录再推进；延期问题在主队列结束后修复，单客户模式不使用本策略。
 */
export function autoNextDecision(job: JobStatus): AutoNextDecision {
  if (job.running) return "ignore";
  if (job.phase === "cancelled") return "stop_cancelled";
  if (job.phase === "complete" || job.phase === "incomplete" || job.phase === "failed" || job.errors.length > 0) return "advance";
  return "ignore";
}
