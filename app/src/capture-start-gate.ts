import { AsyncLocalStorage } from "node:async_hooks";

export class CaptureBusyError extends Error {
  constructor() { super("capture already running or starting"); }
}

/** Reserve synchronously, before browser/lock/file awaits; nested owner work shares the lease.
 * 在浏览器、锁和文件异步等待之前同步占位；同一所有者的嵌套任务共享租约。 */
export class CaptureStartGate {
  private owner: object | null = null;
  private users = 0;
  private readonly context = new AsyncLocalStorage<object>();

  async run<T>(work: () => Promise<T>, inheritOwner = true): Promise<T> {
    const inherited = inheritOwner ? this.context.getStore() : undefined;
    if (this.owner && inherited !== this.owner) throw new CaptureBusyError();
    const owner = this.owner ?? {};
    this.owner = owner;
    this.users++;
    try { return await this.context.run(owner, work); }
    finally {
      if (--this.users === 0) this.owner = null;
    }
  }
}

export async function ensureCustomerIdentity(
  companyId: string,
  currentId: () => Promise<string | null>,
  open: (id: string) => Promise<unknown>
): Promise<void> {
  if (!/^\d+$/.test(companyId)) throw new Error("invalid resume customer identity");
  if (await currentId() !== companyId) await open(companyId);
  if (await currentId() !== companyId) throw new Error("resume customer identity mismatch");
}

export function reservationExpired(pidState: "alive" | "dead" | "unknown", claimedAt: string, now = Date.now()): boolean {
  if (pidState === "alive") return false;
  if (pidState === "dead") return true;
  const claimed = Date.parse(claimedAt);
  return Number.isFinite(claimed) && now - claimed > 12 * 60 * 60_000;
}

export function reservationOwnerMatches(
  record: { instance_id: string; pid: number; company_id: string; claimed_at: string },
  expected: { instanceId: string; pid: number; companyId: string; claimedAt: string }
): boolean {
  return record.instance_id === expected.instanceId && record.pid === expected.pid
    && record.company_id === expected.companyId && record.claimed_at === expected.claimedAt;
}
