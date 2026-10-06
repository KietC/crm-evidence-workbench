/** Immutable local objects plus occurrence ledgers; hashes establish bytes, not business truth.
 * 不可变本地对象与出现台账；哈希证明字节一致，不能证明业务事实。 */
import crypto from "node:crypto";
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import type { StoredResponse } from "./types.js";

const SECRET_QUERY_KEYS = new Set([
  "signature", "token", "access_token", "authorization", "auth", "api_key",
  "expires", "ossaccesskeyid", "awsaccesskeyid", "googleaccessid", "key-pair-id", "policy",
  "x-amz-algorithm", "x-amz-credential", "x-amz-date", "x-amz-expires",
  "x-amz-security-token", "x-amz-signature", "x-amz-signedheaders",
  "x-goog-algorithm", "x-goog-credential", "x-goog-date", "x-goog-expires",
  "x-goog-signature", "x-goog-signedheaders", "x-goog-security-token",
  "x-oss-access-key-id", "x-oss-credential", "x-oss-date", "x-oss-expires",
  "x-oss-security-token", "x-oss-signature", "x-oss-signature-version", "security-token"
]);
const SECRET_HEADERS = new Set(["authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"]);
export const MAX_EVIDENCE_PATH_CHARS = 205;
export const MAX_ATOMIC_PATH_CHARS = 220;
const JSONL_APPEND_GATES = new Map<string, Promise<void>>();

export async function hideDirectoryOnWindows(directory: string): Promise<void> {
  if (process.platform !== "win32") return;
  await new Promise<void>((resolve, reject) => {
    execFile("attrib.exe", ["+H", directory], { windowsHide: true }, error => error ? reject(error) : resolve());
  });
}

export function nowIso(): string {
  return new Date().toISOString();
}

export function stamp(): string {
  return nowIso().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

export function sha256(data: crypto.BinaryLike): string {
  return crypto.createHash("sha256").update(data).digest("hex");
}

export function safeName(value: string, max = 120): string {
  const clean = String(value)
    .normalize("NFKC")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
    .replace(/\s+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^\.+|\.+$/g, "")
    .slice(0, max);
  return clean || "unnamed";
}

export function compatibleEvidencePath(directory: string, preferredFileName: string, digest: string): string {
  const preferred = path.join(directory, path.basename(preferredFileName));
  if (preferred.length <= MAX_EVIDENCE_PATH_CHARS) return preferred;
  // Preserve semantic compound suffixes when shortening long Windows paths.
  // path.extname("x.response.rawhtml") returns only ".rawhtml", which loses the
  // distinction between raw evidence, an offline viewer and an empty body.
  // 缩短 Windows 路径时保留复合后缀，以区分原始证据、离线页面与空响应。
  const lowerName = path.basename(preferredFileName).toLowerCase();
  const compoundExtension = [".response.rawhtml", ".dom.rawhtml", ".empty.bin"]
    .find((candidate) => lowerName.endsWith(candidate));
  const extension = (compoundExtension ?? path.extname(preferredFileName)).slice(0, 20).toLowerCase();
  const sequence = path.basename(preferredFileName).match(/^(\d{4,9})__/)?.[1] ?? "obj";
  for (const candidate of [
    `${sequence}__${digest.slice(0, 24)}${extension}`,
    `${digest.slice(0, 24)}${extension}`,
    `${digest.slice(0, 16)}${extension}`
  ]) {
    const compact = path.join(directory, candidate);
    if (compact.length <= MAX_EVIDENCE_PATH_CHARS) return compact;
  }
  throw new Error(`证据目录自身过长，无法生成兼容路径：${sha256(directory).slice(0, 16)}`);
}

export function redactUrl(rawUrl: string): string {
  try {
    const url = new URL(rawUrl);
    const redacted = new URLSearchParams();
    for (const [key, value] of url.searchParams) {
      redacted.append(key, SECRET_QUERY_KEYS.has(key.toLowerCase())
        ? `<redacted:sha256:${sha256(value).slice(0, 16)}>` : value);
    }
    url.search = redacted.toString();
    return url.href;
  } catch {
    return rawUrl;
  }
}

export function sourceUrlIdentityKey(rawUrl: string): string {
  try {
    const url = new URL(rawUrl.replace(/&amp;/gi, "&"));
    const stableObjectHosts = new Set([
      "cdn.xiaoman.cn",
      "v4client-oss.xiaoman.cn",
      "v4client-oss-new.xiaoman.cn",
      "v4client.oss-cn-hangzhou.aliyuncs.com"
    ]);
    if (stableObjectHosts.has(url.hostname.toLowerCase())) {
      const canonical = new URL(`${url.origin}${url.pathname}`);
      for (const key of ["x-oss-process", "versionId", "versionid"]) {
        const value = url.searchParams.get(key);
        if (value !== null) canonical.searchParams.set(key, value);
      }
      return sha256(canonical.href);
    }
  } catch {
    // Fall through to the redacted exact URL for malformed or non-HTTP values.
  }
  // Preserve existing dedup identities independently of the stronger audit redaction.
  // 对象去重身份与审计脱敏分开，防止脱敏升级改变已保存对象的身份。
  try {
    const legacy = new URL(rawUrl);
    const identitySecretKeys = new Set(["signature", "token", "access_token", "authorization", "auth", "api_key"]);
    for (const key of [...legacy.searchParams.keys()]) {
      if (identitySecretKeys.has(key.toLowerCase())) {
        const value = legacy.searchParams.get(key) ?? "";
        legacy.searchParams.set(key, `<redacted:sha256:${sha256(value).slice(0, 16)}>`);
      }
    }
    return sha256(legacy.href);
  } catch {
    return sha256(rawUrl);
  }
}

export function sanitizeHeaders(headers: Record<string, string>): Record<string, string> {
  const output: Record<string, string> = {};
  for (const [key, value] of Object.entries(headers)) {
    output[key] = SECRET_HEADERS.has(key.toLowerCase())
      ? `<redacted:sha256:${sha256(value).slice(0, 16)}>`
      : value;
  }
  return output;
}

async function ensureDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
}

async function writeAtomic(filePath: string, data: string | Buffer): Promise<void> {
  if (filePath.length > MAX_EVIDENCE_PATH_CHARS) {
    throw new Error(`证据文件路径超过兼容上限 ${MAX_EVIDENCE_PATH_CHARS}：${sha256(filePath).slice(0, 16)}`);
  }
  await ensureDir(path.dirname(filePath));
  const temp = `${filePath}.${crypto.randomUUID().slice(0, 8)}.tmp`;
  if (temp.length > MAX_ATOMIC_PATH_CHARS) {
    throw new Error(`原子写入临时路径超过兼容上限 ${MAX_ATOMIC_PATH_CHARS}：${sha256(temp).slice(0, 16)}`);
  }
  await fs.writeFile(temp, data);
  await fs.rename(temp, filePath);
}

async function appendJsonl(filePath: string, value: unknown): Promise<void> {
  await ensureDir(path.dirname(filePath));
  const previous = JSONL_APPEND_GATES.get(filePath) ?? Promise.resolve();
  let release!: () => void;
  const current = new Promise<void>(resolve => { release = resolve; });
  JSONL_APPEND_GATES.set(filePath, current);
  await previous;
  try {
    await fs.appendFile(filePath, `${encodeJsonlRecord(value)}\n`, "utf8");
  } finally {
    release();
    if (JSONL_APPEND_GATES.get(filePath) === current) JSONL_APPEND_GATES.delete(filePath);
  }
}

function encodeJsonlRecord(value: unknown): string {
  return JSON.stringify(value)
    .replaceAll("\u0085", "\\u0085")
    .replaceAll("\u2028", "\\u2028")
    .replaceAll("\u2029", "\\u2029");
}

function contentExtension(data: Buffer): string | null {
  if (data.subarray(0, 5).toString("ascii") === "%PDF-") return ".pdf";
  if (data.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return ".png";
  if (data[0] === 0xff && data[1] === 0xd8) return ".jpg";
  if (["GIF87a", "GIF89a"].includes(data.subarray(0, 6).toString("ascii"))) return ".gif";
  if (data.subarray(0, 4).toString("ascii") === "RIFF" && data.subarray(8, 12).toString("ascii") === "WEBP") return ".webp";
  if (data.subarray(0, 2).toString("ascii") === "BM") return ".bmp";
  const textPrefix = data.subarray(0, Math.min(data.length, 4096)).toString("utf8").trimStart();
  if (/^(?:<!doctype\s+html|<html|<head|<body)/i.test(textPrefix)) return ".response.rawhtml";
  return null;
}

function extensionFor(mimeType: string, urlPath: string, data?: Buffer): string {
  if (data && data.byteLength === 0) return ".empty.bin";
  if (data?.subarray(0, 8).equals(Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]))) {
    const hinted = path.extname(urlPath).toLowerCase();
    const legacyOffice: Record<string, string> = {
      ".doc": ".doc", ".docx": ".doc",
      ".xls": ".xls", ".xlsx": ".xls",
      ".ppt": ".ppt", ".pptx": ".ppt"
    };
    return legacyOffice[hinted] ?? ".ole";
  }
  if (data?.subarray(0, 6).equals(Buffer.from([0x37, 0x7a, 0xbc, 0xaf, 0x27, 0x1c]))) return ".7z";
  if (data?.subarray(0, 7).toString("binary") === "Rar!\x1a\x07\x00" || data?.subarray(0, 8).toString("binary") === "Rar!\x1a\x07\x01\x00") return ".rar";
  if (data?.subarray(0, 4).equals(Buffer.from([0x50, 0x4b, 0x03, 0x04]))) {
    const hinted = path.extname(urlPath).toLowerCase();
    return [".docx", ".xlsx", ".pptx", ".zip"].includes(hinted) ? hinted : ".zip";
  }
  const detected = data ? contentExtension(data) : null;
  if (detected) return detected;
  const mime = mimeType.split(";")[0]?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    "application/json": ".json",
    "text/html": ".response.rawhtml",
    "text/plain": ".txt",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/zip": ".zip"
  };
  if (map[mime]) return map[mime];
  const pathExt = path.extname(urlPath).slice(0, 12);
  if (pathExt && /^\.[a-z0-9]{1,10}$/i.test(pathExt)) return pathExt.toLowerCase();
  return ".bin";
}

function escapeHtml(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

export function offlinePageHtml(label: string, url: string, text: string, screenshot: Buffer): string {
  const extension = contentExtension(screenshot);
  const imageMime = extension === ".jpg" ? "image/jpeg"
    : extension === ".webp" ? "image/webp"
      : extension === ".gif" ? "image/gif"
        : "image/png";
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${escapeHtml(label)} - 离线证据页</title>
  <style>
    body{margin:0;background:#101418;color:#e8eef3;font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
    main{max-width:1500px;margin:auto;padding:24px}.card{background:#182028;border:1px solid #2d3a45;border-radius:12px;padding:18px;margin-bottom:18px}
    h1{margin:0 0 8px;font-size:20px}.meta{color:#9fb0bf;overflow-wrap:anywhere}.shot{width:100%;height:auto;border:1px solid #344552;border-radius:8px;background:white}
    pre{white-space:pre-wrap;word-break:break-word;margin:0;color:#dce8f0}.notice{color:#ffd17a}
  </style>
</head>
<body><main>
  <section class="card"><h1>${escapeHtml(label)}</h1><div class="meta">${escapeHtml(url)}</div><p class="notice">这是自包含离线证据页，不加载原站脚本、CSS或网络资源；原始DOM另行保存在同一会话中。</p></section>
  <section class="card"><img class="shot" alt="页面截图" src="data:${imageMime};base64,${screenshot.toString("base64")}"></section>
  <section class="card"><pre>${escapeHtml(text)}</pre></section>
</main></body></html>`;
}

function htmlVisibleText(html: string): string {
  return html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style\s*>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/\s+/g, " ")
    .trim();
}

export function offlineResponseHtml(url: string, status: number, mimeType: string, rawHtml: Buffer): string {
  const source = rawHtml.toString("utf8");
  const visible = htmlVisibleText(source);
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>离线 HTML 响应</title><style>
body{margin:0;background:#101418;color:#e8eef3;font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1300px;margin:auto;padding:24px}
.card{background:#182028;border:1px solid #2d3a45;border-radius:12px;padding:18px;margin-bottom:18px}.meta{color:#9fb0bf;overflow-wrap:anywhere}.notice{color:#ffd17a}
pre{white-space:pre-wrap;word-break:break-word;margin:0}details summary{cursor:pointer;color:#75c7ff}</style></head><body><main>
<section class="card"><h1>离线 HTML 响应</h1><div class="meta">HTTP ${status} · ${escapeHtml(mimeType)} · ${escapeHtml(redactUrl(url))}</div>
<p class="notice">此查看页不执行脚本、不加载原站资源。原始响应字节另存为 .response.rawhtml 并由清单哈希校验。</p></section>
<section class="card"><pre>${escapeHtml(visible)}</pre></section>
<section class="card"><details><summary>查看转义后的原始 HTML</summary><pre>${escapeHtml(source)}</pre></details></section>
</main></body></html>`;
}

interface HashIndexEntry {
  schema: 1;
  sha256: string;
  relative_path: string;
  size: number;
  first_indexed_at: string;
}

interface SourceIndexEntry {
  schema: 1;
  source_url_sha256: string;
  source_url: string;
  sha256: string;
  relative_path: string;
  size: number;
  http_status: number;
  mime_type: string;
  indexed_at: string;
}

export interface DedupStats {
  hash_index_entries: number;
  source_url_index_entries: number;
  objects_written: number;
  objects_reused: number;
  bytes_written: number;
  bytes_avoided: number;
  downloads_skipped: number;
  download_bytes_avoided: number;
}

const WINDOWS_NAME_REPLACEMENTS: Record<string, string> = {
  "<": "＜", ">": "＞", ":": "：", "\"": "＂", "/": "／",
  "\\": "＼", "|": "｜", "?": "？", "*": "＊"
};

export function sanitizeCaseFolderName(customerName: string, companyId: string): string {
  let value = customerName.normalize("NFC")
    .replace(/[<>:"/\\|?*]/g, character => WINDOWS_NAME_REPLACEMENTS[character] ?? "_")
    .replace(/[\u0000-\u001f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[. ]+$/g, "");
  if (!value) value = `未命名客户__${companyId}`;
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(value)) value = `_${value}`;
  return value.slice(0, 100).replace(/[. ]+$/g, "") || `未命名客户__${companyId}`;
}

export function caseDirectoryNameKey(value: string): string {
  return String(value)
    .normalize("NFKC")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[. ]+$/g, "")
    .toLocaleLowerCase("en-US");
}

export function candidateCaseDirectoryKeys(customerName: string, companyId: string): Set<string> {
  const preferred = sanitizeCaseFolderName(customerName, companyId);
  return new Set([
    caseDirectoryNameKey(customerName),
    caseDirectoryNameKey(preferred),
    caseDirectoryNameKey(sanitizeCaseFolderName(`${preferred}__${companyId}`, companyId)),
    caseDirectoryNameKey(`company_${companyId}`)
  ].filter(Boolean));
}

async function readCaseIdentity(directory: string): Promise<{ company_id?: string } | null> {
  try {
    return JSON.parse(await fs.readFile(path.join(directory, "case_identity.json"), "utf8")) as { company_id?: string };
  } catch {
    return null;
  }
}

interface CaseIdentityRecord {
  schema: 1;
  company_id: string;
  customer_name: string;
  folder_name: string;
  root_url: string;
  adapter_id: string;
  created_at: string;
}

/**
 * Atomically establishes the immutable owner of a case directory.
 *
 * The fully written temporary file is hard-linked into place, so another
 * process can either observe the complete identity or an existing identity;
 * it never observes a partially written file. Existing same-customer cases
 * are reused without rewriting their identity, while foreign or corrupt
 * identities fail closed.
 * 完整临时身份文件通过原子硬链接发布；同客户可复用，不同或损坏身份必须拒绝。
 */
async function establishCaseIdentity(filePath: string, identity: CaseIdentityRecord): Promise<void> {
  await ensureDir(path.dirname(filePath));
  const temporary = `${filePath}.${process.pid}.${crypto.randomUUID().slice(0, 8)}.claim`;
  const handle = await fs.open(temporary, "wx");
  try {
    await handle.writeFile(`${JSON.stringify(identity, null, 2)}\n`, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }

  try {
    await fs.link(temporary, filePath);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    let existing: { company_id?: unknown };
    try {
      existing = JSON.parse(await fs.readFile(filePath, "utf8")) as { company_id?: unknown };
    } catch {
      throw new Error("客户案例身份文件已存在但无法验证，拒绝覆盖");
    }
    if (existing.company_id !== identity.company_id) {
      throw new Error("客户案例目录已由其他企业身份占用，拒绝混写");
    }
  } finally {
    await fs.rm(temporary, { force: true }).catch(() => undefined);
  }
}

export async function resolveCaseFolderName(workspaceRoot: string, companyId: string, customerName: string): Promise<string> {
  const casesRoot = path.join(workspaceRoot, "cases");
  await ensureDir(casesRoot);
  const preferred = sanitizeCaseFolderName(customerName, companyId);
  const collisionName = sanitizeCaseFolderName(`${preferred}__${companyId}`, companyId);
  let preferredCollision = false;
  for (const candidate of [preferred, collisionName, `company_${companyId}`]) {
    const candidatePath = path.join(casesRoot, candidate);
    try {
      const stat = await fs.stat(candidatePath);
      if (!stat.isDirectory()) continue;
      const identity = await readCaseIdentity(candidatePath);
      if (identity?.company_id === companyId) return candidate;
      if (candidate === preferred) preferredCollision = true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }

  // The shared inventory turns a legacy-name lookup from thousands of file
  // reads into one small local JSON lookup. The exact identity is still
  // revalidated before the cached directory is trusted.
  try {
    const inventoryPath = path.join(workspaceRoot, "crm_single_customer_capture", "app", "runtime", "case-inventory-v1.json");
    const snapshot = JSON.parse(await fs.readFile(inventoryPath, "utf8")) as {
      records?: Record<string, { company_id?: unknown }>;
    };
    for (const [directoryName, record] of Object.entries(snapshot.records ?? {})) {
      if (record.company_id !== companyId) continue;
      const identity = await readCaseIdentity(path.join(casesRoot, directoryName));
      if (identity?.company_id === companyId) return directoryName;
    }
  } catch { /* cache is optional; use the compatibility fallback */ }

  const directories = (await fs.readdir(casesRoot, { withFileTypes: true })).filter(entry => entry.isDirectory());
  let cursor = 0;
  let matched: string | null = null;
  await Promise.all(Array.from({ length: Math.min(64, Math.max(1, directories.length)) }, async () => {
    for (;;) {
      if (matched) return;
      const index = cursor++;
      if (index >= directories.length) return;
      const entry = directories[index]!;
      const identity = await readCaseIdentity(path.join(casesRoot, entry.name));
      if (identity?.company_id === companyId) {
        matched = entry.name;
        return;
      }
    }
  }));
  return matched ?? (preferredCollision ? collisionName : preferred);
}

async function readJsonl(filePath: string): Promise<Record<string, unknown>[]> {
  try {
    const content = await fs.readFile(filePath, "utf8");
    return content.split(/\r?\n/).filter(Boolean).flatMap(line => {
      try {
        const value = JSON.parse(line) as unknown;
        return value && typeof value === "object" ? [value as Record<string, unknown>] : [];
      } catch {
        return [];
      }
    });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
}

export class EvidenceStore {
  readonly caseRoot: string;
  readonly sessionId: string;
  readonly sessionRoot: string;
  readonly rawObjectsDir: string;
  readonly pagesDir: string;
  readonly screenshotsDir: string;
  readonly networkDir: string;
  readonly manifestsDir: string;
  readonly verificationDir: string;
  readonly workDir: string;
  readonly logsDir: string;
  private sequence = 0;
  private hashIndex = new Map<string, HashIndexEntry>();
  private sourceIndex = new Map<string, SourceIndexEntry>();
  private validatedObjects = new Set<string>();
  private mutationGate: Promise<void> = Promise.resolve();
  private stats = {
    objects_written: 0,
    objects_reused: 0,
    bytes_written: 0,
    bytes_avoided: 0,
    downloads_skipped: 0,
    download_bytes_avoided: 0
  };

  constructor(
    readonly workspaceRoot: string,
    readonly companyId: string,
    readonly rootUrl: string,
    readonly customerName = `company_${companyId}`,
    readonly caseFolderName = sanitizeCaseFolderName(customerName, companyId)
  ) {
    this.caseRoot = path.join(workspaceRoot, "cases", caseFolderName);
    // A seconds-only directory can collide when two collector instances begin
    // repairing the same case in the same second. Keep the sortable timestamp,
    // but add process and random entropy so append-only snapshots never share
    // a physical path across EvidenceStore instances.
    // 时间戳附加进程与随机量，避免同秒修复任务写入同一物理会话目录。
    this.sessionId = `capture_${stamp()}_${process.pid}_${crypto.randomBytes(3).toString("hex")}`;
    this.sessionRoot = path.join(this.caseRoot, "raw", "sessions", this.sessionId);
    this.rawObjectsDir = path.join(this.sessionRoot, "objects");
    this.pagesDir = path.join(this.sessionRoot, "pages");
    this.screenshotsDir = path.join(this.sessionRoot, "screenshots");
    this.networkDir = path.join(this.sessionRoot, "network");
    this.manifestsDir = path.join(this.caseRoot, "manifests");
    this.verificationDir = path.join(this.caseRoot, "verification");
    this.workDir = path.join(this.caseRoot, "work");
    this.logsDir = path.join(this.caseRoot, "logs");
  }

  async initialize(adapterId: string): Promise<void> {
    await ensureDir(this.caseRoot);
    await hideDirectoryOnWindows(this.caseRoot);
    await establishCaseIdentity(path.join(this.caseRoot, "case_identity.json"), {
      schema: 1,
      company_id: this.companyId,
      customer_name: this.customerName,
      folder_name: this.caseFolderName,
      root_url: this.rootUrl,
      adapter_id: adapterId,
      created_at: nowIso()
    });
    await Promise.all([
      ensureDir(this.rawObjectsDir), ensureDir(this.pagesDir), ensureDir(this.screenshotsDir),
      ensureDir(this.networkDir), ensureDir(this.manifestsDir), ensureDir(this.verificationDir),
      ensureDir(this.workDir), ensureDir(this.logsDir)
    ]);
    await this.loadIndexes();
    await writeAtomic(path.join(this.sessionRoot, "session.json"), JSON.stringify({
      schema: 2,
      session_id: this.sessionId,
      company_id: this.companyId,
      root_url: this.rootUrl,
      adapter_id: adapterId,
      started_at: nowIso(),
      privacy: {
        external_ai_upload: "forbidden_and_blocked",
        page_native_upstream: "allowed_and_audited",
        storage: "local_only",
        raw_evidence: "append_only"
      }
    }, null, 2));
  }

  private async exclusive<T>(action: () => Promise<T>): Promise<T> {
    const previous = this.mutationGate;
    let release!: () => void;
    this.mutationGate = new Promise<void>(resolve => { release = resolve; });
    await previous;
    try {
      return await action();
    } finally {
      release();
    }
  }

  private absolute(relativePath: string): string {
    const resolved = path.resolve(this.caseRoot, relativePath);
    const root = path.resolve(this.caseRoot);
    if (resolved !== root && !resolved.startsWith(`${root}${path.sep}`)) {
      throw new Error("索引路径越出客户证据目录");
    }
    return resolved;
  }

  private sourceKey(rawUrl: string): string {
    return sourceUrlIdentityKey(rawUrl);
  }

  private async loadIndexes(): Promise<void> {
    const objectsPath = path.join(this.manifestsDir, "objects.jsonl");
    const hashPath = path.join(this.manifestsDir, "sha256_index.jsonl");
    const sourcePath = path.join(this.manifestsDir, "source_url_index.jsonl");
    const objectRows = await readJsonl(objectsPath);
    for (const row of objectRows) {
      const digest = typeof row.sha256 === "string" ? row.sha256 : null;
      const relativePath = typeof row.relative_path === "string" ? row.relative_path : null;
      const size = typeof row.size === "number" ? row.size : null;
      if (digest && relativePath && size !== null && !this.hashIndex.has(digest)) {
        this.hashIndex.set(digest, {
          schema: 1,
          sha256: digest,
          relative_path: relativePath,
          size,
          first_indexed_at: typeof row.recorded_at === "string" ? row.recorded_at : nowIso()
        });
      }
      if (digest && relativePath && size !== null && typeof row.source_url === "string" && typeof row.http_status === "number") {
        const key = this.sourceKey(row.source_url);
        this.sourceIndex.set(key, {
          schema: 1,
          source_url_sha256: key,
          source_url: row.source_url,
          sha256: digest,
          relative_path: relativePath,
          size,
          http_status: row.http_status,
          mime_type: typeof row.mime_type === "string" ? row.mime_type : "application/octet-stream",
          indexed_at: typeof row.recorded_at === "string" ? row.recorded_at : nowIso()
        });
      }
    }

    const hashRows = await readJsonl(hashPath);
    for (const row of hashRows) {
      if (typeof row.sha256 === "string" && typeof row.relative_path === "string" && typeof row.size === "number") {
        this.hashIndex.set(row.sha256, row as unknown as HashIndexEntry);
      }
    }
    const sourceRows = await readJsonl(sourcePath);
    for (const row of sourceRows) {
      if (typeof row.source_url_sha256 === "string" && typeof row.relative_path === "string" && typeof row.sha256 === "string") {
        this.sourceIndex.set(row.source_url_sha256, row as unknown as SourceIndexEntry);
      }
    }

    if (hashRows.length === 0 && this.hashIndex.size) {
      for (const entry of this.hashIndex.values()) await appendJsonl(hashPath, entry);
    }
    if (sourceRows.length === 0 && this.sourceIndex.size) {
      for (const entry of this.sourceIndex.values()) await appendJsonl(sourcePath, entry);
    }
  }

  private async validIndexedObject(entry: HashIndexEntry | SourceIndexEntry, verifyHash = false): Promise<boolean> {
    const cacheKey = `${entry.relative_path}|${entry.sha256}`;
    if (this.validatedObjects.has(cacheKey)) return true;
    try {
      const absolute = this.absolute(entry.relative_path);
      const stat = await fs.stat(absolute);
      if (!stat.isFile() || stat.size !== entry.size) return false;
      if (verifyHash && sha256(await fs.readFile(absolute)) !== entry.sha256) return false;
      this.validatedObjects.add(cacheKey);
      return true;
    } catch {
      return false;
    }
  }

  private async persistHashedObject(data: Buffer, preferredPath: string): Promise<{ relativePath: string; reused: boolean }> {
    const digest = sha256(data);
    return this.exclusive(async () => {
      const existing = this.hashIndex.get(digest);
      if (existing && await this.validIndexedObject(existing)) {
        this.stats.objects_reused += 1;
        this.stats.bytes_avoided += data.byteLength;
        return { relativePath: existing.relative_path, reused: true };
      }
      await writeAtomic(preferredPath, data);
      const relativePath = path.relative(this.caseRoot, preferredPath);
      const entry: HashIndexEntry = {
        schema: 1,
        sha256: digest,
        relative_path: relativePath,
        size: data.byteLength,
        first_indexed_at: nowIso()
      };
      this.hashIndex.set(digest, entry);
      this.validatedObjects.add(`${relativePath}|${digest}`);
      await appendJsonl(path.join(this.manifestsDir, "sha256_index.jsonl"), entry);
      this.stats.objects_written += 1;
      this.stats.bytes_written += data.byteLength;
      return { relativePath, reused: false };
    });
  }

  private async indexWrittenSnapshot(filePath: string, data: Buffer): Promise<string> {
    const digest = sha256(data);
    const relativePath = path.relative(this.caseRoot, filePath);
    await this.exclusive(async () => {
      const existing = this.hashIndex.get(digest);
      if (!existing || !await this.validIndexedObject(existing)) {
        const entry: HashIndexEntry = {
          schema: 1,
          sha256: digest,
          relative_path: relativePath,
          size: data.byteLength,
          first_indexed_at: nowIso()
        };
        this.hashIndex.set(digest, entry);
        this.validatedObjects.add(`${relativePath}|${digest}`);
        await appendJsonl(path.join(this.manifestsDir, "sha256_index.jsonl"), entry);
      }
      this.stats.objects_written += 1;
      this.stats.bytes_written += data.byteLength;
    });
    return digest;
  }

  private async indexSource(rawUrl: string, digest: string, relativePath: string, size: number, status: number, mimeType: string): Promise<void> {
    if (status < 200 || status >= 400 || size <= 0) return;
    await this.exclusive(async () => {
      const key = this.sourceKey(rawUrl);
      const current = this.sourceIndex.get(key);
      if (current?.sha256 === digest && current.relative_path === relativePath) return;
      const entry: SourceIndexEntry = {
        schema: 1,
        source_url_sha256: key,
        source_url: redactUrl(rawUrl),
        sha256: digest,
        relative_path: relativePath,
        size,
        http_status: status,
        mime_type: mimeType,
        indexed_at: nowIso()
      };
      this.sourceIndex.set(key, entry);
      await appendJsonl(path.join(this.manifestsDir, "source_url_index.jsonl"), entry);
    });
  }

  dedupStats(): DedupStats {
    return {
      hash_index_entries: this.hashIndex.size,
      source_url_index_entries: this.sourceIndex.size,
      ...this.stats
    };
  }

  private async compactIndexes(): Promise<void> {
    await this.exclusive(async () => {
      const encode = (rows: Iterable<HashIndexEntry | SourceIndexEntry>): string => {
        const lines = [...rows].map(row => encodeJsonlRecord(row));
        return lines.length ? `${lines.join("\n")}\n` : "";
      };
      await Promise.all([
        writeAtomic(path.join(this.manifestsDir, "sha256_index.jsonl"), encode(this.hashIndex.values())),
        writeAtomic(path.join(this.manifestsDir, "source_url_index.jsonl"), encode(this.sourceIndex.values()))
      ]);
    });
  }

  async writeDedupSummary(): Promise<void> {
    // Indexes are derivative maintenance data, not immutable raw evidence. Compact them
    // at the end so every in-memory/backfilled key is persisted exactly once.
    await this.compactIndexes();
    await this.writeJson(path.join("manifests", "dedup_summary_latest.json"), {
      schema: 1,
      generated_at: nowIso(),
      session_id: this.sessionId,
      ...this.dedupStats(),
      hash_index: path.join("manifests", "sha256_index.jsonl"),
      source_url_index: path.join("manifests", "source_url_index.jsonl")
    });
  }

  async reuseResponseByUrl(input: { sequence: number; method: string; url: string; resourceType: string; parseJson?: boolean }): Promise<StoredResponse | null> {
    const entry = this.sourceIndex.get(this.sourceKey(input.url));
    if (!entry
      || entry.http_status < 200
      || entry.http_status >= 300
      || entry.size <= 0
      || !await this.validIndexedObject(entry, true)) return null;
    this.stats.downloads_skipped += 1;
    this.stats.download_bytes_avoided += entry.size;
    await appendJsonl(path.join(this.manifestsDir, "objects.jsonl"), {
      schema: 2,
      recorded_at: nowIso(),
      session_id: this.sessionId,
      sequence: input.sequence,
      relative_path: entry.relative_path,
      size: entry.size,
      sha256: entry.sha256,
      object_type: input.resourceType,
      source_url: redactUrl(input.url),
      source_url_sha256: this.sourceKey(input.url),
      http_status: entry.http_status,
      mime_type: entry.mime_type,
      reused_existing: true,
      network_download_skipped: true
    });
    const parsed = new URL(input.url);
    let json: unknown | null = null;
    if (input.parseJson) {
      try { json = JSON.parse(await fs.readFile(this.absolute(entry.relative_path), "utf8")); } catch { json = null; }
    }
    const record: StoredResponse = {
      sequence: input.sequence,
      method: input.method,
      url: redactUrl(input.url),
      path: parsed.pathname,
      status: entry.http_status,
      resourceType: input.resourceType,
      mimeType: entry.mime_type,
      bodyRelativePath: entry.relative_path,
      bodySha256: entry.sha256,
      bodyBytes: entry.size,
      json,
      error: null
    };
    await appendJsonl(path.join(this.networkDir, "responses.jsonl"), {
      ...record,
      reused_existing: true,
      network_download_skipped: true
    });
    return record;
  }

  nextSequence(): number {
    this.sequence += 1;
    return this.sequence;
  }

  async appendRequest(row: unknown): Promise<void> {
    await appendJsonl(path.join(this.caseRoot, "request_ledger.jsonl"), row);
  }

  async appendScope(row: unknown): Promise<void> {
    await appendJsonl(path.join(this.caseRoot, "scope_ledger.jsonl"), row);
  }

  async appendError(row: unknown): Promise<void> {
    await appendJsonl(path.join(this.logsDir, "capture_errors.jsonl"), row);
  }

  async appendEvent(row: unknown): Promise<void> {
    await appendJsonl(path.join(this.networkDir, "events.jsonl"), row);
  }

  async storeRequestBody(sequence: number, method: string, url: string, body: Buffer): Promise<string> {
    const parsed = new URL(url);
    const digest = sha256(body);
    const fileName = `${String(sequence).padStart(7, "0")}__${safeName(method)}__${safeName(parsed.pathname.replaceAll("/", "__"), 140)}__${digest.slice(0, 16)}.bin`;
    const requestBodyDir = path.join(this.sessionRoot, "request_bodies");
    const filePath = compatibleEvidencePath(requestBodyDir, fileName, digest);
    const persisted = await this.persistHashedObject(body, filePath);
    const relative = persisted.relativePath;
    await appendJsonl(path.join(this.manifestsDir, "objects.jsonl"), {
      schema: 2,
      recorded_at: nowIso(),
      session_id: this.sessionId,
      sequence,
      relative_path: relative,
      size: body.byteLength,
      sha256: digest,
      object_type: "request_body",
      source_url: redactUrl(url),
      method,
      reused_existing: persisted.reused
    });
    return relative;
  }

  async storeResponse(input: {
    sequence: number;
    method: string;
    url: string;
    status: number;
    resourceType: string;
    mimeType: string;
    body: Buffer | null;
    error: string | null;
    requestBodySha256?: string | null;
    responseHeaders?: Record<string, string>;
    statusText?: string;
    serverAddress?: { ipAddress: string; port: number } | null;
    securityDetails?: Record<string, unknown> | null;
    timing?: Record<string, number>;
    fromServiceWorker?: boolean;
  }): Promise<StoredResponse> {
    const parsed = new URL(input.url);
    let bodyRelativePath: string | null = null;
    let bodySha256: string | null = null;
    let bodyBytes: number | null = null;
    let json: unknown | null = null;
    if (input.body) {
      bodySha256 = sha256(input.body);
      bodyBytes = input.body.byteLength;
      const ext = extensionFor(input.mimeType, parsed.pathname, input.body);
      const host = safeName(parsed.hostname, 80);
      const endpoint = safeName(parsed.pathname.replace(/^\/+/, "").replaceAll("/", "__"), 150);
      const statusLabel = input.status >= 400 ? `__http${input.status}` : "";
      const fileName = `${String(input.sequence).padStart(7, "0")}__${host}__${endpoint}${statusLabel}__${bodySha256.slice(0, 16)}${ext}`;
      const filePath = compatibleEvidencePath(this.rawObjectsDir, fileName, bodySha256);
      const persisted = await this.persistHashedObject(input.body, filePath);
      bodyRelativePath = persisted.relativePath;
      if (input.mimeType.includes("json") || ext === ".json") {
        try { json = JSON.parse(input.body.toString("utf8")); } catch { json = null; }
      }
      await appendJsonl(path.join(this.manifestsDir, "objects.jsonl"), {
        schema: 2,
        recorded_at: nowIso(),
        session_id: this.sessionId,
        sequence: input.sequence,
        relative_path: bodyRelativePath,
        size: bodyBytes,
        sha256: bodySha256,
        object_type: input.resourceType,
        source_url: redactUrl(input.url),
        source_url_sha256: this.sourceKey(input.url),
        http_status: input.status,
        mime_type: input.mimeType,
        reused_existing: persisted.reused
      });
      if (ext === ".response.rawhtml") {
        const viewerData = Buffer.from(offlineResponseHtml(input.url, input.status, input.mimeType, input.body));
        const viewerName = fileName.slice(0, -ext.length) + ".html";
        const viewerPath = compatibleEvidencePath(this.rawObjectsDir, viewerName, sha256(viewerData));
        const viewerPersisted = await this.persistHashedObject(viewerData, viewerPath);
        await appendJsonl(path.join(this.manifestsDir, "objects.jsonl"), {
          schema: 2,
          recorded_at: nowIso(),
          session_id: this.sessionId,
          sequence: input.sequence,
          relative_path: viewerPersisted.relativePath,
          size: viewerData.byteLength,
          sha256: sha256(viewerData),
          object_type: "offline_response_viewer",
          source_url: redactUrl(input.url),
          http_status: input.status,
          mime_type: "text/html",
          reused_existing: viewerPersisted.reused
        });
      }
      // Never let an expired signed URL's 4xx/5xx body poison the reusable
      // source cache for the same logical attachment.
      // 过期签名产生的错误响应不能污染同一附件的成功缓存。
      if (!input.error && input.status >= 200 && input.status < 300) {
        await this.indexSource(input.url, bodySha256, bodyRelativePath, bodyBytes, input.status, input.mimeType);
      }
    }
    const record: StoredResponse = {
      sequence: input.sequence,
      method: input.method,
      url: redactUrl(input.url),
      path: parsed.pathname,
      status: input.status,
      resourceType: input.resourceType,
      mimeType: input.mimeType,
      bodyRelativePath,
      bodySha256,
      bodyBytes,
      requestBodySha256: input.requestBodySha256 ?? null,
      json,
      error: input.error,
      responseHeaders: input.responseHeaders,
      statusText: input.statusText,
      serverAddress: input.serverAddress,
      securityDetails: input.securityDetails,
      timing: input.timing,
      fromServiceWorker: input.fromServiceWorker
    };
    await appendJsonl(path.join(this.networkDir, "responses.jsonl"), record);
    return record;
  }

  async storePage(label: string, url: string, html: string, text: string, screenshot: Buffer, mhtml: string | null = null): Promise<void> {
    const base = `${String(this.nextSequence()).padStart(7, "0")}__${safeName(label)}`;
    const htmlData = Buffer.from(html);
    const textData = Buffer.from(text);
    const viewerData = Buffer.from(offlinePageHtml(label, url, text, screenshot));
    const htmlPath = compatibleEvidencePath(this.pagesDir, `${base}.dom.rawhtml`, sha256(htmlData));
    const viewerPath = compatibleEvidencePath(this.pagesDir, `${base}.html`, sha256(viewerData));
    const textPath = compatibleEvidencePath(this.pagesDir, `${base}.txt`, sha256(textData));
    const screenshotExtension = contentExtension(screenshot) ?? ".bin";
    const shotPath = compatibleEvidencePath(this.screenshotsDir, `${base}${screenshotExtension}`, sha256(screenshot));
    const pageObjects: Array<readonly [string, string, Buffer]> = [
      [htmlPath, "dom_html", htmlData],
      [viewerPath, "offline_viewer", viewerData],
      [textPath, "visible_text", textData],
      [shotPath, "screenshot", screenshot]
    ];
    if (mhtml !== null) {
      const mhtmlData = Buffer.from(mhtml, "utf8");
      const mhtmlPath = compatibleEvidencePath(this.pagesDir, `${base}.mhtml`, sha256(mhtmlData));
      pageObjects.push([mhtmlPath, "mhtml_snapshot", mhtmlData]);
    }
    await Promise.all(pageObjects.map(([filePath, , data]) => writeAtomic(filePath, data)));
    for (const [filePath, objectType, data] of pageObjects) {
      const digest = await this.indexWrittenSnapshot(filePath, data);
      await appendJsonl(path.join(this.manifestsDir, "objects.jsonl"), {
        schema: 2,
        recorded_at: nowIso(),
        session_id: this.sessionId,
        relative_path: path.relative(this.caseRoot, filePath),
        size: data.byteLength,
        sha256: digest,
        object_type: objectType,
        source_url: redactUrl(url),
        source_label: label
      });
    }
  }

  async writeJson(relativePath: string, value: unknown): Promise<string> {
    const filePath = path.join(this.caseRoot, relativePath);
    await writeAtomic(filePath, JSON.stringify(value, null, 2));
    return filePath;
  }

  async writeState(value: unknown): Promise<void> {
    await this.writeJson(path.join("work", "capture_state.json"), value);
  }
}
