/** Recover observed mail-relation windows within one bound customer; this is not thread order.
 * 在单客户范围内补采实际观察到的邮件关系窗口；窗口顺序不等于邮件线程顺序。 */
import crypto from "node:crypto";
import fs from "node:fs/promises";
import { createReadStream } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "playwright-core";
import { loadAdapter, parseCustomerUrl } from "./adapter.js";
import { BrowserManager } from "./browser-manager.js";
import { sha256, stamp } from "./evidence-store.js";

type JsonObject = Record<string, unknown>;

interface ArtifactRef {
  path: string;
  bytes: number;
  sha256: string;
}

export interface RelationWindowItem {
  mail_id: string;
  user_id: string | null;
}

export interface RelationWindowPayload {
  code: number;
  msg: unknown;
  now: unknown;
  data: {
    previous: RelationWindowItem[];
    next: RelationWindowItem[];
    total: number;
  };
}

interface WindowArtifactEnvelope {
  schema: "okki.crm.mail_relation_window_artifact.v1";
  anchor_mail_id: string;
  response_sha256: string;
  response: RelationWindowPayload;
}

interface CaptureResult {
  anchor_mail_id: string;
  artifact: ArtifactRef;
  previous_count: number;
  next_count: number;
  total: number;
  reused: boolean;
}

function jsonObject(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be an object`);
  return value as JsonObject;
}

function exactKeys(value: JsonObject, expected: readonly string[], label: string): void {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((item, index) => item !== wanted[index])) {
    throw new Error(`${label} keys invalid`);
  }
}

function numericId(value: unknown, label: string): string {
  const text = typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
  if (!/^\d+$/.test(text) || text === "0") throw new Error(`${label} invalid`);
  return text;
}

function relationItem(value: unknown, label: string): RelationWindowItem {
  const row = jsonObject(value, label);
  exactKeys(row, ["mail_id", "user_id"], label);
  const rawUserId = row.user_id;
  if (rawUserId !== null && typeof rawUserId !== "string" && typeof rawUserId !== "number") {
    throw new Error(`${label}.user_id invalid`);
  }
  const userId = rawUserId === null ? null : String(rawUserId).trim() || null;
  return { mail_id: numericId(row.mail_id, `${label}.mail_id`), user_id: userId };
}

export function validateRelationWindowPayload(value: unknown, minimumTotal = 1): RelationWindowPayload {
  const root = jsonObject(value, "relation window response");
  exactKeys(root, ["code", "msg", "now", "data"], "relation window response");
  if (root.code !== 0) throw new Error("relation window response code invalid");
  const data = jsonObject(root.data, "relation window data");
  exactKeys(data, ["previous", "next", "total"], "relation window data");
  if (!Array.isArray(data.previous) || !Array.isArray(data.next)) throw new Error("relation window arrays invalid");
  if (data.previous.length > 100 || data.next.length > 100) throw new Error("relation window array exceeds contract");
  const total = Number(data.total);
  if (!Number.isSafeInteger(total) || total < minimumTotal) throw new Error("relation window total invalid");
  return {
    code: 0,
    msg: root.msg,
    now: root.now,
    data: {
      previous: data.previous.map((item, index) => relationItem(item, `previous[${index}]`)),
      next: data.next.map((item, index) => relationItem(item, `next[${index}]`)),
      total
    }
  };
}

export function validateProbeRequest(value: unknown, companyId: string): URL {
  const root = jsonObject(value, "probe request");
  exactKeys(root, ["url", "method", "headers"], "probe request");
  if (root.method !== "GET") throw new Error("probe request method invalid");
  jsonObject(root.headers, "probe request headers");
  const url = new URL(String(root.url ?? ""));
  if (url.origin !== "https://crm.xiaoman.cn" || url.pathname !== "/api/customerRead/mailPreviousNext") {
    throw new Error("probe request endpoint invalid");
  }
  if (url.searchParams.get("company_id") !== companyId) throw new Error("probe request company_id mismatch");
  numericId(url.searchParams.get("mail_id"), "probe request mail_id");
  const required = ["company_id", "curPage", "pageSize", "mail_id"];
  for (const key of required) if (!url.searchParams.has(key)) throw new Error(`probe request missing ${key}`);
  return url;
}

function parseArgs(argv: string[]): Map<string, string> {
  const values = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith("--") || value === undefined) throw new Error(`invalid argument near ${name ?? "<end>"}`);
    values.set(name.slice(2), value);
  }
  return values;
}

function requiredArg(args: Map<string, string>, name: string): string {
  const value = args.get(name);
  if (!value) throw new Error(`missing required argument --${name}`);
  return value;
}

function normalizeSha(value: string): string {
  const text = value.trim().toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(text)) throw new Error("SHA-256 must contain 64 hexadecimal characters");
  return text;
}

async function sha256File(filePath: string): Promise<string> {
  const hash = crypto.createHash("sha256");
  for await (const chunk of createReadStream(filePath)) hash.update(chunk as Buffer);
  return hash.digest("hex");
}

async function artifactRef(filePath: string): Promise<ArtifactRef> {
  const stat = await fs.stat(filePath);
  if (!stat.isFile()) throw new Error(`artifact is not a file: ${filePath}`);
  return { path: path.resolve(filePath), bytes: stat.size, sha256: await sha256File(filePath) };
}

async function readBoundJson(filePath: string, expectedSha: string, label: string): Promise<unknown> {
  const bytes = await fs.readFile(filePath);
  if (sha256(bytes) !== expectedSha) throw new Error(`${label} SHA-256 mismatch`);
  try { return JSON.parse(bytes.toString("utf8")); }
  catch { throw new Error(`${label} JSON invalid`); }
}

async function writeExclusive(filePath: string, data: Buffer | string): Promise<void> {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, data, { flag: "wx" });
}

function validateCaptureManifest(value: unknown): { companyId: string; sessionId: string; mailIds: string[] } {
  const root = jsonObject(value, "capture manifest");
  if (root.schema !== "okki.crm.mail_capture_manifest.v2" || root.status !== "PASS") {
    throw new Error("capture manifest is not PASS v2");
  }
  const companyId = numericId(root.company_id, "capture company_id");
  const sessionId = typeof root.session_id === "string" ? root.session_id.trim() : "";
  if (!sessionId) throw new Error("capture manifest session_id invalid");
  const rows = Array.isArray(root.mails) ? root.mails : null;
  if (!rows?.length) throw new Error("capture manifest mails invalid");
  const mailIds = rows.map((row, index) => numericId(jsonObject(row, `mails[${index}]`).mail_id, `mails[${index}].mail_id`));
  if (new Set(mailIds).size !== mailIds.length) throw new Error("capture manifest mail_id duplicate");
  const counts = jsonObject(root.counts, "capture counts");
  if (Number(counts.mails) !== mailIds.length) throw new Error("capture manifest mail count mismatch");
  return { companyId, sessionId, mailIds };
}

async function pageFetch(page: Page, rawUrl: string): Promise<{ status: number; text: string; error: string | null }> {
  return page.evaluate(async innerUrl => {
    try {
      const url = new URL(innerUrl, location.origin);
      if (url.origin !== location.origin) throw new Error("cross-origin relation capture refused");
      const response = await fetch(url.href, { method: "GET", credentials: "include", cache: "no-store" });
      return { status: response.status, text: await response.text(), error: response.ok ? null : `HTTP ${response.status}` };
    } catch (error) {
      return { status: 0, text: "", error: error instanceof Error ? error.message : String(error) };
    }
  }, rawUrl);
}

function artifactName(mailId: string): string {
  return `${sha256(mailId).slice(0, 24)}.json`;
}

function validateEnvelope(value: unknown, anchorMailId: string, minimumTotal: number): WindowArtifactEnvelope {
  const root = jsonObject(value, "relation artifact envelope");
  exactKeys(root, ["schema", "anchor_mail_id", "response_sha256", "response"], "relation artifact envelope");
  if (root.schema !== "okki.crm.mail_relation_window_artifact.v1") throw new Error("relation artifact schema invalid");
  if (root.anchor_mail_id !== anchorMailId) throw new Error("relation artifact anchor mismatch");
  const response = validateRelationWindowPayload(root.response, minimumTotal);
  const responseSha = String(root.response_sha256 ?? "");
  const canonical = JSON.stringify(response);
  if (!/^[a-f0-9]{64}$/.test(responseSha) || sha256(canonical) !== responseSha) {
    throw new Error("relation artifact response SHA invalid");
  }
  return { schema: "okki.crm.mail_relation_window_artifact.v1", anchor_mail_id: anchorMailId, response_sha256: responseSha, response };
}

async function fetchOne(
  page: Page,
  baseUrl: URL,
  mailId: string,
  objectRoot: string,
  minimumTotal: number,
  attempts: number
): Promise<CaptureResult> {
  const outputPath = path.join(objectRoot, artifactName(mailId));
  try {
    const existing = JSON.parse(await fs.readFile(outputPath, "utf8"));
    const envelope = validateEnvelope(existing, mailId, minimumTotal);
    return {
      anchor_mail_id: mailId,
      artifact: await artifactRef(outputPath),
      previous_count: envelope.response.data.previous.length,
      next_count: envelope.response.data.next.length,
      total: envelope.response.data.total,
      reused: true
    };
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "ENOENT") throw error;
  }
  const url = new URL(baseUrl.href);
  url.searchParams.set("mail_id", mailId);
  let lastError = "relation capture failed";
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const result = await pageFetch(page, url.href);
    if (result.status === 200 && result.error === null) {
      try {
        const parsed = validateRelationWindowPayload(JSON.parse(result.text), minimumTotal);
        const canonical = JSON.stringify(parsed);
        const envelope: WindowArtifactEnvelope = {
          schema: "okki.crm.mail_relation_window_artifact.v1",
          anchor_mail_id: mailId,
          response_sha256: sha256(canonical),
          response: parsed
        };
        await writeExclusive(outputPath, `${JSON.stringify(envelope)}\n`);
        return {
          anchor_mail_id: mailId,
          artifact: await artifactRef(outputPath),
          previous_count: parsed.data.previous.length,
          next_count: parsed.data.next.length,
          total: parsed.data.total,
          reused: false
        };
      } catch (error) { lastError = error instanceof Error ? error.message : String(error); }
    } else lastError = result.error ?? `HTTP ${result.status}`;
    if (attempt < attempts) await new Promise(resolve => setTimeout(resolve, attempt * 250));
  }
  throw new Error(lastError);
}

async function run(args: Map<string, string>): Promise<JsonObject> {
  const capturePath = path.resolve(requiredArg(args, "capture-manifest"));
  const captureSha = normalizeSha(requiredArg(args, "capture-sha256"));
  const probePath = path.resolve(requiredArg(args, "probe-request"));
  const probeSha = normalizeSha(requiredArg(args, "probe-sha256"));
  const outputRoot = path.resolve(requiredArg(args, "output-root"));
  const capture = validateCaptureManifest(await readBoundJson(capturePath, captureSha, "capture manifest"));
  const probeUrl = validateProbeRequest(await readBoundJson(probePath, probeSha, "probe request"), capture.companyId);
  const probeAnchor = numericId(probeUrl.searchParams.get("mail_id"), "probe request mail_id");
  const concurrency = Math.min(16, Math.max(1, Number(args.get("concurrency") ?? "8")));
  const attempts = Math.min(6, Math.max(1, Number(args.get("attempts") ?? "4")));
  if (!Number.isInteger(concurrency) || !Number.isInteger(attempts)) throw new Error("concurrency or attempts invalid");

  const controlPort = Number(args.get("control-port") ?? "3211");
  const controlResponse = await fetch(`http://127.0.0.1:${controlPort}/api/status`, { signal: AbortSignal.timeout(3_000) });
  if (!controlResponse.ok) throw new Error(`collector control status HTTP ${controlResponse.status}`);
  const control = jsonObject(await controlResponse.json(), "collector status");
  const job = jsonObject(control.job, "collector job");
  const browserStatus = jsonObject(control.browser, "collector browser");
  if (job.running === true) throw new Error("collector capture is running; relation capture refused");
  if (browserStatus.connected !== true) throw new Error("collector browser is not connected");
  const cdpEndpoint = new URL(String(browserStatus.cdpEndpoint ?? ""));
  if (!["127.0.0.1", "localhost"].includes(cdpEndpoint.hostname)) throw new Error("collector CDP is not loopback");
  const cdpPort = Number(args.get("cdp-port") ?? cdpEndpoint.port);
  if (!Number.isInteger(cdpPort) || cdpPort <= 0) throw new Error("collector CDP port invalid");

  await fs.mkdir(outputRoot, { recursive: true });
  const objectRoot = path.join(outputRoot, "objects");
  await fs.mkdir(objectRoot, { recursive: true });
  const adapter = await loadAdapter();
  const browserManager = new BrowserManager(cdpPort, path.join(outputRoot, ".unused-profile"), adapter);
  let page = await browserManager.customerPage();
  const current = page ? parseCustomerUrl(page.url(), adapter) : null;
  if (!current || current.companyId !== capture.companyId) page = await browserManager.openCustomer(capture.companyId);
  if (!page) throw new Error("collector browser has no customer page");
  const active = page;
  const parsedPage = parseCustomerUrl(active.url(), adapter);
  if (!parsedPage || parsedPage.companyId !== capture.companyId) throw new Error("collector browser customer mismatch");

  const results = new Map<string, CaptureResult>();
  const failures: string[] = [];
  const sampledSeeds = capture.mailIds.filter((_, index) => index % 100 === 0);
  const pending = [...new Set([probeAnchor, ...sampledSeeds, capture.mailIds.at(-1)!])];
  const discovered = new Set<string>(pending);
  const queued = new Set(pending);
  let cursor = 0;
  let stableWaves = 0;
  let reportedTotal: number | null = null;
  while (cursor < pending.length && (reportedTotal === null || discovered.size < reportedTotal)) {
    const beforeDiscovered = discovered.size;
    const wave = pending.slice(cursor, cursor + concurrency);
    cursor += wave.length;
    await Promise.all(wave.map(async mailId => {
      try {
        const row = await fetchOne(active, probeUrl, mailId, objectRoot, capture.mailIds.length, attempts);
        results.set(mailId, row);
        if (reportedTotal === null) reportedTotal = row.total;
        else if (row.total !== reportedTotal) throw new Error("relation window total drift");
        const envelope = validateEnvelope(JSON.parse(await fs.readFile(row.artifact.path, "utf8")), mailId, capture.mailIds.length);
        const previous = envelope.response.data.previous;
        const next = envelope.response.data.next;
        for (const item of [...previous, ...next]) discovered.add(item.mail_id);
        // The live anchor already exposes the complete small look-behind.  The
        // outer look-ahead item advances by the full page; enqueueing inner or
        // look-behind items repeats the same window thousands of times.
        // 外层前向窗口按整页推进；重复入队内部或回看条目会反复采同一窗口。
        const boundaries = [next.at(-1)].filter(
          (item): item is RelationWindowItem => item !== undefined
        );
        for (const item of boundaries) {
          if (!queued.has(item.mail_id)) {
            queued.add(item.mail_id);
            pending.push(item.mail_id);
          }
        }
      } catch { failures.push(mailId); }
    }));
    if (results.size % 25 < wave.length || (reportedTotal !== null && discovered.size >= reportedTotal)) {
      process.stderr.write(`${JSON.stringify({ status: "RUNNING", windows: results.size, discovered: discovered.size, reported_total: reportedTotal, failures: failures.length })}\n`);
    }
    stableWaves = discovered.size === beforeDiscovered ? stableWaves + 1 : 0;
    if (stableWaves >= 5) break;
  }
  if (failures.length) throw new Error(`relation window capture incomplete: ${failures.length} failures`);

  const orderedResults = [...results.values()];
  if (!orderedResults.length || reportedTotal === null) throw new Error("relation window capture result missing");
  const totals = new Set(orderedResults.map(row => row.total));
  if (totals.size !== 1) throw new Error("relation window total drift");
  const windowsPath = path.join(outputRoot, "relation_windows.private.jsonl");
  const handle = await fs.open(windowsPath, "wx");
  const referenced = new Set<string>();
  let previousItems = 0;
  let nextItems = 0;
  try {
    for (const row of orderedResults) {
      const envelope = validateEnvelope(JSON.parse(await fs.readFile(row.artifact.path, "utf8")), row.anchor_mail_id, capture.mailIds.length);
      for (const item of envelope.response.data.previous) referenced.add(item.mail_id);
      for (const item of envelope.response.data.next) referenced.add(item.mail_id);
      referenced.add(row.anchor_mail_id);
      previousItems += envelope.response.data.previous.length;
      nextItems += envelope.response.data.next.length;
      await handle.write(`${JSON.stringify({
        anchor_mail_id: row.anchor_mail_id,
        previous: envelope.response.data.previous,
        next: envelope.response.data.next,
        total: envelope.response.data.total,
        source_artifact: row.artifact
      })}\n`);
    }
  } finally { await handle.close(); }
  const missingCaptureIds = capture.mailIds.filter(mailId => !referenced.has(mailId));
  const captureIdSet = new Set(capture.mailIds);
  const liveDiscoveredMailIds = [...referenced].filter(mailId => !captureIdSet.has(mailId)).length;
  const completionStatus = referenced.size === reportedTotal
    ? "PASS_MAIL_RELATION_WINDOW_CAPTURE"
    : "PASS_MAIL_RELATION_WINDOW_CAPTURE_WITH_SOURCE_GAPS";

  const windowsRef = await artifactRef(windowsPath);
  const scriptRef = await artifactRef(fileURLToPath(import.meta.url));
  const manifest = {
    schema: "okki.crm.mail_relation_window_capture_manifest.v1",
    status: completionStatus,
    generated_at: new Date().toISOString(),
    capture_id: `relation_window_${stamp()}`,
    company_id: capture.companyId,
    source_session_id: capture.sessionId,
    counts: {
      source_mail_ids: capture.mailIds.length,
      windows: orderedResults.length,
      reported_ui_total: reportedTotal,
      previous_items: previousItems,
      next_items: nextItems,
      unique_referenced_mail_ids: referenced.size,
      missing_capture_mail_ids: missingCaptureIds.length,
      live_discovered_mail_ids: liveDiscoveredMailIds,
      interface_unreachable_mail_ids: Math.max(0, reportedTotal - referenced.size),
      reused_windows: orderedResults.filter(row => row.reused).length,
      downloaded_windows: orderedResults.filter(row => !row.reused).length,
      failures: 0
    },
    bindings: {
      capture_manifest: await artifactRef(capturePath),
      probe_request: await artifactRef(probePath),
      implementation: scriptRef
    },
    output: windowsRef,
    execution: { control_port: controlPort, cdp_port: cdpPort, concurrency, attempts, model_calls: 0, browser_navigation_visible: false }
  };
  const manifestPath = path.join(outputRoot, "MAIL_RELATION_WINDOW_CAPTURE_MANIFEST.json");
  await writeExclusive(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  const receipt = {
    schema: "okki.crm.mail_relation_window_capture_receipt.v1",
    status: completionStatus,
    generated_at: new Date().toISOString(),
    counts: manifest.counts,
    manifest: await artifactRef(manifestPath),
    original_capture_modified: false,
    model_calls: 0,
    customer_text_printed: false
  };
  const receiptPath = path.join(outputRoot, "MAIL_RELATION_WINDOW_CAPTURE_RECEIPT.json");
  await writeExclusive(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`);
  return { status: receipt.status, windows: orderedResults.length, failures: 0, receipt_path: receiptPath, receipt_sha256: (await artifactRef(receiptPath)).sha256 };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  run(parseArgs(process.argv.slice(2)))
    .then(result => { process.stdout.write(`${JSON.stringify(result)}\n`); })
    .catch(error => {
      process.stdout.write(`${JSON.stringify({ status: "FAIL_MAIL_RELATION_WINDOW_CAPTURE", error_code: "MAIL_RELATION_WINDOW_CAPTURE_FAILED" })}\n`);
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      process.exitCode = 2;
    });
}
