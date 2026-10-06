import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright-core";

// Local synthetic page only; a browser must be supplied explicitly, with no logged-in profile.
// 仅测试合成的本地页面；必须指定浏览器，不能使用已登录的浏览器配置。
const executablePath = process.env.OKKI_BROWSER_EXECUTABLE;
if (!executablePath) throw new Error("Set OKKI_BROWSER_EXECUTABLE to a Chrome/Chromium executable for this optional smoke test.");
await fs.access(executablePath);
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "okki-mhtml-smoke-"));
const source = path.join(temporary, "source.html");
const archive = path.join(temporary, "snapshot.mhtml");
await fs.writeFile(source, "<!doctype html><html><head><title>MHTML smoke</title><style>body{color:#123}</style></head><body><div id='ok'>local snapshot</div></body></html>", "utf8");
const browser = await chromium.launch({ executablePath, headless: true });
let externalRequests = 0;
try {
  const context = await browser.newContext();
  await context.route(/https?:\/\//, route => { externalRequests += 1; return route.abort("blockedbyclient"); });
  const page = await context.newPage();
  await page.goto(pathToFileURL(source).href, { waitUntil: "load" });
  const cdp = await context.newCDPSession(page);
  const snapshot = await cdp.send("Page.captureSnapshot", { format: "mhtml" });
  await fs.writeFile(archive, snapshot.data, "utf8");
  await page.goto(pathToFileURL(archive).href, { waitUntil: "load" });
  const pass = await page.locator("#ok").count() === 1 && await page.title() === "MHTML smoke" && externalRequests === 0;
  process.stdout.write(`${JSON.stringify({ pass, mhtml_bytes: Buffer.byteLength(snapshot.data), external_requests: externalRequests, external_upload: false })}\n`);
  if (!pass) process.exitCode = 1;
  await context.close();
} finally {
  await browser.close();
  await fs.rm(temporary, { recursive: true, force: true });
}
