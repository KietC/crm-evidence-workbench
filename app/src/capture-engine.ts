/**
 * Scoped, read-only CRM capture with append-only evidence and explicit reconciliation.
 * 范围受限的只读 CRM 采集；证据追加保存，覆盖情况必须显式对账。
 */
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import type { CDPSession, Locator, Page, Request, Response } from "playwright-core";
import { isAllowedCaptureUrl, isAutoCaptureExcludedUrl, isBlockedAiUrl, isSafeObservedGetRecoveryUrl, isTelemetryUrl, parseCustomerUrl } from "./adapter.js";
import { EvidenceStore, nowIso, redactUrl, resolveCaseFolderName, sanitizeHeaders, sha256, sourceUrlIdentityKey } from "./evidence-store.js";
import type { BrowserManager } from "./browser-manager.js";
import type { JobStatus, OkkiAdapter, ReconciliationCheck, StoredResponse } from "./types.js";

interface SyntheticResult {
  ok: boolean;
  status: number;
  url: string;
  contentType: string;
  base64: string;
  error: string | null;
}

export interface ReadOnlyPostReplayInput {
  url: string;
  body: Buffer;
  headers: Record<string, string>;
  requestBodySha256: string;
}

interface FailedRequestBodyCapture {
  sequence: number;
  method: string;
  url: string;
  body: Buffer;
  role: ReturnType<typeof requestBodyRole>;
}

interface MailCaptureDraft {
  mailId: string;
  userId: string | null;
  detailStatus: "AVAILABLE" | "SOURCE_DELETED" | "UNAVAILABLE";
  detail: StoredResponse;
  track: StoredResponse;
  relatedResources: Array<{ sourceIdentity: string; aliasKey: string | null }>;
}

export interface CaptureStartOptions {
  expectedCompanyId?: string;
  resumeExisting?: boolean;
  revisitUiGaps?: boolean;
}

export const UI_GAP_OTHER_DYNAMIC_LABELS = ["其他动态", "其它", "其他"] as const;
export const UI_GAP_DYNAMIC_WRAPPER_SELECTOR = ".customer-dynamic-wrapper";
export const UI_GAP_DYNAMIC_FILTER_SELECTOR = ".flex-1.overflow-x-auto button.okki-btn-rect.okki-btn-sm";

/** Bind revisits to an explicitly selected one-shot identity; never infer it from a page.
 * 将补采绑定到明确指定的单客户身份，不能从当前页面猜测绑定目标。 */
export function assertUiGapRevisitIdentity(companyId: string, expectedCompanyId: string): void {
  if (!/^\d+$/.test(expectedCompanyId) || companyId !== expectedCompanyId) {
    throw new Error("UI gap revisit requires an explicit matching one-shot company binding");
  }
}

function normalizeUiFilterLabel(value: string): string {
  return value
    .normalize("NFKC")
    .replace(/\s+/g, " ")
    .replace(/\s*\(\s*\d[\d,]*\s*\)\s*$/u, "")
    .replace(/\s+\d[\d,]*\s*$/u, "")
    .trim();
}

/**
 * Return only extra visible inner tabs which are not configured already.
 * Root customer tabs and the manual trade lane are excluded before any click.
 */
export function additionalVisibleUiGapFilters(adapter: OkkiAdapter, visibleTabLabels: string[]): string[] {
  const rootLabels = new Set(adapter.root_tabs.flatMap(tab => tab.labels).map(normalizeUiFilterLabel));
  const configuredLabels = new Set([
    ...adapter.dynamic.history_labels,
    ...adapter.dynamic.filter_labels.flat(),
    ...adapter.dynamic.mail_labels,
    ...UI_GAP_OTHER_DYNAMIC_LABELS
  ].map(normalizeUiFilterLabel));
  const forbidden = /^(?:\u8d38\u6613\u6570\u636e|\u6d77\u5173\u6570\u636e|\u8fdb\u51fa\u53e3\u6570\u636e)$/u;
  const output: string[] = [];
  const seen = new Set<string>();
  for (const raw of visibleTabLabels) {
    const label = normalizeUiFilterLabel(raw);
    if (!label || label.length > 40 || rootLabels.has(label) || configuredLabels.has(label) || forbidden.test(label)) continue;
    if (seen.has(label)) continue;
    seen.add(label);
    output.push(label);
  }
  return output;
}

export function uiGapMatchingFilterIndex(labels: string[], visibleButtonTexts: string[]): number {
  const wanted = new Set(labels.map(normalizeUiFilterLabel));
  return visibleButtonTexts.findIndex(text => wanted.has(normalizeUiFilterLabel(text)));
}

/**
 * Count how many configured dynamic-filter groups are represented by a button bar.
 * This deliberately ignores the numeric badges because they change while the page hydrates.
 */
export function uiGapDynamicFilterCoverage(
  expectedLabelGroups: readonly (readonly string[])[],
  visibleButtonTexts: readonly string[]
): number {
  const visible = new Set(visibleButtonTexts.map(normalizeUiFilterLabel));
  return expectedLabelGroups.filter(group => group.some(label => visible.has(normalizeUiFilterLabel(label)))).length;
}

/** Pick the most complete visible filter bar, rather than the first transient/stale bar. */
export function uiGapBestDynamicFilterCandidateIndex(
  expectedLabelGroups: readonly (readonly string[])[],
  candidateButtonTexts: readonly (readonly string[])[]
): number {
  let bestIndex = -1;
  let bestCoverage = -1;
  let bestButtonCount = -1;
  candidateButtonTexts.forEach((texts, index) => {
    const coverage = uiGapDynamicFilterCoverage(expectedLabelGroups, texts);
    if (coverage > bestCoverage || (coverage === bestCoverage && texts.length > bestButtonCount)) {
      bestIndex = index;
      bestCoverage = coverage;
      bestButtonCount = texts.length;
    }
  });
  return bestIndex;
}

export type UiPaginationTerminalReason =
  | "next_absent"
  | "next_disabled"
  | "content_unchanged"
  | "safety_cap_reached"
  | "filter_not_found"
  | "capture_error";

export function uiPaginationTerminalClosed(reason: UiPaginationTerminalReason): boolean {
  return reason === "next_absent" || reason === "next_disabled";
}

interface UiPaginationClosure {
  pagesCaptured: number;
  terminalClosed: boolean;
  terminalReason: UiPaginationTerminalReason;
}

interface UiFilterCaptureResult extends UiPaginationClosure {
  label: string;
  source: "configured" | "visible_tab";
  found: boolean;
}

export const ABSOLUTE_API_PAGE_CAP = 20_000;
function boundedEnvironmentNumber(name: string, fallback: number, minimum: number, maximum: number): number {
  const parsed = Number(process.env[name]);
  return Math.min(maximum, Math.max(minimum, Number.isFinite(parsed) ? parsed : fallback));
}

const HOST_PARALLELISM = Math.max(1, os.availableParallelism());
const HOST_MEMORY_GB = os.totalmem() / 1024 ** 3;
const DEFAULT_API_PAGE_CONCURRENCY = Math.min(24, Math.max(6, Math.floor(HOST_PARALLELISM / 8)));
const DEFAULT_MAIL_DETAIL_CONCURRENCY = Math.min(16, Math.max(6, Math.floor(HOST_PARALLELISM / 12)));
const DEFAULT_RESOURCE_DOWNLOAD_CONCURRENCY = HOST_MEMORY_GB >= 192 ? 8 : HOST_MEMORY_GB >= 96 ? 6 : 3;

export const API_PAGE_CONCURRENCY = boundedEnvironmentNumber(
  "OKKI_API_PAGE_CONCURRENCY", DEFAULT_API_PAGE_CONCURRENCY, 2, 32
);
export const MAIL_DETAIL_CONCURRENCY = boundedEnvironmentNumber(
  "OKKI_MAIL_DETAIL_CONCURRENCY", DEFAULT_MAIL_DETAIL_CONCURRENCY, 2, 24
);
// Network.loadNetworkResource materializes each response in the Node heap
// before the evidence store can hash and persist it. Twelve concurrent PDFs or
// images can exhaust an 8 GiB old-space heap. Keep API pagination parallel,
// but bound large binary downloads tightly so capture stays lossless.
export const RESOURCE_DOWNLOAD_CONCURRENCY = boundedEnvironmentNumber(
  "OKKI_RESOURCE_DOWNLOAD_CONCURRENCY", DEFAULT_RESOURCE_DOWNLOAD_CONCURRENCY, 2, 10
);
const SETTLE_SCALE = boundedEnvironmentNumber("OKKI_SETTLE_SCALE_PERCENT", 65, 50, 100) / 100;

export function paginationPagePlan(total: number, pageSize: number, configuredCap: number): { requiredPages: number; pageLimit: number } {
  const requiredPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
  if (requiredPages > ABSOLUTE_API_PAGE_CAP) {
    throw new Error(`接口声明需要 ${requiredPages} 页，超过绝对安全上限 ${ABSOLUTE_API_PAGE_CAP}`);
  }
  return {
    requiredPages,
    pageLimit: Math.min(ABSOLUTE_API_PAGE_CAP, Math.max(configuredCap, requiredPages + 1))
  };
}

export function paginationBatchPageNumbers(
  nextPage: number,
  pageLimit: number,
  remainingRows: number,
  pageSize: number,
  concurrency = API_PAGE_CONCURRENCY
): number[] {
  if (nextPage > pageLimit || remainingRows <= 0) return [];
  const pagesNeeded = Math.max(1, Math.ceil(remainingRows / Math.max(1, pageSize)));
  const count = Math.min(concurrency, pagesNeeded, pageLimit - nextPage + 1);
  return Array.from({ length: count }, (_, offset) => nextPage + offset);
}

function freshStatus(): JobStatus {
  return {
    id: null, phase: "idle", running: false, progress: 0,
    headline: "等待采集", detail: "启动专用浏览器并打开一个客户详情页",
    companyId: null, caseRoot: null, sessionRoot: null,
    startedAt: null, finishedAt: null, errors: [], warnings: [], metrics: {}
  };
}

function recursiveValues(value: unknown, keyPattern: RegExp, output: string[] = []): string[] {
  if (Array.isArray(value)) {
    for (const item of value) recursiveValues(item, keyPattern, output);
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      if (keyPattern.test(key) && (typeof item === "string" || typeof item === "number")) output.push(String(item));
      recursiveValues(item, keyPattern, output);
    }
  }
  return output;
}

function recursiveObjects(value: unknown, predicate: (item: Record<string, unknown>) => boolean, output: Record<string, unknown>[] = []): Record<string, unknown>[] {
  if (Array.isArray(value)) {
    for (const item of value) recursiveObjects(item, predicate, output);
  } else if (value && typeof value === "object") {
    const object = value as Record<string, unknown>;
    if (predicate(object)) output.push(object);
    for (const item of Object.values(object)) recursiveObjects(item, predicate, output);
  }
  return output;
}

function normalizeExtractedUrl(raw: string): string | null {
  let value = raw.replace(/&amp;(?:;)?/gi, "&");
  const encodedHtml = value.search(/(?:%22(?=%3e|$)|%3c)/i);
  if (encodedHtml >= 0) value = value.slice(0, encodedHtml);
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}

export function recursiveUrls(value: unknown, output: string[] = []): string[] {
  if (typeof value === "string") {
    const matches = value.match(/https?:\/\/[^\s<>"'`}\]]+/gi);
    if (matches) output.push(...matches.map(normalizeExtractedUrl).filter((item): item is string => Boolean(item)));
  } else if (Array.isArray(value)) {
    for (const item of value) recursiveUrls(item, output);
  } else if (value && typeof value === "object") {
    for (const item of Object.values(value)) recursiveUrls(item, output);
  }
  return output;
}

export function resourceAliasKey(rawUrl: string): string | null {
  try {
    const url = new URL(rawUrl);
    const decodedPath = decodeURIComponent(url.pathname);
    return decodedPath.replace(/\.{2,}(?=(?:png|jpe?g|gif|webp|svg|pdf)$)/i, ".");
  } catch {
    return null;
  }
}

const VOLATILE_SIGNED_URL_KEYS = new Set([
  "expires", "ossaccesskeyid", "signature", "awsaccesskeyid", "googleaccessid",
  "key-pair-id", "policy",
  "x-amz-algorithm", "x-amz-credential", "x-amz-date", "x-amz-expires",
  "x-amz-security-token", "x-amz-signature", "x-amz-signedheaders",
  "x-goog-algorithm", "x-goog-credential", "x-goog-date", "x-goog-expires",
  "x-goog-signature", "x-goog-signedheaders",
  "x-oss-access-key-id", "x-oss-credential", "x-oss-date", "x-oss-expires",
  "x-oss-security-token", "x-oss-signature", "x-oss-signature-version"
]);

/**
 * Signed object-store URLs frequently rotate only authentication fields while
 * still naming the same immutable object. Strip only known volatile signature
 * fields. Representation-changing fields (for example x-oss-process, width,
 * response-content-type) remain in the key, so transformed objects cannot be
 * used to mask a genuinely missing response body.
 */
export function signedResourceVariantKey(rawUrl: string): string | null {
  try {
    const url = new URL(rawUrl);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    const keys = [...url.searchParams.keys()];
    if (!keys.some(key => VOLATILE_SIGNED_URL_KEYS.has(key.toLowerCase()))) return null;
    for (const key of keys) {
      if (VOLATILE_SIGNED_URL_KEYS.has(key.toLowerCase())) url.searchParams.delete(key);
    }
    url.hash = "";
    url.searchParams.sort();
    return url.href;
  } catch {
    return null;
  }
}

export function responseBodyTimeoutMs(resourceType: string): number {
  return ["image", "media", "font"].includes(resourceType) ? 90_000 : 45_000;
}

export function isConfiguredMailFilterUrl(rawUrl: string, adapter: OkkiAdapter): boolean {
  try {
    const url = new URL(rawUrl);
    const modules = url.searchParams.getAll("modules[]");
    return adapter.dynamic.mail_module_values.every(value => modules.includes(value))
      && !url.searchParams.get("begin_time")
      && !url.searchParams.get("end_time");
  } catch {
    return false;
  }
}

export function isConfiguredMailTrailRow(value: unknown, adapter: OkkiAdapter): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  const allowed = new Set(adapter.dynamic.mail_module_values.map(item => String(item)));
  return [row.type, row.node_type]
    .filter(item => typeof item === "string" || typeof item === "number")
    .some(item => allowed.has(String(item)));
}

export function completeConfiguredMailIds(
  dedicatedRows: unknown[],
  broadRows: unknown[],
  adapter: OkkiAdapter
): string[] {
  const configuredBroadRows = broadRows.filter(row => isConfiguredMailTrailRow(row, adapter));
  return [...new Set([...dedicatedRows, ...configuredBroadRows]
    .flatMap(row => recursiveValues(row, /^mail_?id$/i))
    .filter(Boolean))];
}

export function configuredMailIdentityClosure(
  rows: unknown[],
  adapter: OkkiAdapter,
  expectedCompanyId: string
): {
  complete: boolean;
  configuredRows: number;
  rowsWithoutExactIdentity: number;
  companyMismatches: number;
  mailToTrailConflicts: number;
  trailToMailConflicts: number;
  uniqueMailIds: number;
  uniqueTrailIds: number;
} {
  const configuredRows = rows.filter(row => isConfiguredMailTrailRow(row, adapter));
  const mailToTrails = new Map<string, Set<string>>();
  const trailToMails = new Map<string, Set<string>>();
  let rowsWithoutExactIdentity = 0;
  let companyMismatches = 0;
  for (const value of configuredRows) {
    const row = value as Record<string, unknown>;
    const mailIds = unique(recursiveValues(row, /^mail_?id$/i));
    const trailIds = unique(recursiveValues(row, /^trail_?id$/i));
    if (String(row.company_id ?? "") !== expectedCompanyId) companyMismatches += 1;
    if (mailIds.length !== 1 || trailIds.length !== 1) {
      rowsWithoutExactIdentity += 1;
      continue;
    }
    const mailId = mailIds[0]!;
    const trailId = trailIds[0]!;
    if (!mailToTrails.has(mailId)) mailToTrails.set(mailId, new Set());
    if (!trailToMails.has(trailId)) trailToMails.set(trailId, new Set());
    mailToTrails.get(mailId)!.add(trailId);
    trailToMails.get(trailId)!.add(mailId);
  }
  const mailToTrailConflicts = [...mailToTrails.values()].filter(values => values.size !== 1).length;
  const trailToMailConflicts = [...trailToMails.values()].filter(values => values.size !== 1).length;
  return {
    complete: configuredRows.length > 0
      && rowsWithoutExactIdentity === 0
      && companyMismatches === 0
      && mailToTrailConflicts === 0
      && trailToMailConflicts === 0
      && mailToTrails.size === trailToMails.size,
    configuredRows: configuredRows.length,
    rowsWithoutExactIdentity,
    companyMismatches,
    mailToTrailConflicts,
    trailToMailConflicts,
    uniqueMailIds: mailToTrails.size,
    uniqueTrailIds: trailToMails.size
  };
}

export function selectUniqueAliasArtifact<T extends { bodySha256: string | null; sequence: number }>(
  candidates: T[]
): T | null {
  if (!candidates.length || candidates.some(candidate => !candidate.bodySha256)) return null;
  if (new Set(candidates.map(candidate => candidate.bodySha256)).size !== 1) return null;
  return [...candidates].sort((left, right) => left.sequence - right.sequence)[0] ?? null;
}

/**
 * trailList's stat_info=1 response is a dashboard/statistics slice, not a
 * normal list page. Synthetic pagination must always use stat_info=0,
 * including page 1, otherwise the declared total can look complete while the
 * first page silently contains only a small summary subset.
 */
export function normalizeObservedPaginationUrl(rawUrl: string, trailEndpoint: string): string {
  const url = new URL(rawUrl);
  if (url.pathname === trailEndpoint) url.searchParams.set("stat_info", "0");
  return url.href;
}

export function paginationUrlForPage(
  rawUrl: string,
  pageParam: string,
  pageNo: number,
  obsoleteParams: string[] = []
): string {
  const url = new URL(rawUrl);
  for (const parameter of obsoleteParams) url.searchParams.delete(parameter);
  url.searchParams.set(pageParam, String(pageNo));
  return url.href;
}

export function canonicalRequestUrl(rawUrl: string): string | null {
  try {
    const url = new URL(rawUrl);
    url.hash = "";
    url.searchParams.sort();
    return url.href;
  } catch {
    return null;
  }
}

export function exactGetRecoveryKey(method: string, rawUrl: string): string | null {
  if (method.toUpperCase() !== "GET" || isAutoCaptureExcludedUrl(rawUrl)) return null;
  return canonicalRequestUrl(rawUrl);
}

const SAFE_READ_ONLY_POST_PATHS = new Set([
  "/api/opportunityRead/list",
  "/api/reportRead/detail",
  "/api/customerV2Read/companyCustomerList"
]);

export function isReadOnlyApiPath(pathname: string): boolean {
  return SAFE_READ_ONLY_POST_PATHS.has(pathname)
    || /^\/api\/[^/]*Read\/[^/]+$/.test(pathname);
}

export function isExplicitlyUnavailableDiscoveredResource(resourceType: string, status: number): boolean {
  return resourceType === "discovered_file" && [400, 403, 404, 410].includes(status);
}

export function exactReadOnlyPostKey(method: string, rawUrl: string, requestBodySha256?: string | null): string | null {
  if (method.toUpperCase() !== "POST" || !requestBodySha256) return null;
  try {
    const url = new URL(rawUrl);
    if (isAutoCaptureExcludedUrl(url.href) || !isReadOnlyApiPath(url.pathname)) return null;
    const canonicalUrl = canonicalRequestUrl(url.href);
    return canonicalUrl === null ? null : `POST\n${canonicalUrl}\n${requestBodySha256}`;
  } catch {
    return null;
  }
}

export function externalPassiveSourceUnavailableKeys<T extends {
  method: string;
  url: string;
  status: number;
  resourceType: string;
  bodyRelativePath?: string | null;
  error?: string | null;
}>(records: T[], automaticOrigin: string): Set<string> {
  const sourceKey = (record: T): string | null =>
    signedResourceVariantKey(record.url) ?? exactGetRecoveryKey(record.method, record.url);
  const passiveTypes = new Set(["document", "stylesheet", "script", "image", "media", "font", "manifest"]);
  const passiveFailures = new Set(records.filter(record => {
    if (record.method.toUpperCase() !== "GET" || record.status < 200 || record.status >= 300
      || !record.error || record.bodyRelativePath || !passiveTypes.has(record.resourceType)) return false;
    try { return new URL(record.url).origin !== automaticOrigin; } catch { return false; }
  }).map(sourceKey).filter((key): key is string => Boolean(key)));
  const unavailableRetries = new Set(records.filter(record =>
    record.method.toUpperCase() === "GET"
    && record.resourceType === "response_retry"
    && [400, 403, 404, 410].includes(record.status)
    && Boolean(record.error)
    && !record.bodyRelativePath
  ).map(sourceKey).filter((key): key is string => Boolean(key)));
  return new Set([...passiveFailures].filter(key => unavailableRetries.has(key)));
}

export function externalPassiveSourceUnavailableKey(method: string, rawUrl: string): string | null {
  return signedResourceVariantKey(rawUrl) ?? exactGetRecoveryKey(method, rawUrl);
}

export function replayablePostHeaders(headers: Record<string, string>): Record<string, string> {
  const allowed = new Set(["accept", "content-type", "x-requested-with", "x-csrf-token", "x-xsrf-token"]);
  return Object.fromEntries(Object.entries(headers)
    .map(([key, value]) => [key.toLowerCase(), value] as const)
    .filter(([key]) => allowed.has(key) || key.startsWith("x-okki-")));
}

const POST_PAGE_KEYS = ["curPage", "currentPage", "page", "page_no", "pageNo", "contact_page_index"] as const;
const POST_PAGE_SIZE_KEYS = ["pageSize", "page_size", "limit", "per_page", "contact_page_size"] as const;

function updateObjectPage(value: Record<string, unknown>, pageNo: number): boolean {
  let updated = false;
  for (const key of POST_PAGE_KEYS) {
    if (!(key in value)) continue;
    value[key] = typeof value[key] === "string" ? String(pageNo) : pageNo;
    updated = true;
  }
  if (updated) return true;
  for (const containerKey of ["data", "params", "query", "pagination", "pageInfo"]) {
    const nested = value[containerKey];
    if (nested && typeof nested === "object" && !Array.isArray(nested)
      && updateObjectPage(nested as Record<string, unknown>, pageNo)) return true;
  }
  return false;
}

/**
 * Derive a page-specific replay from a POST request already emitted by the
 * page. Only observed pagination fields are changed; all customer filters and
 * the original content type remain untouched. Unknown request shapes fail
 * closed instead of inventing a new API contract.
 */
export function readOnlyPostPaginationInputForPage(
  input: ReadOnlyPostReplayInput,
  pageNo: number
): ReadOnlyPostReplayInput | null {
  if (!Number.isSafeInteger(pageNo) || pageNo < 1 || isAutoCaptureExcludedUrl(input.url)) return null;
  const url = new URL(input.url);
  let updated = false;
  for (const key of POST_PAGE_KEYS) {
    if (!url.searchParams.has(key)) continue;
    url.searchParams.set(key, String(pageNo));
    updated = true;
  }

  let body = Buffer.from(input.body);
  const contentType = input.headers["content-type"]?.toLowerCase() ?? "";
  const text = input.body.toString("utf8");
  if (contentType.includes("application/json") || text.trimStart().startsWith("{")) {
    try {
      const parsed = JSON.parse(text) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)
        && updateObjectPage(parsed as Record<string, unknown>, pageNo)) {
        body = Buffer.from(JSON.stringify(parsed));
        updated = true;
      }
    } catch {
      if (!updated) return null;
    }
  } else if (contentType.includes("application/x-www-form-urlencoded") || text.includes("=")) {
    const form = new URLSearchParams(text);
    let formUpdated = false;
    for (const key of POST_PAGE_KEYS) {
      if (!form.has(key)) continue;
      form.set(key, String(pageNo));
      formUpdated = true;
    }
    if (formUpdated) {
      body = Buffer.from(form.toString());
      updated = true;
    }
  }
  if (!updated) return null;
  return { ...input, url: url.href, body, requestBodySha256: sha256(body) };
}

export function readOnlyPostAdvertisedPageSize(input: ReadOnlyPostReplayInput): number | null {
  const values: unknown[] = [];
  const url = new URL(input.url);
  for (const key of POST_PAGE_SIZE_KEYS) if (url.searchParams.has(key)) values.push(url.searchParams.get(key));
  const text = input.body.toString("utf8");
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>;
    for (const key of POST_PAGE_SIZE_KEYS) values.push(parsed[key]);
    for (const containerKey of ["data", "params", "query", "pagination", "pageInfo"]) {
      const nested = parsed[containerKey];
      if (nested && typeof nested === "object" && !Array.isArray(nested)) {
        for (const key of POST_PAGE_SIZE_KEYS) values.push((nested as Record<string, unknown>)[key]);
      }
    }
  } catch {
    const form = new URLSearchParams(text);
    for (const key of POST_PAGE_SIZE_KEYS) if (form.has(key)) values.push(form.get(key));
  }
  const parsed = values.map(value => Number(value)).find(value => Number.isSafeInteger(value) && value > 0);
  return parsed ?? null;
}

export function mailRelationAnchorIds(mailIds: string[], windowSize = 100): string[] {
  if (!mailIds.length) return [];
  const step = Math.max(1, Math.floor(windowSize));
  const anchors = mailIds.filter((_mailId, index) => index % step === 0);
  anchors.push(mailIds.at(-1)!);
  return unique(anchors);
}

export function nextUncoveredMailRelationAnchor(
  mailIds: string[],
  seen: ReadonlySet<string>,
  queued: ReadonlySet<string>
): string | null {
  return mailIds.find(mailId => !seen.has(mailId) && !queued.has(mailId)) ?? null;
}

/**
 * Read-only first-page probes for contracts which some OKKI layouts do not
 * request when a tab is hidden, empty, virtualized, or renamed. The parameter
 * shapes come from the locally observed API contract; only the current
 * company id is inserted. Normal pagination subsequently expands any non-zero
 * total, so this is not a page-one-only fallback.
 */
export function requiredCoreGetUrls(companyId: string, adapter: OkkiAdapter): string[] {
  const make = (endpoint: string, params: Record<string, string>, repeated: Array<[string, string]> = []): string => {
    const url = new URL(endpoint, adapter.origin);
    for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
    for (const [key, value] of repeated) url.searchParams.append(key, value);
    return url.href;
  };
  const trailBase = {
    adjust_email_dynamic: "0",
    begin_time: "",
    company_id: companyId,
    create_user: "",
    curPage: "1",
    customer_id: "",
    end_time: "",
    keyword: "",
    module: "",
    pageSize: "20",
    scene: "drawer",
    sort: "",
    stat_info: "0"
  };
  return [
    make("/api/customerV2Read/FormFieldList", {
      company_id: companyId, scene: "info", archive_flag: "1", privilege_scene: "drawer"
    }),
    make("/api/customerRead/companyDiffList", { company_id: companyId, curPage: "1", pageSize: "20" }),
    make("/api/customerRead/customerOrderList", {
      company_id: companyId, page: "1", page_size: "10", sort_field: "create_time", sort_type: "desc"
    }),
    make("/api/customerRead/relatedProductList", {
      company_id: companyId, page: "1", page_size: "10"
    }),
    make(adapter.documents.list_endpoint, {
      folder_id: "0", keyword: "", object_id: companyId, object_name: "objCompany", page_no: "1", page_size: "20"
    }),
    make(adapter.documents.folder_endpoint, {
      object_id: companyId, object_name: "objCompany"
    }),
    make(adapter.dynamic.trail_endpoint, trailBase),
    make(
      adapter.dynamic.trail_endpoint,
      trailBase,
      adapter.dynamic.mail_module_values.map(value => ["modules[]", value] as [string, string])
    )
  ];
}

async function retryTransient<T>(operation: () => Promise<T>, attempts = 3): Promise<T> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try { return await operation(); }
    catch (error) {
      lastError = error;
      if (attempt < attempts) await new Promise(resolve => setTimeout(resolve, attempt * 75));
    }
  }
  throw lastError;
}

export function requestBodyRole(method: string, rawUrl: string, adapter: OkkiAdapter): "contract_business" | "same_origin_other" | "external_service" {
  try {
    const url = new URL(rawUrl);
    const normalizedMethod = method.toUpperCase();
    if (url.origin !== adapter.origin) return "external_service";
    return adapter.expected_endpoint_contracts.some(contract =>
      contract.method.toUpperCase() === normalizedMethod && contract.path === url.pathname
    ) ? "contract_business" : "same_origin_other";
  } catch {
    return "external_service";
  }
}

export function isNonEvidenceMutationResponse(method: string, rawUrl: string, adapter: OkkiAdapter): boolean {
  if (!new Set(["POST", "PUT", "PATCH", "DELETE"]).has(method.toUpperCase())) return false;
  try {
    const url = new URL(rawUrl);
    if (url.origin !== adapter.origin || !/^\/api\/[^/]*Write\//i.test(url.pathname)) return false;
    return !adapter.expected_endpoint_contracts.some(contract =>
      contract.method.toUpperCase() === method.toUpperCase() && contract.path === url.pathname);
  } catch {
    return false;
  }
}

export function customerNameFromDetail(value: unknown): string | null {
  const candidate = valueAtPath(value, "data.values.name");
  return typeof candidate === "string" && candidate.trim() ? candidate.trim() : null;
}

function valueAtPath(value: unknown, dottedPath: string): unknown {
  let current: unknown = value;
  for (const part of dottedPath.split(".")) {
    if (!current || typeof current !== "object" || !(part in current)) return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

export function observedTotal(values: unknown[], paths: string[]): number | null {
  const numbers = values.flatMap(value => paths.map(item => valueAtPath(value, item)))
    .map(value => typeof value === "number" ? value : typeof value === "string" && /^\d+$/.test(value) ? Number(value) : NaN)
    .filter(Number.isFinite);
  return numbers.length ? Math.max(...numbers) : null;
}

export function primaryList(value: unknown): unknown[] {
  for (const dotted of ["data.list", "data.items", "list", "items", "data"]) {
    const candidate = valueAtPath(value, dotted);
    if (Array.isArray(candidate)) return candidate;
  }
  return [];
}

export function uniquePrimaryRowCount(values: unknown[]): number {
  return new Set(values.flatMap(primaryList).map(value => JSON.stringify(value))).size;
}

export function listCountReconciliationSummary<T extends { url: string; sequence: number; resourceType: string; json: unknown }>(
  records: T[],
  countPaths: string[]
): { expected: number | null; rowsCaptured: number; uniqueRows: number; duplicateRows: number; paginationBasis: boolean; complete: boolean } {
  const pagination = records.filter(record =>
    record.resourceType === "xhr_pagination" || record.resourceType === "xhr_post_pagination");
  const paginationBasis = pagination.length > 0;
  const basis = latestRecordsPerExactUrl(paginationBasis ? pagination : records);
  const values = basis.map(record => record.json);
  const expected = observedTotal(records.map(record => record.json), countPaths);
  const rawRows = values.flatMap(primaryList);
  const uniqueRows = new Set(rawRows.map(value => JSON.stringify(value))).size;
  const rowsCaptured = paginationBasis ? rawRows.length : uniqueRows;
  return {
    expected,
    rowsCaptured,
    uniqueRows,
    duplicateRows: Math.max(0, rawRows.length - uniqueRows),
    paginationBasis,
    complete: expected !== null && rowsCaptured === expected
  };
}

export function latestRecordsPerExactUrl<T extends { url: string; sequence: number }>(records: T[]): T[] {
  const latest = new Map<string, T>();
  for (const record of records) {
    const requestBodySha256 = "requestBodySha256" in record
      ? String((record as T & { requestBodySha256?: string | null }).requestBodySha256 ?? "")
      : "";
    const key = `${canonicalRequestUrl(record.url) ?? record.url}\n${requestBodySha256}`;
    const previous = latest.get(key);
    if (!previous || record.sequence > previous.sequence) latest.set(key, record);
  }
  return [...latest.values()].sort((left, right) => left.sequence - right.sequence);
}

export function effectivePaginationPageSize(advertised: number, firstPageRows: number): number {
  const safeAdvertised = Number.isFinite(advertised) && advertised > 0 ? Math.floor(advertised) : 20;
  const observed = Number.isFinite(firstPageRows) && firstPageRows > 0 ? Math.floor(firstPageRows) : safeAdvertised;
  return Math.max(1, Math.min(safeAdvertised, observed));
}

export function hasTerminalEmptyPage(values: unknown[]): boolean {
  return values.length > 1 && primaryList(values.at(-1)).length === 0;
}

export function mailPaginationSummary(values: unknown[], countPaths: string[]): {
  expected: number | null;
  rowCount: number;
  rowsWithMailId: number;
  rowsWithoutMailId: number;
  uniqueMailIds: number;
  duplicateMailIdOccurrences: number;
  rowsWithTrailId: number;
  rowsWithoutTrailId: number;
  uniqueTrailIds: number;
  duplicateTrailIdRows: number;
  nonDetailRowsWithoutTrailId: number;
  nonDetailTypeGroups: Array<{ module: string | null; type: string | null; nodeType: string | null; rows: number }>;
  complete: boolean;
} {
  const rows = values.flatMap(primaryList);
  const expected = observedTotal(values, countPaths);
  const rowsWithMailId = rows.filter(row => recursiveValues(row, /^mail_?id$/i).length > 0).length;
  const mailIds = rows.flatMap(row => recursiveValues(row, /^mail_?id$/i).slice(0, 1));
  const uniqueMailIds = new Set(mailIds).size;
  const nonDetailRows = rows.filter(row => recursiveValues(row, /^mail_?id$/i).length === 0);
  const trailIds = rows.flatMap(row => recursiveValues(row, /^trail_?id$/i).slice(0, 1));
  const uniqueTrailIds = new Set(trailIds).size;
  const groups = new Map<string, { module: string | null; type: string | null; nodeType: string | null; rows: number }>();
  const technicalValue = (row: unknown, key: string): string | null => {
    if (!row || typeof row !== "object") return null;
    const value = (row as Record<string, unknown>)[key];
    return typeof value === "string" || typeof value === "number" ? String(value) : null;
  };
  for (const row of nonDetailRows) {
    const group = {
      module: technicalValue(row, "module"),
      type: technicalValue(row, "type"),
      nodeType: technicalValue(row, "node_type")
    };
    const key = JSON.stringify(group);
    const previous = groups.get(key);
    groups.set(key, { ...group, rows: (previous?.rows ?? 0) + 1 });
  }
  return {
    expected,
    rowCount: rows.length,
    rowsWithMailId,
    rowsWithoutMailId: rows.length - rowsWithMailId,
    uniqueMailIds,
    duplicateMailIdOccurrences: mailIds.length - uniqueMailIds,
    rowsWithTrailId: trailIds.length,
    rowsWithoutTrailId: rows.length - trailIds.length,
    uniqueTrailIds,
    duplicateTrailIdRows: trailIds.length - uniqueTrailIds,
    nonDetailRowsWithoutTrailId: nonDetailRows.filter(row => recursiveValues(row, /^trail_?id$/i).length === 0).length,
    nonDetailTypeGroups: [...groups.values()].sort((left, right) => right.rows - left.rows),
    complete: expected !== null
      && rows.length === expected
      && trailIds.length === rows.length
      && uniqueTrailIds === expected
  };
}

export function paginationRowMultisetFingerprint(rows: unknown[]): string {
  const technicalValue = (row: unknown, key: string): string | null => {
    if (!row || typeof row !== "object") return null;
    const value = (row as Record<string, unknown>)[key];
    return typeof value === "string" || typeof value === "number" ? String(value) : null;
  };
  const identities = rows.map(row => JSON.stringify({
    trail_ids: unique(recursiveValues(row, /^trail_?id$/i)).sort(),
    mail_ids: unique(recursiveValues(row, /^mail_?id$/i)).sort(),
    module: technicalValue(row, "module"),
    type: technicalValue(row, "type"),
    node_type: technicalValue(row, "node_type")
  })).sort();
  return sha256(identities.join("\n"));
}

export function stablePaginatedRowClosure(
  expected: number | null,
  rowCount: number,
  rowsWithoutTrailId: number,
  duplicateTrailIdRows: number,
  duplicateMailIdOccurrences: number,
  duplicateProbeStable: boolean
): boolean {
  const duplicateRowsExist = duplicateTrailIdRows > 0 || duplicateMailIdOccurrences > 0;
  return expected !== null
    && rowCount === expected
    && rowsWithoutTrailId === 0
    && (!duplicateRowsExist || duplicateProbeStable);
}

export function zeroCountPaginationClosed(
  observedExpected: number | null,
  paginationExpected: number | null,
  paginationRowCount: number,
  unionRowCount: number,
  paginationRowsWithoutTrailId: number,
  unionRowsWithoutTrailId: number
): boolean {
  return observedExpected === 0
    && (paginationExpected === null || paginationExpected === 0)
    && paginationRowCount === 0
    && unionRowCount === 0
    && paginationRowsWithoutTrailId === 0
    && unionRowsWithoutTrailId === 0;
}

export function sameStringSet(left: string[], right: string[]): boolean {
  const leftSet = new Set(left);
  const rightSet = new Set(right);
  return leftSet.size === rightSet.size && [...leftSet].every(value => rightSet.has(value));
}

export function mailRelationCoverageComplete(
  relationExpected: number | null,
  expectedMailObjects: number | null,
  relationMailIds: string[],
  capturedMailIds: string[]
): boolean {
  if (relationExpected === null || expectedMailObjects === null || relationExpected !== expectedMailObjects) return false;
  const relationSet = new Set(relationMailIds);
  const capturedSet = new Set(capturedMailIds);
  return relationSet.size === relationExpected
    && [...capturedSet].every(mailId => relationSet.has(mailId));
}

export function mailPaginationExpectedMatches(
  expectedMailObjects: number,
  paginationExpected: number | null
): boolean {
  return expectedMailObjects === 0 || paginationExpected === expectedMailObjects;
}

export function dynamicPaginationSummary(values: unknown[], countPaths: string[]): {
  expected: number | null;
  rowCount: number;
  rowsWithTrailId: number;
  rowsWithoutTrailId: number;
  uniqueTrailIds: number;
  duplicateTrailIdRows: number;
  complete: boolean;
} {
  const rows = values.flatMap(primaryList);
  const expected = observedTotal(values, countPaths);
  const trailIds = rows.flatMap(row => recursiveValues(row, /^trail_?id$/i).slice(0, 1));
  const uniqueTrailIds = new Set(trailIds).size;
  return {
    expected,
    rowCount: rows.length,
    rowsWithTrailId: trailIds.length,
    rowsWithoutTrailId: rows.length - trailIds.length,
    uniqueTrailIds,
    duplicateTrailIdRows: trailIds.length - uniqueTrailIds,
    complete: expected !== null
      && rows.length === expected
      && trailIds.length === rows.length
      && uniqueTrailIds === expected
  };
}

export type MailApiResponseKind = "detail" | "track";

export function mailDetailAttemptsComplete(
  attempted: number,
  available: number,
  sourceDeleted: number,
  unavailable: number
): boolean {
  return attempted >= 0
    && available >= 0
    && sourceDeleted >= 0
    && unavailable >= 0
    && available + sourceDeleted === attempted
    && unavailable === 0;
}

export function validateMailApiResponse(
  status: number,
  error: string | null,
  json: unknown,
  requestedMailId: string,
  kind: MailApiResponseKind
): { valid: boolean; reason: string } {
  if (status < 200 || status >= 300) return { valid: false, reason: `http_${status}` };
  if (error) return { valid: false, reason: "transport_error" };
  if (!json || typeof json !== "object" || Array.isArray(json)) return { valid: false, reason: "invalid_json_object" };
  const object = json as Record<string, unknown>;
  const applicationCode = Number(object.code);
  if (kind === "detail" && applicationCode === 1219) {
    return { valid: true, reason: "source_deleted" };
  }
  if (!Number.isFinite(applicationCode) || applicationCode !== 0) {
    return { valid: false, reason: "application_code_not_zero" };
  }
  if (!("data" in object) || object.data === null || object.data === undefined) {
    return { valid: false, reason: "missing_data" };
  }
  if (typeof object.data !== "object") return { valid: false, reason: "invalid_data_shape" };
  const responseMailIds = unique(recursiveValues(object.data, /^mail_?id$/i));
  if (kind === "detail" && !responseMailIds.includes(requestedMailId)) {
    return { valid: false, reason: responseMailIds.length ? "mail_id_mismatch" : "mail_id_missing" };
  }
  if (kind === "track" && responseMailIds.length > 0 && !responseMailIds.includes(requestedMailId)) {
    return { valid: false, reason: "mail_id_mismatch" };
  }
  return { valid: true, reason: "ok" };
}

export function documentPaginationSummary(values: unknown[], countPaths: string[]): {
  expected: number | null;
  rowCount: number;
  rowsWithFileId: number;
  rowsWithoutFileId: number;
  uniqueFileIds: number;
  duplicateFileIdRows: number;
  complete: boolean;
} {
  const rows = values.flatMap(primaryList);
  const expected = observedTotal(values, countPaths);
  const fileIds = rows.flatMap(row => {
    if (!row || typeof row !== "object") return [];
    const value = (row as Record<string, unknown>).file_id;
    return value === undefined || value === null || value === "" ? [] : [String(value)];
  });
  const uniqueFileIds = new Set(fileIds).size;
  return {
    expected,
    rowCount: rows.length,
    rowsWithFileId: fileIds.length,
    rowsWithoutFileId: rows.length - fileIds.length,
    uniqueFileIds,
    duplicateFileIdRows: fileIds.length - uniqueFileIds,
    complete: expected !== null
      && fileIds.length === rows.length
      && (rows.length === expected || uniqueFileIds === expected)
  };
}

export function unionObjectCoverageComplete(
  expected: number | null,
  unionUniqueIds: number,
  unionRows: number,
  unionRowsWithoutId: number
): boolean {
  return expected !== null && unionRows > 0 && unionRowsWithoutId === 0 && unionUniqueIds === expected;
}

export function stableVisibleGapCoverageComplete(
  expected: number | null,
  visibleRows: number,
  rowsWithoutId: number,
  terminalEmptyPage: boolean,
  probeStable: boolean
): boolean {
  return expected !== null && visibleRows > 0 && visibleRows < expected
    && rowsWithoutId === 0 && terminalEmptyPage && probeStable;
}

function unique<T>(values: T[]): T[] {
  return [...new Set(values)];
}

async function settle(page: Page, milliseconds = 1400): Promise<void> {
  await page.waitForLoadState("domcontentloaded", { timeout: 15_000 }).catch(() => undefined);
  await page.waitForTimeout(Math.max(350, Math.round(milliseconds * SETTLE_SCALE)));
}

async function withTimeout<T>(promise: Promise<T>, milliseconds: number, label: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timeout after ${milliseconds}ms`)), milliseconds);
      })
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export class CaptureEngine {
  private state: JobStatus = freshStatus();
  private cancelled = false;
  private responseRecords: StoredResponse[] = [];
  private pendingBodies = new Set<Promise<void>>();
  private syntheticFetchUrlsInFlight = new Set<string>();
  private readOnlyPostReplayInputs = new Map<string, ReadOnlyPostReplayInput>();
  private failedRequestBodyCaptures = new Map<number, FailedRequestBodyCapture>();
  private mailCaptureDrafts: MailCaptureDraft[] = [];
  private resumeExisting = false;
  private revisitUiGaps = false;
  private revisitBoundCompanyId = "";
  private requestSequences = new WeakMap<Request, number>();
  private listeners: { request: (request: Request) => void; response: (response: Response) => void } | null = null;

  constructor(
    private readonly workspaceRoot: string,
    private readonly adapter: OkkiAdapter,
    private readonly browserManager: BrowserManager
  ) {}

  status(): JobStatus {
    return structuredClone(this.state);
  }

  cancel(): void {
    this.cancelled = true;
    if (this.state.running) this.update("cancelled", this.state.progress, "正在停止", "完成当前原子写入后停止");
  }

  failUnhandled(error: unknown): void {
    if (!this.state.running) return;
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    this.state.errors.push(message);
    this.update("failed", this.state.progress, "采集失败", message);
    this.state.running = false;
    this.state.finishedAt = nowIso();
  }

  private starting = false;

  async start(options: CaptureStartOptions = {}): Promise<JobStatus> {
    if (this.starting || this.state.running) throw new Error("已有采集任务正在运行或启动");
    this.starting = true;
    try { return await this.startOwned(options); }
    catch (error) { this.failUnhandled(error); throw error; }
    finally { this.starting = false; }
  }

  private async startOwned(options: CaptureStartOptions): Promise<JobStatus> {
    if (this.state.running) throw new Error("已有采集任务正在运行");
    const page = await this.browserManager.customerPage();
    if (!page) throw new Error("专用浏览器中没有打开 OKKI 客户详情页");
    const parsed = parseCustomerUrl(page.url(), this.adapter);
    if (!parsed) throw new Error("当前页不是合法的单客户详情 URL");
    if (options.expectedCompanyId && parsed.companyId !== options.expectedCompanyId) {
      throw new Error("当前客户身份与启动绑定不一致");
    }

    this.state = freshStatus();
    this.cancelled = false;
    this.responseRecords = [];
    this.pendingBodies.clear();
    this.syntheticFetchUrlsInFlight.clear();
    this.readOnlyPostReplayInputs.clear();
    this.failedRequestBodyCaptures.clear();
    this.mailCaptureDrafts = [];
    this.resumeExisting = options.resumeExisting === true;
    this.revisitUiGaps = options.revisitUiGaps === true;
    this.revisitBoundCompanyId = options.expectedCompanyId ?? "";
    if (this.revisitUiGaps) assertUiGapRevisitIdentity(parsed.companyId, this.revisitBoundCompanyId);
    this.state = {
      ...this.state,
      id: crypto.randomUUID(), running: true, phase: "preflight", progress: 1,
      headline: "预检与隐私门", detail: "在重载前安装范围限制和外部 AI 阻断",
      companyId: parsed.companyId, startedAt: nowIso()
    };
    this.state.metrics.resume_existing = this.resumeExisting;
    this.state.metrics.revisit_ui_gaps = this.revisitUiGaps;

    const customerName = await this.fetchCustomerName(page, parsed.companyId);
    const caseFolderName = await resolveCaseFolderName(this.workspaceRoot, parsed.companyId, customerName);
    const store = new EvidenceStore(this.workspaceRoot, parsed.companyId, page.url(), customerName, caseFolderName);
    this.state.caseRoot = store.caseRoot;
    this.state.sessionRoot = store.sessionRoot;
    await store.initialize(this.adapter.adapter_id);
    if (this.revisitUiGaps) {
      await store.writeJson(path.join("work", "ui_gap_revisit_state_latest.json"), this.state);
    } else {
      await store.writeState(this.state);
    }

    let cdp: CDPSession | null = null;
    try {
      cdp = await page.context().newCDPSession(page);
      await this.installGuards(page, cdp, parsed.companyId, store);
      this.installCdpTransportCapture(cdp, store);
      this.installNetworkCapture(page, store);

      if (this.revisitUiGaps) {
        await this.captureUiGapRevisit(page, store, cdp, parsed.companyId);
      } else {
      this.assertActive();
      this.update("root", 5, "采集客户根页面", "隐私门已在重载前生效；正在保存完整网络响应、DOM、文字与截图");
      await store.writeState(this.state);
      // A one-customer run may be started while the operator is manually
      // viewing seaData. Move to the first automatic tab before any reload or
      // snapshot so the automatic lane never persists the manual trade page.
      const automaticRootUrl = new URL(this.adapter.customer_path, this.adapter.origin);
      automaticRootUrl.searchParams.set("company_id", parsed.companyId);
      automaticRootUrl.searchParams.set("tab", this.adapter.root_tabs[0]!.id);
      await page.goto(automaticRootUrl.href, { waitUntil: "domcontentloaded", timeout: 45_000 });
      await settle(page, 2200);
      await this.snapshot(page, store, "root_reload", cdp);

      this.assertActive();
      this.update("tabs", 12, "遍历客户详情全部标签", "仅点击当前客户详情页内部标签，不进入其他客户");
      await this.captureRootTabs(page, store, cdp);

      this.assertActive();
      await this.captureRequiredCoreGets(page, store, parsed.companyId);

      this.assertActive();
      await this.captureObservedGetPagination(page, store);

      this.assertActive();
      await this.captureObservedReadOnlyPostPagination(page, store);

      this.assertActive();
      this.update("mail", 43, "核对动态与邮件", "解析全部已观察邮件对象，逐 mail_id 读取详情和追踪");
      await store.writeState(this.state);
      await this.captureMailDetails(page, store);

      this.assertActive();
      this.update("documents", 65, "下载文档、图片与附件", "对所有已观察资源 URL 分类下载并计算 SHA-256");
      await store.writeState(this.state);
      await this.captureDiscoveredResources(cdp, store);

      await this.awaitBodies();
      await this.recoverFailedGetBodies(page, cdp, store);
      await this.recoverFailedReadOnlyPostBodies(page, store);
      await this.recoverFailedRequestBodies(store);
      await this.awaitBodies();
      this.assertActive();
      this.update("reconcile", 90, "对象级对账", "检查接口契约、响应体、分页、哈希、隐私与失败分类");
      const checks = await this.reconcile(store);
      Object.assign(this.state.metrics, store.dedupStats());
      const failed = checks.filter(check => check.status === "fail" || check.status === "not_observed");
      const warnings = checks.filter(check => check.status === "warning");
      this.state.warnings.push(...warnings.map(item => `${item.title}: ${item.detail}`));
      await store.writeJson(path.join("verification", "capture_reconciliation_latest.json"), {
        schema: 2,
        generated_at: nowIso(),
        session_id: store.sessionId,
        company_id: parsed.companyId,
        status: failed.length ? "incomplete" : "pass",
        checks
      });

      if (failed.length) {
        this.state.errors.push(...failed.map(item => `${item.title}: ${item.detail}`));
        this.update("incomplete", 100, "采集结束，但未通过完整性门", `${failed.length} 项未对账；原始证据已安全落盘，可修复适配器后续跑`);
      } else {
        this.update("complete", 100, "完整采集与对账通过", "全部已发现对象均有证据、哈希和状态；没有静默遗漏");
        const mailCaptureManifest = this.buildMailCaptureManifest(store, parsed.companyId);
        const mailCaptureManifestBytes = Buffer.from(JSON.stringify(mailCaptureManifest, null, 2));
        const mailCaptureManifestPath = path.join(
          "manifests", `mail_capture_manifest__${store.sessionId}.json`
        );
        await store.writeJson(mailCaptureManifestPath, mailCaptureManifest);
        await store.writeJson(path.join("manifests", "processing_scope_latest.json"), {
          schema: 1,
          generated_at: nowIso(),
          company_id: parsed.companyId,
          capture_status: "pass",
          default_session_id: store.sessionId,
          object_manifest: path.join("manifests", "objects.jsonl"),
          default_object_filter: { field: "session_id", equals: store.sessionId },
          mail_capture_manifest: {
            path: mailCaptureManifestPath,
            bytes: mailCaptureManifestBytes.byteLength,
            sha256: sha256(mailCaptureManifestBytes)
          },
          request_ledger: "request_ledger.jsonl",
          default_request_filter: { field: "session_id", equals: store.sessionId },
          request_body_roles: ["contract_business", "same_origin_other", "external_service"],
          raw_history_policy: "preserve_all_sessions",
          downstream_policy: "默认处理最新通过完整性门的会话；历史、取消和不完整会话仅用于审计或显式比较"
        });
        // Latest is a convenience copy. The processing scope above binds the
        // immutable session artifact, so a crash during this final pointer
        // update cannot invalidate the previously published PASS scope.
        await store.writeJson(path.join("manifests", "mail_capture_manifest_latest.json"), mailCaptureManifest);
      }
      }
    } catch (error) {
      const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      if (this.cancelled) {
        this.update("cancelled", this.state.progress, "采集已停止", "已落盘数据保持有效，可重新开始新会话");
      } else {
        this.state.errors.push(message);
        this.update("failed", this.state.progress, "采集失败", message);
      }
      await store.appendError({ at: nowIso(), session_id: store.sessionId, phase: this.state.phase, error: message });
    } finally {
      await this.awaitBodies();
      this.removeNetworkCapture(page);
      await page.unroute("**/*").catch(() => undefined);
      await cdp?.detach().catch(() => undefined);
      try {
        if (this.revisitUiGaps) {
          const boundUrl = new URL(this.adapter.customer_path, this.adapter.origin);
          boundUrl.searchParams.set("company_id", this.revisitBoundCompanyId);
          boundUrl.searchParams.set("tab", "dynamic");
          await page.goto(boundUrl.href, { waitUntil: "domcontentloaded", timeout: 45_000 });
          this.state.metrics.returned_to_bound_customer = true;
          this.state.metrics.returned_to_customer_list = false;
        } else {
          await page.goto(this.adapter.customer_list_url, { waitUntil: "domcontentloaded", timeout: 45_000 });
          this.state.metrics.returned_to_customer_list = true;
        }
      } catch (error) {
        if (this.revisitUiGaps) this.state.metrics.returned_to_bound_customer = false;
        else this.state.metrics.returned_to_customer_list = false;
        const message = error instanceof Error ? error.message : String(error);
        this.state.warnings.push(`${this.revisitUiGaps ? "返回绑定客户页" : "返回客户列表页"}失败: ${message}`);
      }
      this.state.running = false;
      this.state.finishedAt = nowIso();
      Object.assign(this.state.metrics, store.dedupStats());
      if (this.revisitUiGaps) {
        await store.writeJson(path.join("work", "ui_gap_revisit_state_latest.json"), this.state);
      } else {
        await store.writeState(this.state);
        await store.writeDedupSummary();
      }
      await store.writeJson(path.join("raw", "sessions", store.sessionId, "final_status.json"), this.state);
    }
    return this.status();
  }

  private update(phase: JobStatus["phase"], progress: number, headline: string, detail: string): void {
    this.state.phase = phase;
    this.state.progress = progress;
    this.state.headline = headline;
    this.state.detail = detail;
  }

  private async fetchCustomerName(page: Page, companyId: string): Promise<string> {
    const detailPath = this.adapter.expected_endpoint_contracts.find(contract => contract.id === "customer_detail")?.path
      ?? "/api/customerV2Read/detail";
    const detailUrl = new URL(detailPath, this.adapter.origin);
    detailUrl.searchParams.set("company_id", companyId);
    detailUrl.searchParams.set("directOwner", "1");
    detailUrl.searchParams.set("scene", "detail");
    const payload = await page.evaluate(async rawUrl => {
      const response = await fetch(rawUrl, { method: "GET", credentials: "include", cache: "no-store" });
      if (!response.ok) throw new Error(`customer detail HTTP ${response.status}`);
      return response.json() as Promise<unknown>;
    }, detailUrl.href);
    const name = customerNameFromDetail(payload);
    if (!name) throw new Error("客户详情接口没有返回有效客户名，已停止以避免创建错误案例目录");
    return name;
  }

  private assertActive(): void {
    if (this.cancelled) throw new Error("capture_cancelled");
  }

  private async installGuards(page: Page, cdp: CDPSession, companyId: string, store: EvidenceStore): Promise<void> {
    await cdp.send("Network.enable", {
      maxTotalBufferSize: 512 * 1024 * 1024,
      maxResourceBufferSize: 128 * 1024 * 1024,
      maxPostDataSize: 64 * 1024 * 1024
    });
    await cdp.send("Network.setBypassServiceWorker", { bypass: true });
    await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
    const blockedPatterns = this.adapter.external_ai_blocked_hosts.flatMap(host => [`*://${host}/*`, `*://*.${host}/*`]);
    await cdp.send("Network.setBlockedURLs", { urls: blockedPatterns });

    await page.route("**/*", async route => {
      const request = route.request();
      const rawUrl = request.url();
      if (isBlockedAiUrl(rawUrl, this.adapter)) {
        await store.appendScope({ at: nowIso(), action: "blocked_external_ai", url: redactUrl(rawUrl), resource_type: request.resourceType() });
        await route.abort("blockedbyclient");
        return;
      }
      if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
        let allowed = false;
        try {
          const url = new URL(rawUrl);
          if (url.href === "about:blank") allowed = true;
          if (url.origin === this.adapter.origin
            && url.pathname === this.adapter.customer_path
            && url.searchParams.get("company_id") === companyId
            && (!this.revisitUiGaps || !isAutoCaptureExcludedUrl(url.href))) allowed = true;
        } catch { allowed = false; }
        await store.appendScope({ at: nowIso(), action: allowed ? "navigation_allowed" : "navigation_blocked", url: redactUrl(rawUrl) });
        if (!allowed) {
          await route.abort("blockedbyclient");
          return;
        }
      }
      await route.continue();
    });
  }

  private installCdpTransportCapture(cdp: CDPSession, store: EvidenceStore): void {
    const enqueue = (row: Record<string, unknown>): void => {
      const task = store.appendEvent({ schema: 1, at: nowIso(), session_id: store.sessionId, ...row })
        .catch((error: unknown) => store.appendError({ at: nowIso(), action: "cdp_transport_event", error: String(error) }))
        .finally(() => this.pendingBodies.delete(task));
      this.pendingBodies.add(task);
    };
    cdp.on("Network.webSocketCreated", event => enqueue({ transport: "websocket", event: "created", request_id: event.requestId, url: redactUrl(event.url) }));
    cdp.on("Network.webSocketClosed", event => enqueue({ transport: "websocket", event: "closed", request_id: event.requestId, timestamp: event.timestamp }));
    cdp.on("Network.webSocketFrameReceived", event => enqueue({
      transport: "websocket", event: "frame_received", request_id: event.requestId, timestamp: event.timestamp,
      opcode: event.response.opcode, mask: event.response.mask, payload_data: event.response.payloadData
    }));
    cdp.on("Network.webSocketFrameSent", event => enqueue({
      transport: "websocket", event: "frame_sent", request_id: event.requestId, timestamp: event.timestamp,
      opcode: event.response.opcode, mask: event.response.mask, payload_data: event.response.payloadData
    }));
    cdp.on("Network.webSocketFrameError", event => enqueue({ transport: "websocket", event: "frame_error", request_id: event.requestId, timestamp: event.timestamp, error: event.errorMessage }));
    cdp.on("Network.eventSourceMessageReceived", event => enqueue({
      transport: "eventsource", event: "message_received", request_id: event.requestId, timestamp: event.timestamp,
      event_name: event.eventName, event_id: event.eventId, data: event.data
    }));
  }

  private installNetworkCapture(page: Page, store: EvidenceStore): void {
    const onRequest = (request: Request): void => {
      if (isAutoCaptureExcludedUrl(request.url())) {
        const task = store.appendScope({
          at: nowIso(), action: "auto_capture_excluded_manual_trade_lane",
          method: request.method(), resource_type: request.resourceType()
        }).catch((error: unknown) => store.appendError({ at: nowIso(), action: "anonymous_exclusion_event", error: String(error) }))
          .finally(() => this.pendingBodies.delete(task));
        this.pendingBodies.add(task);
        return;
      }
      const sequence = store.nextSequence();
      this.requestSequences.set(request, sequence);
      const postData = request.postDataBuffer();
      const task = (async () => {
        const role = requestBodyRole(request.method(), request.url(), this.adapter);
        if (postData?.byteLength) {
          this.state.metrics.request_bodies_observed = Number(this.state.metrics.request_bodies_observed ?? 0) + 1;
          this.state.metrics.request_body_bytes_observed = Number(this.state.metrics.request_body_bytes_observed ?? 0) + postData.byteLength;
          const roleMetric = `request_bodies_${role}`;
          this.state.metrics[roleMetric] = Number(this.state.metrics[roleMetric] ?? 0) + 1;
        }
        let requestBodyRelativePath: string | null = null;
        let requestBodyError: unknown = null;
        if (postData?.byteLength) {
          try {
            requestBodyRelativePath = await retryTransient(
              () => store.storeRequestBody(sequence, request.method(), request.url(), postData),
              3
            );
          } catch (error) {
            requestBodyError = error;
            this.failedRequestBodyCaptures.set(sequence, {
              sequence, method: request.method(), url: request.url(),
              body: Buffer.from(postData), role
            });
          }
        }
        if (requestBodyRelativePath && postData?.byteLength) {
          this.state.metrics.request_bodies_persisted = Number(this.state.metrics.request_bodies_persisted ?? 0) + 1;
          this.state.metrics.request_body_bytes_persisted = Number(this.state.metrics.request_body_bytes_persisted ?? 0) + postData.byteLength;
        }
        const headers = await withTimeout(request.allHeaders(), 10_000, "request.allHeaders")
          .catch(() => ({} as Record<string, string>));
        if (postData?.byteLength) {
          const requestBodySha256 = sha256(postData);
          const replayKey = exactReadOnlyPostKey(request.method(), request.url(), requestBodySha256);
          if (replayKey && new URL(request.url()).origin === this.adapter.origin) {
            this.readOnlyPostReplayInputs.set(replayKey, {
              url: request.url(), body: Buffer.from(postData),
              headers: replayablePostHeaders(headers), requestBodySha256
            });
          }
        }
        await store.appendRequest({
          schema: 2,
          at: nowIso(), session_id: store.sessionId, sequence,
          method: request.method(), url: redactUrl(request.url()),
          resource_type: request.resourceType(), navigation: request.isNavigationRequest(),
          headers: sanitizeHeaders(headers),
          post_data_size: postData?.byteLength ?? 0,
          post_data_sha256: postData ? sha256(postData) : null,
          post_data_relative_path: requestBodyRelativePath,
          post_data_storage: postData?.byteLength
            ? requestBodyRelativePath ? "body_saved" : "body_save_failed"
            : "none",
          post_data_role: postData?.byteLength ? role : "none",
          initiator: "page_native"
        });
        if (requestBodyError) throw requestBodyError;
      })().catch(error => store.appendError({ at: nowIso(), sequence, error: String(error) }))
        .finally(() => this.pendingBodies.delete(task));
      this.pendingBodies.add(task);
    };
    const onResponse = (response: Response): void => {
      if (isAutoCaptureExcludedUrl(response.url()) || this.syntheticFetchUrlsInFlight.has(response.url())) return;
      const task = this.captureResponse(response, store).finally(() => this.pendingBodies.delete(task));
      this.pendingBodies.add(task);
    };
    page.on("request", onRequest);
    page.on("response", onResponse);
    this.listeners = { request: onRequest, response: onResponse };
  }

  private removeNetworkCapture(page: Page): void {
    if (!this.listeners) return;
    page.off("request", this.listeners.request);
    page.off("response", this.listeners.response);
    this.listeners = null;
  }

  private async captureResponse(response: Response, store: EvidenceStore): Promise<void> {
    const request = response.request();
    if (isAutoCaptureExcludedUrl(response.url())) return;
    const sequence = this.requestSequences.get(request) ?? store.nextSequence();
    const [headers, serverAddress, securityDetails] = await Promise.all([
      withTimeout(response.allHeaders(), 10_000, "response.allHeaders").catch(() => ({} as Record<string, string>)),
      withTimeout(response.serverAddr(), 10_000, "response.serverAddr").catch(() => null),
      withTimeout(response.securityDetails(), 10_000, "response.securityDetails").catch(() => null)
    ]);
    const mimeType = headers["content-type"] ?? "application/octet-stream";
    let body: Buffer | null = null;
    let error: string | null = null;
    const passivePageResource = ["document", "stylesheet", "script", "image", "media", "font", "manifest"].includes(request.resourceType());
    if (isAllowedCaptureUrl(response.url(), this.adapter) || (passivePageResource && !isBlockedAiUrl(response.url(), this.adapter))) {
      try { body = await withTimeout(response.body(), responseBodyTimeoutMs(request.resourceType()), "response.body"); }
      catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    }
    const record = await store.storeResponse({
      sequence, method: request.method(), url: response.url(), status: response.status(),
      resourceType: request.resourceType(), mimeType, body, error,
      requestBodySha256: request.postDataBuffer() ? sha256(request.postDataBuffer()!) : null,
      responseHeaders: sanitizeHeaders(headers),
      statusText: response.statusText(),
      serverAddress,
      securityDetails: securityDetails ? { ...securityDetails } : null,
      timing: { ...request.timing() },
      fromServiceWorker: response.fromServiceWorker()
    });
    this.responseRecords.push(record);
    if (error) await store.appendError({ at: nowIso(), sequence, url: redactUrl(response.url()), error });
  }

  private async awaitBodies(): Promise<void> {
    while (this.pendingBodies.size) await Promise.allSettled([...this.pendingBodies]);
  }

  private async snapshot(page: Page, store: EvidenceStore, label: string, cdp: CDPSession): Promise<void> {
    if (isAutoCaptureExcludedUrl(page.url())) {
      await store.appendScope({ at: nowIso(), action: "auto_snapshot_excluded_manual_trade_lane", label });
      return;
    }
    const html = await page.content();
    const text = await page.locator("body").innerText({ timeout: 15_000 }).catch(() => "");
    const screenshot = await page.screenshot({ fullPage: true, animations: "disabled", caret: "hide" });
    let mhtml: string | null = null;
    try {
      mhtml = (await cdp.send("Page.captureSnapshot", { format: "mhtml" })).data;
    } catch (error) {
      await store.appendError({ at: nowIso(), action: "mhtml_snapshot", label, error: String(error) });
    }
    await store.storePage(label, page.url(), html, text, screenshot, mhtml);
  }

  private async findVisibleByLabels(root: Page | Locator, labels: string[], roles: Array<"tab" | "button" | "link">): Promise<Locator | null> {
    for (const label of labels) {
      const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const countedLabel = new RegExp(`^${escaped}(?:\\s*(?:\\(\\d[\\d,]*\\)|\\d[\\d,]*))?$`, "i");
      for (const role of roles) {
        const locator = root.getByRole(role, { name: countedLabel }).filter({ visible: true }).first();
        if (await locator.count().catch(() => 0)) return locator;
      }
      const text = root.getByText(countedLabel).filter({ visible: true }).first();
      if (await text.count().catch(() => 0)) return text;
    }
    return null;
  }

  private dynamicFilterLabelGroups(): string[][] {
    return [...this.adapter.dynamic.filter_labels, this.adapter.dynamic.mail_labels]
      .map(group => normalizeUiFilterLabel(group[0] ?? "") === "其他动态"
        ? unique([...group, ...UI_GAP_OTHER_DYNAMIC_LABELS].map(normalizeUiFilterLabel).filter(Boolean))
        : unique(group.map(normalizeUiFilterLabel).filter(Boolean)));
  }

  private async bestDynamicWrapper(page: Page): Promise<{
    wrapper: Locator;
    labels: string[];
    coverage: number;
  } | null> {
    const wrappers = page.locator(UI_GAP_DYNAMIC_WRAPPER_SELECTOR);
    const wrapperCount = await wrappers.count().catch(() => 0);
    const candidates: Array<{ wrapper: Locator; labels: string[] }> = [];
    for (let index = 0; index < wrapperCount; index += 1) {
      const wrapper = wrappers.nth(index);
      if (!await wrapper.isVisible().catch(() => false)) continue;
      const buttons = wrapper.locator(UI_GAP_DYNAMIC_FILTER_SELECTOR);
      const buttonCount = await buttons.count().catch(() => 0);
      const labels: string[] = [];
      for (let buttonIndex = 0; buttonIndex < buttonCount; buttonIndex += 1) {
        const button = buttons.nth(buttonIndex);
        if (!await button.isVisible().catch(() => false)) continue;
        labels.push(await button.innerText().catch(() => ""));
      }
      if (labels.length) candidates.push({ wrapper, labels });
    }
    if (!candidates.length) return null;
    const expected = this.dynamicFilterLabelGroups();
    const bestIndex = uiGapBestDynamicFilterCandidateIndex(expected, candidates.map(item => item.labels));
    if (bestIndex < 0) return null;
    const selected = candidates[bestIndex];
    if (!selected) return null;
    return {
      wrapper: selected.wrapper,
      labels: selected.labels,
      coverage: uiGapDynamicFilterCoverage(expected, selected.labels)
    };
  }

  private async waitForDynamicUi(page: Page, requireCompleteFilterBar = false): Promise<Locator> {
    const requiredCoverage = this.dynamicFilterLabelGroups().length;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const deadline = Date.now() + 15_000;
      let previousSignature = "";
      let stableSamples = 0;
      while (Date.now() < deadline) {
        const candidate = await this.bestDynamicWrapper(page);
        if (candidate) {
          const signature = candidate.labels.map(normalizeUiFilterLabel).join("\u001f");
          stableSamples = signature === previousSignature ? stableSamples + 1 : 1;
          previousSignature = signature;
          const coverageReady = !requireCompleteFilterBar || candidate.coverage >= requiredCoverage;
          if (coverageReady && stableSamples >= 2) return candidate.wrapper;
        } else {
          previousSignature = "";
          stableSamples = 0;
        }
        await page.waitForTimeout(350);
      }
      if (attempt === 0) {
        await page.reload({ waitUntil: "domcontentloaded", timeout: 45_000 });
        await settle(page, 2500);
      }
    }
    throw new Error(requireCompleteFilterBar
      ? "ui_gap_dynamic_filter_bar_not_fully_hydrated"
      : "ui_gap_dynamic_ui_not_ready");
  }

  private async visibleDynamicFilterLabels(wrapper: Locator): Promise<string[]> {
    const buttons = wrapper.locator(UI_GAP_DYNAMIC_FILTER_SELECTOR).filter({ visible: true });
    const labels = await buttons.allInnerTexts().catch(() => []);
    return unique(labels.map(normalizeUiFilterLabel).filter(Boolean));
  }

  private async findDynamicFilterButton(wrapper: Locator, labels: string[]): Promise<Locator | null> {
    const buttons = wrapper.locator(UI_GAP_DYNAMIC_FILTER_SELECTOR).filter({ visible: true });
    const count = await buttons.count().catch(() => 0);
    const texts: string[] = [];
    for (let index = 0; index < count; index += 1) {
      texts.push(await buttons.nth(index).innerText().catch(() => ""));
    }
    const match = uiGapMatchingFilterIndex(labels, texts);
    return match >= 0 ? buttons.nth(match) : null;
  }

  private assertBoundAutomaticTab(page: Page, companyId: string, expectedTab: "dynamic" | "relational"): void {
    const parsed = parseCustomerUrl(page.url(), this.adapter);
    const tab = new URL(page.url()).searchParams.get("tab");
    if (!parsed || parsed.companyId !== companyId || tab !== expectedTab || isAutoCaptureExcludedUrl(page.url())) {
      throw new Error(`ui_gap_navigation_scope_violation:${expectedTab}`);
    }
  }

  private async navigateBoundAutomaticTab(page: Page, companyId: string, tab: "dynamic" | "relational"): Promise<void> {
    const target = new URL(this.adapter.customer_path, this.adapter.origin);
    target.searchParams.set("company_id", companyId);
    target.searchParams.set("tab", tab);
    await page.goto(target.href, { waitUntil: "domcontentloaded", timeout: 45_000 });
    await settle(page, 1800);
    this.assertBoundAutomaticTab(page, companyId, tab);
    if (tab === "dynamic") await this.waitForDynamicUi(page);
  }

  private async nextUiPageControl(page: Page, nextLabels: string[], scope?: Locator): Promise<Locator | null> {
    const root: Page | Locator = scope ?? page;
    let next = await this.findVisibleByLabels(root, nextLabels, ["button", "link"]);
    if (next) return next;
    for (const nextLabel of nextLabels) {
      const css = root.locator(`[title="${nextLabel}"], [aria-label="${nextLabel}"]`).filter({ visible: true }).first();
      if (await css.count().catch(() => 0)) return css;
    }
    const fallback = root.locator(
      ".pagination-next, .ant-pagination-next, .el-pagination .btn-next, a[rel=next], button[aria-label*=next i]"
    ).filter({ visible: true }).first();
    if (await fallback.count().catch(() => 0)) return fallback;
    return null;
  }

  private async paginateVisibleTableToTerminal(
    page: Page,
    store: EvidenceStore,
    cdp: CDPSession,
    companyId: string,
    tab: "dynamic" | "relational",
    label: string,
    nextLabels: string[],
    cap: number,
    paginationScope?: Locator
  ): Promise<UiPaginationClosure> {
    let pagesCaptured = 1;
    let previous = sha256(await page.locator("body").innerText().catch(() => ""));
    while (true) {
      this.assertActive();
      const next = await this.nextUiPageControl(page, nextLabels, paginationScope);
      if (!next) return { pagesCaptured, terminalClosed: true, terminalReason: "next_absent" };
      const disabled = await next.isDisabled().catch(() => false)
        || (await next.getAttribute("aria-disabled")) === "true"
        || ((await next.getAttribute("class")) ?? "").toLowerCase().includes("disabled");
      if (disabled) return { pagesCaptured, terminalClosed: true, terminalReason: "next_disabled" };
      if (pagesCaptured >= cap) {
        return { pagesCaptured, terminalClosed: false, terminalReason: "safety_cap_reached" };
      }
      await next.click({ timeout: 15_000 });
      await settle(page, 900);
      this.assertBoundAutomaticTab(page, companyId, tab);
      const current = sha256(await page.locator("body").innerText().catch(() => ""));
      if (current === previous) {
        return { pagesCaptured, terminalClosed: false, terminalReason: "content_unchanged" };
      }
      previous = current;
      pagesCaptured += 1;
      await this.snapshot(page, store, `${label}_page_${String(pagesCaptured).padStart(3, "0")}`, cdp);
    }
  }

  private async captureUiGapFilter(
    page: Page,
    store: EvidenceStore,
    cdp: CDPSession,
    companyId: string,
    labels: string[],
    evidenceLabel: string,
    source: UiFilterCaptureResult["source"],
    controlKind: "history" | "filter_bar"
  ): Promise<UiFilterCaptureResult> {
    const label = labels[0] ?? evidenceLabel;
    const parsed = parseCustomerUrl(page.url(), this.adapter);
    const currentTab = new URL(page.url()).searchParams.get("tab");
    if (!parsed || parsed.companyId !== companyId || currentTab !== "dynamic") {
      await this.navigateBoundAutomaticTab(page, companyId, "dynamic");
    } else {
      this.assertBoundAutomaticTab(page, companyId, "dynamic");
    }
    let wrapper: Locator;
    try {
      // History is the read-only hydration trigger on this page. Once it has been
      // captured, keep the live dynamic page and require the complete filter bar.
      wrapper = await this.waitForDynamicUi(page, controlKind === "filter_bar");
    } catch (error) {
      await store.appendError({
        at: nowIso(), session_id: store.sessionId, action: "ui_gap_filter_bar_ready",
        label, error: error instanceof Error ? error.message : String(error)
      });
      return {
        label, source, found: false, pagesCaptured: 0,
        terminalClosed: false, terminalReason: "filter_not_found"
      };
    }
    const control = controlKind === "filter_bar"
      ? await this.findDynamicFilterButton(wrapper, labels)
      : await this.findVisibleByLabels(wrapper, labels, ["tab", "button", "link"]);
    if (!control) {
      return {
        label, source, found: false, pagesCaptured: 0,
        terminalClosed: false, terminalReason: "filter_not_found"
      };
    }
    try {
      await control.click({ timeout: 15_000 });
      await settle(page, 900);
      this.assertBoundAutomaticTab(page, companyId, "dynamic");
      await this.snapshot(page, store, `${evidenceLabel}_page_001`, cdp);
      if (controlKind === "history") {
        return {
          label, source, found: true, pagesCaptured: 1,
          terminalClosed: true, terminalReason: "next_absent"
        };
      }
      const closure = await this.paginateVisibleTableToTerminal(
        page, store, cdp, companyId, "dynamic", evidenceLabel,
        this.adapter.dynamic.next_labels, this.adapter.dynamic.safety_page_cap,
        wrapper
      );
      return { label, source, found: true, ...closure };
    } catch (error) {
      await store.appendError({
        at: nowIso(), session_id: store.sessionId, action: "ui_gap_filter_capture",
        label, error: error instanceof Error ? error.message : String(error)
      });
      return {
        label, source, found: true, pagesCaptured: 0,
        terminalClosed: false, terminalReason: "capture_error"
      };
    }
  }

  private async captureUiGapRevisit(page: Page, store: EvidenceStore, cdp: CDPSession, companyId: string): Promise<void> {
    assertUiGapRevisitIdentity(companyId, this.revisitBoundCompanyId);
    this.assertActive();
    this.update("tabs", 10, "补采 UI 标签与末页", "仅访问绑定客户的动态和文档标签；贸易数据保持手工通道");
    await store.writeJson(path.join("work", "ui_gap_revisit_state_latest.json"), this.state);

    await this.navigateBoundAutomaticTab(page, companyId, "dynamic");
    await this.snapshot(page, store, "ui_gap_dynamic_root", cdp);
    const filters: UiFilterCaptureResult[] = [];

    const history = await this.captureUiGapFilter(
      page, store, cdp, companyId, this.adapter.dynamic.history_labels,
      "ui_gap_dynamic_history", "configured", "history"
    );
    filters.push(history);

    // The first navigation can expose a transient one-button bar (for example
    // "全部(0)"). Clicking history hydrates the real five-button bar; enumerate
    // visible filters only after that transition and never navigate away between
    // filter captures.
    const dynamicWrapper = await this.waitForDynamicUi(page, true);
    const visibleFilterLabels = await this.visibleDynamicFilterLabels(dynamicWrapper);
    const additionalLabels = additionalVisibleUiGapFilters(this.adapter, visibleFilterLabels);

    for (const labels of this.adapter.dynamic.filter_labels) {
      const extendedLabels = normalizeUiFilterLabel(labels[0] ?? "") === "其他动态"
        ? unique([...labels, ...UI_GAP_OTHER_DYNAMIC_LABELS])
        : labels;
      const evidenceLabel = `ui_gap_dynamic_${normalizeUiFilterLabel(extendedLabels[0] ?? "filter")}`;
      filters.push(await this.captureUiGapFilter(
        page, store, cdp, companyId, extendedLabels, evidenceLabel,
        "configured", "filter_bar"
      ));
    }
    filters.push(await this.captureUiGapFilter(
      page, store, cdp, companyId, this.adapter.dynamic.mail_labels,
      "ui_gap_dynamic_mail", "configured", "filter_bar"
    ));
    for (const label of additionalLabels) {
      filters.push(await this.captureUiGapFilter(
        page, store, cdp, companyId, [label],
        `ui_gap_dynamic_visible_${label}`, "visible_tab", "filter_bar"
      ));
    }

    this.assertActive();
    this.update("documents", 75, "补采文档 UI 末页", "只读点击当前客户文档分页直到末页或安全上限");
    await store.writeJson(path.join("work", "ui_gap_revisit_state_latest.json"), this.state);
    await this.navigateBoundAutomaticTab(page, companyId, "relational");
    await this.snapshot(page, store, "ui_gap_documents_page_001", cdp);
    const documents = await this.paginateVisibleTableToTerminal(
      page, store, cdp, companyId, "relational", "ui_gap_documents",
      this.adapter.documents.next_labels, this.adapter.documents.safety_page_cap
    );

    const otherDynamic = filters.find(item => normalizeUiFilterLabel(item.label) === "\u5176\u4ed6\u52a8\u6001");
    const foundFilters = filters.filter(item => item.found);
    const failedTerminalFilters = foundFilters.filter(item => !item.terminalClosed);
    const pass = otherDynamic?.found === true
      && otherDynamic.terminalClosed
      && failedTerminalFilters.length === 0
      && documents.terminalClosed;
    const manifest = {
      schema: "okki.ui_gap_revisit.v1",
      generated_at: nowIso(),
      company_id: companyId,
      session_id: store.sessionId,
      status: pass ? "PASS" : "INCOMPLETE",
      no_auto_next: true,
      automatic_tabs: ["dynamic", "relational"],
      manual_trade_automatic_capture: false,
      processing_scope_updated: false,
      visible_dynamic_filters_observed: visibleFilterLabels,
      additional_visible_filters: additionalLabels,
      filters,
      documents,
      checks: {
        other_dynamic_found: otherDynamic?.found === true,
        other_dynamic_terminal_closed: otherDynamic?.terminalClosed === true,
        all_found_filters_terminal_closed: failedTerminalFilters.length === 0,
        documents_terminal_closed: documents.terminalClosed
      }
    };
    const immutableManifest = path.join("manifests", `ui_gap_revisit__${store.sessionId}.json`);
    await store.writeJson(immutableManifest, manifest);
    await store.writeJson(path.join("manifests", "ui_gap_revisit_latest.json"), manifest);
    this.state.metrics.ui_gap_filters_observed = visibleFilterLabels.length;
    this.state.metrics.ui_gap_filters_found = foundFilters.length;
    this.state.metrics.ui_gap_filters_terminal_closed = foundFilters.length - failedTerminalFilters.length;
    this.state.metrics.ui_gap_other_dynamic_closed = otherDynamic?.terminalClosed === true;
    this.state.metrics.ui_gap_document_pages = documents.pagesCaptured;
    this.state.metrics.ui_gap_documents_terminal_closed = documents.terminalClosed;
    this.state.metrics.ui_gap_processing_scope_updated = false;
    if (pass) {
      this.update("complete", 100, "UI 缺口补采完成", "其他动态、实际可见筛选和文档分页均已闭合；正式处理范围未改写");
    } else {
      this.state.errors.push("ui_gap_revisit_incomplete");
      this.update("incomplete", 100, "UI 缺口仍未闭合", "独立补采清单已保留未闭合筛选或分页原因；正式处理范围未改写");
    }
  }

  private async captureRootTabs(page: Page, store: EvidenceStore, cdp: CDPSession): Promise<void> {
    const current = new URL(page.url());
    const companyId = current.searchParams.get("company_id");
    if (!companyId) throw new Error("当前客户详情页缺少 company_id");
    for (let index = 0; index < this.adapter.root_tabs.length; index += 1) {
      this.assertActive();
      const tab = this.adapter.root_tabs[index]!;
      const tabUrl = new URL(this.adapter.customer_path, this.adapter.origin);
      tabUrl.searchParams.set("company_id", companyId);
      tabUrl.searchParams.set("tab", tab.id);
      await page.goto(tabUrl.href, { waitUntil: "domcontentloaded", timeout: 45_000 });
      await settle(page, tab.id === "statistics" ? 3000 : 1800);
      if (tab.id === "statistics") {
        await this.awaitBodies();
        const analyticsMinimum = this.adapter.expected_endpoint_contracts
          .find(contract => contract.id === "analytics")?.min ?? 1;
        let analyticsResponses = this.responseRecords.filter(item => item.path === "/api/reportRead/detail" && item.bodyRelativePath && !item.error).length;
        if (analyticsResponses < analyticsMinimum) {
          await page.reload({ waitUntil: "domcontentloaded", timeout: 45_000 });
          await settle(page, 7000);
          await this.awaitBodies();
          analyticsResponses = this.responseRecords.filter(item => item.path === "/api/reportRead/detail" && item.bodyRelativePath && !item.error).length;
        }
        this.state.metrics.analytics_responses = analyticsResponses;
      }
      await this.snapshot(page, store, `tab_${tab.id}`, cdp);
      if (tab.id === "dynamic") await this.captureDynamicPages(page, store, cdp);
      if (tab.id === "relational") await this.paginateVisibleTable(page, store, cdp, "documents", this.adapter.documents.next_labels, this.adapter.documents.visible_page_cap);
      this.state.progress = 12 + Math.round(((index + 1) / this.adapter.root_tabs.length) * 29);
      await store.writeState(this.state);
    }
  }

  private async captureDynamicPages(page: Page, store: EvidenceStore, cdp: CDPSession): Promise<void> {
    const history = await this.findVisibleByLabels(page, this.adapter.dynamic.history_labels, ["tab", "button", "link"]);
    if (history) {
      await history.click();
      await settle(page);
      await this.snapshot(page, store, "dynamic_history", cdp);
    }
    for (const labels of this.adapter.dynamic.filter_labels) {
      const filter = await this.findVisibleByLabels(page, labels, ["button", "tab", "link"]);
      if (!filter) {
        // The read-only trailList replay below is authoritative and closes all pages.
        // A hidden/renamed visual filter is therefore not an omission warning.
        continue;
      }
      await filter.click();
      await settle(page, 900);
      const label = labels[0] ?? "filter";
      await this.snapshot(page, store, `dynamic_${label}_page_001`, cdp);
      await this.paginateVisibleTable(page, store, cdp, `dynamic_${label}`, this.adapter.dynamic.next_labels, this.adapter.dynamic.visible_page_cap, 2);
    }
    const mail = await this.findVisibleByLabels(page, this.adapter.dynamic.mail_labels, ["tab", "button", "link"]);
    if (mail) {
      await mail.click();
      await settle(page);
      await this.snapshot(page, store, "dynamic_mail_page_001", cdp);
      await this.paginateVisibleTable(page, store, cdp, "dynamic_mail", this.adapter.dynamic.next_labels, this.adapter.dynamic.visible_page_cap, 2);
    }
  }

  private async captureRequiredCoreGets(page: Page, store: EvidenceStore, companyId: string): Promise<void> {
    const urls = requiredCoreGetUrls(companyId, this.adapter);
    let succeeded = 0;
    for (const url of urls) {
      this.assertActive();
      const result = await this.pageFetchUrl(page, url);
      await this.storeSynthetic(result, store, "core_api_replay");
      if (result.ok) succeeded += 1;
    }
    this.state.metrics.core_gets_attempted = urls.length;
    this.state.metrics.core_gets_succeeded = succeeded;
    this.state.metrics.core_form_fields_replayed = succeeded > 0;
  }

  private async paginateVisibleTable(page: Page, store: EvidenceStore, cdp: CDPSession, label: string, nextLabels: string[], cap: number, startPage = 2): Promise<void> {
    let previous = sha256(await page.locator("body").innerText().catch(() => ""));
    for (let pageNo = startPage; pageNo <= cap; pageNo += 1) {
      this.assertActive();
      let next = await this.findVisibleByLabels(page, nextLabels, ["button", "link"]);
      if (!next) {
        for (const nextLabel of nextLabels) {
          const css = page.locator(`[title="${nextLabel}"], [aria-label="${nextLabel}"]`).filter({ visible: true }).first();
          if (await css.count().catch(() => 0)) { next = css; break; }
        }
      }
      if (!next) break;
      const disabled = await next.isDisabled().catch(() => false)
        || (await next.getAttribute("aria-disabled")) === "true"
        || ((await next.getAttribute("class")) ?? "").toLowerCase().includes("disabled");
      if (disabled) break;
      await next.click({ timeout: 15_000 });
      await settle(page, 900);
      const current = sha256(await page.locator("body").innerText().catch(() => ""));
      if (current === previous) {
        this.state.warnings.push(`${label} 下一页点击后页面未变化，停止分页`);
        break;
      }
      previous = current;
      await this.snapshot(page, store, `${label}_page_${String(pageNo).padStart(3, "0")}`, cdp);
    }
  }

  private async captureObservedGetPagination(page: Page, store: EvidenceStore): Promise<void> {
    await this.awaitBodies();
    const configs = [
      {
        id: "dynamic",
        path: this.adapter.dynamic.trail_endpoint,
        countKey: "trail",
        pageParam: "curPage",
        sizeParam: "pageSize",
        cap: this.adapter.dynamic.safety_page_cap,
        accept: (url: URL) => !url.searchParams.has("modules[]")
          && !url.searchParams.get("begin_time")
          && !url.searchParams.get("end_time")
      },
      {
        id: "dynamic_mail",
        path: this.adapter.dynamic.trail_endpoint,
        countKey: "trail",
        pageParam: "curPage",
        sizeParam: "pageSize",
        cap: this.adapter.dynamic.safety_page_cap,
        accept: (url: URL) => isConfiguredMailFilterUrl(url.href, this.adapter)
      },
      {
        id: "operation_history",
        path: "/api/customerRead/companyDiffList",
        countKey: "operation_history",
        pageParam: "curPage",
        sizeParam: "pageSize",
        cap: 500,
        accept: (_url: URL) => true
      },
      {
        id: "orders",
        path: "/api/customerRead/customerOrderList",
        countKey: "orders",
        pageParam: "page",
        sizeParam: "page_size",
        cap: 500,
        accept: (_url: URL) => true
      },
      {
        id: "related_products",
        path: "/api/customerRead/relatedProductList",
        countKey: "related_products",
        pageParam: "page",
        sizeParam: "page_size",
        obsoletePageParams: ["page_no"],
        cap: 500,
        accept: (_url: URL) => true
      },
      {
        id: "documents",
        path: this.adapter.documents.list_endpoint,
        countKey: "documents",
        pageParam: "page_no",
        sizeParam: "page_size",
        cap: this.adapter.documents.safety_page_cap,
        // Preserve the exact observed customer/object/folder/keyword scope and
        // mutate only pagination fields. This prevents cross-customer listing.
        accept: (_url: URL) => true
      }
    ];

    for (const config of configs) {
      const candidates = this.responseRecords
        .filter(record => record.method.toUpperCase() === "GET" && record.path === config.path && record.json)
        .map(record => {
          try { return { record, url: new URL(record.url) }; } catch { return null; }
        })
        .filter((item): item is { record: StoredResponse; url: URL } => Boolean(item && config.accept(item.url)));
      const ranked = candidates.map(item => ({
        ...item,
        total: observedTotal([item.record.json], this.adapter.known_count_paths[config.countKey] ?? [])
      })).filter(item => item.total !== null).sort((left, right) => (right.total ?? 0) - (left.total ?? 0));
      const template = ranked[0];
      if (!template || template.total === null || template.total <= 0) continue;

      const normalizedTemplateUrl = new URL(normalizeObservedPaginationUrl(
        template.url.href,
        this.adapter.dynamic.trail_endpoint
      ));
      const advertisedPageSize = Math.max(1, Number(normalizedTemplateUrl.searchParams.get(config.sizeParam) ?? 20));
      const pageSize = normalizedTemplateUrl.pathname === this.adapter.dynamic.trail_endpoint
        ? advertisedPageSize
        : effectivePaginationPageSize(advertisedPageSize, primaryList(template.record.json).length);
      const plan = paginationPagePlan(template.total, pageSize, config.cap);
      const estimatedPages = plan.requiredPages;
      let capturedRows = 0;
      const capturedObjectIds: string[] = [];
      const capturedMailIds: string[] = [];
      const capturedPaginationRows: unknown[] = [];
      let paginationEnded = false;
      this.state.metrics.api_page_concurrency = API_PAGE_CONCURRENCY;
      let nextPage = 1;
      while (nextPage <= plan.pageLimit && capturedRows < template.total && !paginationEnded) {
        this.assertActive();
        const pageNumbers = paginationBatchPageNumbers(
          nextPage, plan.pageLimit, template.total - capturedRows, pageSize
        );
        if (!pageNumbers.length) break;
        nextPage += pageNumbers.length;
        const batch = await Promise.all(pageNumbers.map(async pageNo => {
          const url = paginationUrlForPage(
            normalizedTemplateUrl.href,
            config.pageParam,
            pageNo,
            "obsoletePageParams" in config ? config.obsoletePageParams : []
          );
          const record = await this.fetchPaginationRecord(page, store, url, "xhr_pagination");
          return { pageNo, record };
        }));
        for (const { pageNo, record } of batch.sort((left, right) => left.pageNo - right.pageNo)) {
          const rows = primaryList(record.json);
          const pageRows = rows.length;
          if (config.id === "dynamic" || config.id === "dynamic_mail") {
            capturedObjectIds.push(...rows.flatMap(row => recursiveValues(row, /^trail_?id$/i).slice(0, 1)));
            capturedMailIds.push(...rows.flatMap(row => recursiveValues(row, /^mail_?id$/i).slice(0, 1)));
            capturedPaginationRows.push(...rows);
          } else if (config.id === "documents") {
            capturedObjectIds.push(...rows.flatMap(row => recursiveValues(row, /^file_?id$/i).slice(0, 1)));
          }
          capturedRows += pageRows;
          this.state.metrics[`api_pages_${config.id}`] = pageNo;
          this.state.metrics[`api_rows_${config.id}`] = capturedRows;
          this.state.detail = `${config.id} API 分页 ${pageNo}/约${estimatedPages}；行 ${capturedRows}/${template.total}`;
          if (pageRows === 0) paginationEnded = true;
        }
        await store.writeState(this.state);
      }

      if (config.id === "dynamic" || config.id === "dynamic_mail") {
        const metricPrefix = config.id === "dynamic" ? "api_dynamic" : "api_dynamic_mail";
        const duplicateTrailRows = Math.max(0, capturedObjectIds.length - new Set(capturedObjectIds).size);
        const duplicateMailRows = Math.max(0, capturedMailIds.length - new Set(capturedMailIds).size);
        this.state.metrics[`${metricPrefix}_duplicate_trail_rows`] = duplicateTrailRows;
        this.state.metrics[`${metricPrefix}_duplicate_mail_id_occurrences`] = duplicateMailRows;
        this.state.metrics[`${metricPrefix}_rows_without_trail_id`] = Math.max(0, capturedRows - capturedObjectIds.length);
        if (duplicateTrailRows > 0 || duplicateMailRows > 0) {
          let previousFingerprint = paginationRowMultisetFingerprint(capturedPaginationRows);
          let previousRows = capturedRows;
          let stable = false;
          let probePasses = 0;
          for (let pass = 1; pass <= 2 && !stable; pass += 1) {
            const probePaginationRows: unknown[] = [];
            let probeRows = 0;
            let probeEnded = false;
            let probeNextPage = 1;
            while (probeNextPage <= plan.pageLimit && probeRows < template.total && !probeEnded) {
              this.assertActive();
              const pageNumbers = paginationBatchPageNumbers(
                probeNextPage, plan.pageLimit, template.total - probeRows, pageSize
              );
              if (!pageNumbers.length) break;
              probeNextPage += pageNumbers.length;
              const batch = await Promise.all(pageNumbers.map(async pageNo => {
                const url = paginationUrlForPage(normalizedTemplateUrl.href, config.pageParam, pageNo);
                const record = await this.fetchPaginationRecord(page, store, url, "xhr_pagination_probe");
                return { pageNo, record };
              }));
              for (const { record } of batch.sort((left, right) => left.pageNo - right.pageNo)) {
                const rows = primaryList(record.json);
                probeRows += rows.length;
                probePaginationRows.push(...rows);
                if (rows.length === 0) probeEnded = true;
              }
              const probePage = pageNumbers.at(-1)!;
              this.state.metrics[`${metricPrefix}_duplicate_probe_active_pass`] = pass;
              this.state.metrics[`${metricPrefix}_duplicate_probe_page`] = probePage;
              this.state.metrics[`${metricPrefix}_duplicate_probe_rows`] = probeRows;
              this.state.detail = `${config.id} 重复项复查 ${pass}/2：${probePage}/约${estimatedPages} 页；行 ${probeRows}/${template.total}`;
              await store.writeState(this.state);
            }
            probePasses = pass;
            const fingerprint = paginationRowMultisetFingerprint(probePaginationRows);
            stable = probeRows === template.total && probeRows === previousRows && fingerprint === previousFingerprint;
            previousRows = probeRows;
            previousFingerprint = fingerprint;
          }
          this.state.metrics[`${metricPrefix}_duplicate_probe_passes`] = probePasses;
          this.state.metrics[`${metricPrefix}_duplicate_probe_stable`] = stable;
          this.state.detail = stable
            ? `${config.id} API ${capturedRows} 行存在源端重复映射；整表技术对象 multiset 已复查稳定`
            : `${config.id} API 重复映射在整表复查中不稳定；完整性门将阻止通过`;
          await store.writeState(this.state);
        } else {
          this.state.metrics[`${metricPrefix}_duplicate_probe_passes`] = 0;
          this.state.metrics[`${metricPrefix}_duplicate_probe_stable`] = true;
        }
      } else if (config.id === "documents" && paginationEnded && capturedRows < template.total) {
        let previousFingerprint = sha256([...capturedObjectIds].sort().join("\n"));
        let previousRows = capturedRows;
        let stable = false;
        let probePasses = 0;
        for (let pass = 1; pass <= 2 && !stable; pass += 1) {
          const probeIds: string[] = [];
          let probeRows = 0;
          let probeEnded = false;
          let probeNextPage = 1;
          while (probeNextPage <= plan.pageLimit && probeRows < template.total && !probeEnded) {
            this.assertActive();
            const pageNumbers = paginationBatchPageNumbers(
              probeNextPage, plan.pageLimit, template.total - probeRows, pageSize
            );
            if (!pageNumbers.length) break;
            probeNextPage += pageNumbers.length;
            const batch = await Promise.all(pageNumbers.map(async pageNo => {
              const url = paginationUrlForPage(normalizedTemplateUrl.href, config.pageParam, pageNo);
              const record = await this.fetchPaginationRecord(page, store, url, "xhr_pagination_probe");
              return { pageNo, record };
            }));
            for (const { record } of batch.sort((left, right) => left.pageNo - right.pageNo)) {
              const rows = primaryList(record.json);
              probeRows += rows.length;
              probeIds.push(...rows.flatMap(row => recursiveValues(row, /^file_?id$/i).slice(0, 1)));
              if (rows.length === 0) probeEnded = true;
            }
          }
          probePasses = pass;
          const fingerprint = sha256([...probeIds].sort().join("\n"));
          stable = probeRows === previousRows && fingerprint === previousFingerprint && probeEnded;
          previousRows = probeRows;
          previousFingerprint = fingerprint;
        }
        this.state.metrics.api_documents_visibility_probe_passes = probePasses;
        this.state.metrics.api_documents_visibility_probe_stable = stable;
      } else if (config.id === "documents") {
        this.state.metrics.api_documents_visibility_probe_passes = 0;
        this.state.metrics.api_documents_visibility_probe_stable = true;
      }
      await this.awaitBodies();
    }
  }

  private async captureObservedReadOnlyPostPagination(page: Page, store: EvidenceStore): Promise<void> {
    await this.awaitBodies();
    const configs = [
      { id: "contacts", path: "/api/customerV2Read/companyCustomerList", countKey: "contacts", cap: 5000 },
      { id: "opportunity", path: "/api/opportunityRead/list", countKey: "opportunity", cap: 5000 }
    ];
    for (const config of configs) {
      const candidates = this.responseRecords
        .filter(record => record.method.toUpperCase() === "POST" && record.path === config.path && record.json)
        .map(record => {
          const key = exactReadOnlyPostKey(record.method, record.url, record.requestBodySha256);
          const input = key ? this.readOnlyPostReplayInputs.get(key) : null;
          const total = observedTotal([record.json], this.adapter.known_count_paths[config.countKey] ?? []);
          return input && total !== null ? { record, input, total } : null;
        })
        .filter((item): item is { record: StoredResponse; input: ReadOnlyPostReplayInput; total: number } => item !== null)
        .sort((left, right) => right.total - left.total);
      const template = candidates[0];
      if (!template) {
        this.state.metrics[`api_${config.id}_post_pagination_available`] = false;
        continue;
      }
      const firstPageRows = primaryList(template.record.json).length;
      const advertised = readOnlyPostAdvertisedPageSize(template.input) ?? (firstPageRows || 20);
      const pageSize = effectivePaginationPageSize(advertised, firstPageRows);
      const plan = paginationPagePlan(template.total, pageSize, config.cap);
      let rows = 0;
      let pages = 0;
      for (let pageNo = 1; pageNo <= plan.requiredPages && rows < template.total; pageNo += 1) {
        this.assertActive();
        const input = readOnlyPostPaginationInputForPage(template.input, pageNo);
        if (!input) break;
        const result = await this.pageFetchReadOnlyPost(page, input);
        const record = await this.storeSyntheticPost(result, store, input, "xhr_post_pagination", true);
        pages += 1;
        rows += primaryList(record.json).length;
        this.state.metrics[`api_pages_${config.id}`] = pages;
        this.state.metrics[`api_rows_${config.id}`] = rows;
        this.state.detail = `${config.id} POST API 分页 ${pageNo}/${plan.requiredPages}；行 ${rows}/${template.total}`;
        if (result.error || primaryList(record.json).length === 0) break;
        if (pageNo % 25 === 0 || rows >= template.total) await store.writeState(this.state);
      }
      this.state.metrics[`api_${config.id}_post_pagination_available`] = pages > 0;
      await store.writeState(this.state);
    }
  }

  private async captureMailDetails(page: Page, store: EvidenceStore): Promise<void> {
    await this.awaitBodies();
    const mailFilterRecords = this.responseRecords.filter(item =>
      item.path === this.adapter.dynamic.trail_endpoint
      && item.json
      && isConfiguredMailFilterUrl(item.url, this.adapter)
    );
    const dedicatedPaginationRecords = mailFilterRecords.filter(item => item.resourceType === "xhr_pagination");
    const dedicatedRows = (dedicatedPaginationRecords.length ? dedicatedPaginationRecords : mailFilterRecords)
      .map(item => item.json)
      .flatMap(primaryList);
    const broadPaginationRecords = this.responseRecords.filter(item => {
      if (item.path !== this.adapter.dynamic.trail_endpoint || !item.json || item.resourceType !== "xhr_pagination") return false;
      try {
        const url = new URL(item.url);
        return url.searchParams.getAll("modules[]").length === 0
          && !url.searchParams.get("begin_time")
          && !url.searchParams.get("end_time");
      } catch {
        return false;
      }
    });
    const broadRows = broadPaginationRecords.map(item => item.json).flatMap(primaryList);
    const configuredBroadRows = broadRows.filter(row => isConfiguredMailTrailRow(row, this.adapter));
    const trailRows = [...dedicatedRows, ...configuredBroadRows];
    const trailIds = unique(trailRows.flatMap(item => recursiveValues(item, /^trail_?id$/i))).filter(id => /^\d+$/.test(id));
    const mailObjects = trailRows.flatMap(item => recursiveObjects(item, object => Object.keys(object).some(key => /^mail_?id$/i.test(key))));
    const mailEntries = new Map<string, string | null>();
    for (const object of mailObjects) {
      const mailKey = Object.keys(object).find(key => /^mail_?id$/i.test(key));
      if (!mailKey) continue;
      const mailId = String(object[mailKey] ?? "");
      if (!mailId) continue;
      const userKey = Object.keys(object).find(key => /^user_?id$/i.test(key));
      const userId = userKey ? String(object[userKey] ?? "") || null : null;
      if (!mailEntries.has(mailId) || (!mailEntries.get(mailId) && userId)) mailEntries.set(mailId, userId);
    }
    const mailIds = completeConfiguredMailIds(dedicatedRows, broadRows, this.adapter);
    const dedicatedMailIds = completeConfiguredMailIds(dedicatedRows, [], this.adapter);
    this.state.metrics.trail_objects_observed = trailIds.length;
    this.state.metrics.unique_mail_ids_observed = mailIds.length;
    this.state.metrics.dedicated_filter_unique_mail_ids_observed = dedicatedMailIds.length;
    this.state.metrics.mail_ids_recovered_from_broad_dynamic = mailIds.filter(mailId => !dedicatedMailIds.includes(mailId)).length;
    if (!mailIds.length) {
      this.state.warnings.push("未从 trailList 响应中解析到 mail_id；邮件详情未主动重放");
      return;
    }
    const relationAnchors = mailRelationAnchorIds(mailIds);
    const relationQueued = new Set(relationAnchors);
    const relationSeen = new Set<string>();
    let relationValid = 0;
    let relationRows = 0;
    let relationCursor = 0;
    while (relationCursor < relationAnchors.length) {
      this.assertActive();
      const mailId = relationAnchors[relationCursor++]!;
      const relation = await this.fetchOrReuseSynthetic(
        page, store, "/api/customerRead/mailPreviousNext",
        { company_id: this.state.companyId!, curPage: "1", pageSize: "100", mail_id: mailId },
        "mail_relation_window", true, true
      );
      const expected = observedTotal([relation.record.json], this.adapter.known_count_paths.mail_previous_next ?? []);
      const data = relation.record.json && typeof relation.record.json === "object"
        ? (relation.record.json as Record<string, unknown>).data
        : null;
      const relationData = data && typeof data === "object" && !Array.isArray(data)
        ? data as Record<string, unknown>
        : null;
      const previous = Array.isArray(relationData?.previous) ? relationData.previous : [];
      const next = Array.isArray(relationData?.next) ? relationData.next : [];
      relationSeen.add(mailId);
      for (const item of [...previous, ...next]) {
        const relationId = recursiveValues(item, /^mail_?id$/i)[0];
        if (relationId) relationSeen.add(relationId);
      }
      if (!relation.record.error && expected !== null) relationValid += 1;
      relationRows += previous.length + next.length;
      if (relationCursor === relationAnchors.length) {
        const uncovered = nextUncoveredMailRelationAnchor(mailIds, relationSeen, relationQueued);
        if (uncovered) {
          relationQueued.add(uncovered);
          relationAnchors.push(uncovered);
        }
      }
    }
    this.state.metrics.mail_relation_windows_attempted = relationAnchors.length;
    this.state.metrics.mail_relation_windows_valid = relationValid;
    this.state.metrics.mail_relation_rows_observed = relationRows;
    let available = 0;
    let sourceDeleted = 0;
    let unavailable = 0;
    let trackValid = 0;
    let trackUnavailable = 0;
    let reused = 0;
    let downloaded = 0;
    let completed = 0;
    let cursor = 0;
    let lastPersisted = 0;
    const concurrency = Math.min(MAIL_DETAIL_CONCURRENCY, mailIds.length);
    const persistProgress = async (): Promise<void> => {
      if (completed < mailIds.length && completed - lastPersisted < 25) return;
      lastPersisted = completed;
      this.state.metrics.mail_details_attempted = completed;
      this.state.metrics.mail_details_valid = available;
      this.state.metrics.mail_details_source_deleted = sourceDeleted;
      this.state.metrics.mail_details_unavailable = unavailable;
      this.state.metrics.mail_tracks_attempted = completed;
      this.state.metrics.mail_tracks_valid = trackValid;
      this.state.metrics.mail_tracks_unavailable = trackUnavailable;
      this.state.metrics.mail_detail_responses_reused = reused;
      this.state.metrics.mail_detail_responses_downloaded = downloaded;
      this.state.metrics.mail_detail_concurrency = concurrency;
      this.state.detail = `邮件详情 ${completed}/${mailIds.length}；复用 ${reused}；新取 ${downloaded}；可用 ${available}；源端已删除 ${sourceDeleted}；不可用 ${unavailable}`;
      await store.writeState(this.state);
    };
    await Promise.all(Array.from({ length: concurrency }, async () => {
      for (;;) {
        this.assertActive();
        const index = cursor++;
        if (index >= mailIds.length) return;
        const mailId = mailIds[index]!;
        const detailResult = await this.fetchOrReuseSynthetic(
          page, store, this.adapter.dynamic.mail_info_endpoint, { mail_id: mailId }, "xhr", true, true
        );
        const detailRecord = detailResult.record;
        if (detailResult.reused) reused += 1; else downloaded += 1;
        const detailValidation = validateMailApiResponse(
          detailRecord.status, detailRecord.error, detailRecord.json, mailId, "detail"
        );
        const detailStatus: MailCaptureDraft["detailStatus"] = detailValidation.reason === "source_deleted"
          ? "SOURCE_DELETED"
          : detailValidation.valid ? "AVAILABLE" : "UNAVAILABLE";
        if (detailStatus === "AVAILABLE") available += 1;
        else if (detailStatus === "SOURCE_DELETED") sourceDeleted += 1;
        else unavailable += 1;
        const userId = mailEntries.get(mailId) ?? null;
        const trackParams: Record<string, string> = { mail_id: mailId };
        if (userId) trackParams.user_id = userId;
        const trackResult = await this.fetchOrReuseSynthetic(
          page, store, this.adapter.dynamic.mail_track_endpoint, trackParams, "xhr", true, true
        );
        if (trackResult.reused) reused += 1; else downloaded += 1;
        const trackValidation = validateMailApiResponse(
          trackResult.record.status, trackResult.record.error, trackResult.record.json, mailId, "track"
        );
        if (trackValidation.valid) trackValid += 1;
        else trackUnavailable += 1;
        const relatedResourcesByIdentity = new Map<string, { sourceIdentity: string; aliasKey: string | null }>();
        for (const url of unique([
          ...recursiveUrls(detailRecord.json),
          ...recursiveUrls(trackResult.record.json)
        ]
          .filter(url => isAllowedCaptureUrl(url, this.adapter))
          .filter(url => !isBlockedAiUrl(url, this.adapter))
          .filter(url => !isAutoCaptureExcludedUrl(url)))) {
          const sourceIdentity = sourceUrlIdentityKey(url);
          if (!relatedResourcesByIdentity.has(sourceIdentity)) {
            relatedResourcesByIdentity.set(sourceIdentity, { sourceIdentity, aliasKey: resourceAliasKey(url) });
          }
        }
        this.mailCaptureDrafts.push({
          mailId,
          userId,
          detailStatus,
          detail: detailRecord,
          track: trackResult.record,
          relatedResources: [...relatedResourcesByIdentity.values()]
        });
        completed += 1;
        await persistProgress();
      }
    }));
    await persistProgress();
  }

  private mailArtifactReference(record: StoredResponse, sourceIdentity = sourceUrlIdentityKey(record.url)): Record<string, unknown> {
    return {
      sequence: record.sequence,
      relative_path: record.bodyRelativePath,
      sha256: record.bodySha256,
      bytes: record.bodyBytes,
      http_status: record.status,
      mime_type: record.mimeType,
      source_identity_sha256: sourceIdentity
    };
  }

  private buildMailCaptureManifest(store: EvidenceStore, companyId: string): Record<string, unknown> {
    const artifactsBySourceIdentity = new Map<string, StoredResponse>();
    const artifactsByAliasKey = new Map<string, StoredResponse[]>();
    const sourceUnavailableBySourceIdentity = new Map<string, StoredResponse>();
    for (const record of this.responseRecords) {
      if (isExplicitlyUnavailableDiscoveredResource(record.resourceType, record.status)) {
        sourceUnavailableBySourceIdentity.set(sourceUrlIdentityKey(record.url), record);
        sourceUnavailableBySourceIdentity.set(sha256(record.url), record);
      }
      if (record.status < 200
        || record.status >= 300
        || record.error
        || !record.bodyRelativePath
        || !record.bodySha256
        || Number(record.bodyBytes) <= 0) continue;
      artifactsBySourceIdentity.set(sourceUrlIdentityKey(record.url), record);
      artifactsBySourceIdentity.set(sha256(record.url), record);
      if (record.resourceType === "discovered_file" || record.resourceType === "response_retry") {
        const aliasKey = resourceAliasKey(record.url);
        if (aliasKey) {
          const candidates = artifactsByAliasKey.get(aliasKey) ?? [];
          if (!candidates.some(candidate => sourceUrlIdentityKey(candidate.url) === sourceUrlIdentityKey(record.url))) {
            candidates.push(record);
          }
          artifactsByAliasKey.set(aliasKey, candidates);
        }
      }
    }
    const artifactForSourceIdentity = (sourceIdentity: string): StoredResponse | null =>
      artifactsBySourceIdentity.get(sourceIdentity) ?? null;
    const mails = [...this.mailCaptureDrafts]
      .sort((left, right) => left.mailId.localeCompare(right.mailId, "en", { numeric: true }))
      .map(draft => {
        const related = draft.relatedResources.map(resource => {
          const exactArtifact = artifactForSourceIdentity(resource.sourceIdentity);
          const aliasArtifact = !exactArtifact && resource.aliasKey
            ? selectUniqueAliasArtifact(artifactsByAliasKey.get(resource.aliasKey) ?? [])
            : null;
          const artifact = exactArtifact ?? aliasArtifact;
          const sourceUnavailable = !artifact
            ? sourceUnavailableBySourceIdentity.get(resource.sourceIdentity) ?? null
            : null;
          return {
            source_identity_sha256: resource.sourceIdentity,
            resolution: exactArtifact
              ? "EXACT_SOURCE_IDENTITY"
              : aliasArtifact
                ? "RECOVERED_BROKEN_ALIAS"
                : sourceUnavailable
                  ? "SOURCE_UNAVAILABLE"
                  : "UNRESOLVED",
            artifact_source_identity_sha256: artifact ? sourceUrlIdentityKey(artifact.url) : null,
            source_unavailable_http_status: sourceUnavailable?.status ?? null,
            artifact: artifact ? this.mailArtifactReference(artifact, resource.sourceIdentity) : null
          };
        });
        return {
          mail_id: draft.mailId,
          user_id: draft.userId,
          detail_status: draft.detailStatus,
          detail: this.mailArtifactReference(draft.detail),
          track: this.mailArtifactReference(draft.track),
          related_resources: related
        };
      });
    const relatedCandidates = mails.reduce((total, mail) => total + mail.related_resources.length, 0);
    const relatedResolved = mails.reduce(
      (total, mail) => total + mail.related_resources.filter(resource => resource.artifact !== null).length,
      0
    );
    const relatedSourceUnavailable = mails.reduce(
      (total, mail) => total + mail.related_resources.filter(resource => resource.resolution === "SOURCE_UNAVAILABLE").length,
      0
    );
    const relatedUnresolved = mails.reduce(
      (total, mail) => total + mail.related_resources.filter(resource => resource.resolution === "UNRESOLVED").length,
      0
    );
    return {
      schema: "okki.crm.mail_capture_manifest.v2",
      schema_version: 2,
      artifact_type: "mail_capture_manifest.v2",
      status: "PASS",
      generated_at: nowIso(),
      company_id: companyId,
      session_id: store.sessionId,
      counts: {
        mails: mails.length,
        detail_available: mails.filter(mail => mail.detail_status === "AVAILABLE").length,
        detail_source_deleted: mails.filter(mail => mail.detail_status === "SOURCE_DELETED").length,
        detail_artifacts: mails.filter(mail => mail.detail.relative_path && mail.detail.sha256).length,
        track_artifacts: mails.filter(mail => mail.track.relative_path && mail.track.sha256).length,
        related_resource_candidates: relatedCandidates,
        related_resource_artifacts: relatedResolved,
        related_resource_source_unavailable: relatedSourceUnavailable,
        related_resource_unresolved: relatedUnresolved
      },
      mails
    };
  }

  private async pageFetch(page: Page, endpoint: string, params: Record<string, string>): Promise<SyntheticResult> {
    return this.pageFetchUrl(page, this.endpointUrl(endpoint, params));
  }

  private async pageFetchUrl(page: Page, rawUrl: string): Promise<SyntheticResult> {
    const exactUrl = new URL(rawUrl, this.adapter.origin).href;
    if (isAutoCaptureExcludedUrl(exactUrl)) throw new Error("manual trade route is excluded from automatic GET capture");
    this.syntheticFetchUrlsInFlight.add(exactUrl);
    try {
      return await page.evaluate(async innerUrl => {
        try {
          const url = new URL(innerUrl, location.origin);
          if (url.origin !== location.origin) throw new Error("cross-origin pagination is not allowed");
          const response = await fetch(url.href, { method: "GET", credentials: "include", cache: "no-store" });
          const buffer = await response.arrayBuffer();
          const bytes = new Uint8Array(buffer);
          let binary = "";
          const chunk = 0x8000;
          for (let offset = 0; offset < bytes.length; offset += chunk) {
            binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
          }
          return {
            ok: response.ok,
            status: response.status,
            url: response.url,
            contentType: response.headers.get("content-type") ?? "application/octet-stream",
            base64: btoa(binary),
            error: response.ok ? null : `HTTP ${response.status}`
          };
        } catch (error) {
          return {
            ok: false, status: 0, url: new URL(innerUrl, location.origin).href,
            contentType: "application/octet-stream", base64: "",
            error: error instanceof Error ? error.message : String(error)
          };
        }
      }, exactUrl);
    } finally {
      this.syntheticFetchUrlsInFlight.delete(exactUrl);
    }
  }

  private async pageFetchReadOnlyPost(page: Page, input: ReadOnlyPostReplayInput): Promise<SyntheticResult> {
    const exactUrl = new URL(input.url, this.adapter.origin).href;
    if (new URL(exactUrl).origin !== this.adapter.origin) throw new Error("cross-origin POST recovery is not allowed");
    if (isAutoCaptureExcludedUrl(exactUrl)) throw new Error("manual trade route is excluded from automatic POST capture");
    if (!exactReadOnlyPostKey("POST", exactUrl, input.requestBodySha256)) throw new Error("non-read-only POST recovery is not allowed");
    this.syntheticFetchUrlsInFlight.add(exactUrl);
    try {
      return await page.evaluate(async ({ innerUrl, bodyBase64, headers }) => {
        try {
          const url = new URL(innerUrl, location.origin);
          if (url.origin !== location.origin) throw new Error("cross-origin POST recovery is not allowed");
          const binary = atob(bodyBase64);
          const bytes = new Uint8Array(binary.length);
          for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
          const response = await fetch(url.href, {
            method: "POST", body: bytes, headers, credentials: "include", cache: "no-store"
          });
          const buffer = await response.arrayBuffer();
          const responseBytes = new Uint8Array(buffer);
          let responseBinary = "";
          const chunk = 0x8000;
          for (let offset = 0; offset < responseBytes.length; offset += chunk) {
            responseBinary += String.fromCharCode(...responseBytes.subarray(offset, offset + chunk));
          }
          return {
            ok: response.ok, status: response.status, url: response.url,
            contentType: response.headers.get("content-type") ?? "application/octet-stream",
            base64: btoa(responseBinary), error: response.ok ? null : `HTTP ${response.status}`
          };
        } catch (error) {
          return {
            ok: false, status: 0, url: new URL(innerUrl, location.origin).href,
            contentType: "application/octet-stream", base64: "",
            error: error instanceof Error ? error.message : String(error)
          };
        }
      }, { innerUrl: exactUrl, bodyBase64: input.body.toString("base64"), headers: input.headers });
    } finally {
      this.syntheticFetchUrlsInFlight.delete(exactUrl);
    }
  }

  private async storeSynthetic(
    result: SyntheticResult,
    store: EvidenceStore,
    resourceType: string,
    retainJson = true,
    forceParseJson = false
  ): Promise<StoredResponse> {
    const body = result.base64 ? Buffer.from(result.base64, "base64") : null;
    const record = await store.storeResponse({
      sequence: store.nextSequence(), method: "GET", url: result.url, status: result.status,
      resourceType, mimeType: result.contentType,
      body,
      error: result.error
    });
    if (forceParseJson && record.json === null && body) {
      try { record.json = JSON.parse(body.toString("utf8")) as unknown; } catch { record.json = null; }
    }
    this.responseRecords.push(retainJson ? record : { ...record, json: null });
    if (result.error) await store.appendError({ at: nowIso(), url: redactUrl(result.url), error: result.error });
    return record;
  }

  private async storeSyntheticPost(
    result: SyntheticResult,
    store: EvidenceStore,
    input: ReadOnlyPostReplayInput,
    resourceType: string,
    forceParseJson = false
  ): Promise<StoredResponse> {
    const body = result.base64 ? Buffer.from(result.base64, "base64") : null;
    const record = await store.storeResponse({
      sequence: store.nextSequence(), method: "POST", url: result.url, status: result.status,
      resourceType, mimeType: result.contentType, body, error: result.error,
      requestBodySha256: input.requestBodySha256
    });
    if (forceParseJson && record.json === null && body) {
      try { record.json = JSON.parse(body.toString("utf8")) as unknown; } catch { record.json = null; }
    }
    this.responseRecords.push(record);
    if (result.error) await store.appendError({ at: nowIso(), url: redactUrl(result.url), error: result.error });
    return record;
  }

  private endpointUrl(endpoint: string, params: Record<string, string>): string {
    const url = new URL(endpoint, this.adapter.origin);
    for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
    return url.href;
  }

  private async fetchOrReuseSynthetic(
    page: Page,
    store: EvidenceStore,
    endpoint: string,
    params: Record<string, string>,
    resourceType: string,
    parseJson: boolean,
    fresh = false
  ): Promise<{ record: StoredResponse; reused: boolean }> {
    const url = this.endpointUrl(endpoint, params);
    if (!fresh) {
      const reused = await store.reuseResponseByUrl({
        sequence: store.nextSequence(), method: "GET", url, resourceType, parseJson
      });
      if (reused) {
        this.responseRecords.push(reused);
        return { record: reused, reused: true };
      }
    }
    const result = await this.pageFetchUrl(page, url);
    return {
      record: await this.storeSynthetic(result, store, resourceType, parseJson, parseJson),
      reused: false
    };
  }

  private async fetchPaginationRecord(page: Page, store: EvidenceStore, url: string, resourceType: string): Promise<StoredResponse> {
    // List pages are volatile. Mixing a fresh total with a reused older page
    // creates false gaps or hides new objects. Refresh the small JSON response;
    // EvidenceStore still deduplicates identical bytes by SHA-256, while large
    // linked files keep their normal reuse path.
    const result = await this.pageFetchUrl(page, url);
    this.state.metrics.pagination_responses_downloaded = Number(this.state.metrics.pagination_responses_downloaded ?? 0) + 1;
    return this.storeSynthetic(result, store, resourceType);
  }

  private async recoverFailedGetBodies(page: Page, cdp: CDPSession, store: EvidenceStore): Promise<void> {
    await this.awaitBodies();
    const targets = unique(this.responseRecords
      .filter(item => item.error && !item.bodyRelativePath && item.status >= 200 && item.status < 300)
      .map(item => exactGetRecoveryKey(item.method, item.url))
      .filter((item): item is string => Boolean(item))
      .filter(url => isSafeObservedGetRecoveryUrl(url, this.adapter)));
    let recovered = 0;
    const frameTree = await cdp.send("Page.getFrameTree");
    const frameId = frameTree.frameTree.frame.id;
    for (const url of targets) {
      this.assertActive();
      let record: StoredResponse;
      if (new URL(url).origin === this.adapter.origin) {
        const result = await this.pageFetchUrl(page, url);
        record = await this.storeSynthetic(result, store, "response_retry");
      } else {
        try {
          const result = await cdp.send("Network.loadNetworkResource", {
            frameId, url,
            options: { disableCache: true, includeCredentials: true }
          });
          const resource = result.resource;
          const status = resource.httpStatusCode ?? 0;
          const bodyMayBeEmpty = status === 204 || status === 205;
          const body = resource.stream
            ? await this.readCdpStream(cdp, resource.stream)
            : bodyMayBeEmpty ? Buffer.alloc(0) : null;
          const headers = (resource.headers ?? {}) as Record<string, string>;
          const resourceError = !resource.success
            ? `Network.loadNetworkResource success=${String(resource.success)}`
            : !resource.stream && !bodyMayBeEmpty
              ? "Network.loadNetworkResource returned no response stream"
              : null;
          record = await store.storeResponse({
            sequence: store.nextSequence(), method: "GET", url,
            status,
            resourceType: "response_retry",
            mimeType: headers["content-type"] ?? headers["Content-Type"] ?? "application/octet-stream",
            body,
            error: resourceError
          });
          this.responseRecords.push(record);
          if (resourceError) await store.appendError({ at: nowIso(), url: redactUrl(url), error: resourceError });
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          record = await store.storeResponse({
            sequence: store.nextSequence(), method: "GET", url, status: 0,
            resourceType: "response_retry", mimeType: "application/octet-stream",
            body: null, error: message
          });
          this.responseRecords.push(record);
          await store.appendError({ at: nowIso(), url: redactUrl(url), error: message });
        }
      }
      if (record.bodyRelativePath && !record.error) recovered += 1;
    }
    this.state.metrics.response_body_retries_attempted = targets.length;
    this.state.metrics.response_body_retries_recovered = recovered;
  }

  private async recoverFailedReadOnlyPostBodies(page: Page, store: EvidenceStore): Promise<void> {
    await this.awaitBodies();
    const targets = new Map<string, ReadOnlyPostReplayInput>();
    for (const item of this.responseRecords) {
      if (!item.error || item.bodyRelativePath || item.status < 200 || item.status >= 300) continue;
      const key = exactReadOnlyPostKey(item.method, item.url, item.requestBodySha256);
      const input = key ? this.readOnlyPostReplayInputs.get(key) : null;
      if (key && input) targets.set(key, input);
    }
    let recovered = 0;
    for (const input of targets.values()) {
      this.assertActive();
      const result = await this.pageFetchReadOnlyPost(page, input);
      const record = await store.storeResponse({
        sequence: store.nextSequence(), method: "POST", url: result.url, status: result.status,
        resourceType: "response_retry_post", mimeType: result.contentType,
        body: result.base64 ? Buffer.from(result.base64, "base64") : null,
        error: result.error, requestBodySha256: input.requestBodySha256
      });
      this.responseRecords.push(record);
      if (result.error) await store.appendError({ at: nowIso(), url: redactUrl(result.url), error: result.error });
      if (record.bodyRelativePath && !record.error) recovered += 1;
    }
    this.state.metrics.read_only_post_body_retries_attempted = targets.size;
    this.state.metrics.read_only_post_body_retries_recovered = recovered;
  }

  private async recoverFailedRequestBodies(store: EvidenceStore): Promise<void> {
    let recovered = 0;
    let recoveredBytes = 0;
    for (const item of this.failedRequestBodyCaptures.values()) {
      this.assertActive();
      try {
        const relativePath = await retryTransient(
          () => store.storeRequestBody(item.sequence, item.method, item.url, item.body),
          5
        );
        this.state.metrics.request_bodies_persisted = Number(this.state.metrics.request_bodies_persisted ?? 0) + 1;
        this.state.metrics.request_body_bytes_persisted = Number(this.state.metrics.request_body_bytes_persisted ?? 0) + item.body.byteLength;
        recovered += 1;
        recoveredBytes += item.body.byteLength;
        await store.appendRequest({
          schema: 2, at: nowIso(), session_id: store.sessionId, sequence: item.sequence,
          method: item.method, url: redactUrl(item.url), resource_type: "request_body_retry",
          navigation: false, headers: {}, post_data_size: item.body.byteLength,
          post_data_sha256: sha256(item.body), post_data_relative_path: relativePath,
          post_data_storage: "body_recovered", post_data_role: item.role,
          initiator: "local_request_body_retry"
        });
        this.failedRequestBodyCaptures.delete(item.sequence);
      } catch (error) {
        await store.appendError({
          at: nowIso(), sequence: item.sequence, url: redactUrl(item.url),
          error: `request body retry failed: ${error instanceof Error ? error.message : String(error)}`
        });
      }
    }
    this.state.metrics.request_body_retries_attempted = recovered + this.failedRequestBodyCaptures.size;
    this.state.metrics.request_body_retries_recovered = recovered;
    this.state.metrics.request_body_retry_bytes_recovered = recoveredBytes;
  }

  private async captureDiscoveredResources(cdp: CDPSession, store: EvidenceStore): Promise<void> {
    await this.awaitBodies();
    const urls = unique(this.responseRecords.flatMap(item => recursiveUrls(item.json)))
      .filter(url => isAllowedCaptureUrl(url, this.adapter))
      .filter(url => !isBlockedAiUrl(url, this.adapter))
      .filter(url => !isAutoCaptureExcludedUrl(url));
    const groups = new Map<string, string[]>();
    for (const url of urls) {
      const key = sourceUrlIdentityKey(url);
      const variants = groups.get(key) ?? [];
      variants.push(url);
      groups.set(key, variants);
    }
    const logicalResources = [...groups.values()];
    const frameTree = await cdp.send("Page.getFrameTree");
    const frameId = frameTree.frameTree.frame.id;
    let downloaded = 0;
    let skipped = 0;
    let failed = 0;
    let unavailableEmpty = 0;
    let processed = 0;

    const updateProgress = async (force = false): Promise<void> => {
      this.state.metrics.resource_url_variants_observed = urls.length;
      this.state.metrics.discovered_resources = logicalResources.length;
      this.state.metrics.resource_alias_variants_collapsed = urls.length - logicalResources.length;
      this.state.metrics.resource_download_concurrency = RESOURCE_DOWNLOAD_CONCURRENCY;
      this.state.metrics.resources_downloaded = downloaded;
      this.state.metrics.resources_skipped_existing = skipped;
      this.state.metrics.resources_failed = failed;
      this.state.metrics.resources_unavailable_empty = unavailableEmpty;
      this.state.detail = `资源 ${processed}/${logicalResources.length}（原始 URL ${urls.length}，归并 ${urls.length - logicalResources.length}）；新下载 ${downloaded}；复用已有 ${skipped}；源站空正文 ${unavailableEmpty}；失败 ${failed}`;
      if (force || processed % 25 === 0) await store.writeState(this.state);
    };
    await updateProgress(true);

    let nextIndex = 0;
    const worker = async (): Promise<void> => {
      for (;;) {
        const index = nextIndex++;
        if (index >= logicalResources.length) return;
        this.assertActive();
        const variants = logicalResources[index]!;
        const preferredUrl = variants[variants.length - 1]!;
        let complete = false;
        let emptyVariants = 0;
        try {
          const reused = await store.reuseResponseByUrl({
            sequence: store.nextSequence(), method: "GET", url: preferredUrl, resourceType: "discovered_file"
          });
          if (reused) {
            this.responseRecords.push(reused);
            skipped += 1;
            complete = true;
          }
        } catch (error) {
          await store.appendError({ at: nowIso(), action: "resource_reuse", url: redactUrl(preferredUrl), error: String(error) });
        }

        if (!complete) {
          // Prefer the newest signed URL, then fall back to older aliases.
          // Failed attempts remain in raw evidence, but only a valid 2xx body
          // becomes reusable in the source URL index.
          for (const url of [...variants].reverse()) {
            try {
              const result = await cdp.send("Network.loadNetworkResource", {
                frameId, url,
                options: { disableCache: true, includeCredentials: true }
              });
              const resource = result.resource;
              const body = resource.stream ? await this.readCdpStream(cdp, resource.stream) : null;
              const responseHeaders = (resource.headers ?? {}) as Record<string, string>;
              const mimeType = responseHeaders["content-type"] ?? responseHeaders["Content-Type"] ?? "application/octet-stream";
              const status = resource.httpStatusCode ?? 0;
              const success = Boolean(resource.success && body && body.byteLength > 0 && status >= 200 && status < 300);
              const emptySuccess = Boolean(resource.success && body && body.byteLength === 0 && status >= 200 && status < 300);
              const record = await store.storeResponse({
                sequence: store.nextSequence(), method: "GET", url, status,
                resourceType: emptySuccess ? "discovered_file_unavailable_empty" : "discovered_file", mimeType, body,
                error: success || emptySuccess ? null : `Network.loadNetworkResource success=${String(resource.success)} status=${status}`
              });
              this.responseRecords.push({ ...record, json: null });
              if (success) {
                downloaded += 1;
                complete = true;
                break;
              }
              if (emptySuccess) {
                emptyVariants += 1;
                await store.appendEvent({
                  at: nowIso(), action: "resource_unavailable_empty", url: redactUrl(url),
                  status, body_bytes: 0, logical_resource_key: sourceUrlIdentityKey(url)
                });
                continue;
              }
              await store.appendError({ at: nowIso(), action: "resource_download_variant", url: redactUrl(url), error: record.error });
            } catch (error) {
              await store.appendError({ at: nowIso(), action: "resource_download_variant", url: redactUrl(url), error: String(error) });
            }
          }
        }
        if (!complete && emptyVariants === variants.length) {
          unavailableEmpty += 1;
          complete = true;
        }
        if (!complete) failed += 1;
        processed += 1;
        await updateProgress(processed === logicalResources.length);
      }
    };

    await Promise.all(Array.from(
      { length: Math.min(RESOURCE_DOWNLOAD_CONCURRENCY, logicalResources.length) },
      () => worker()
    ));
    if (unavailableEmpty > 0) {
      this.state.warnings.push(`资源源站对 ${unavailableEmpty} 个逻辑对象的全部 URL 变体持续返回 HTTP 2xx 空正文；已逐变体落盘并标记 source_unavailable_empty`);
    }
    await updateProgress(true);
  }

  private async captureDiscoveredResourcesLegacy(cdp: CDPSession, store: EvidenceStore): Promise<void> {
    await this.awaitBodies();
    const urls = unique(this.responseRecords.flatMap(item => recursiveUrls(item.json)))
      .filter(url => isAllowedCaptureUrl(url, this.adapter))
      .filter(url => !isBlockedAiUrl(url, this.adapter))
      .filter(url => !isAutoCaptureExcludedUrl(url));
    let downloaded = 0;
    let skipped = 0;
    let failed = 0;
    const frameTree = await cdp.send("Page.getFrameTree");
    const frameId = frameTree.frameTree.frame.id;
    for (let index = 0; index < urls.length; index += 1) {
      this.assertActive();
      const url = urls[index]!;
      const sequence = store.nextSequence();
      try {
        const reused = await store.reuseResponseByUrl({ sequence, method: "GET", url, resourceType: "discovered_file" });
        if (reused) {
          this.responseRecords.push(reused);
          skipped += 1;
          this.state.metrics.discovered_resources = urls.length;
          this.state.metrics.resources_downloaded = downloaded;
          this.state.metrics.resources_skipped_existing = skipped;
          this.state.metrics.resources_failed = failed;
          this.state.detail = `资源 ${index + 1}/${urls.length}；新下载 ${downloaded}；复用已有 ${skipped}；失败 ${failed}`;
          if ((index + 1) % 10 === 0 || index + 1 === urls.length) await store.writeState(this.state);
          continue;
        }
        const result = await cdp.send("Network.loadNetworkResource", {
          frameId, url,
          options: { disableCache: true, includeCredentials: true }
        });
        const resource = result.resource;
        const body = resource.stream ? await this.readCdpStream(cdp, resource.stream) : null;
        const responseHeaders = (resource.headers ?? {}) as Record<string, string>;
        const mimeType = responseHeaders["content-type"] ?? responseHeaders["Content-Type"] ?? "application/octet-stream";
        const record = await store.storeResponse({
          sequence, method: "GET", url,
          status: resource.httpStatusCode ?? 0,
          resourceType: "discovered_file",
          mimeType,
          body,
          error: resource.success ? null : `Network.loadNetworkResource success=${String(resource.success)}`
        });
        this.responseRecords.push(record);
        if (resource.success && body) downloaded += 1; else failed += 1;
      } catch (error) {
        failed += 1;
        await store.appendError({ at: nowIso(), action: "resource_download", url: redactUrl(url), error: String(error) });
      }
      if ((index + 1) % 10 === 0 || index + 1 === urls.length) {
        this.state.metrics.discovered_resources = urls.length;
        this.state.metrics.resources_downloaded = downloaded;
        this.state.metrics.resources_skipped_existing = skipped;
        this.state.metrics.resources_failed = failed;
        this.state.detail = `资源 ${index + 1}/${urls.length}；新下载 ${downloaded}；复用已有 ${skipped}；失败 ${failed}`;
        await store.writeState(this.state);
      }
    }
  }

  private async reconcile(store: EvidenceStore): Promise<ReconciliationCheck[]> {
    const checks: ReconciliationCheck[] = [];
    for (const contract of this.adapter.expected_endpoint_contracts) {
      const matches = this.responseRecords.filter(item => item.path === contract.path && item.method.toUpperCase() === contract.method.toUpperCase());
      const withBody = matches.filter(item => item.bodyRelativePath && !item.error);
      checks.push({
        id: `endpoint_${contract.id}`,
        title: `接口契约 ${contract.id}`,
        status: matches.length < contract.min ? "not_observed" : withBody.length < contract.min ? "fail" : "pass",
        expected: { method: contract.method, path: contract.path, minimum: contract.min },
        actual: { observed: matches.length, bodies_saved: withBody.length },
        evidence: withBody.map(item => item.bodyRelativePath!).slice(0, 20),
        detail: matches.length < contract.min ? "页面改版、权限差异或标签未成功打开" : withBody.length < contract.min ? "已观察请求但响应体未落盘" : "已观察并落盘"
      });
    }
    const bodyFailures = this.responseRecords.filter(item => item.error);
    const ignoredTelemetryFailures = bodyFailures.filter(item => isTelemetryUrl(item.url));
    const nonEvidenceMutationFailures = bodyFailures.filter(item =>
      isNonEvidenceMutationResponse(item.method, item.url, this.adapter));
    const explicitlyUnavailableDiscoveredResources = bodyFailures.filter(item =>
      isExplicitlyUnavailableDiscoveredResource(item.resourceType, item.status));
    const externallyUnavailablePassiveKeys = externalPassiveSourceUnavailableKeys(
      this.responseRecords,
      this.adapter.origin
    );
    const externallyUnavailablePassiveFailures = bodyFailures.filter(item => {
      const key = externalPassiveSourceUnavailableKey(item.method, item.url);
      return key !== null && externallyUnavailablePassiveKeys.has(key);
    });
    const successfulResourceAliases = new Set(this.responseRecords
      .filter(item => item.resourceType === "discovered_file" && item.bodyRelativePath && !item.error)
      .map(item => resourceAliasKey(item.url))
      .filter((item): item is string => Boolean(item)));
    const recoveredBrokenAliases = bodyFailures.filter(item => {
      const key = item.resourceType === "discovered_file" ? resourceAliasKey(item.url) : null;
      return key !== null && successfulResourceAliases.has(key);
    });
    const successfulRetryKeys = new Set(this.responseRecords
      .filter(item => item.resourceType === "response_retry" && item.bodyRelativePath && !item.error)
      .map(item => exactGetRecoveryKey(item.method, item.url))
      .filter((item): item is string => Boolean(item)));
    const recoveredGetBodyRetries = bodyFailures.filter(item => {
      const key = exactGetRecoveryKey(item.method, item.url);
      return key !== null && successfulRetryKeys.has(key);
    });
    const successfulExactGetKeys = new Set(this.responseRecords
      .filter(item => item.method.toUpperCase() === "GET" && item.bodyRelativePath && !item.error)
      .map(item => exactGetRecoveryKey(item.method, item.url))
      .filter((item): item is string => Boolean(item)));
    const coveredExactGetDuplicates = bodyFailures.filter(item => {
      if (recoveredGetBodyRetries.includes(item)) return false;
      const key = exactGetRecoveryKey(item.method, item.url);
      return key !== null && successfulExactGetKeys.has(key);
    });
    const successfulExactReadOnlyPostKeys = new Set(this.responseRecords
      .filter(item => item.method.toUpperCase() === "POST" && item.bodyRelativePath && !item.error)
      .map(item => exactReadOnlyPostKey(item.method, item.url, item.requestBodySha256))
      .filter((item): item is string => Boolean(item)));
    const coveredExactReadOnlyPostDuplicates = bodyFailures.filter(item => {
      const key = exactReadOnlyPostKey(item.method, item.url, item.requestBodySha256);
      return key !== null && successfulExactReadOnlyPostKeys.has(key);
    });
    const successfulSignedVariantKeys = new Set(this.responseRecords
      .filter(item => item.method.toUpperCase() === "GET" && item.bodyRelativePath && !item.error)
      .map(item => signedResourceVariantKey(item.url))
      .filter((item): item is string => Boolean(item)));
    const coveredSignedResourceVariants = bodyFailures.filter(item => {
      if (recoveredGetBodyRetries.includes(item)
        || coveredExactGetDuplicates.includes(item)
        || coveredExactReadOnlyPostDuplicates.includes(item)) return false;
      const key = signedResourceVariantKey(item.url);
      return item.method.toUpperCase() === "GET" && key !== null && successfulSignedVariantKeys.has(key);
    });
    const unresolvedBodyFailures = bodyFailures.filter(item =>
      !ignoredTelemetryFailures.includes(item)
      && !nonEvidenceMutationFailures.includes(item)
      && !explicitlyUnavailableDiscoveredResources.includes(item)
      && !externallyUnavailablePassiveFailures.includes(item)
      && !recoveredBrokenAliases.includes(item)
      && !recoveredGetBodyRetries.includes(item)
      && !coveredExactGetDuplicates.includes(item)
      && !coveredExactReadOnlyPostDuplicates.includes(item)
      && !coveredSignedResourceVariants.includes(item));
    checks.push({
      id: "response_body_failures", title: "响应体完整性",
      status: unresolvedBodyFailures.length ? "fail" : "pass",
      expected: 0,
      actual: {
        unresolved: unresolvedBodyFailures.length,
        broken_aliases_with_captured_object: recoveredBrokenAliases.length,
        exact_get_retries_recovered: recoveredGetBodyRetries.length,
        exact_get_duplicates_covered: coveredExactGetDuplicates.length,
        exact_read_only_post_duplicates_covered: coveredExactReadOnlyPostDuplicates.length,
        redundant_contract_responses_covered: 0,
        signed_resource_variants_with_captured_object: coveredSignedResourceVariants.length,
        telemetry_excluded: ignoredTelemetryFailures.length,
        non_evidence_mutations_excluded: nonEvidenceMutationFailures.length,
        explicitly_unavailable_discovered_resources: explicitlyUnavailableDiscoveredResources.length,
        externally_unavailable_passive_resources: externallyUnavailablePassiveFailures.length
      },
      evidence: unresolvedBodyFailures.map(item => `${item.sequence}:${item.path}`).slice(0, 50),
      detail: unresolvedBodyFailures.length ? "存在未被正确对象覆盖的响应体读取或下载失败，不能宣称完整" : "所有可用响应体均已落盘；坏别名另行告警且对应正确对象已有哈希证据"
    });
    if (recoveredGetBodyRetries.length) {
      checks.push({
        id: "recovered_get_response_bodies", title: "GET 响应体主动补取",
        status: "warning",
        expected: 0,
        actual: recoveredGetBodyRetries.length,
        evidence: recoveredGetBodyRetries.map(item => `${item.sequence}:${item.path}`).slice(0, 50),
        detail: "原始 CDP 读体失败已按完全相同的 GET URL 主动补取并落盘；原失败记录保留供审计"
      });
    }
    if (coveredExactReadOnlyPostDuplicates.length) {
      checks.push({
        id: "covered_exact_read_only_post_body_failures", title: "只读 POST 精确重复响应覆盖",
        status: "warning",
        expected: 0,
        actual: coveredExactReadOnlyPostDuplicates.length,
        evidence: coveredExactReadOnlyPostDuplicates.map(item => `${item.sequence}:${item.path}`).slice(0, 50),
        detail: "相同只读接口、精确 URL 与请求正文 SHA-256 已有成功响应体；原 CDP 读体失败继续保留审计，不以不同 POST 请求互相替代"
      });
    }
    if (ignoredTelemetryFailures.length) {
      checks.push({
        id: "telemetry_response_bodies_excluded", title: "遥测响应体排除",
        status: "warning",
        expected: 0,
        actual: ignoredTelemetryFailures.length,
        evidence: ignoredTelemetryFailures.map(item => `${item.sequence}:${item.path}`).slice(0, 50),
        detail: "遥测与统计上报不属于客户页面证据；失败记录保留，但不触发客户资料完整性失败"
      });
    }
    if (nonEvidenceMutationFailures.length) {
      checks.push({
        id: "non_evidence_mutation_response_bodies", title: "非证据型页面设置写接口响应",
        status: "warning",
        expected: 0,
        actual: nonEvidenceMutationFailures.length,
        evidence: nonEvidenceMutationFailures.map(item => `${item.sequence}:${item.path}`).slice(0, 50),
        detail: "页面自动保存界面设置的写接口不承载客户证据；请求正文与失败事件仍原样落盘，但其响应读体失败不判定为客户资料缺失"
      });
    }
    if (explicitlyUnavailableDiscoveredResources.length) {
      checks.push({
        id: "source_unavailable_discovered_resources", title: "源端明确不存在的发现资源",
        status: "warning", expected: 0,
        actual: explicitlyUnavailableDiscoveredResources.length,
        evidence: explicitlyUnavailableDiscoveredResources.map(item => `${item.sequence}:${item.path}`).slice(0, 50),
        detail: "发现资源端点明确返回 HTTP 404/410；失败响应、来源引用与重试证据均保留，但源端没有可下载正文，不判定为采集器漏盘"
      });
    }
    if (externallyUnavailablePassiveFailures.length) {
      checks.push({
        id: "external_passive_source_unavailable_after_retry", title: "跨域页面资源源端不可重取",
        status: "warning", expected: 0,
        actual: {
          failure_rows: externallyUnavailablePassiveFailures.length,
          unique_urls: externallyUnavailablePassiveKeys.size
        },
        evidence: externallyUnavailablePassiveFailures.map(item => `${item.sequence}:${item.path}`).slice(0, 50),
        detail: "页面原生加载曾返回 2xx，但浏览器未暴露正文；完全相同 URL 的独立补取明确返回 400/403/404/410。原事件与补取失败均保留，不伪造正文，并作为源端不可重取项目交付"
      });
    }
    if (recoveredBrokenAliases.length) {
      checks.push({
        id: "source_broken_resource_aliases", title: "源数据中的失效资源别名",
        status: "warning",
        expected: 0,
        actual: recoveredBrokenAliases.length,
        evidence: recoveredBrokenAliases.map(item => `${item.sequence}:${item.url}`).slice(0, 50),
        detail: "源正文含返回 404 的重复点号别名；对应单点路径对象已成功落盘并通过 SHA-256 索引，坏别名原样保留供审计"
      });
    }
    if (coveredSignedResourceVariants.length) {
      checks.push({
        id: "covered_signed_resource_variants", title: "已落盘对象的临时签名 URL 变体",
        status: "warning",
        expected: 0,
        actual: coveredSignedResourceVariants.length,
        evidence: coveredSignedResourceVariants.map(item => `${item.sequence}:${item.path}`).slice(0, 50),
        detail: "临时签名字段已过期或原始 CDP 读体超时；仅剥离认证签名字段后的同一资源键已有成功响应体。所有表示变换参数均保留参与匹配，原失败记录继续保留审计"
      });
    }
    const trailRecords = this.responseRecords.filter(item => item.path === this.adapter.dynamic.trail_endpoint && item.json);
    const trailJson = trailRecords.map(item => item.json);
    const trailRows = trailJson.flatMap(primaryList);
    const expectedTrail = observedTotal(trailJson, this.adapter.known_count_paths.trail ?? []);
    const uniqueTrailIds = unique(trailRows.flatMap(item => recursiveValues(item, /^trail_?id$/i)));
    const uniqueMailIds = unique(trailRows.flatMap(item => recursiveValues(item, /^mail_?id$/i)));
    const dynamicPaginationRecords = trailRecords.filter(record => {
      if (record.resourceType !== "xhr_pagination") return false;
      try {
        const url = new URL(record.url);
        return url.searchParams.getAll("modules[]").length === 0
          && !url.searchParams.get("begin_time")
          && !url.searchParams.get("end_time");
      } catch {
        return false;
      }
    });
    const dynamicPaginationJson = dynamicPaginationRecords.map(record => record.json);
    const dynamicPagination = dynamicPaginationSummary(
      dynamicPaginationJson,
      this.adapter.known_count_paths.trail ?? []
    );
    const dynamicStatModeErrors = dynamicPaginationRecords.filter(record => {
      try { return new URL(record.url).searchParams.get("stat_info") !== "0"; } catch { return true; }
    });
    const dynamicPaginationRows = dynamicPaginationJson.flatMap(primaryList);
    const dynamicPaginationTrailIds = unique(dynamicPaginationRows.flatMap(item => recursiveValues(item, /^trail_?id$/i)));
    const dynamicPaginationMailIdOccurrences = dynamicPaginationRows.flatMap(item => recursiveValues(item, /^mail_?id$/i).slice(0, 1));
    const dynamicPaginationUniqueMailIds = unique(dynamicPaginationMailIdOccurrences);
    const dynamicDuplicateMailIdOccurrences = dynamicPaginationMailIdOccurrences.length - dynamicPaginationUniqueMailIds.length;
    const dynamicRowsWithoutTrailId = dynamicPagination.rowsWithoutTrailId;
    const unionRowsWithoutTrailId = trailRows.filter(item => recursiveValues(item, /^trail_?id$/i).length === 0).length;
    const dynamicTerminalEmpty = hasTerminalEmptyPage(dynamicPaginationJson);
    const dynamicReportedGap = expectedTrail === null ? 0 : Math.max(0, expectedTrail - dynamicPaginationRows.length);
    const duplicateProbeStable = dynamicPagination.duplicateTrailIdRows === 0
      || this.state.metrics.api_dynamic_duplicate_probe_stable === true;
    const dynamicExact = dynamicPagination.complete;
    const dynamicRowClosed = zeroCountPaginationClosed(
      expectedTrail,
      dynamicPagination.expected,
      dynamicPagination.rowCount,
      trailRows.length,
      dynamicPagination.rowsWithoutTrailId,
      unionRowsWithoutTrailId
    ) || stablePaginatedRowClosure(
      dynamicPagination.expected,
      dynamicPagination.rowCount,
      dynamicPagination.rowsWithoutTrailId,
      dynamicPagination.duplicateTrailIdRows,
      dynamicDuplicateMailIdOccurrences,
      duplicateProbeStable
    );
    const dynamicVisibleClosed = expectedTrail !== null
      && dynamicPaginationRows.length > 0
      && dynamicTerminalEmpty
      && dynamicRowsWithoutTrailId === 0
      && dynamicPaginationTrailIds.length === uniqueTrailIds.length;
    const dynamicUnionClosed = unionObjectCoverageComplete(
      expectedTrail, uniqueTrailIds.length, trailRows.length, unionRowsWithoutTrailId
    );
    const dynamicPass = dynamicStatModeErrors.length === 0 && dynamicRowClosed;
    checks.push({
      id: "dynamic_objects", title: "动态与邮件对象对账",
      status: expectedTrail === null ? "not_observed" : dynamicPass ? "pass" : "fail",
      expected: expectedTrail,
      actual: {
        reported_total: expectedTrail,
        visible_rows_captured: dynamicPaginationRows.length,
        visible_unique_trail_ids: dynamicPaginationTrailIds.length,
        rows_with_trail_id: dynamicPagination.rowsWithTrailId,
        rows_without_trail_id: dynamicRowsWithoutTrailId,
        duplicate_trail_id_rows: dynamicPagination.duplicateTrailIdRows,
        duplicate_mail_id_occurrences: dynamicDuplicateMailIdOccurrences,
        duplicate_probe_passes: this.state.metrics.api_dynamic_duplicate_probe_passes ?? 0,
        duplicate_probe_stable: duplicateProbeStable,
        stable_row_closure_complete: dynamicRowClosed,
        unique_trail_ids: uniqueTrailIds.length,
        union_rows_captured: trailRows.length,
        union_rows_without_trail_id: unionRowsWithoutTrailId,
        union_coverage_complete: dynamicUnionClosed,
        unique_mail_ids: uniqueMailIds.length,
        terminal_empty_page: dynamicTerminalEmpty,
        invalid_stat_mode_pages: dynamicStatModeErrors.length,
        reported_but_not_visible: dynamicReportedGap,
        detail_attempts: this.state.metrics.mail_details_attempted ?? 0
      },
      evidence: trailRecords.map(item => item.bodyRelativePath!).filter(Boolean),
      detail: expectedTrail === null ? "未解析到动态总数" : dynamicRowClosed
        ? `动态分页原始行 ${dynamicPagination.rowCount}/${expectedTrail} 精确闭合且无缺 trail_id；存在重复映射时整表技术对象 multiset 已独立复跑稳定`
        : "动态分页必须精确覆盖源端声明原始行数、每行具备 trail_id，且任何重复映射都必须通过整表独立复跑；未知差额仍 fail-closed"
    });
    if (dynamicUnionClosed && !dynamicExact && !dynamicVisibleClosed) {
      checks.push({
        id: "dynamic_union_coverage", title: "动态对象响应并集闭合",
        status: "warning", expected: expectedTrail,
        actual: { pagination_rows: dynamicPaginationRows.length, union_unique_trail_ids: uniqueTrailIds.length },
        evidence: trailRecords.map(item => item.bodyRelativePath!).filter(Boolean),
        detail: "分页接口提前返回空页，但页面原生响应与主动分页响应的唯一 trail_id 并集已精确覆盖接口声明总数；全部原始响应保留审计"
      });
    }
    const mailFilterRecords = trailRecords.filter(record => isConfiguredMailFilterUrl(record.url, this.adapter));
    const mailFilterJson = mailFilterRecords.map(record => record.json);
    const mailPaginationRecords = mailFilterRecords.filter(record => record.resourceType === "xhr_pagination");
    const mailPaginationJson = mailPaginationRecords.map(record => record.json);
    const mailStatModeErrors = mailPaginationRecords.filter(record => {
      try { return new URL(record.url).searchParams.get("stat_info") !== "0"; } catch { return true; }
    });
    const mailPaginationRows = mailPaginationJson.flatMap(primaryList);
    const expectedMailObjects = observedTotal(mailFilterJson, this.adapter.known_count_paths.trail ?? []);
    const mailPagination = mailPaginationSummary(mailPaginationJson, this.adapter.known_count_paths.trail ?? []);
    const filterRowsWithMail = mailPaginationRows.filter(row => recursiveValues(row, /^mail_?id$/i).length > 0);
    const filterRowsWithoutMail = mailPagination.rowsWithoutMailId;
    const filterMailTrailIds = unique(filterRowsWithMail.flatMap(item => recursiveValues(item, /^trail_?id$/i)));
    const filterMailIds = unique(filterRowsWithMail.flatMap(item => recursiveValues(item, /^mail_?id$/i)));
    const broadConfiguredRows = dynamicPaginationRows.filter(row => isConfiguredMailTrailRow(row, this.adapter));
    const broadConfiguredRowsWithMail = broadConfiguredRows.filter(row => recursiveValues(row, /^mail_?id$/i).length > 0);
    const broadConfiguredRowsWithoutMail = broadConfiguredRows.length - broadConfiguredRowsWithMail.length;
    const broadConfiguredMailTrailIds = unique(broadConfiguredRowsWithMail.flatMap(item => recursiveValues(item, /^trail_?id$/i)));
    const broadConfiguredMailIds = unique(broadConfiguredRowsWithMail.flatMap(item => recursiveValues(item, /^mail_?id$/i)));
    const completeMailIds = unique([...filterMailIds, ...broadConfiguredMailIds]);
    const completeMailTrailIds = unique([...filterMailTrailIds, ...broadConfiguredMailTrailIds]);
    const recoveredFromBroadMailIds = completeMailIds.filter(mailId => !filterMailIds.includes(mailId));
    const broadConfiguredMailIdSet = new Set(broadConfiguredMailIds);
    const dedicatedMailIdsAreBroadSubset = filterMailIds.every(mailId => broadConfiguredMailIdSet.has(mailId));
    const expectedCompanyId = String(this.state.companyId ?? "");
    const paginationCompanyBound = expectedCompanyId.length > 0
      && [...dynamicPaginationRecords, ...mailPaginationRecords].every(record => {
        try { return new URL(record.url).searchParams.get("company_id") === expectedCompanyId; }
        catch { return false; }
      });
    const mailIdentityClosure = configuredMailIdentityClosure(
      [...filterRowsWithMail, ...broadConfiguredRowsWithMail],
      this.adapter,
      expectedCompanyId
    );
    const detailAttempts = Number(this.state.metrics.mail_details_attempted ?? 0);
    const detailValid = Number(this.state.metrics.mail_details_valid ?? 0);
    const detailSourceDeleted = Number(this.state.metrics.mail_details_source_deleted ?? 0);
    const detailUnavailable = Number(this.state.metrics.mail_details_unavailable ?? 0);
    const trackAttempts = Number(this.state.metrics.mail_tracks_attempted ?? 0);
    const trackValid = Number(this.state.metrics.mail_tracks_valid ?? 0);
    const trackUnavailable = Number(this.state.metrics.mail_tracks_unavailable ?? 0);
    const outsideFilterObjects = broadConfiguredMailTrailIds.filter(trailId => !filterMailTrailIds.includes(trailId)).length;
    const mailDuplicateProbeStable = (
      mailPagination.duplicateTrailIdRows === 0
      && mailPagination.duplicateMailIdOccurrences === 0
    ) || this.state.metrics.api_dynamic_mail_duplicate_probe_stable === true;
    const mailTerminalEmpty = hasTerminalEmptyPage(mailPaginationJson);
    const mailReportedGap = expectedMailObjects === null ? 0 : Math.max(0, expectedMailObjects - mailPaginationRows.length);
    const mailVisiblePageClosed = expectedMailObjects === 0 || stablePaginatedRowClosure(
      mailPagination.expected,
      mailPagination.rowCount,
      mailPagination.rowsWithoutTrailId,
      mailPagination.duplicateTrailIdRows,
      mailPagination.duplicateMailIdOccurrences,
      mailDuplicateProbeStable
    );
    const mailPass = expectedMailObjects !== null
      && dynamicPass
      && mailFilterJson.length > 0
      && (expectedMailObjects === 0 || mailPaginationJson.length > 0)
      && mailStatModeErrors.length === 0
      && mailVisiblePageClosed
      && mailPaginationExpectedMatches(expectedMailObjects, mailPagination.expected)
      && mailPagination.nonDetailRowsWithoutTrailId === 0
      && broadConfiguredRowsWithoutMail === 0
      && paginationCompanyBound
      && mailIdentityClosure.complete
      && mailIdentityClosure.uniqueMailIds === completeMailIds.length
      && detailAttempts === completeMailIds.length
      && mailDetailAttemptsComplete(detailAttempts, detailValid, detailSourceDeleted, detailUnavailable)
      && trackAttempts === completeMailIds.length
      && trackValid === trackAttempts
      && trackUnavailable === 0;
    checks.push({
      id: "mail_205_203_model", title: "邮件对象、唯一详情与不可用项对账",
      status: expectedMailObjects === null ? "not_observed" : mailPass ? "pass" : "fail",
      expected: { mail_filter_total_minimum: expectedMailObjects },
      actual: {
        mail_filter_rows_captured: mailPaginationRows.length,
        mail_filter_rows_with_mail_id: filterRowsWithMail.length,
        mail_filter_rows_without_mail_id: filterRowsWithoutMail,
        mail_filter_rows_with_trail_id: mailPagination.rowsWithTrailId,
        mail_filter_rows_without_trail_id: mailPagination.rowsWithoutTrailId,
        mail_filter_unique_trail_ids: mailPagination.uniqueTrailIds,
        mail_filter_duplicate_trail_id_rows: mailPagination.duplicateTrailIdRows,
        mail_filter_unique_mail_ids: mailPagination.uniqueMailIds,
        mail_filter_duplicate_mail_id_occurrences: mailPagination.duplicateMailIdOccurrences,
        mail_filter_duplicate_probe_passes: this.state.metrics.api_dynamic_mail_duplicate_probe_passes ?? 0,
        mail_filter_duplicate_probe_stable: mailDuplicateProbeStable,
        stable_row_closure_complete: mailVisiblePageClosed,
        non_detail_rows_without_trail_id: mailPagination.nonDetailRowsWithoutTrailId,
        non_detail_type_groups: mailPagination.nonDetailTypeGroups,
        unique_mail_trail_ids: completeMailTrailIds.length,
        unique_mail_ids: completeMailIds.length,
        dedicated_filter_unique_mail_ids: filterMailIds.length,
        broad_dynamic_configured_rows: broadConfiguredRows.length,
        broad_dynamic_configured_rows_without_mail_id: broadConfiguredRowsWithoutMail,
        broad_dynamic_configured_unique_mail_ids: broadConfiguredMailIds.length,
        dedicated_filter_mail_ids_are_broad_subset: dedicatedMailIdsAreBroadSubset,
        pagination_company_bound: paginationCompanyBound,
        mail_identity_rows_without_exact_identity: mailIdentityClosure.rowsWithoutExactIdentity,
        mail_identity_company_mismatches: mailIdentityClosure.companyMismatches,
        mail_to_trail_conflicts: mailIdentityClosure.mailToTrailConflicts,
        trail_to_mail_conflicts: mailIdentityClosure.trailToMailConflicts,
        mail_identity_unique_mail_ids: mailIdentityClosure.uniqueMailIds,
        mail_identity_unique_trail_ids: mailIdentityClosure.uniqueTrailIds,
        dedicated_broad_mail_union_valid: mailIdentityClosure.complete
          && mailIdentityClosure.uniqueMailIds === completeMailIds.length,
        mail_ids_recovered_from_broad_dynamic: recoveredFromBroadMailIds.length,
        outside_filter_or_shared_trail_objects: outsideFilterObjects,
        extra_trail_objects_for_known_mail_ids: Math.max(0, completeMailTrailIds.length - completeMailIds.length),
        terminal_empty_page: mailTerminalEmpty,
        invalid_stat_mode_pages: mailStatModeErrors.length,
        reported_but_not_visible: mailReportedGap,
        details_attempted: detailAttempts,
        available_details: detailValid,
        source_deleted_details: detailSourceDeleted,
        unavailable_details: detailUnavailable,
        tracks_attempted: trackAttempts,
        valid_tracks: trackValid,
        unavailable_tracks: trackUnavailable
      },
      evidence: trailRecords.map(item => item.bodyRelativePath!).filter(Boolean),
      detail: expectedMailObjects === null ? "未识别邮件筛选响应" : mailPass
        ? `邮件筛选原始行 ${mailPagination.rowCount}/${expectedMailObjects} 精确闭合且重复 multiset 稳定；专用筛选与闭合的全动态邮件行并集经客户、mail_id、trail_id 一一映射验证后得到 ${completeMailIds.length} 个唯一 mail_id（补回 ${recoveredFromBroadMailIds.length}），详情/追踪逐项闭合，其中 ${detailValid} 可用、${detailSourceDeleted} 源端已删除`
        : "邮件筛选原始行数、trail_id、重复 multiset、客户绑定、一一映射、闭合全动态邮件行并集或详情/追踪清单未严格闭合；任何未知差额仍 fail-closed"
    });
    const artifactReady = (record: StoredResponse): boolean => record.status >= 200
      && record.status < 300
      && !record.error
      && Boolean(record.bodyRelativePath && record.bodySha256 && Number(record.bodyBytes) > 0);
    const manifestDraftIds = unique(this.mailCaptureDrafts.map(draft => draft.mailId));
    const invalidManifestDrafts = this.mailCaptureDrafts.filter(draft =>
      draft.detailStatus === "UNAVAILABLE" || !artifactReady(draft.detail) || !artifactReady(draft.track)
    );
    const manifestDraftPass = sameStringSet(manifestDraftIds, completeMailIds)
      && this.mailCaptureDrafts.length === manifestDraftIds.length
      && invalidManifestDrafts.length === 0;
    checks.push({
      id: "mail_capture_manifest_v2", title: "邮件采集清单 v2",
      status: manifestDraftPass ? "pass" : "fail",
      expected: {
        unique_mail_ids: completeMailIds.length,
        detail_and_track_artifacts_per_mail: 1,
        detail_statuses: ["AVAILABLE", "SOURCE_DELETED"]
      },
      actual: {
        unique_manifest_mail_ids: manifestDraftIds.length,
        manifest_rows: this.mailCaptureDrafts.length,
        invalid_or_missing_artifact_rows: invalidManifestDrafts.length
      },
      evidence: this.mailCaptureDrafts.flatMap(draft => [
        draft.detail.bodyRelativePath,
        draft.track.bodyRelativePath
      ]).filter((value): value is string => Boolean(value)).slice(0, 50),
      detail: manifestDraftPass
        ? "每个唯一 mail_id 均绑定本次详情与追踪响应的路径、SHA-256 和字节数；通过后才原子更新 latest 清单"
        : "邮件 ID 与详情/追踪证据不能一一绑定，禁止更新 latest 处理范围"
    });
    checks.push({
      id: "trail_pagination_stat_mode", title: "动态与邮件分页列表模式",
      status: dynamicStatModeErrors.length === 0 && mailStatModeErrors.length === 0 ? "pass" : "fail",
      expected: { invalid_stat_mode_pages: 0 },
      actual: {
        dynamic_invalid_stat_mode_pages: dynamicStatModeErrors.length,
        mail_invalid_stat_mode_pages: mailStatModeErrors.length
      },
      evidence: [...dynamicStatModeErrors, ...mailStatModeErrors]
        .map(item => `${item.sequence}:${item.path}`),
      detail: "主动分页必须固定 stat_info=0；stat_info=1 是统计摘要模式，不能用于完整列表对账"
    });
    if (dynamicReportedGap > 0 || mailReportedGap > 0) {
      checks.push({
        id: "source_reported_total_visibility_gap", title: "源端声明总数与可分页行数差额",
        status: "fail",
        expected: {
          dynamic_reported_total: expectedTrail,
          mail_reported_total: expectedMailObjects
        },
        actual: {
          dynamic_visible_rows: dynamicPaginationRows.length,
          dynamic_reported_but_not_visible: dynamicReportedGap,
          mail_visible_rows: mailPaginationRows.length,
          mail_reported_but_not_visible: mailReportedGap,
          dynamic_terminal_empty_page: dynamicTerminalEmpty,
          mail_terminal_empty_page: mailTerminalEmpty
        },
        evidence: [...dynamicPaginationRecords, ...mailFilterRecords]
          .map(item => item.bodyRelativePath!)
          .filter(Boolean),
        detail: "接口声明总数大于唯一可分页对象数；该差额是采集不完整，必须阻止 processing scope 发布"
      });
    }
    const relationRecords = latestRecordsPerExactUrl(this.responseRecords.filter(item =>
      item.path === "/api/customerRead/mailPreviousNext" && item.json));
    const relationExpected = observedTotal(
      relationRecords.map(item => item.json),
      this.adapter.known_count_paths.mail_previous_next ?? []
    );
    const relationMailIds = unique(relationRecords.flatMap(record => {
      const ids = recursiveValues(record.json, /^mail_?id$/i);
      try {
        const anchor = new URL(record.url).searchParams.get("mail_id");
        if (anchor) ids.push(anchor);
      } catch { /* invalid URLs are already rejected elsewhere */ }
      return ids;
    }));
    const capturedMailIds = unique(this.mailCaptureDrafts.map(item => item.mailId));
    const effectiveRelationExpected = relationExpected ?? (expectedMailObjects === 0 ? 0 : null);
    const relationIdSet = new Set(relationMailIds);
    const capturedIdSet = new Set(capturedMailIds);
    const relationOnlyMailIds = relationMailIds.filter(mailId => !capturedIdSet.has(mailId));
    const capturedMissingFromRelations = capturedMailIds.filter(mailId => !relationIdSet.has(mailId));
    const relationComplete = mailRelationCoverageComplete(
      effectiveRelationExpected,
      expectedMailObjects,
      relationMailIds,
      capturedMailIds
    );
    checks.push({
      id: "mail_previous_next_relations", title: "邮件前后关系窗口对账",
      status: effectiveRelationExpected === null ? "not_observed" : relationComplete ? "pass" : "fail",
      expected: effectiveRelationExpected,
      actual: {
        relation_windows: relationRecords.length,
        reported_relation_rows: effectiveRelationExpected,
        unique_relation_mail_ids: relationMailIds.length,
        captured_mail_ids: capturedMailIds.length,
        relation_only_mail_ids: relationOnlyMailIds.length,
        captured_missing_from_relations: capturedMissingFromRelations.length
      },
      evidence: relationRecords.map(item => item.bodyRelativePath!).filter(Boolean),
      detail: effectiveRelationExpected === null
        ? "未观察到 mailPreviousNext 总数"
        : relationComplete
          ? `关系接口声明原始行 ${effectiveRelationExpected}；关系窗口唯一 mail_id ${relationMailIds.length} 已全量闭合；其中 ${relationOnlyMailIds.length} 个仅存在于关系源，已在原始关系证据中保留`
          : `关系接口声明原始行 ${effectiveRelationExpected}；关系窗口唯一 mail_id ${relationMailIds.length}；详情中仍有 ${capturedMissingFromRelations.length} 个 mail_id 未被关系源覆盖`
    });
    const allDocumentRecords = this.responseRecords.filter(item => item.path === this.adapter.documents.list_endpoint && item.json);
    const documentRecords = latestRecordsPerExactUrl(allDocumentRecords);
    const documentJson = documentRecords.map(item => item.json);
    const documentPagination = documentPaginationSummary(documentJson, this.adapter.known_count_paths.documents ?? []);
    const documentTerminalEmpty = hasTerminalEmptyPage(documentJson);
    const documentVisibleGapClosed = stableVisibleGapCoverageComplete(
      documentPagination.expected,
      documentPagination.rowCount,
      documentPagination.rowsWithoutFileId,
      documentTerminalEmpty,
      this.state.metrics.api_documents_visibility_probe_stable === true
    );
    const documentPass = documentPagination.complete || documentVisibleGapClosed;
    checks.push({
      id: "document_objects", title: "文档对象与分页对账",
      status: documentPagination.expected === null ? "not_observed" : documentPass ? "pass" : "fail",
      expected: documentPagination.expected,
      actual: {
        rows_captured: documentPagination.rowCount,
        rows_with_file_id: documentPagination.rowsWithFileId,
        rows_without_file_id: documentPagination.rowsWithoutFileId,
        unique_file_ids: documentPagination.uniqueFileIds,
        duplicate_file_id_rows: documentPagination.duplicateFileIdRows,
        repeated_exact_url_responses: allDocumentRecords.length - documentRecords.length,
        terminal_empty_page: documentTerminalEmpty,
        visibility_probe_passes: this.state.metrics.api_documents_visibility_probe_passes ?? 0,
        visibility_probe_stable: this.state.metrics.api_documents_visibility_probe_stable ?? false
      },
      evidence: allDocumentRecords.map(item => item.bodyRelativePath!).filter(Boolean),
      detail: documentPagination.expected === null ? "未解析到文档总数" : documentPass
        ? documentVisibleGapClosed && !documentPagination.complete
          ? `文档接口声明 ${documentPagination.expected} 条，独立刷新复核至空页后稳定可见 ${documentPagination.rowCount} 行；差额保留为源端不可见证据`
          : documentPagination.uniqueFileIds === documentPagination.expected
          ? `文档唯一 file_id ${documentPagination.uniqueFileIds}/${documentPagination.expected} 已完整覆盖；原始分页行 ${documentPagination.rowCount}，重复 file_id 行 ${documentPagination.duplicateFileIdRows}、同一精确 URL 重复响应 ${allDocumentRecords.length - documentRecords.length} 已单独审计`
          : `文档分页行数 ${documentPagination.rowCount}/${documentPagination.expected}；重复 file_id 行 ${documentPagination.duplicateFileIdRows}、同一精确 URL 重复响应 ${allDocumentRecords.length - documentRecords.length} 已单独审计`
        : "文档分页行数、直接 file_id 覆盖与 API 总数未闭合"
    });
    if (documentVisibleGapClosed && !documentPagination.complete) {
      checks.push({
        id: "document_source_visibility_gap", title: "文档声明总数与可见分页差额",
        status: "warning", expected: documentPagination.expected,
        actual: {
          visible_rows: documentPagination.rowCount,
          visible_unique_file_ids: documentPagination.uniqueFileIds,
          terminal_empty_page: documentTerminalEmpty,
          probe_stable: true
        },
        evidence: allDocumentRecords.map(item => item.bodyRelativePath!).filter(Boolean),
        detail: "文档列表已翻到明确空页并以独立刷新复核得到相同指纹；全部可见行与文件对象已落盘，接口声明差额保留为源端不可见证据"
      });
    }
    if (documentPagination.duplicateFileIdRows > 0) {
      checks.push({
        id: "document_duplicate_file_ids", title: "文档接口重复 file_id",
        status: "warning",
        expected: 0,
        actual: documentPagination.duplicateFileIdRows,
        evidence: allDocumentRecords.map(item => item.bodyRelativePath!).filter(Boolean),
        detail: "源接口分页包含重复 file_id；全部原始响应已保留，完整性按接口总数与唯一 file_id 覆盖共同核对"
      });
    }
    const documentFolderRecords = latestRecordsPerExactUrl(this.responseRecords.filter(record =>
      record.path === this.adapter.documents.folder_endpoint && record.json));
    const documentFolderJson = documentFolderRecords.map(record => record.json);
    const declaredDocumentFolders = observedTotal(
      documentFolderJson,
      this.adapter.known_count_paths.document_folders ?? []
    );
    const documentFolderRows = documentFolderJson.flatMap(primaryList);
    const documentFolderUniqueRows = new Set(documentFolderRows.map(row => JSON.stringify(row))).size;
    const expectedDocumentFolders = declaredDocumentFolders ?? (documentFolderRecords.length ? documentFolderRows.length : null);
    const documentFolderComplete = expectedDocumentFolders !== null
      && documentFolderRows.length === expectedDocumentFolders;
    checks.push({
      id: "count_document_folders", title: "document_folders 数量对账",
      status: expectedDocumentFolders === null ? "not_observed" : documentFolderComplete ? "pass" : "fail",
      expected: expectedDocumentFolders,
      actual: {
        rows_captured: documentFolderRows.length,
        unique_rows: documentFolderUniqueRows,
        duplicate_rows: Math.max(0, documentFolderRows.length - documentFolderUniqueRows),
        count_basis: declaredDocumentFolders === null ? "complete_data_array_length" : "declared_total"
      },
      evidence: documentFolderRecords.map(record => record.bodyRelativePath!).filter(Boolean),
      detail: expectedDocumentFolders === null
        ? "未观察到文档目录响应"
        : documentFolderComplete
          ? `文档目录完整 data 数组 ${documentFolderRows.length} 行已落盘`
          : `文档目录可见行 ${documentFolderRows.length}/${expectedDocumentFolders}，尚未闭合`
    });
    for (const item of [
      { id: "orders", path: "/api/customerRead/customerOrderList", countKey: "orders" },
      { id: "related_products", path: "/api/customerRead/relatedProductList", countKey: "related_products" },
      { id: "operation_history", path: "/api/customerRead/companyDiffList", countKey: "operation_history" },
      { id: "opportunity", path: "/api/opportunityRead/list", countKey: "opportunity" },
      { id: "contacts", path: "/api/customerV2Read/companyCustomerList", countKey: "contacts" }
    ]) {
      const records = this.responseRecords.filter(record => record.path === item.path && record.json);
      const summary = listCountReconciliationSummary(records, this.adapter.known_count_paths[item.countKey] ?? []);
      checks.push({
        id: `count_${item.id}`, title: `${item.id} 数量对账`,
        status: summary.expected === null ? "not_observed" : summary.complete ? "pass" : "fail",
        expected: summary.expected,
        actual: {
          rows_captured: summary.rowsCaptured,
          unique_rows: summary.uniqueRows,
          duplicate_rows: summary.duplicateRows,
          pagination_basis: summary.paginationBasis
        },
        evidence: records.map(record => record.bodyRelativePath!).filter(Boolean),
        detail: summary.expected === null ? "未解析到接口总数" : summary.complete
          ? `分页原始行数 ${summary.rowsCaptured}/${summary.expected}；重复行 ${summary.duplicateRows} 单独审计并原样保留`
          : `分页原始行数 ${summary.rowsCaptured}/${summary.expected}，尚未闭合`
      });
    }
    const blockedAi = await this.countLedgerRows(path.join(store.caseRoot, "scope_ledger.jsonl"), "blocked_external_ai");
    const observedAi = this.responseRecords.filter(item => isBlockedAiUrl(item.url, this.adapter)).length;
    checks.push({
      id: "external_ai_privacy", title: "外部 AI 隔离",
      status: observedAi ? "fail" : "pass",
      expected: 0, actual: observedAi,
      evidence: [],
      detail: `外部 AI 响应 ${observedAi}；预加载阻断命中 ${blockedAi}`
    });
    const observedRequestBodies = Number(this.state.metrics.request_bodies_observed ?? 0);
    const persistedRequestBodies = Number(this.state.metrics.request_bodies_persisted ?? 0);
    const observedRequestBodyBytes = Number(this.state.metrics.request_body_bytes_observed ?? 0);
    const persistedRequestBodyBytes = Number(this.state.metrics.request_body_bytes_persisted ?? 0);
    const missingRequestBodies = Math.max(0, observedRequestBodies - persistedRequestBodies);
    checks.push({
      id: "request_body_completeness", title: "上行请求正文完整性与分类",
      status: missingRequestBodies === 0 && observedRequestBodyBytes === persistedRequestBodyBytes ? "pass" : "fail",
      expected: { missing_request_bodies: 0, missing_request_body_bytes: 0 },
      actual: {
        observed_request_bodies: observedRequestBodies,
        persisted_request_bodies: persistedRequestBodies,
        missing_request_bodies: missingRequestBodies,
        observed_bytes: observedRequestBodyBytes,
        persisted_bytes: persistedRequestBodyBytes,
        missing_bytes: Math.max(0, observedRequestBodyBytes - persistedRequestBodyBytes),
        contract_business: Number(this.state.metrics.request_bodies_contract_business ?? 0),
        same_origin_other: Number(this.state.metrics.request_bodies_same_origin_other ?? 0),
        external_service: Number(this.state.metrics.request_bodies_external_service ?? 0)
      },
      evidence: ["request_ledger.jsonl"],
      detail: `观察 ${observedRequestBodies} 个带正文请求并原样保存 ${persistedRequestBodies} 个；同时按业务契约、同源其他、外部服务打标签，后续处理可过滤但原始层不删减`
    });
    this.state.metrics.responses_observed = this.responseRecords.length;
    this.state.metrics.response_body_failures = unresolvedBodyFailures.length;
    this.state.metrics.response_body_failures_raw = bodyFailures.length;
    this.state.metrics.response_body_failures_recovered_aliases = recoveredBrokenAliases.length;
    this.state.metrics.response_body_failures_covered_duplicates = coveredExactGetDuplicates.length
      + coveredExactReadOnlyPostDuplicates.length;
    this.state.metrics.response_body_failures_covered_signed_variants = coveredSignedResourceVariants.length;
    this.state.metrics.response_body_failures_telemetry_excluded = ignoredTelemetryFailures.length;
    this.state.metrics.response_body_failures_non_evidence_mutations_excluded = nonEvidenceMutationFailures.length;
    this.state.metrics.reconciliation_checks = checks.length;
    this.state.metrics.reconciliation_failures = checks.filter(item => item.status === "fail" || item.status === "not_observed").length;
    return checks;
  }

  private async readCdpStream(cdp: CDPSession, handle: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    try {
      for (;;) {
        const result = await cdp.send("IO.read", { handle, size: 1024 * 1024 });
        if (result.data) chunks.push(result.base64Encoded ? Buffer.from(result.data, "base64") : Buffer.from(result.data, "utf8"));
        if (result.eof) break;
      }
      return Buffer.concat(chunks);
    } finally {
      await cdp.send("IO.close", { handle }).catch(() => undefined);
    }
  }

  private async countLedgerRows(filePath: string, needle: string): Promise<number> {
    try {
      const text = await (await import("node:fs/promises")).readFile(filePath, "utf8");
      return text.split(/\r?\n/).filter(line => line.includes(needle)).length;
    } catch { return 0; }
  }
}

