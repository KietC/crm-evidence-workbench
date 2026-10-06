/** Synthetic process launch regression; never opens a browser or a CRM page.
 * 合成进程启动回归测试；不打开浏览器或 CRM 页面。 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const appRoot = fileURLToPath(new URL("../../", import.meta.url));
const helper = path.join(appRoot, "scripts", "native-arguments.ps1");

test("native launcher preserves a script and output path containing spaces", { skip: process.platform !== "win32" }, async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "evidence trail launch "));
  const entry = path.join(temporary, "entry with spaces.cjs");
  const output = path.join(temporary, "output with spaces.json");
  await fs.writeFile(entry, "require('node:fs').writeFileSync(process.argv[2], JSON.stringify(process.argv.slice(2)));", "utf8");
  const ps = (value: string): string => `'${value.replaceAll("'", "''")}'`;
  const command = [
    "$ErrorActionPreference = 'Stop'",
    `. ${ps(helper)}`,
    `$arguments = @((ConvertTo-NativePathArgument ${ps(entry)}), (ConvertTo-NativePathArgument ${ps(output)}))`,
    `$child = Start-Process -FilePath ${ps(process.execPath)} -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru`,
    "exit $child.ExitCode"
  ].join("; ");
  try {
    execFileSync("powershell.exe", ["-NoProfile", "-Command", command], { windowsHide: true, timeout: 20_000, stdio: "pipe" });
    assert.deepEqual(JSON.parse(await fs.readFile(output, "utf8")), [output]);
    const start = await fs.readFile(path.join(appRoot, "scripts", "start.ps1"), "utf8");
    assert.match(start, /ConvertTo-NativePathArgument \$MonitorEntry/);
    assert.match(start, /ConvertTo-NativePathArgument \$Entry/);
  } finally {
    await fs.rm(temporary, { recursive: true, force: true });
  }
});
