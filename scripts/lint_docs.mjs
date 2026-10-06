// Lint only the public guides; never traverse runtime/customer directories.
// 仅检查公开指南，不遍历运行时或客户目录。
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { lint } from "markdownlint/promise";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const files = ["README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md"];
for (const directory of ["docs", "app"]) {
  const entries = await fs.readdir(path.join(root, directory), { withFileTypes: true });
  for (const entry of entries) {
    if (entry.isFile() && entry.name.endsWith(".md")) files.push(`${directory}/${entry.name}`);
  }
}
const config = JSON.parse(await fs.readFile(path.join(root, ".markdownlint.json"), "utf8"));
const result = await lint({ files: files.sort().map(file => path.join(root, file)), config });
const diagnostics = Object.entries(result).flatMap(([file, errors]) =>
  errors.map(error => `${path.relative(root, file)}:${error.lineNumber} ${error.ruleNames[0]} ${error.ruleDescription}`));
if (diagnostics.length) {
  console.error(diagnostics.join("\n"));
  process.exitCode = 1;
} else {
  console.log(`Markdown: ${files.length} public guides, 0 issues / 公开指南格式零问题`);
}
