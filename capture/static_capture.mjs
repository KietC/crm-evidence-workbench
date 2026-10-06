import crypto from "node:crypto";
import path from "node:path";
import { assertAutomaticCaptureUrl } from "./scope.mjs";

function safeName(url, index) {
  const ext = path.extname(url.pathname) || ".bin";
  const digest = crypto.createHash("sha256").update(url.href).digest("hex").slice(0, 16);
  const base = path.basename(url.pathname, ext).replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 80) || "resource";
  return `${String(index + 1).padStart(4, "0")}__${base}__${digest}${ext}`;
}

function assertAllowed(rawUrl) {
  assertAutomaticCaptureUrl(rawUrl);
  const url = new URL(rawUrl);
  if (!["https://crm.xiaoman.cn", "https://cdn.xiaoman.cn"].includes(url.origin)) {
    throw new Error(`out-of-scope static resource: ${url.origin}`);
  }
  if (!/^\/(crm_web|v5|lang|captcha-frontend|g)\//.test(url.pathname)) {
    throw new Error(`unsupported static resource path: ${url.pathname}`);
  }
  return url;
}

/** Validate the complete resource batch before starting any fetch.
 * 启动请求前校验整个静态资源批次。 */
export async function captureStaticResources({ cdp, runtime, session, urls, relativeDir = "raw/static" }) {
  const unique = [...new Set(urls)].map(assertAllowed);
  const results = [];
  for (let index = 0; index < unique.length; index += 1) {
    const url = unique[index];
    const expression = `fetch(${JSON.stringify(url.href)},{credentials:'include'}).then(async r=>({status:r.status,contentType:r.headers.get('content-type'),body:await r.text()}))`;
    try {
      const evaluated = await cdp.send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true
      }, { timeoutMs: 30000 });
      const value = evaluated?.result?.value;
      if (!value || typeof value.body !== "string") throw new Error("body unavailable");
      const object = await runtime.storeArtifact(session, relativeDir, safeName(url, index), value.body, {
        object_type: "static_resource",
        source_endpoint: url.pathname,
        source_query_keys: [...url.searchParams.keys()],
        http_status: value.status,
        mime_type: value.contentType
      });
      results.push({ index: index + 1, ok: value.status === 200, status: value.status, bytes: object.bytes, path: object.relative_path });
    } catch (error) {
      results.push({ index: index + 1, ok: false, error: String(error?.message ?? error) });
    }
  }
  return {
    attempted: results.length,
    ok: results.filter(x => x.ok).length,
    failed: results.filter(x => !x.ok).length,
    bytes: results.reduce((sum, x) => sum + (x.bytes || 0), 0),
    results
  };
}
