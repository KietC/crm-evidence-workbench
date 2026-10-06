import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { assertAutomaticCaptureUrl, isAutoCaptureExcludedUrl } from "./scope.mjs";

const SENSITIVE_QUERY_KEYS = new Set([
  "signature", "token", "access_token", "authorization", "auth", "api_key",
  "expires", "ossaccesskeyid", "awsaccesskeyid", "googleaccessid", "key-pair-id", "policy",
  "x-amz-algorithm", "x-amz-credential", "x-amz-date", "x-amz-expires",
  "x-amz-security-token", "x-amz-signature", "x-amz-signedheaders",
  "x-goog-algorithm", "x-goog-credential", "x-goog-date", "x-goog-expires",
  "x-goog-signature", "x-goog-signedheaders", "x-goog-security-token",
  "x-oss-access-key-id", "x-oss-credential", "x-oss-date", "x-oss-expires",
  "x-oss-security-token", "x-oss-signature", "x-oss-signature-version", "security-token"
]);
const SENSITIVE_HEADERS = new Set([
  "authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"
]);

function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function safeSegment(value, max = 96) {
  const cleaned = String(value ?? "unknown")
    .normalize("NFKC")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/\s+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^\.+|\.+$/g, "")
    .slice(0, max);
  return cleaned || "unnamed";
}

function nowIso() {
  return new Date().toISOString();
}

function stamp() {
  return nowIso().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

function redactSecret(value) {
  return `<redacted:sha256:${sha256Bytes(String(value ?? "")).slice(0, 16)}>`;
}

/** Sanitize persisted metadata only; never alter the URL used for requests.
 * 仅脱敏持久化元数据，不修改实际请求使用的网址。 */
export function redactUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    const redacted = new URLSearchParams();
    for (const [key, value] of url.searchParams) {
      redacted.append(key, SENSITIVE_QUERY_KEYS.has(key.toLowerCase()) ? redactSecret(value) : value);
    }
    url.search = redacted.toString();
    return url.href;
  } catch {
    return rawUrl;
  }
}

function sanitizeHeaders(headers = {}) {
  const output = {};
  for (const [key, value] of Object.entries(headers)) {
    output[key] = SENSITIVE_HEADERS.has(key.toLowerCase()) ? redactSecret(value) : value;
  }
  return output;
}

async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

async function writeAtomic(filePath, data) {
  await ensureDir(path.dirname(filePath));
  const tmp = `${filePath}.${crypto.randomUUID()}.tmp`;
  await fs.writeFile(tmp, data);
  await fs.rename(tmp, filePath);
}

async function appendJsonl(filePath, object) {
  await ensureDir(path.dirname(filePath));
  await fs.appendFile(filePath, `${JSON.stringify(object)}\n`, "utf8");
}

async function recordObject(session, filePath, metadata = {}) {
  const buffer = await fs.readFile(filePath);
  const item = {
    recorded_at: nowIso(),
    relative_path: path.relative(session.caseRoot, filePath),
    size: buffer.byteLength,
    sha256: sha256Bytes(buffer),
    ...metadata
  };
  await appendJsonl(session.objectsLedger, item);
  return item;
}

function extensionForMime(mime = "") {
  const normalized = mime.split(";")[0].trim().toLowerCase();
  if (normalized.includes("json")) return ".json";
  if (normalized.includes("html")) return ".html";
  if (normalized.startsWith("text/")) return ".txt";
  if (normalized === "image/jpeg") return ".jpg";
  if (normalized === "image/png") return ".png";
  if (normalized === "image/webp") return ".webp";
  if (normalized === "image/gif") return ".gif";
  if (normalized === "application/pdf") return ".pdf";
  if (normalized.includes("zip")) return ".zip";
  return ".bin";
}

export function classifyUrl(rawUrl) {
  try {
    if (isAutoCaptureExcludedUrl(rawUrl)) return "excluded_manual_trade";
    const url = new URL(rawUrl);
    if (url.origin === "https://crm.xiaoman.cn" && url.pathname.startsWith("/api/")) return "api";
    if (url.pathname.startsWith("/crm/customer/personal")) return "root_page";
    if (url.hostname === "datasink-sensorsdata.xiaoman.cn" || url.hostname.endsWith("log.aliyuncs.com")) return "page_native_telemetry";
    return "page_asset_or_other";
  } catch {
    return "invalid_url";
  }
}

export async function createSession({ caseRoot, companyId, rootUrl }) {
  const sessionId = `capture_${stamp()}`;
  const sessionRoot = path.join(caseRoot, "raw", "sessions", sessionId);
  const dirs = {
    sessionRoot,
    pages: path.join(sessionRoot, "pages"),
    api: path.join(sessionRoot, "api"),
    network: path.join(sessionRoot, "network"),
    assets: path.join(sessionRoot, "assets"),
    screenshots: path.join(sessionRoot, "screenshots")
  };
  await Promise.all(Object.values(dirs).map(ensureDir));
  const session = {
    caseRoot,
    companyId: String(companyId),
    rootUrl,
    sessionId,
    ...dirs,
    requestLedger: path.join(caseRoot, "request_ledger.jsonl"),
    scopeLedger: path.join(caseRoot, "scope_ledger.jsonl"),
    objectsLedger: path.join(caseRoot, "manifests", "objects.jsonl"),
    commandLog: path.join(caseRoot, "logs", "commands.jsonl"),
    stateFile: path.join(caseRoot, "work", "capture_state.json")
  };
  await writeAtomic(path.join(sessionRoot, "session.json"), JSON.stringify({
    schema: 1,
    session_id: sessionId,
    company_id: session.companyId,
    root_url: rootUrl,
    started_at: nowIso(),
    privacy: {
      page_native_upstream: "allowed",
      external_ai_upload: "forbidden",
      chat_output: "counts_hashes_paths_only"
    }
  }, null, 2));
  return session;
}

export async function logCommand(session, action, details = {}) {
  await appendJsonl(session.commandLog, { at: nowIso(), action, ...details });
}

export async function startNetworkCapture(cdp, session) {
  await cdp.send("Network.enable", {
    maxTotalBufferSize: 268435456,
    maxResourceBufferSize: 67108864,
    maxPostDataSize: 16777216
  });
  const baseline = await cdp.readEvents();
  await logCommand(session, "network_enable", { cursor: baseline.cursor });
  return baseline.cursor;
}

export async function currentCursor(cdp) {
  return (await cdp.readEvents()).cursor;
}

async function collectEvents(cdp, afterSequence, timeoutMs = 500) {
  const methods = [
    "Network.requestWillBeSent",
    "Network.responseReceived",
    "Network.loadingFinished",
    "Network.loadingFailed"
  ];
  const all = [];
  let cursor = afterSequence;
  let truncated = false;
  for (;;) {
    const page = await cdp.readEvents({ afterSequence: cursor, limit: 1000, methods, timeoutMs });
    all.push(...page.events);
    cursor = page.cursor;
    truncated ||= page.truncated;
    if (!page.hasMore) break;
  }
  return { events: all, cursor, truncated };
}

async function persistNetworkCapture(cdp, session, captured, afterSequence, label) {
  const requests = new Map();
  const responses = [];
  const failures = [];

  for (const event of captured.events) {
    const params = event.params ?? {};
    if (event.method === "Network.requestWillBeSent" && params.requestId && params.request) {
      requests.set(params.requestId, params.request);
      const request = params.request;
      const postDataBuffer = request.postData ? Buffer.from(request.postData, "utf8") : null;
      await appendJsonl(session.requestLedger, {
        at: nowIso(),
        session_id: session.sessionId,
        sequence: event.sequence,
        request_id: params.requestId,
        label,
        classification: classifyUrl(request.url),
        method: request.method,
        url: redactUrl(request.url),
        headers: sanitizeHeaders(request.headers),
        post_data_size: postDataBuffer?.byteLength ?? 0,
        post_data_sha256: postDataBuffer ? sha256Bytes(postDataBuffer) : null,
        initiator_type: params.initiator?.type ?? null
      });
    } else if (event.method === "Network.loadingFailed") {
      failures.push({
        request_id: params.requestId,
        error_text: params.errorText,
        canceled: Boolean(params.canceled),
        blocked_reason: params.blockedReason ?? null
      });
    } else if (event.method === "Network.responseReceived" && params.response) {
      responses.push({ event, params });
    }
  }

  const bodyResults = [];
  for (const { event, params } of responses) {
    const response = params.response;
    const request = requests.get(params.requestId);
    const classification = classifyUrl(response.url);
    // Check both sides of a redirect before requesting a response body.
    // 读取正文前同时检查原始请求与重定向后的响应网址。
    const isStorable = !isAutoCaptureExcludedUrl(request?.url ?? response.url)
      && (classification === "api" || classification === "root_page");
    let bodyRecord = null;
    if (isStorable) {
      try {
        const body = await cdp.send("Network.getResponseBody", { requestId: params.requestId });
        const buffer = body.base64Encoded ? Buffer.from(body.body, "base64") : Buffer.from(body.body, "utf8");
        const url = new URL(response.url);
        const endpoint = safeSegment(url.pathname.replace(/^\/api\//, "").replaceAll("/", "__"));
        const suffix = extensionForMime(response.mimeType || response.headers?.["content-type"] || "");
        const fileName = `${safeSegment(label, 48)}__${endpoint}__${event.sequence}__${sha256Bytes(response.url).slice(0, 12)}${suffix}`;
        const targetDir = classification === "api" ? session.api : session.pages;
        const filePath = path.join(targetDir, fileName);
        await writeAtomic(filePath, buffer);
        bodyRecord = await recordObject(session, filePath, {
          object_type: classification,
          source_url: redactUrl(response.url),
          source_label: label,
          http_status: response.status,
          mime_type: response.mimeType || null,
          request_id: params.requestId
        });
      } catch (error) {
        bodyRecord = { error: String(error?.message ?? error) };
      }
    }
    const ledgerEntry = {
      at: nowIso(),
      session_id: session.sessionId,
      sequence: event.sequence,
      request_id: params.requestId,
      label,
      classification,
      method: request?.method ?? null,
      url: redactUrl(response.url),
      status: response.status,
      mime_type: response.mimeType || null,
      headers: sanitizeHeaders(response.headers),
      body: bodyRecord
    };
    await appendJsonl(path.join(session.network, "responses.jsonl"), ledgerEntry);
    bodyResults.push(ledgerEntry);
  }

  await writeAtomic(path.join(session.network, `${safeSegment(label)}__events_summary.json`), JSON.stringify({
    label,
    captured_at: nowIso(),
    start_cursor: afterSequence,
    end_cursor: captured.cursor,
    truncated: captured.truncated,
    event_count: captured.events.length,
    request_count: requests.size,
    response_count: responses.length,
    failure_count: failures.length,
    failures
  }, null, 2));

  return {
    cursor: captured.cursor,
    truncated: captured.truncated,
    eventCount: captured.events.length,
    requestCount: requests.size,
    responseCount: responses.length,
    storedBodyCount: bodyResults.filter(x => x.body?.relative_path).length,
    failedBodyCount: bodyResults.filter(x => x.body?.error).length,
    loadingFailureCount: failures.length
  };
}

export async function drainNetwork(cdp, session, afterSequence, label) {
  const captured = await collectEvents(cdp, afterSequence);
  return persistNetworkCapture(cdp, session, captured, afterSequence, label);
}

export async function captureNetworkAction(cdp, session, label, action, options = {}) {
  const methods = [
    "Network.requestWillBeSent",
    "Network.responseReceived",
    "Network.loadingFinished",
    "Network.loadingFailed"
  ];
  const settleMs = options.settleMs ?? 1200;
  const pollTimeoutMs = options.pollTimeoutMs ?? 100;
  const start = await currentCursor(cdp);
  let cursor = start;
  let truncated = false;
  let actionFinished = false;
  let stopAt = Number.POSITIVE_INFINITY;
  const events = [];

  const pump = (async () => {
    while (!actionFinished || Date.now() < stopAt) {
      const page = await cdp.readEvents({
        afterSequence: cursor,
        limit: 1000,
        methods,
        timeoutMs: pollTimeoutMs
      });
      events.push(...page.events);
      cursor = page.cursor;
      truncated ||= page.truncated;
      while (page.hasMore) {
        const more = await cdp.readEvents({
          afterSequence: cursor,
          limit: 1000,
          methods,
          timeoutMs: pollTimeoutMs
        });
        events.push(...more.events);
        cursor = more.cursor;
        truncated ||= more.truncated;
        if (!more.hasMore) break;
      }
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    const tail = await collectEvents(cdp, cursor, pollTimeoutMs);
    events.push(...tail.events);
    cursor = tail.cursor;
    truncated ||= tail.truncated;
  })();

  let actionError = null;
  try {
    await action();
  } catch (error) {
    actionError = error;
  } finally {
    actionFinished = true;
    stopAt = Date.now() + settleMs;
    await pump;
  }
  if (actionError) throw actionError;

  return persistNetworkCapture(cdp, session, { events, cursor, truncated }, start, label);
}

export async function capturePageState(tab, session, label) {
  const pageUrl = await tab.url();
  assertAutomaticCaptureUrl(pageUrl);
  const title = await tab.title();
  const dom = await tab.playwright.domSnapshot();
  const domPath = path.join(session.pages, `${safeSegment(label)}__dom.txt`);
  await writeAtomic(domPath, dom);
  const domObject = await recordObject(session, domPath, {
    object_type: "dom_snapshot",
    source_url: redactUrl(pageUrl),
    source_label: label,
    title
  });
  const png = await tab.screenshot({ fullPage: false });
  const screenshotPath = path.join(session.screenshots, `${safeSegment(label)}__viewport.png`);
  await writeAtomic(screenshotPath, png);
  const screenshotObject = await recordObject(session, screenshotPath, {
    object_type: "screenshot",
    source_url: redactUrl(pageUrl),
    source_label: label
  });
  return { label, url: redactUrl(pageUrl), title, dom: domObject, screenshot: screenshotObject };
}

export async function captureAssets(assetsCapability, session, label, kinds = ["image"]) {
  const inventory = await assetsCapability.list();
  assertAutomaticCaptureUrl(inventory.pageUrl);
  const sanitizedInventory = {
    id: inventory.id,
    page_url: redactUrl(inventory.pageUrl),
    summary: inventory.summary,
    assets: inventory.assets.map(asset => ({ ...asset, url: redactUrl(asset.url) })),
    inline_svgs: inventory.inlineSvgs
  };
  const inventoryPath = path.join(session.assets, `${safeSegment(label)}__inventory.json`);
  await writeAtomic(inventoryPath, JSON.stringify(sanitizedInventory, null, 2));
  await recordObject(session, inventoryPath, {
    object_type: "asset_inventory",
    source_label: label,
    source_url: redactUrl(inventory.pageUrl)
  });

  const requested = inventory.assets.filter(asset => kinds.includes(asset.kind) && !isAutoCaptureExcludedUrl(asset.url));
  let bundled = { summary: { requestedCount: 0, downloadedCount: 0, failedCount: 0 }, assets: [], failures: [] };
  if (requested.length > 0) {
    bundled = await assetsCapability.bundle({ inventoryId: inventory.id, assetIds: requested.map(x => x.id) });
    const bundleDir = path.join(session.assets, safeSegment(label));
    await ensureDir(bundleDir);
    for (const asset of bundled.assets) {
      const target = path.join(bundleDir, `${safeSegment(asset.name, 72)}__${asset.id}${path.extname(asset.path)}`);
      await fs.copyFile(asset.path, target);
      await recordObject(session, target, {
        object_type: `asset_${asset.kind}`,
        source_label: label,
        source_url: redactUrl(asset.url),
        content_type: asset.contentType
      });
    }
    await fs.rm(bundled.directoryPath, { recursive: true, force: true });
  }
  const summary = {
    label,
    inventory_total: inventory.summary.totalCount,
    inline_svg_count: inventory.summary.inlineSvgCount,
    requested: bundled.summary.requestedCount,
    downloaded: bundled.summary.downloadedCount,
    failed: bundled.summary.failedCount,
    failures: bundled.failures.map(x => ({ ...x, url: redactUrl(x.url) }))
  };
  await writeAtomic(path.join(session.assets, `${safeSegment(label)}__bundle_summary.json`), JSON.stringify(summary, null, 2));
  return summary;
}

export async function writeState(session, state) {
  await writeAtomic(session.stateFile, JSON.stringify({ updated_at: nowIso(), ...state }, null, 2));
}

export async function storeArtifact(session, relativeDir, fileName, data, metadata = {}) {
  const target = path.join(session.caseRoot, relativeDir, safeSegment(fileName, 160));
  const buffer = Buffer.isBuffer(data) ? data : Buffer.from(String(data), "utf8");
  await writeAtomic(target, buffer);
  return recordObject(session, target, metadata);
}

export async function finalizeSession(session, summary) {
  const target = path.join(session.sessionRoot, "completion.json");
  await writeAtomic(target, JSON.stringify({ completed_at: nowIso(), ...summary }, null, 2));
  return recordObject(session, target, { object_type: "session_completion" });
}
