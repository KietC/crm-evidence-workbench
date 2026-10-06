/** Versioned vendor contract and URL guards; tenant configuration stays outside source.
 * 版本化厂商接口与网址保护；租户配置应留在源码之外。 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { OkkiAdapter } from "./types.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, "..");
export const DEFAULT_ADAPTER_PATH = path.join(APP_ROOT, "adapters", "okki", "v1", "adapter.json");

/**
 * Trade-data evidence belongs to the deliberately slow, visible manual lane.
 * The automatic seven-tab collector must never persist, replay, or discover
 * these routes. Keep this predicate independent from adapter configuration so
 * a malformed or stale adapter cannot silently widen the automatic scope.
 * 贸易数据必须走慢速可见人工通道；此限制独立于适配器配置，防止旧配置扩大采集范围。
 */
export function isAutoCaptureExcludedUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl, "https://crm.xiaoman.cn");
    const pathname = url.pathname.toLowerCase().replace(/\/+$/, "") || "/";
    return (pathname === "/crm/customer/personal" && url.searchParams.get("tab")?.toLowerCase() === "seadata")
      || pathname === "/ciq/info"
      || pathname.startsWith("/ciq/info/")
      || pathname === "/api/ciqread"
      || pathname.startsWith("/api/ciqread/")
      || pathname === "/api/aiwordread/wordcloud"
      || pathname.startsWith("/api/aiwordread/wordcloud/");
  } catch {
    return false;
  }
}

export async function loadAdapter(adapterPath = DEFAULT_ADAPTER_PATH): Promise<OkkiAdapter> {
  const raw = await fs.readFile(adapterPath, "utf8");
  const adapter = JSON.parse(raw) as OkkiAdapter;
  if (adapter.schema !== 1 || !adapter.origin || !adapter.customer_path || !adapter.root_tabs?.length) {
    throw new Error(`无效的 OKKI 适配器: ${adapterPath}`);
  }
  if (adapter.root_tabs.some(tab => tab.id.toLowerCase() === "seadata")) {
    throw new Error(`无效的 OKKI 适配器（贸易数据不得进入自动标签）: ${adapterPath}`);
  }
  if (adapter.expected_endpoint_contracts.some(contract =>
    isAutoCaptureExcludedUrl(new URL(contract.path, adapter.origin).href))) {
    throw new Error(`无效的 OKKI 适配器（贸易接口不得进入自动契约）: ${adapterPath}`);
  }
  return adapter;
}

export function parseCustomerUrl(rawUrl: string, adapter: OkkiAdapter): { companyId: string; url: URL } | null {
  try {
    const url = new URL(rawUrl);
    if (url.origin !== adapter.origin || url.pathname !== adapter.customer_path) return null;
    const companyId = url.searchParams.get("company_id") ?? "";
    if (!/^\d+$/.test(companyId)) return null;
    return { companyId, url };
  } catch {
    return null;
  }
}

export function isBlockedAiUrl(rawUrl: string, adapter: OkkiAdapter): boolean {
  try {
    const host = new URL(rawUrl).hostname.toLowerCase();
    return adapter.external_ai_blocked_hosts.some(item => host === item || host.endsWith(`.${item}`));
  } catch {
    return false;
  }
}

export function isAllowedCaptureUrl(rawUrl: string, adapter: OkkiAdapter): boolean {
  try {
    const url = new URL(rawUrl);
    return adapter.page_asset_hosts.some(item => url.hostname === item || url.hostname.endsWith(`.${item}`));
  } catch {
    return false;
  }
}

export function isTelemetryUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    const host = url.hostname.toLowerCase();
    const pathname = url.pathname.toLowerCase();
    return host.includes("sensorsdata")
      || host === "hm.baidu.com"
      || host.endsWith(".hm.baidu.com")
      || host === "www.google-analytics.com"
      || host.endsWith(".google-analytics.com")
      || host === "www.googletagmanager.com"
      || host.endsWith(".googletagmanager.com")
      || host.includes("arms-retcode")
      || host.endsWith("log.aliyuncs.com")
      || pathname === "/sa.gif"
      || pathname.includes("/logstores/")
      || pathname.includes("/trace/track");
  } catch {
    return false;
  }
}

/**
 * Only retry an exact GET URL already observed by the browser. This does not
 * widen recursive discovery, but it lets page assets move to a new CDN without
 * being silently lost. External AI and telemetry remain excluded.
 * 仅重试浏览器实际观察到的 GET；不扩大递归发现范围，外部 AI 与遥测始终排除。
 */
export function isSafeObservedGetRecoveryUrl(rawUrl: string, adapter: OkkiAdapter): boolean {
  try {
    const url = new URL(rawUrl);
    return (url.protocol === "http:" || url.protocol === "https:")
      && !isBlockedAiUrl(url.href, adapter)
      && !isTelemetryUrl(url.href)
      && !isAutoCaptureExcludedUrl(url.href);
  } catch {
    return false;
  }
}
