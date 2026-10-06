import crypto from "node:crypto";
import path from "node:path";
import { assertAutomaticCaptureUrl } from "./scope.mjs";

const EXTERNAL_AI_HOSTS = new Set([
  "api.openai.com",
  "chatgpt.com",
  "openai.com",
  "api.anthropic.com",
  "generativelanguage.googleapis.com",
  "api.mistral.ai",
  "api.cohere.com"
]);
const RESOURCE_HOSTS = new Set([
  "crm.xiaoman.cn",
  "v4client-oss.xiaoman.cn",
  "v4client.oss-cn-hangzhou.aliyuncs.com"
]);

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function assertAllowed(rawUrl) {
  assertAutomaticCaptureUrl(rawUrl);
  const url = new URL(rawUrl);
  if (EXTERNAL_AI_HOSTS.has(url.hostname) || [...EXTERNAL_AI_HOSTS].some(host => url.hostname.endsWith(`.${host}`))) {
    throw new Error(`external AI host forbidden: ${url.hostname}`);
  }
  if (!RESOURCE_HOSTS.has(url.hostname)) throw new Error(`resource host out of scope: ${url.hostname}`);
  if (url.hostname === "crm.xiaoman.cn" && !url.pathname.startsWith("/api/")) {
    throw new Error(`non-API CRM resource rejected: ${url.pathname}`);
  }
  return url;
}

export async function getMainFrameId(cdp) {
  await cdp.send("Page.enable");
  const tree = await cdp.send("Page.getFrameTree");
  const frameId = tree?.frameTree?.frame?.id;
  if (!frameId) throw new Error("main frame id unavailable");
  return frameId;
}

/** Enforce the boundary before issuing a credentialed resource request.
 * 携带凭据请求资源前执行采集边界检查。 */
export async function captureResourceGet({ cdp, runtime, session, frameId, url: rawUrl, label, relativeDir }) {
  const url = assertAllowed(rawUrl);
  const result = await cdp.send("Network.loadNetworkResource", {
    frameId,
    url: url.href,
    options: {
      disableCache: true,
      includeCredentials: true
    }
  }, { timeoutMs: 30000 });
  const resource = result?.resource;
  if (!resource) throw new Error(`no resource result for ${url.pathname}`);
  const content = resource.content ?? "";
  const buffer = resource.base64Encoded ? Buffer.from(content, "base64") : Buffer.from(content, "utf8");
  const ext = (resource.mimeType || "").includes("json") ? ".json" : ".bin";
  const fileName = `${label}__${sha256(url.href).slice(0, 16)}${ext}`;
  const object = await runtime.storeArtifact(
    session,
    relativeDir,
    fileName,
    buffer,
    {
      object_type: "cdp_resource_get",
      source_label: label,
      source_url: runtime.redactUrl(url.href),
      http_status: resource.httpStatusCode ?? null,
      mime_type: resource.mimeType ?? null
    }
  );
  return {
    success: Boolean(resource.success),
    status: resource.httpStatusCode ?? null,
    mimeType: resource.mimeType ?? null,
    bytes: buffer.byteLength,
    sha256: object.sha256,
    relativePath: object.relative_path
  };
}
