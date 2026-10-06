export type CodexWakeEventKind = "capture_terminal" | "queue_exhausted";
export type ReviewerDisposition = "keep" | "stop_obsolete" | "stop_timeout";

export interface CurrentReviewEventState {
  event_id: string;
  status: string;
}

/**
 * Capture reviews are deliberately single-turn. Disabling the goals feature
 * for that resumed CLI invocation prevents the persistent goal runtime from
 * immediately starting another model turn while the next capture is running.
 * Queue exhaustion keeps goals enabled so the final reviewer can complete it.
 * 单客户终态仅触发一次复验，避免采集期间产生连续模型回合；队列耗尽事件另行收尾。
 */
export function codexResumeArgs(kind: CodexWakeEventKind, threadId: string): string[] {
  return [
    "exec",
    "resume",
    ...(kind === "capture_terminal" ? ["--disable", "goals"] : []),
    "--skip-git-repo-check",
    threadId,
    "-"
  ];
}

export function reviewerDisposition(
  activeEventId: string,
  activeSinceMs: number,
  current: CurrentReviewEventState | null,
  nowMs: number,
  timeoutMs: number
): ReviewerDisposition {
  if (!current || current.event_id !== activeEventId || current.status !== "claimed") return "stop_obsolete";
  if (nowMs - activeSinceMs >= Math.max(30_000, timeoutMs)) return "stop_timeout";
  return "keep";
}
