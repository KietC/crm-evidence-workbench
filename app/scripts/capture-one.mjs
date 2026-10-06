/** Windows one-shot launcher; identity is explicit and auto-next is always disabled.
 * Windows 单客户启动器；身份必须明确，自动下一客户始终关闭。 */
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(2);
}
const values = new Map();
const flags = new Set();
const args = process.argv.slice(2);
for (let index = 0; index < args.length; index += 1) {
  const arg = args[index];
  if (arg === "--company-id") {
    const value = args[index + 1];
    if (!value) fail("--company-id requires a value");
    values.set(arg, value);
    index += 1;
  } else if (["--no-auto-next", "--resume-existing", "--revisit-ui-gaps", "--skip-build", "--no-profile-clone", "--clone-local-profile"].includes(arg)) {
    flags.add(arg);
  } else {
    fail(`unsupported argument: ${arg}`);
  }
}

const companyId = values.get("--company-id") ?? "";
if (!/^\d+$/.test(companyId)) {
  fail("usage: node capture-one.mjs --company-id DIGITS --no-auto-next [--resume-existing] [--skip-build]");
}

const powerShellArgs = [
  "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
  "-File", path.join(here, "capture-one.ps1"),
  "-CompanyId", companyId,
  "-NoAutoNext"
];
if (flags.has("--resume-existing")) powerShellArgs.push("-ResumeExisting");
if (flags.has("--skip-build")) powerShellArgs.push("-SkipBuild");
if (flags.has("--no-profile-clone")) powerShellArgs.push("-NoProfileClone");
if (flags.has("--clone-local-profile")) powerShellArgs.push("-CloneLocalProfile");
if (flags.has("--revisit-ui-gaps")) powerShellArgs.push("-RevisitUiGaps");

const result = spawnSync("powershell.exe", powerShellArgs, { cwd: here, stdio: "inherit", windowsHide: false });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
