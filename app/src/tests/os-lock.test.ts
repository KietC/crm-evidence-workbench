import test from "node:test";
import assert from "node:assert/strict";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const moduleUrl = new URL("../runtime-lock.js", import.meta.url).href;

function worker(lock: string, label: string, log: string, cleanup = false): ChildProcessWithoutNullStreams {
  const source = `
    import fs from 'node:fs/promises';
    import { withDirectoryLock, removeStaleDirectoryLock } from ${JSON.stringify(moduleUrl)};
    const lock = ${JSON.stringify(lock)};
    ${cleanup ? "await removeStaleDirectoryLock(lock, 1, Date.now() + 60000);" : ""}
    await withDirectoryLock(lock, async () => {
      await fs.appendFile(${JSON.stringify(log)}, ${JSON.stringify(label + ":enter\n")});
      console.log('ENTERED');
      await new Promise(resolve => process.stdin.once('data', resolve));
      process.stdin.pause();
      process.stdin.unref?.();
      await fs.appendFile(${JSON.stringify(log)}, ${JSON.stringify(label + ":exit\n")});
    }, { timeoutMs: 15000 });
  `;
  return spawn(process.execPath, ["--input-type=module", "-e", source], { stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
}

function observe(child: ChildProcessWithoutNullStreams) {
  let output = "";
  let stderr = "";
  let entered = false;
  child.stderr.on("data", chunk => { stderr += String(chunk); });
  const entry = new Promise<void>((resolve, reject) => {
    const deadline = setTimeout(() => reject(new Error(`worker entry timeout: ${stderr}`)), 20000);
    child.stdout.on("data", chunk => {
      output += String(chunk);
      if (output.includes("ENTERED")) { entered = true; clearTimeout(deadline); resolve(); }
    });
    child.once("close", code => { clearTimeout(deadline); if (!entered) reject(new Error(`worker ended ${code}: ${stderr}`)); });
    child.once("error", reject);
  });
  const exit = boundedExit(child);
  void entry.catch(() => {});
  void exit.catch(() => {});
  return { entry, exit, entered: () => entered };
}

function boundedExit(child: ChildProcessWithoutNullStreams, timeoutMs = 25000): Promise<number | null> {
  if (child.exitCode !== null) return Promise.resolve(child.exitCode);
  return new Promise((resolve, reject) => {
    const deadline = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("synthetic worker exit timeout")); }, timeoutMs);
    child.once("close", code => { clearTimeout(deadline); resolve(code); });
    child.once("error", error => { clearTimeout(deadline); reject(error); });
  });
}

async function cleanupWorkers(children: ChildProcessWithoutNullStreams[]): Promise<void> {
  await Promise.all(children.map(async child => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const exited = boundedExit(child, 10000);
    child.stdin.end();
    child.kill("SIGKILL");
    await exited;
  }));
}

test("OS mutex serializes concurrent stale cleanup and release preserves replacement metadata", { timeout: 60000 }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "okki-os-lock-synthetic-"));
  const lock = path.join(root, "queue.lock");
  const log = path.join(root, "events.txt");
  const children: ChildProcessWithoutNullStreams[] = [];
  try {
    await fs.mkdir(lock);
    await fs.writeFile(path.join(lock, "owner.json"), JSON.stringify({ schema: 1, pid: 2147483647, acquired_at: "2020-01-01" }));
    const a = worker(lock, "A", log, true); children.push(a);
    const aState = observe(a); await aState.entry;
    // This is the formerly vulnerable window: dead-owner directory was removed,
    // A owns a new live kernel lock and B/C simultaneously attempt stale cleanup.
    await fs.mkdir(lock);
    const replacement = JSON.stringify({ schema: 1, pid: process.pid, acquired_at: "replacement" });
    await fs.writeFile(path.join(lock, "owner.json"), replacement);
    const b = worker(lock, "B", log, true); const c = worker(lock, "C", log, true);
    children.push(b, c);
    const bState = observe(b); const cState = observe(c);
    await new Promise(resolve => setTimeout(resolve, 2500));
    assert.equal(bState.entered(), false); assert.equal(cState.entered(), false);
    a.stdin.end("release\n"); assert.equal(await aState.exit, 0);
    assert.equal(await fs.readFile(path.join(lock, "owner.json"), "utf8"), replacement);
    const next = await Promise.race([bState.entry.then(() => "B"), cState.entry.then(() => "C")]);
    if (next === "B") { assert.equal(cState.entered(), false); b.stdin.end("release\n"); await bState.exit; await cState.entry; c.stdin.end("release\n"); await cState.exit; }
    else { assert.equal(bState.entered(), false); c.stdin.end("release\n"); await cState.exit; await bState.entry; b.stdin.end("release\n"); await bState.exit; }
    const events = (await fs.readFile(log, "utf8")).trim().split(/\r?\n/);
    let active = 0;
    for (const event of events) { active += event.endsWith(":enter") ? 1 : -1; assert.ok(active >= 0 && active <= 1, events.join(",")); }
    assert.equal(active, 0);
  } finally {
    await cleanupWorkers(children);
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("killed Node owner releases helper-held OS mutex without stale timeout", { timeout: 45000 }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "okki-os-lock-kill-"));
  const lock = path.join(root, "owner.lock"); const log = path.join(root, "events.txt");
  const a = worker(lock, "A", log); const aState = observe(a);
  let b: ChildProcessWithoutNullStreams | undefined;
  try {
    await aState.entry;
    b = worker(lock, "B", log); const bState = observe(b);
    await new Promise(resolve => setTimeout(resolve, 1500)); assert.equal(bState.entered(), false);
    a.kill("SIGKILL"); await aState.exit;
    await bState.entry;
    b.stdin.end("release\n"); assert.equal(await bState.exit, 0);
  } finally {
    await cleanupWorkers(b ? [a, b] : [a]);
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("killed waiting Node owner leaves no helper able to acquire the next lease", { timeout: 45000 }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "okki-os-lock-wait-kill-"));
  const lock = path.join(root, "owner.lock"); const log = path.join(root, "events.txt");
  const a = worker(lock, "A", log); const aState = observe(a);
  const children = [a];
  try {
    await aState.entry;
    const waiting = worker(lock, "KILLED", log); children.push(waiting);
    const waitingExit = boundedExit(waiting);
    waiting.stderr.resume(); waiting.stdout.resume();
    await new Promise(resolve => setTimeout(resolve, 1500));
    waiting.kill("SIGKILL"); await waitingExit;
    const b = worker(lock, "B", log); children.push(b); const bState = observe(b);
    a.stdin.end("release\n"); await aState.exit;
    await bState.entry;
    b.stdin.end("release\n"); assert.equal(await bState.exit, 0);
    assert.equal((await fs.readFile(log, "utf8")).includes("KILLED:enter"), false);
  } finally {
    await cleanupWorkers(children);
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("contender OS wait has a deadline and never enters while holder is active", { timeout: 45000 }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "okki-os-lock-deadline-"));
  const lock = path.join(root, "owner.lock"); const log = path.join(root, "events.txt");
  const a = worker(lock, "A", log); const aState = observe(a);
  let contender: ChildProcessWithoutNullStreams | undefined;
  try {
    await aState.entry;
    const source = `import { withDirectoryLock } from ${JSON.stringify(moduleUrl)};
      await withDirectoryLock(${JSON.stringify(lock)}, async () => { console.log('UNEXPECTED'); }, {timeoutMs:1000});`;
    contender = spawn(process.execPath, ["--input-type=module", "-e", source], { stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
    let stdout = ""; let stderr = "";
    contender.stdout.on("data", chunk => { stdout += String(chunk); });
    contender.stderr.on("data", chunk => { stderr += String(chunk); });
    const code = await boundedExit(contender);
    assert.notEqual(code, 0); assert.match(stderr, /OS lock timeout/); assert.equal(stdout.includes("UNEXPECTED"), false);
    a.stdin.end("release\n"); assert.equal(await aState.exit, 0);
  } finally {
    await cleanupWorkers(contender ? [a, contender] : [a]);
    await fs.rm(root, { recursive: true, force: true });
  }
});
