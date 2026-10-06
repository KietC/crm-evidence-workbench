import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { assertAutomaticCaptureUrl } from "./scope.mjs";

function safeSegment(value, max = 140) {
  return String(value ?? "unknown")
    .normalize("NFKC")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/\s+/g, "_")
    .replace(/_+/g, "_")
    .slice(0, max) || "unnamed";
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

/** Validate automatic GET scope before navigation or response inspection.
 * 导航或查看响应前校验自动 GET 的范围。 */
export function assertAllowedApi(rawUrl, companyId) {
  assertAutomaticCaptureUrl(rawUrl);
  const url = new URL(rawUrl);
  if (url.origin !== "https://crm.xiaoman.cn" || !url.pathname.startsWith("/api/")) {
    throw new Error(`out-of-scope API URL: ${url.origin}${url.pathname}`);
  }
  const scopedId = url.searchParams.get("company_id") ?? url.searchParams.get("id") ?? url.searchParams.get("object_id");
  if (scopedId && scopedId !== String(companyId)) {
    throw new Error(`company scope mismatch: ${scopedId}`);
  }
  return url;
}

function summaryOf(json) {
  const data = json?.data;
  const list = Array.isArray(data?.list) ? data.list : null;
  return {
    code: json?.code ?? null,
    count: data?.count ?? null,
    total: data?.total ?? null,
    totalItem: data?.totalItem ?? null,
    listLength: list?.length ?? null
  };
}

async function appendJsonl(filePath, row) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.appendFile(filePath, `${JSON.stringify(row)}\n`, "utf8");
}

export async function captureJsonGet(tab, runtime, session, rawUrl, label) {
  const url = assertAllowedApi(rawUrl, session.companyId);
  await runtime.logCommand(session, "direct_observed_api_get", {
    label,
    endpoint: url.pathname,
    query_keys: [...url.searchParams.keys()]
  });
  await tab.goto(url.href);
  await tab.playwright.waitForLoadState({ state: "domcontentloaded", timeoutMs: 15000 });
  const pre = tab.playwright.locator("pre");
  const count = await pre.count();
  if (count !== 1) throw new Error(`raw JSON <pre> count=${count} for ${url.pathname}`);
  const text = await pre.textContent({ timeoutMs: 15000 });
  if (!text) throw new Error(`empty JSON body for ${url.pathname}`);
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new Error(`invalid JSON for ${url.pathname}: ${error.message}`);
  }
  const fileName = `${safeSegment(label)}__${sha256(url.href).slice(0, 16)}.json`;
  const object = await runtime.storeArtifact(
    session,
    path.join("raw", "sessions", session.sessionId, "api_direct"),
    fileName,
    text,
    {
      object_type: "api_direct_get",
      source_label: label,
      source_endpoint: url.pathname,
      source_query_keys: [...url.searchParams.keys()],
      http_status: 200,
      mime_type: "application/json"
    }
  );
  await appendJsonl(path.join(session.caseRoot, "logs", "direct_api.jsonl"), {
    at: new Date().toISOString(),
    session_id: session.sessionId,
    label,
    endpoint: url.pathname,
    query_keys: [...url.searchParams.keys()],
    file: object.relative_path,
    sha256: object.sha256,
    summary: summaryOf(parsed)
  });
  return { object, summary: summaryOf(parsed), json: parsed };
}

export function withPage(rawUrl, pageKey, pageValue, pageSizeKey = null, pageSizeValue = null) {
  const url = new URL(rawUrl);
  url.searchParams.set(pageKey, String(pageValue));
  if (pageSizeKey && pageSizeValue != null) url.searchParams.set(pageSizeKey, String(pageSizeValue));
  return url.href;
}
