import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { assertAutomaticCaptureUrl } from "./scope.mjs";

function sha256(value) {
  return crypto.createHash("sha256").update(String(value)).digest("hex");
}

function assertSameOriginApi(rawUrl) {
  assertAutomaticCaptureUrl(rawUrl);
  const url = new URL(rawUrl);
  if (url.origin !== "https://crm.xiaoman.cn" || !url.pathname.startsWith("/api/")) {
    throw new Error(`out-of-scope page fetch: ${url.origin}${url.pathname}`);
  }
  return url;
}

async function appendJsonl(filePath, row) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.appendFile(filePath, `${JSON.stringify(row)}\n`, "utf8");
}

/** Fetch an observed API only after enforcing the automatic capture boundary.
 * 先执行自动采集边界校验，再请求已观察到的 API。 */
export async function capturePageFetchGet({ cdp, runtime, session, url: rawUrl, label, relativeDir, objectType }) {
  const url = assertSameOriginApi(rawUrl);
  const expression = `fetch(${JSON.stringify(url.href)},{credentials:'include'}).then(async r=>({status:r.status,contentType:r.headers.get('content-type'),body:await r.text()}))`;
  const evaluated = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true
  }, { timeoutMs: 30000 });
  const value = evaluated?.result?.value;
  if (!value || typeof value.body !== "string") throw new Error(`fetch body unavailable: ${url.pathname}`);
  let applicationCode = null;
  try {
    applicationCode = JSON.parse(value.body)?.code ?? null;
  } catch {
    applicationCode = "invalid_json";
  }
  const object = await runtime.storeArtifact(session, relativeDir, `${label}.json`, value.body, {
    object_type: objectType,
    source_label: label,
    source_endpoint: url.pathname,
    source_query_keys: [...url.searchParams.keys()],
    http_status: value.status,
    mime_type: value.contentType
  });
  const status = {
    at: new Date().toISOString(),
    session_id: session.sessionId,
    label,
    endpoint: url.pathname,
    query_keys: [...url.searchParams.keys()],
    request_key_sha256: sha256(url.href),
    http_status: value.status,
    application_code: applicationCode,
    bytes: Buffer.byteLength(value.body, "utf8"),
    sha256: object.sha256,
    relative_path: object.relative_path
  };
  await appendJsonl(path.join(session.caseRoot, "logs", `${objectType}_status.jsonl`), status);
  return status;
}

export async function captureMailDetailBatch({ cdp, runtime, session, entries, startIndex, endIndex }) {
  const results = [];
  for (let index = startIndex; index < endIndex && index < entries.length; index += 1) {
    const entry = entries[index];
    const url = new URL("https://crm.xiaoman.cn/api/mailRead/info");
    url.searchParams.set("mail_id", entry.mail_id);
    if (entry.user_id) url.searchParams.set("user_id", entry.user_id);
    url.searchParams.set("skip_view_privilege", "1");
    const label = `mail_detail_${String(index + 1).padStart(4, "0")}`;
    try {
      const status = await capturePageFetchGet({
        cdp,
        runtime,
        session,
        url: url.href,
        label,
        relativeDir: "raw/mail/details",
        objectType: "mail_detail"
      });
      results.push({ index: index + 1, ok: status.http_status === 200 && status.application_code === 0, status: status.http_status, code: status.application_code, bytes: status.bytes });
    } catch (error) {
      const failed = {
        at: new Date().toISOString(),
        session_id: session.sessionId,
        label,
        request_key_sha256: sha256(url.href),
        error: String(error?.message ?? error)
      };
      await appendJsonl(path.join(session.caseRoot, "logs", "mail_detail_failures.jsonl"), failed);
      results.push({ index: index + 1, ok: false, error: failed.error });
    }
  }
  return {
    start: startIndex + 1,
    end: Math.min(endIndex, entries.length),
    attempted: results.length,
    ok: results.filter(x => x.ok).length,
    failed: results.filter(x => !x.ok).length,
    statusHistogram: results.reduce((acc, x) => {
      const key = x.error ? "error" : `${x.status}/${x.code}`;
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {})
  };
}

export async function captureMailTrackDetailBatch({ cdp, runtime, session, entries, startIndex, endIndex }) {
  const results = [];
  for (let index = startIndex; index < endIndex && index < entries.length; index += 1) {
    const entry = entries[index];
    const url = new URL("https://crm.xiaoman.cn/api/mailRead/trackDetail");
    url.searchParams.set("mail_id", entry.mail_id);
    if (entry.user_id) url.searchParams.set("user_id", entry.user_id);
    const label = `mail_track_detail_${String(index + 1).padStart(4, "0")}`;
    try {
      const status = await capturePageFetchGet({
        cdp,
        runtime,
        session,
        url: url.href,
        label,
        relativeDir: "raw/mail/track_details",
        objectType: "mail_track_detail"
      });
      results.push({ index: index + 1, ok: status.http_status === 200 && status.application_code === 0, status: status.http_status, code: status.application_code, bytes: status.bytes });
    } catch (error) {
      const failed = {
        at: new Date().toISOString(),
        session_id: session.sessionId,
        label,
        request_key_sha256: sha256(url.href),
        error: String(error?.message ?? error)
      };
      await appendJsonl(path.join(session.caseRoot, "logs", "mail_track_detail_failures.jsonl"), failed);
      results.push({ index: index + 1, ok: false, error: failed.error });
    }
  }
  return {
    start: startIndex + 1,
    end: Math.min(endIndex, entries.length),
    attempted: results.length,
    ok: results.filter(x => x.ok).length,
    failed: results.filter(x => !x.ok).length,
    statusHistogram: results.reduce((acc, x) => {
      const key = x.error ? "error" : `${x.status}/${x.code}`;
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {})
  };
}
