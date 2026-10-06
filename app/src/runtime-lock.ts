import fs from "node:fs/promises";
import path from "node:path";
import { withOsLock } from "./os-lock.js";

interface DirectoryLockOwner {
  schema: 1;
  pid: number;
  acquired_at: string;
}

const OWNER_FILE = "owner.json";

export function pidIsAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export async function removeStalePidFileLock(lockFile: string): Promise<boolean> {
  let pid = 0;
  try { pid = Number((await fs.readFile(lockFile, "utf8")).trim()); }
  catch { return false; }
  if (pidIsAlive(pid)) return false;
  await fs.rm(lockFile, { force: true });
  return true;
}

async function readDirectoryOwner(lockDirectory: string): Promise<DirectoryLockOwner | null> {
  try {
    const value = JSON.parse(await fs.readFile(path.join(lockDirectory, OWNER_FILE), "utf8")) as DirectoryLockOwner;
    if (value.schema === 1 && Number.isInteger(value.pid) && value.pid > 0) return value;
  } catch { /* legacy directory locks do not have owner metadata */ }
  return null;
}

export async function removeStaleDirectoryLock(
  lockDirectory: string,
  staleAfterMs = 5 * 60_000,
  nowMs = Date.now()
): Promise<boolean> {
  try {
    return await withOsLock(lockDirectory, () => removeLegacyDirectoryOwned(lockDirectory, staleAfterMs, nowMs), 1_000);
  } catch { return false; } // A live kernel owner is never reclaimed.
}

async function removeLegacyDirectoryOwned(lockDirectory: string, staleAfterMs: number, nowMs: number): Promise<boolean> {
  let stat;
  try { stat = await fs.stat(lockDirectory); }
  catch { return false; }
  if (!stat.isDirectory()) return false;

  const owner = await readDirectoryOwner(lockDirectory);
  if (owner && pidIsAlive(owner.pid)) return false;
  if (!owner && nowMs - stat.mtimeMs <= staleAfterMs) return false;

  await fs.rm(lockDirectory, { recursive: true, force: true });
  return true;
}

export async function withDirectoryLock<T>(
  lockDirectory: string,
  action: () => Promise<T>,
  options: { timeoutMs?: number; staleAfterMs?: number; retryMs?: number } = {}
): Promise<T> {
  const timeoutMs = Math.max(1_000, options.timeoutMs ?? 120_000);
  await fs.mkdir(path.dirname(lockDirectory), { recursive: true });
  // Directory remnants are legacy metadata, never the synchronization primitive.
  // Kernel mutex/flock has no stale-owner deletion or path-replacement race.
  // 旧锁目录只是元数据；实际同步由内核锁负责，不靠删除旧所有者目录抢锁。
  return withOsLock(lockDirectory, action, timeoutMs);
}
