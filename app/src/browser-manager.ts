/** Dedicated local browser management; vendor URLs are adapter metadata, not tenant data.
 * 专用本地浏览器管理；厂商网址属于适配器元数据，不包含租户数据。 */
import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { chromium, type Browser, type Page } from "playwright-core";
import type { OkkiAdapter } from "./types.js";
import { parseCustomerUrl } from "./adapter.js";
import { candidateCaseDirectoryKeys } from "./evidence-store.js";

const CHROME_CANDIDATES = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"
];

export interface BrowserTargetInfo {
  mode: "embedded" | "external";
  connected: boolean;
  browserVersion: string | null;
  customerUrl: string | null;
  companyId: string | null;
  pageTitle: string | null;
  allPageCount: number;
  cdpEndpoint: string;
  profileDir: string;
  executablePath: string | null;
}

export interface CustomerStageDefinition {
  id: string;
  label: string;
  domIndex: number;
}

export interface NextCustomerCandidate {
  companyId: string;
  name: string;
  stageId: string;
  stageName: string;
  stageDomIndex: number;
  page: number;
  row: number;
}

export interface CustomerQueueCursor {
  stageId: string;
  page: number;
  row: number;
}

export function customerListQueryFromUrl(rawUrl: string): Record<string, unknown> {
  const url = new URL(rawUrl);
  const raw = url.searchParams.get("query");
  if (!raw) throw new Error("客户列表 URL 缺少 query 参数");
  const value = JSON.parse(raw) as unknown;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("客户列表 query 不是对象");
  return value as Record<string, unknown>;
}

export function buildCustomerListForm(
  rawUrl: string,
  stageId: string,
  pageNo: number,
  pageSize: number
): Array<[string, string]> {
  const query = customerListQueryFromUrl(rawUrl);
  const output: Array<[string, string]> = [
    ["curPage", String(pageNo)],
    ["layout_flag", "1"],
    ["pageSize", String(pageSize)],
    ["show_all", String(query.show_all ?? 1)],
    ["show_field_key", String(query.show_field_key ?? "company.private.list.field")],
    ["sort_scene", String(query.sort_scene ?? "setting")],
    ["swarm_id", stageId]
  ];
  const users = Array.isArray(query.user_num) ? query.user_num : [];
  users.forEach((value, index) => output.push([`user_num[${index}]`, String(value)]));
  return output;
}

export function stageProcessingOrder<T>(domOrder: T[]): T[] {
  return [...domOrder].reverse();
}

export function normalizeStageName(label: string): string {
  return label.replace(/\s*\d+\s*$/u, "").trim();
}

export function shouldSkipCandidateByCaseDirectory(
  customerName: string,
  companyId: string,
  existingCaseDirectoryKeys: ReadonlySet<string>
): boolean {
  for (const key of candidateCaseDirectoryKeys(customerName, companyId)) {
    if (existingCaseDirectoryKeys.has(key)) return true;
  }
  return false;
}

export function completedCustomerRefreshIds(raw: string | undefined): Set<string> {
  return new Set((raw ?? "")
    .split(/[\s,;]+/u)
    .map(value => value.trim())
    .filter(value => /^\d+$/.test(value)));
}

export function shouldSkipCompletedCandidate(
  companyId: string,
  completedCompanyIds: ReadonlySet<string>,
  refreshCompletedCompanyIds: ReadonlySet<string>,
  refreshedCompletedCompanyIds: ReadonlySet<string>
): boolean {
  return completedCompanyIds.has(companyId)
    && (!refreshCompletedCompanyIds.has(companyId) || refreshedCompletedCompanyIds.has(companyId));
}

export class BrowserManager {
  private browser: Browser | null = null;
  private process: ChildProcess | null = null;
  private refreshedCompletedCompanyIds = new Set<string>();

  constructor(
    readonly cdpPort: number,
    readonly profileDir: string,
    private readonly adapter: OkkiAdapter
  ) {}

  private get embeddedMode(): boolean {
    return process.env.OKKI_EMBEDDED_MODE === "1";
  }

  get endpoint(): string {
    return `http://127.0.0.1:${this.cdpPort}`;
  }

  findExecutable(): string | null {
    const configured = process.env.OKKI_BROWSER_EXECUTABLE?.trim();
    if (configured) return fs.existsSync(configured) ? configured : null;
    return CHROME_CANDIDATES.find(candidate => fs.existsSync(candidate)) ?? null;
  }

  async launch(): Promise<BrowserTargetInfo> {
    if (this.embeddedMode) {
      await this.waitForEndpoint(20_000);
      await this.connect();
      return this.status();
    }
    const executable = this.findExecutable();
    if (!executable) throw new Error("未找到 Google Chrome 或 Microsoft Edge");
    await fs.promises.mkdir(this.profileDir, { recursive: true });
    if (!(await this.isEndpointAlive())) {
      this.process = spawn(executable, [
        `--remote-debugging-port=${this.cdpPort}`,
        `--user-data-dir=${this.profileDir}`,
        `--host-resolver-rules=${this.adapter.external_ai_blocked_hosts.flatMap(host => [`MAP ${host} ~NOTFOUND`, `MAP *.${host} ~NOTFOUND`]).join(", ")}`,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-component-update",
        "--start-maximized",
        "about:blank"
      ], {
        detached: false,
        stdio: "ignore",
        windowsHide: false
      });
      await this.waitForEndpoint(20_000);
    }
    await this.connect();
    return this.status();
  }

  async connect(): Promise<Browser> {
    if (this.browser?.isConnected()) return this.browser;
    this.browser = await chromium.connectOverCDP(this.endpoint, { timeout: 15_000, isLocal: true });
    this.browser.on("disconnected", () => { this.browser = null; });
    return this.browser;
  }

  async customerPage(): Promise<Page | null> {
    let browser: Browser;
    try { browser = await this.connect(); } catch { return null; }
    const pages = browser.contexts().flatMap(context => context.pages());
    const candidates = pages.filter(page => parseCustomerUrl(page.url(), this.adapter));
    return candidates.at(-1) ?? null;
  }

  async customerListPage(): Promise<Page | null> {
    let browser: Browser;
    try { browser = await this.connect(); } catch { return null; }
    const pages = browser.contexts().flatMap(context => context.pages());
    return pages.filter(page => {
      try {
        const url = new URL(page.url());
        return url.origin === this.adapter.origin && url.pathname === "/crm/customer/list";
      } catch { return false; }
    }).at(-1) ?? null;
  }

  async nextCustomerCandidate(
    completedCompanyIds: Set<string>,
    existingCaseDirectoryKeys: ReadonlySet<string> = new Set<string>(),
    cursor: CustomerQueueCursor | null = null,
    refreshCompletedCompanyIds: ReadonlySet<string> = completedCustomerRefreshIds(
      process.env.OKKI_REFRESH_COMPLETED_COMPANY_IDS
    )
  ): Promise<NextCustomerCandidate | null> {
    // A directory name alone is not proof of a successful, current capture.
    // Keep the positional parameter for API compatibility, but only the PASS
    // inventory controls normal skipping. Completed cases can be refreshed
    // exactly once per process by listing their IDs in the environment value.
    void existingCaseDirectoryKeys;
    const page = await this.customerListPage();
    if (!page) throw new Error("内置浏览器当前不在客户列表页");
    const stageTitle = this.adapter.customer_queue.stage_title;
    const readStages = (): Promise<CustomerStageDefinition[]> => page.evaluate((title): CustomerStageDefinition[] => {
      const anchors = Array.from(document.querySelectorAll("span.okki-menu-title-content"));
      const anchor = anchors.find(element => (element.textContent ?? "").trim() === title);
      const root = anchor?.closest("li.okki-menu-submenu");
      if (!root) return [];
      return Array.from(root.querySelectorAll(":scope > ul.okki-menu-sub > li[data-menu-id]"))
        .map((element, domIndex) => ({
          id: element.getAttribute("data-menu-id") ?? "",
          label: (element.textContent ?? "").replace(/\s+/g, " ").trim(),
          domIndex
        }))
        .filter(item => /^\d+$/.test(item.id));
    }, stageTitle);
    let domStages = await readStages();
    if (!domStages.length) {
      const expanded = await page.evaluate((title): boolean => {
        const anchors = Array.from(document.querySelectorAll("span.okki-menu-title-content"));
        const anchor = anchors.find(element => (element.textContent ?? "").trim() === title);
        const trigger = anchor?.closest("div.okki-menu-submenu-title");
        if (!(trigger instanceof HTMLElement)) return false;
        trigger.click();
        return true;
      }, stageTitle);
      if (expanded) {
        await page.waitForTimeout(500);
        domStages = await readStages();
      }
    }
    if (!domStages.length) throw new Error("未识别到客户阶段子选项；页面结构可能已变化");

    const endpoint = new URL(this.adapter.customer_queue.list_endpoint, this.adapter.origin).href;
    const pageSize = this.adapter.customer_queue.page_size;
    const seen = new Set<string>();
    const orderedStages = stageProcessingOrder(domStages);
    const cursorStageIndex = cursor ? orderedStages.findIndex(stage => stage.id === cursor.stageId) : -1;
    const startStageIndex = cursorStageIndex >= 0 ? cursorStageIndex : 0;
    for (let stageIndex = startStageIndex; stageIndex < orderedStages.length; stageIndex += 1) {
      const stage = orderedStages[stageIndex]!;
      const cursorApplies = cursorStageIndex === stageIndex && cursor !== null;
      const firstPage = cursorApplies ? Math.max(1, cursor.page) : 1;
      for (let pageNo = firstPage; pageNo <= this.adapter.customer_queue.safety_page_cap; pageNo += 1) {
        const formEntries = buildCustomerListForm(this.adapter.customer_list_url, stage.id, pageNo, pageSize);
        const result = await page.evaluate(async ({ requestUrl, entries }) => {
          const response = await fetch(requestUrl, {
            method: "POST",
            credentials: "include",
            headers: { "content-type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams(entries).toString()
          });
          if (!response.ok) throw new Error(`companyList HTTP ${response.status}`);
          const value = await response.json() as {
            data?: { totalItem?: number; list?: Array<Record<string, unknown>> };
          };
          const rows = Array.isArray(value.data?.list) ? value.data!.list! : [];
          return {
            total: Number(value.data?.totalItem ?? rows.length),
            rows: rows.map(row => ({
              companyId: row.company_id === undefined || row.company_id === null ? "" : String(row.company_id),
              name: typeof row.name === "string" ? row.name.trim() : ""
            }))
          };
        }, { requestUrl: endpoint, entries: formEntries });
        for (let rowIndex = 0; rowIndex < result.rows.length; rowIndex += 1) {
          if (cursorApplies && pageNo === firstPage && rowIndex + 1 <= cursor!.row) continue;
          const candidate = result.rows[rowIndex]!;
          if (!/^\d+$/.test(candidate.companyId) || !candidate.name || seen.has(candidate.companyId)) continue;
          seen.add(candidate.companyId);
          const completed = completedCompanyIds.has(candidate.companyId);
          if (shouldSkipCompletedCandidate(
            candidate.companyId,
            completedCompanyIds,
            refreshCompletedCompanyIds,
            this.refreshedCompletedCompanyIds
          )) continue;
          if (completed) this.refreshedCompletedCompanyIds.add(candidate.companyId);
          return {
            companyId: candidate.companyId,
            name: candidate.name,
            stageId: stage.id,
            stageName: normalizeStageName(stage.label),
            stageDomIndex: stage.domIndex,
            page: pageNo,
            row: rowIndex + 1
          };
        }
        if (!result.rows.length || pageNo * pageSize >= result.total) break;
      }
    }
    return null;
  }

  async openCustomer(companyId: string): Promise<Page> {
    if (!/^\d+$/.test(companyId)) throw new Error("company_id 无效");
    const browser = await this.connect();
    const pages = browser.contexts().flatMap(context => context.pages());
    const page = await this.customerListPage() ?? pages.at(-1);
    if (!page) throw new Error("内置浏览器没有可用页面");
    const url = new URL(this.adapter.customer_path, this.adapter.origin);
    url.searchParams.set("company_id", companyId);
    await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: 45_000 });
    return page;
  }

  async status(): Promise<BrowserTargetInfo> {
    const executablePath = this.embeddedMode ? process.execPath : this.findExecutable();
    if (!(await this.isEndpointAlive())) {
      return {
        mode: this.embeddedMode ? "embedded" : "external",
        connected: false, browserVersion: null, customerUrl: null, companyId: null,
        pageTitle: null, allPageCount: 0, cdpEndpoint: this.endpoint,
        profileDir: this.profileDir, executablePath
      };
    }
    try {
      const browser = await this.connect();
      const pages = browser.contexts().flatMap(context => context.pages());
      const page = pages.filter(item => parseCustomerUrl(item.url(), this.adapter)).at(-1) ?? null;
      const parsed = page ? parseCustomerUrl(page.url(), this.adapter) : null;
      return {
        mode: this.embeddedMode ? "embedded" : "external",
        connected: true,
        browserVersion: browser.version(),
        customerUrl: page?.url() ?? null,
        companyId: parsed?.companyId ?? null,
        pageTitle: page ? await page.title().catch(() => "") : null,
        allPageCount: pages.length,
        cdpEndpoint: this.endpoint,
        profileDir: this.profileDir,
        executablePath
      };
    } catch {
      return {
        mode: this.embeddedMode ? "embedded" : "external",
        connected: false, browserVersion: null, customerUrl: null, companyId: null,
        pageTitle: null, allPageCount: 0, cdpEndpoint: this.endpoint,
        profileDir: this.profileDir, executablePath
      };
    }
  }

  private async isEndpointAlive(): Promise<boolean> {
    try {
      const response = await fetch(`${this.endpoint}/json/version`, { signal: AbortSignal.timeout(1200) });
      return response.ok;
    } catch { return false; }
  }

  private async waitForEndpoint(timeoutMs: number): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await this.isEndpointAlive()) return;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    throw new Error(`浏览器调试端口 ${this.cdpPort} 未在 ${timeoutMs}ms 内就绪`);
  }
}
