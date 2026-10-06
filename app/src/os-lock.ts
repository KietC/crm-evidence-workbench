import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import path from "node:path";
import fs from "node:fs/promises";

// The helper owns the kernel lock and holds a pipe from Node. EOF (including
// abrupt Node death) releases it, even while waiting for another owner.
// 锁由内核持有；Node 管道断开（包括强制终止）后自动释放，等待中的辅助进程也会退出。
const WINDOWS = `
$ErrorActionPreference = 'Stop'
$mutex = New-Object System.Threading.Mutex($false, $env:OKKI_LOCK_NAME)
$stream = [Console]::OpenStandardInput()
$buffer = New-Object byte[] 1
$eof = $stream.ReadAsync($buffer, 0, 1)
$held = $false
$deadline = [DateTime]::UtcNow.AddMilliseconds([int]$env:OKKI_LOCK_TIMEOUT)
try {
  while (!$held) {
    if ($eof.IsCompleted) { exit 0 }
    try { $held = $mutex.WaitOne(50) }
    catch [System.Threading.AbandonedMutexException] { $held = $true }
    if (!$held -and [DateTime]::UtcNow -ge $deadline) { throw 'OS lock timeout' }
  }
  if (!$eof.IsCompleted) {
    [Console]::Out.WriteLine('LOCKED')
    [Console]::Out.Flush()
    $eof.GetAwaiter().GetResult() | Out-Null
  }
} finally {
  if ($held) { $mutex.ReleaseMutex() }
  $mutex.Dispose()
  $stream.Dispose()
}
`;

const POSIX = `
import fcntl, os, sys, threading, time
closed = threading.Event()
threading.Thread(target=lambda: (sys.stdin.buffer.read(1), closed.set()), daemon=True).start()
deadline = time.monotonic() + int(os.environ['OKKI_LOCK_TIMEOUT']) / 1000
# Persistent inode: never unlink an advisory lock file while contenders exist.
# 锁文件 inode 必须持久保留；竞争者存在时不能删除以免形成两个独立锁。
fd = os.open(os.environ['OKKI_LOCK_FILE'], os.O_CREAT | os.O_RDWR, 0o600)
try:
    while not closed.is_set():
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline: raise TimeoutError('OS lock timeout')
            closed.wait(.05)
    if not closed.is_set():
        print('LOCKED', flush=True)
        closed.wait()
finally:
    os.close(fd)
`;

export async function withOsLock<T>(lockPath: string, action: () => Promise<T>, timeoutMs: number): Promise<T> {
  const resolved = path.join(await fs.realpath(path.dirname(path.resolve(lockPath))), path.basename(lockPath));
  const canonical = process.platform === "win32" ? resolved.toLowerCase() : resolved;
  const digest = createHash("sha256").update(canonical).digest("hex");
  const windows = process.platform === "win32";
  const child = spawn(windows ? "powershell.exe" : "python3", windows
    ? ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(WINDOWS, "utf16le").toString("base64")]
    : ["-u", "-c", POSIX], {
    windowsHide: true,
    stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, OKKI_LOCK_NAME: `Global\\OKKI_${digest}`,
      OKKI_LOCK_FILE: `${canonical}.os-lock`, OKKI_LOCK_TIMEOUT: String(timeoutMs) }
  });
  let diagnostic = "";
  child.stderr.on("data", chunk => { diagnostic = (diagnostic + String(chunk)).slice(-4000); });
  child.stdin.on("error", () => {});
  let acquired = false;
  let releasing = false;
  let output = "";
  const ended = new Promise<void>(resolve => { child.once("close", () => resolve()); child.once("error", () => resolve()); });
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`${path.basename(lockPath)} OS lock timeout`));
        child.stdin.end(); child.kill();
      }, timeoutMs + 10_000);
      child.stdout.on("data", chunk => {
        output += String(chunk);
        if (!acquired && /LOCKED\r?\n/.test(output)) {
          acquired = true; clearTimeout(timer); resolve();
        }
      });
      child.once("error", error => { clearTimeout(timer); reject(error); });
      child.once("close", code => {
        clearTimeout(timer);
        if (!acquired) reject(new Error(`${path.basename(lockPath)} OS lock failed (${code}): ${diagnostic}`));
        // Never permit application work to continue after losing the OS lease.
        // 内核租约意外丢失时立即失败关闭，不能继续执行应用写操作。
        else if (!releasing) process.exit(70);
      });
    });
    return await action();
  } finally {
    releasing = true;
    child.stdin.end();
    await ended;
  }
}
