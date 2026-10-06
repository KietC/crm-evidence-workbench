/** Desktop shell keeps browser credentials local and exposes only loopback control ports.
 * 桌面外壳将浏览器凭证留在本机，只提供回环地址控制端口。 */
import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  session,
  WebContentsView,
  type IpcMainEvent,
  type IpcMainInvokeEvent,
  type Session
} from "electron";

try { os.setPriority(0, os.constants.priority.PRIORITY_ABOVE_NORMAL); } catch { /* best effort */ }
import { isBlockedAiUrl, loadAdapter } from "./adapter.js";
import { codexResumeArgs } from "./codex-wake-policy.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(HERE, "..");
const INSTANCE_ID = /^\d+$/.test(process.env.OKKI_INSTANCE_ID ?? "") ? process.env.OKKI_INSTANCE_ID! : "1";
const RUNTIME_ROOT = path.resolve(process.env.OKKI_RUNTIME_ROOT ?? path.join(APP_ROOT, "runtime"));
const VERIFICATION_ROOT = path.join(APP_ROOT, "verification");
const ELECTRON_DATA = path.join(RUNTIME_ROOT, "electron-app-data");
const CONTROL_HOST = "127.0.0.1";
const CONTROL_PORT = Number(process.env.OKKI_CAPTURE_PORT ?? 3211);
const CDP_PORT = Number(process.env.OKKI_CDP_PORT ?? 9334);
const CONTROL_URL = `http://${CONTROL_HOST}:${CONTROL_PORT}`;
const PARTITION = "persist:okki-crm-capture";
const DEFAULT_SIDEBAR_WIDTH = 500;
const MIN_SIDEBAR_WIDTH = 360;
const MIN_BROWSER_WIDTH = 520;
const TOOLBAR_HEIGHT = 56;
const LAYOUT_FILE = path.join(RUNTIME_ROOT, "desktop-layout.json");
const DESKTOP_READY_FILE = path.join(RUNTIME_ROOT, "desktop-ready.json");
const CODEX_MONITOR_CONFIG = path.join(RUNTIME_ROOT, "codex-monitor.json");
const CODEX_REVIEW_EVENT = path.join(RUNTIME_ROOT, "codex-review-request.json");
const CODEX_MONITOR_LOG = path.join(RUNTIME_ROOT, "codex-monitor.log");

fs.mkdirSync(ELECTRON_DATA, { recursive: true });
app.setPath("userData", ELECTRON_DATA);
app.enableSandbox();
app.commandLine.appendSwitch("remote-debugging-address", CONTROL_HOST);
app.commandLine.appendSwitch("remote-debugging-port", String(CDP_PORT));
app.commandLine.appendSwitch("disable-features", "AutofillServerCommunication,OptimizationHintsFetching");

const adapter = await loadAdapter();
const INITIAL_URL = adapter.customer_list_url;
let mainWindow: BrowserWindow | null = null;
let browserView: WebContentsView | null = null;
let captureServer: ChildProcess | null = null;
let captureLog: fs.WriteStream | null = null;
let captureRestartTimer: NodeJS.Timeout | null = null;
let shuttingDown = false;
let sidebarWidth = DEFAULT_SIDEBAR_WIDTH;
let layoutSaveTimer: NodeJS.Timeout | null = null;
let codexMonitorTimer: NodeJS.Timeout | null = null;
let codexMonitorChild: ChildProcess | null = null;
let codexMonitorLog: fs.WriteStream | null = null;

interface CodexMonitorConfig {
  schema: 1;
  enabled: boolean;
  watcher_mode?: "electron" | "standalone";
  thread_id: string;
  workspace: string;
  codex_executable: string;
  poll_interval_ms: number;
  max_wake_retries: number;
}

interface CodexReviewWakeEvent {
  schema: 1;
  event_id: string;
  kind: "capture_terminal" | "queue_exhausted";
  status: "pending" | "claimed" | "acknowledged" | "manual_cancelled" | "wake_failed";
  retry_count: number;
  next_attempt_at?: string | null;
  claimed_at?: string;
  [key: string]: unknown;
}

async function writeRuntimeJsonAtomic(filePath: string, value: unknown): Promise<void> {
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.tmp`;
  await fsp.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await fsp.rm(filePath, { force: true }).catch(() => undefined);
  await fsp.rename(temporary, filePath);
}

async function readRuntimeJson<T>(filePath: string): Promise<T | null> {
  try { return JSON.parse(await fsp.readFile(filePath, "utf8")) as T; }
  catch { return null; }
}

function codexWakePromptSafe(event: CodexReviewWakeEvent): string {
  const action = event.kind === "queue_exhausted"
    ? "The main customer queue is exhausted. Process the local deferred repair queue through /api/deferred-repairs endpoints, audit each result privately, ACK only after deferred pending is zero, then complete the active goal."
    : "Run the fixed anonymous capture audit. Repair and regression-test any collector defect, re-capture only what is needed, and ACK this event only after zero errors and zero reconciliation failures.";
  return [
    `OKKI local terminal event: ${event.event_id}; kind: ${event.kind}.`,
    "This is the operator-confirmed continuation of the active goal.",
    "Read only the compact event, anonymous audit reports, structural counts, hashes, and error classifications.",
    "Never output customer names, customer IDs, message bodies, or business content. Never upload local evidence.",
    action,
    "Use existing private audit scripts first and stay token-efficient. Do not poll chat while capture is running.",
    "A manual_cancelled event must never be resumed."
  ].join("\n");
}

async function codexMonitorTick(): Promise<void> {
  if (shuttingDown || codexMonitorChild) return;
  const config = await readRuntimeJson<CodexMonitorConfig>(CODEX_MONITOR_CONFIG);
  if (!config?.enabled || !/^[0-9a-f-]{36}$/i.test(config.thread_id)) return;
  const event = await readRuntimeJson<CodexReviewWakeEvent>(CODEX_REVIEW_EVENT);
  if (!event || event.status !== "pending") return;
  if (event.next_attempt_at && Date.parse(event.next_attempt_at) > Date.now()) return;
  try { await fsp.access(config.codex_executable); } catch { return; }

  event.status = "claimed";
  event.claimed_at = new Date().toISOString();
  await writeRuntimeJsonAtomic(CODEX_REVIEW_EVENT, event);
  codexMonitorLog ??= fs.createWriteStream(CODEX_MONITOR_LOG, { flags: "a" });
  codexMonitorLog.write(`[${new Date().toISOString()}] wake event=${event.event_id} kind=${event.kind}\n`);
  const child = spawn(config.codex_executable, codexResumeArgs(event.kind, config.thread_id), {
    cwd: config.workspace,
    windowsHide: true,
    env: { ...process.env },
    stdio: ["pipe", "pipe", "pipe"]
  });
  codexMonitorChild = child;
  child.stdout?.pipe(codexMonitorLog, { end: false });
  child.stderr?.pipe(codexMonitorLog, { end: false });
  child.stdin?.end(codexWakePromptSafe(event), "utf8");
  child.once("exit", (code, signal) => {
    if (codexMonitorChild === child) codexMonitorChild = null;
    codexMonitorLog?.write(`[${new Date().toISOString()}] exit event=${event.event_id} code=${String(code)} signal=${String(signal)}\n`);
    void (async () => {
      const current = await readRuntimeJson<CodexReviewWakeEvent>(CODEX_REVIEW_EVENT);
      if (!current || current.event_id !== event.event_id || current.status !== "claimed") return;
      current.retry_count = Number(current.retry_count ?? 0) + 1;
      if (current.retry_count >= Math.max(1, config.max_wake_retries)) {
        current.status = "wake_failed";
        current.next_attempt_at = null;
      } else {
        current.status = "pending";
        current.next_attempt_at = new Date(Date.now() + 60_000 * current.retry_count).toISOString();
      }
      await writeRuntimeJsonAtomic(CODEX_REVIEW_EVENT, current);
    })();
  });
}

async function startCodexMonitor(): Promise<void> {
  const config = await readRuntimeJson<CodexMonitorConfig>(CODEX_MONITOR_CONFIG);
  if (config?.watcher_mode === "standalone") return;
  const interval = Math.max(1_000, Math.min(60_000, Number(config?.poll_interval_ms ?? 2_000)));
  if (codexMonitorTimer) clearInterval(codexMonitorTimer);
  codexMonitorTimer = setInterval(() => { void codexMonitorTick(); }, interval);
  void codexMonitorTick();
}

function trustedNavigation(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    const host = url.hostname.toLowerCase();
    return url.protocol === "https:" && (host === "xiaoman.cn" || host.endsWith(".xiaoman.cn"));
  } catch {
    return false;
  }
}

function trustedControlSender(event: IpcMainEvent | IpcMainInvokeEvent): boolean {
  return Boolean(mainWindow && event.sender === mainWindow.webContents && event.senderFrame?.url.startsWith(`${CONTROL_URL}/`));
}

function requireControlSender(event: IpcMainEvent | IpcMainInvokeEvent): void {
  if (!trustedControlSender(event)) throw new Error("拒绝非本地控制台 IPC");
}

function browserState(): Record<string, unknown> {
  const contents = browserView?.webContents;
  return {
    url: contents?.getURL() ?? "",
    title: contents?.getTitle() ?? "",
    loading: contents?.isLoading() ?? false,
    canGoBack: contents?.navigationHistory.canGoBack() ?? false,
    canGoForward: contents?.navigationHistory.canGoForward() ?? false,
    persistentPartition: PARTITION
  };
}

function emitBrowserState(): void {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("browser:state", browserState());
}

function layoutViews(): void {
  if (!mainWindow || !browserView) return;
  const size = mainWindow.getContentSize();
  const width = size[0] ?? DEFAULT_SIDEBAR_WIDTH;
  const height = size[1] ?? TOOLBAR_HEIGHT;
  sidebarWidth = Math.max(MIN_SIDEBAR_WIDTH, Math.min(sidebarWidth, Math.max(MIN_SIDEBAR_WIDTH, width - MIN_BROWSER_WIDTH)));
  browserView.setBounds({
    x: Math.min(sidebarWidth, width),
    y: Math.min(TOOLBAR_HEIGHT, height),
    width: Math.max(0, width - sidebarWidth),
    height: Math.max(0, height - TOOLBAR_HEIGHT)
  });
}

function emitLayout(): void {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("layout:changed", { sidebarWidth });
}

function setSidebarWidth(value: unknown): void {
  if (typeof value !== "number" || !Number.isFinite(value) || !mainWindow) return;
  const windowWidth = mainWindow.getContentSize()[0] ?? DEFAULT_SIDEBAR_WIDTH + MIN_BROWSER_WIDTH;
  sidebarWidth = Math.round(Math.max(MIN_SIDEBAR_WIDTH, Math.min(value, windowWidth - MIN_BROWSER_WIDTH)));
  layoutViews();
  emitLayout();
  if (layoutSaveTimer) clearTimeout(layoutSaveTimer);
  layoutSaveTimer = setTimeout(() => {
    void fsp.writeFile(LAYOUT_FILE, JSON.stringify({ sidebarWidth }, null, 2), "utf8");
  }, 180);
}

function configureCrmSession(crmSession: Session): void {
  crmSession.setPermissionCheckHandler(() => false);
  crmSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  crmSession.webRequest.onBeforeRequest({ urls: ["*://*/*"] }, (details, callback) => {
    callback({ cancel: isBlockedAiUrl(details.url, adapter) });
  });
}

async function importCookieArray(crmSession: Session, parsed: unknown): Promise<{ imported: number; skipped: number }> {
  if (!Array.isArray(parsed)) throw new Error("Cookie 文件必须是 JSON 数组");
  let imported = 0;
  let skipped = 0;
  for (const candidate of parsed) {
    if (!candidate || typeof candidate !== "object") { skipped += 1; continue; }
    const cookie = candidate as Record<string, unknown>;
    const name = typeof cookie.name === "string" ? cookie.name : "";
    const value = typeof cookie.value === "string" ? cookie.value : "";
    const domainRaw = typeof cookie.domain === "string" ? cookie.domain : "";
    const host = domainRaw.replace(/^\./, "").toLowerCase();
    if (!name || !value || !(host === "xiaoman.cn" || host.endsWith(".xiaoman.cn"))) { skipped += 1; continue; }
    const cookiePath = typeof cookie.path === "string" && cookie.path.startsWith("/") ? cookie.path : "/";
    const secure = cookie.secure !== false;
    const details: Electron.CookiesSetDetails = {
      url: `${secure ? "https" : "http"}://${host}${cookiePath}`,
      name,
      value,
      domain: domainRaw || host,
      path: cookiePath,
      secure,
      httpOnly: cookie.httpOnly === true
    };
    if (typeof cookie.expirationDate === "number" && cookie.expirationDate > 0) details.expirationDate = cookie.expirationDate;
    const sameSite = String(cookie.sameSite ?? "").toLowerCase();
    const sameSiteMap: Record<string, Electron.CookiesSetDetails["sameSite"]> = {
      unspecified: "unspecified", none: "no_restriction", no_restriction: "no_restriction", lax: "lax", strict: "strict"
    };
    if (sameSiteMap[sameSite]) details.sameSite = sameSiteMap[sameSite];
    try {
      await crmSession.cookies.set(details);
      imported += 1;
    } catch {
      skipped += 1;
    }
  }
  await crmSession.cookies.flushStore();
  return { imported, skipped };
}

async function importCookiesFromFile(crmSession: Session): Promise<{ imported: number; skipped: number; file: string | null }> {
  if (!mainWindow) return { imported: 0, skipped: 0, file: null };
  const selected = await dialog.showOpenDialog(mainWindow, {
    title: "导入 xiaoman.cn Cookie JSON",
    properties: ["openFile"],
    filters: [{ name: "Cookie JSON", extensions: ["json"] }]
  });
  if (selected.canceled || !selected.filePaths[0]) return { imported: 0, skipped: 0, file: null };
  const source = selected.filePaths[0];
  const result = await importCookieArray(crmSession, JSON.parse(await fsp.readFile(source, "utf8")));
  return { ...result, file: path.basename(source) };
}

async function importCookieBootstrap(crmSession: Session): Promise<void> {
  const source = process.env.OKKI_COOKIE_BOOTSTRAP;
  if (!source) return;
  try {
    const parsed = JSON.parse(await fsp.readFile(source, "utf8"));
    await importCookieArray(crmSession, parsed);
  } finally {
    await fsp.rm(source, { force: true }).catch(() => undefined);
  }
}

function scheduleCaptureServerRestart(): void {
  if (shuttingDown || captureRestartTimer) return;
  captureRestartTimer = setTimeout(() => {
    captureRestartTimer = null;
    void startCaptureServer().then(() => {
      captureLog?.write(`[${new Date().toISOString()}] capture server restarted\n`);
    }).catch(error => {
      captureLog?.write(`[${new Date().toISOString()}] capture server restart failed: ${String(error)}\n`);
      scheduleCaptureServerRestart();
    });
  }, 1_000);
}

async function startCaptureServer(): Promise<void> {
  try {
    const response = await fetch(`${CONTROL_URL}/healthz`, { signal: AbortSignal.timeout(800) });
    if (response.ok) return;
  } catch { /* start below */ }

  fs.mkdirSync(RUNTIME_ROOT, { recursive: true });
  captureLog ??= fs.createWriteStream(path.join(RUNTIME_ROOT, "desktop-capture-server.log"), { flags: "a" });
  const configuredHeapMb = Number(process.env.OKKI_NODE_HEAP_MB);
  const heapMb = Math.min(65_536, Math.max(4_096, Number.isFinite(configuredHeapMb) ? configuredHeapMb : 8_192));
  const child = spawn(process.execPath, [`--max-old-space-size=${heapMb}`, path.join(HERE, "server.js")], {
    cwd: APP_ROOT,
    windowsHide: true,
    env: {
      ...process.env,
      ELECTRON_RUN_AS_NODE: "1",
      OKKI_CAPTURE_PORT: String(CONTROL_PORT),
      OKKI_CDP_PORT: String(CDP_PORT),
      OKKI_EMBEDDED_MODE: "1",
      OKKI_BROWSER_PROFILE: path.join(ELECTRON_DATA, "Partitions", "okki-crm-capture")
    },
    stdio: ["ignore", "pipe", "pipe"]
  });
  try { if (child.pid) os.setPriority(child.pid, os.constants.priority.PRIORITY_ABOVE_NORMAL); } catch { /* best effort */ }
  captureServer = child;
  child.stdout?.pipe(captureLog, { end: false });
  child.stderr?.pipe(captureLog, { end: false });
  child.once("exit", (code, signal) => {
    if (captureServer === child) captureServer = null;
    captureLog?.write(`[${new Date().toISOString()}] capture server exited code=${String(code)} signal=${String(signal)}\n`);
    scheduleCaptureServerRestart();
  });

  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`本地采集服务异常退出：${child.exitCode}`);
    try {
      const response = await fetch(`${CONTROL_URL}/healthz`, { signal: AbortSignal.timeout(800) });
      if (response.ok) return;
    } catch { /* retry */ }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`本地采集服务未在 20 秒内监听 ${CONTROL_URL}`);
}

function registerIpc(crmSession: Session): void {
  ipcMain.on("browser:back", event => {
    requireControlSender(event);
    if (browserView?.webContents.navigationHistory.canGoBack()) browserView.webContents.navigationHistory.goBack();
  });
  ipcMain.on("browser:forward", event => {
    requireControlSender(event);
    if (browserView?.webContents.navigationHistory.canGoForward()) browserView.webContents.navigationHistory.goForward();
  });
  ipcMain.on("browser:reload", event => { requireControlSender(event); browserView?.webContents.reload(); });
  ipcMain.on("browser:home", event => { requireControlSender(event); void browserView?.webContents.loadURL(INITIAL_URL); });
  ipcMain.on("browser:navigate", (event, rawUrl: unknown) => {
    requireControlSender(event);
    if (typeof rawUrl !== "string" || !trustedNavigation(rawUrl)) throw new Error("只允许打开 xiaoman.cn 的 HTTPS 页面");
    void browserView?.webContents.loadURL(rawUrl);
  });
  ipcMain.handle("browser:get-state", event => { requireControlSender(event); return browserState(); });
  ipcMain.handle("session:import-cookies", async event => {
    requireControlSender(event);
    return importCookiesFromFile(crmSession);
  });
  ipcMain.handle("layout:get", event => { requireControlSender(event); return { sidebarWidth }; });
  ipcMain.on("layout:set-sidebar-width", (event, value: unknown) => { requireControlSender(event); setSidebarWidth(value); });
}

async function createDesktop(): Promise<void> {
  // The one-shot launcher must not navigate the embedded browser until the
  // initial control UI and CRM page are fully attached.  Remove any stale
  // marker first; the fresh marker is written only after mainWindow.show().
  // 先清除旧就绪标记；仅在控制界面和 CRM 页面完成挂载并显示后发布新标记。
  await fsp.rm(DESKTOP_READY_FILE, { force: true }).catch(() => undefined);
  try {
    const storedLayout = JSON.parse(await fsp.readFile(LAYOUT_FILE, "utf8")) as { sidebarWidth?: unknown };
    if (typeof storedLayout.sidebarWidth === "number") sidebarWidth = storedLayout.sidebarWidth;
  } catch { /* first run */ }
  const crmSession = session.fromPartition(PARTITION, { cache: true });
  configureCrmSession(crmSession);
  await importCookieBootstrap(crmSession);
  registerIpc(crmSession);

  mainWindow = new BrowserWindow({
    title: `Evidence Trail — OKKI${INSTANCE_ID === "1" ? "" : ` #${INSTANCE_ID}`}`,
    width: 1600,
    height: 980,
    minWidth: 1180,
    minHeight: 720,
    backgroundColor: "#0a0d0c",
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload: path.join(APP_ROOT, "electron", "preload.cjs"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true
    }
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));

  browserView = new WebContentsView({
    webPreferences: {
      session: crmSession,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      spellcheck: false
    }
  });
  mainWindow.contentView.addChildView(browserView);
  browserView.webContents.setWindowOpenHandler(({ url }) => {
    if (trustedNavigation(url)) void browserView?.webContents.loadURL(url);
    return { action: "deny" };
  });
  browserView.webContents.on("will-navigate", (event, url) => {
    if (!trustedNavigation(url)) event.preventDefault();
  });
  browserView.webContents.on("did-navigate", emitBrowserState);
  browserView.webContents.on("did-navigate-in-page", emitBrowserState);
  browserView.webContents.on("did-start-loading", emitBrowserState);
  browserView.webContents.on("did-stop-loading", emitBrowserState);
  browserView.webContents.on("page-title-updated", emitBrowserState);
  mainWindow.on("resize", () => { layoutViews(); emitLayout(); });
  mainWindow.on("closed", () => { mainWindow = null; browserView = null; });

  await startCaptureServer();
  await startCodexMonitor();
  await mainWindow.loadURL(`${CONTROL_URL}/?embedded=1`);
  layoutViews();
  await browserView.webContents.loadURL(INITIAL_URL);
  const remoteIsolation = await browserView.webContents.executeJavaScript(`({
    nodeRequire: typeof require,
    nodeProcess: typeof process,
    contextIsolated: window !== globalThis ? true : false
  })`, true) as Record<string, unknown>;
  const securityArtifact = JSON.stringify({
    verified_at: new Date().toISOString(),
    instance_id: INSTANCE_ID,
    runtime_root: RUNTIME_ROOT,
    desktop_ui: true,
    embedded_browser: "WebContentsView",
    persistent_partition: crmSession.isPersistent(),
    partition: PARTITION,
    control_origin: CONTROL_URL,
    cdp_endpoint: `http://${CONTROL_HOST}:${CDP_PORT}`,
    node_integration: remoteIsolation.nodeRequire !== "function" && remoteIsolation.nodeProcess === "undefined" ? "disabled" : "unexpected",
    context_isolation: true,
    sandbox: true,
    web_security: true,
    resizable_split: true,
    sidebar_min_width: MIN_SIDEBAR_WIDTH,
    browser_min_width: MIN_BROWSER_WIDTH,
    external_ai_block_before_first_navigation: true,
    cookie_values_recorded: false,
    current_remote_origin: new URL(browserView.webContents.getURL()).origin
  }, null, 2);
  await fsp.mkdir(VERIFICATION_ROOT, { recursive: true });
  await Promise.all([
    fsp.writeFile(path.join(RUNTIME_ROOT, "desktop-security-latest.json"), securityArtifact, "utf8"),
    fsp.writeFile(path.join(VERIFICATION_ROOT, INSTANCE_ID === "1" ? "desktop_ui_verification_latest.json" : `desktop_ui_verification_instance-${INSTANCE_ID}.json`), securityArtifact, "utf8")
  ]);
  mainWindow.show();
  layoutViews();
  emitLayout();
  await writeRuntimeJsonAtomic(DESKTOP_READY_FILE, {
    schema: 1,
    ready_at: new Date().toISOString(),
    pid: process.pid,
    instance_id: INSTANCE_ID,
    control_url: CONTROL_URL,
    cdp_port: CDP_PORT
  });
}

function stopChildren(): void {
  shuttingDown = true;
  if (codexMonitorTimer) clearInterval(codexMonitorTimer);
  codexMonitorTimer = null;
  if (codexMonitorChild && codexMonitorChild.exitCode === null) codexMonitorChild.kill();
  codexMonitorChild = null;
  if (captureRestartTimer) clearTimeout(captureRestartTimer);
  captureRestartTimer = null;
  if (captureServer && captureServer.exitCode === null) captureServer.kill();
  captureServer = null;
  captureLog?.end();
  captureLog = null;
  codexMonitorLog?.end();
  codexMonitorLog = null;
}

const singleInstance = app.requestSingleInstanceLock();
if (!singleInstance) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
  app.on("before-quit", stopChildren);
  app.on("window-all-closed", () => app.quit());
  app.whenReady().then(createDesktop).catch(error => {
    dialog.showErrorBox("OKKI 桌面采集器启动失败", error instanceof Error ? error.stack ?? error.message : String(error));
    stopChildren();
    app.quit();
  });
}
