import assert from "node:assert/strict";
import test from "node:test";
import { isAutoCaptureExcludedUrl, isBlockedAiUrl, isSafeObservedGetRecoveryUrl, isTelemetryUrl, loadAdapter, parseCustomerUrl } from "../adapter.js";
import {
  API_PAGE_CONCURRENCY,
  MAIL_DETAIL_CONCURRENCY,
  RESOURCE_DOWNLOAD_CONCURRENCY,
  UI_GAP_DYNAMIC_FILTER_SELECTOR,
  UI_GAP_DYNAMIC_WRAPPER_SELECTOR,
  UI_GAP_OTHER_DYNAMIC_LABELS,
  additionalVisibleUiGapFilters,
  assertUiGapRevisitIdentity,
  canonicalRequestUrl,
  completeConfiguredMailIds,
  configuredMailIdentityClosure,
  customerNameFromDetail,
  documentPaginationSummary,
  dynamicPaginationSummary,
  effectivePaginationPageSize,
  externalPassiveSourceUnavailableKeys,
  exactGetRecoveryKey,
  exactReadOnlyPostKey,
  hasTerminalEmptyPage,
  isReadOnlyApiPath,
  isExplicitlyUnavailableDiscoveredResource,
  isNonEvidenceMutationResponse,
  isConfiguredMailFilterUrl,
  isConfiguredMailTrailRow,
  latestRecordsPerExactUrl,
  listCountReconciliationSummary,
  mailPaginationSummary,
  mailPaginationExpectedMatches,
  mailRelationCoverageComplete,
  mailDetailAttemptsComplete,
  mailRelationAnchorIds,
  nextUncoveredMailRelationAnchor,
  normalizeObservedPaginationUrl,
  observedTotal,
  paginationBatchPageNumbers,
  paginationPagePlan,
  paginationRowMultisetFingerprint,
  paginationUrlForPage,
  primaryList,
  replayablePostHeaders,
  recursiveUrls,
  readOnlyPostAdvertisedPageSize,
  readOnlyPostPaginationInputForPage,
  requiredCoreGetUrls,
  stableVisibleGapCoverageComplete,
  stablePaginatedRowClosure,
  zeroCountPaginationClosed,
  sameStringSet,
  uiGapBestDynamicFilterCandidateIndex,
  uiGapDynamicFilterCoverage,
  unionObjectCoverageComplete,
  uiGapMatchingFilterIndex,
  uiPaginationTerminalClosed,
  validateMailApiResponse,
  requestBodyRole,
  responseBodyTimeoutMs,
  resourceAliasKey,
  selectUniqueAliasArtifact,
  signedResourceVariantKey,
  uniquePrimaryRowCount
} from "../capture-engine.js";
import { candidateCaseDirectoryKeys, caseDirectoryNameKey, sanitizeCaseFolderName } from "../evidence-store.js";
import {
  buildCustomerListForm,
  completedCustomerRefreshIds,
  customerListQueryFromUrl,
  normalizeStageName,
  shouldSkipCompletedCandidate,
  shouldSkipCandidateByCaseDirectory,
  stageProcessingOrder
} from "../browser-manager.js";
import { AUTO_NEXT_DELAY_MS, autoNextDecision } from "../auto-next.js";
import { codexResumeArgs, reviewerDisposition } from "../codex-wake-policy.js";
import type { JobStatus } from "../types.js";

const adapter = await loadAdapter();

function terminalJob(phase: JobStatus["phase"], errors: string[] = [], warnings: string[] = []): JobStatus {
  return {
    id: "job", phase, running: false, progress: 100, headline: "", detail: "",
    companyId: null, caseRoot: null, sessionRoot: null, startedAt: null, finishedAt: null,
    errors, warnings, metrics: {}
  };
}

test("warning 和 error 都延期记录并继续，只有人工取消停止", () => {
  assert.equal(AUTO_NEXT_DELAY_MS, 15_000);
  assert.equal(autoNextDecision(terminalJob("complete")), "advance");
  assert.equal(autoNextDecision(terminalJob("complete", [], ["warning"])), "advance");
  assert.equal(autoNextDecision(terminalJob("complete", ["error"])), "advance");
  assert.equal(autoNextDecision(terminalJob("incomplete")), "advance");
  assert.equal(autoNextDecision(terminalJob("failed")), "advance");
  assert.equal(autoNextDecision(terminalJob("cancelled")), "stop_cancelled");
});

test("客户终态唤醒是单回合，只有队列耗尽保留目标自动续跑", () => {
  const captureArgs = codexResumeArgs("capture_terminal", "thread-id");
  const exhaustedArgs = codexResumeArgs("queue_exhausted", "thread-id");
  assert.deepEqual(captureArgs.slice(0, 4), ["exec", "resume", "--disable", "goals"]);
  assert.equal(exhaustedArgs.includes("--disable"), false);
  assert.equal(captureArgs.at(-2), "thread-id");
  assert.equal(captureArgs.at(-1), "-");
});

test("过期 reviewer 不得阻塞更新终态事件", () => {
  assert.equal(reviewerDisposition("old", 0, { event_id: "old", status: "claimed" }, 10_000, 60_000), "keep");
  assert.equal(reviewerDisposition("old", 0, { event_id: "new", status: "pending" }, 10_000, 60_000), "stop_obsolete");
  assert.equal(reviewerDisposition("old", 0, { event_id: "old", status: "acknowledged" }, 10_000, 60_000), "stop_obsolete");
  assert.equal(reviewerDisposition("old", 0, { event_id: "old", status: "claimed" }, 60_000, 60_000), "stop_timeout");
});

test("只识别带数字 company_id 的 OKKI 单一客户详情页", () => {
  assert.equal(parseCustomerUrl("https://crm.xiaoman.cn/crm/customer/personal?company_id=123456789&tab=dynamic", adapter)?.companyId, "123456789");
  assert.equal(parseCustomerUrl("https://crm.xiaoman.cn/crm/customer/list", adapter), null);
  assert.equal(parseCustomerUrl("https://crm.xiaoman.cn/crm/customer/personal?company_id=abc", adapter), null);
  assert.equal(parseCustomerUrl("https://example.com/crm/customer/personal?company_id=123456789", adapter), null);
});

test("启动和采集结束统一返回指定客户列表视图", () => {
  const url = new URL(adapter.customer_list_url);
  assert.equal(url.origin, adapter.origin);
  assert.equal(url.pathname, "/crm/customer/list");
  const query = JSON.parse(url.searchParams.get("query") ?? "null");
  assert.equal(query.show_all, undefined);
  assert.equal(query._p_swarm_id, undefined);
  assert.equal(query.swarm_id, undefined);
  assert.equal(query.curPage, 1);
  assert.equal(query.pageSize, 20);
  assert.equal(query.user_num, undefined);
  assert.equal(query.show_field_key, "company.private.list.field");
  assert.equal(query.sort_scene, "setting");
});

test("下一客户队列按阶段反序并保留阶段内分页顺序", () => {
  assert.deepEqual(stageProcessingOrder(["top", "middle", "bottom"]), ["bottom", "middle", "top"]);
  assert.equal(normalizeStageName("Synthetic stage 18"), "Synthetic stage");
  const query = customerListQueryFromUrl(adapter.customer_list_url);
  assert.equal(query.user_num, undefined);
  assert.deepEqual(Object.fromEntries(buildCustomerListForm(adapter.customer_list_url, "999", 3, 20)), {
    curPage: "3",
    layout_flag: "1",
    pageSize: "20",
    show_all: "1",
    show_field_key: "company.private.list.field",
    sort_scene: "setting",
    swarm_id: "999"
  });
});

test("外部 AI 主机及其子域在首次导航前可判定阻断", () => {
  assert.equal(isBlockedAiUrl("https://api.openai.com/v1/responses", adapter), true);
  assert.equal(isBlockedAiUrl("https://sub.api.anthropic.com/v1/messages", adapter), true);
  assert.equal(isBlockedAiUrl("https://crm.xiaoman.cn/api/customerV2Read/detail", adapter), false);
});

test("已观察 GET 失败可适配新 CDN 精确补取，但外部 AI 和遥测仍禁止", () => {
  assert.equal(isSafeObservedGetRecoveryUrl("https://new-customer-assets.example.test/image.png?version=1", adapter), true);
  assert.equal(isSafeObservedGetRecoveryUrl("ftp://new-customer-assets.example.test/image.png", adapter), false);
  assert.equal(isSafeObservedGetRecoveryUrl("https://api.openai.com/v1/files", adapter), false);
  assert.equal(isTelemetryUrl("https://datasink-sensorsdata.example.test/sa.gif"), true);
  assert.equal(isSafeObservedGetRecoveryUrl("https://datasink-sensorsdata.example.test/sa.gif", adapter), false);
  assert.equal(isTelemetryUrl("https://example.test/logstores/trace/track"), true);
  assert.equal(isSafeObservedGetRecoveryUrl("https://crm.xiaoman.cn/api/ciqRead/list", adapter), false);
  assert.equal(isSafeObservedGetRecoveryUrl("https://crm.xiaoman.cn/api/aiWordRead/WordCloud", adapter), false);
});

test("贸易数据采集功能已从桌面采集器移除", () => {
  assert.equal(adapter.root_tabs.some(tab => tab.id === "seaData"), false);
  assert.equal("customs" in adapter, false);
  assert.equal(adapter.expected_endpoint_contracts.some(contract => contract.id.includes("customs")), false);
  assert.equal(adapter.expected_endpoint_contracts.some(contract =>
    isAutoCaptureExcludedUrl(new URL(contract.path, adapter.origin).href)), false);
  assert.equal(isAutoCaptureExcludedUrl("https://crm.xiaoman.cn/crm/customer/personal?company_id=7&tab=seaData"), true);
  assert.equal(isAutoCaptureExcludedUrl("https://crm.xiaoman.cn/ciq/info?id=7"), true);
  assert.equal(isAutoCaptureExcludedUrl("https://crm.xiaoman.cn/api/ciqRead/detail?id=7"), true);
  assert.equal(isAutoCaptureExcludedUrl("https://crm.xiaoman.cn/api/aiWordRead/WordCloud?company_id=7"), true);
  assert.equal(isAutoCaptureExcludedUrl("https://crm.xiaoman.cn/api/customerV2Read/detail?company_id=7"), false);
});

test("UI 缺口补采硬绑定单客户并排除根标签与贸易数据", () => {
  assert.equal(UI_GAP_DYNAMIC_WRAPPER_SELECTOR, ".customer-dynamic-wrapper");
  assert.match(UI_GAP_DYNAMIC_FILTER_SELECTOR, /button\.okki-btn-rect/);
  assert.deepEqual([...UI_GAP_OTHER_DYNAMIC_LABELS], ["其他动态", "其它", "其他"]);
  assert.doesNotThrow(() => assertUiGapRevisitIdentity("1001", "1001"));
  assert.throws(() => assertUiGapRevisitIdentity("1002", "1001"), /matching one-shot/);
  assert.throws(() => assertUiGapRevisitIdentity("1001", ""), /explicit/);
  assert.deepEqual(additionalVisibleUiGapFilters(adapter, [
    "动态", "资料", "全部 (12)", "其他动态", "往来邮件",
    "语音记录 3", "自定义动态", "贸易数据", "海关数据", "语音记录"
  ]), ["语音记录", "自定义动态"]);
  assert.deepEqual(additionalVisibleUiGapFilters(adapter, [
    "全部(2134)", "跟进记录(0)", "往来邮件(2134)", "聊天记录(0)", "其它(0)", "新增类型"
  ]), ["新增类型"]);
  const liveFilterButtons = ["全部(2134)", "跟进记录(0)", "往来邮件(2134)", "聊天记录(0)", "其它(0)"];
  const expectedFilterGroups = [
    ["全部"], ["跟进记录"], ["往来邮件"], ["聊天记录"], ["其他动态", "其它", "其他"]
  ];
  // Synthetic hydration fixture: a transient button precedes the complete vendor filter bar.
  // 合成渲染测试：完整的厂商筛选条出现之前可能先显示临时按钮。
  const observedFilterBarCandidates = [["全部(0)"], liveFilterButtons];
  assert.equal(uiGapDynamicFilterCoverage(expectedFilterGroups, observedFilterBarCandidates[0] ?? []), 1);
  assert.equal(uiGapDynamicFilterCoverage(expectedFilterGroups, observedFilterBarCandidates[1] ?? []), 5);
  assert.equal(uiGapBestDynamicFilterCandidateIndex(expectedFilterGroups, observedFilterBarCandidates), 1);
  assert.equal(uiGapMatchingFilterIndex(["全部"], liveFilterButtons), 0);
  assert.equal(uiGapMatchingFilterIndex(["其他动态", "其它", "其他"], liveFilterButtons), 4);
  assert.equal(uiGapMatchingFilterIndex(["邮件"], liveFilterButtons), -1);
  assert.equal(uiPaginationTerminalClosed("next_absent"), true);
  assert.equal(uiPaginationTerminalClosed("next_disabled"), true);
  assert.equal(uiPaginationTerminalClosed("content_unchanged"), false);
  assert.equal(uiPaginationTerminalClosed("safety_cap_reached"), false);
  assert.equal(uiPaginationTerminalClosed("filter_not_found"), false);
  assert.equal(uiPaginationTerminalClosed("capture_error"), false);
});

test("数量与分页对账使用真实 data.count 并去重主列表", () => {
  const first = { data: { count: 3, list: [{ file_id: 1, nested: { file_id: 99 } }, { file_id: 2 }] } };
  const second = { data: { count: 3, list: [{ file_id: 2 }, { file_id: 3 }] } };
  assert.equal(observedTotal([first, second], ["data.count"]), 3);
  assert.equal(primaryList(first).length, 2);
  assert.equal(uniquePrimaryRowCount([first, second]), 3);
  assert.equal(primaryList({ data: [{ folder_id: 1 }] }).length, 1);
});

test("跨域页面资源读体失败且精确补取明确拒绝时只按唯一 URL 标记源端不可重取", () => {
  const url = "https://assets.example.test/image.png?Expires=1&OSSAccessKeyId=a&Signature=old";
  const retriedUrl = "https://assets.example.test/image.png?Expires=2&OSSAccessKeyId=a&Signature=new";
  const keys = externalPassiveSourceUnavailableKeys([
    { method: "GET", url, status: 200, resourceType: "image", bodyRelativePath: null, error: "body unavailable" },
    { method: "GET", url, status: 200, resourceType: "image", bodyRelativePath: null, error: "body unavailable" },
    { method: "GET", url: retriedUrl, status: 403, resourceType: "response_retry", bodyRelativePath: null, error: "HTTP 403" },
    { method: "GET", url: "https://crm.xiaoman.cn/api/customerV2Read/detail", status: 403, resourceType: "response_retry", bodyRelativePath: null, error: "HTTP 403" }
  ], adapter.origin);
  assert.equal(keys.size, 1);
  assert.equal(keys.has("https://assets.example.test/image.png"), true);
});

test("分页总数按原始行闭合，接口自身重复单独审计而不误报漏采", () => {
  const records = [
    { url: "https://crm.xiaoman.cn/api/customerRead/companyDiffList?curPage=1&pageSize=2", sequence: 1, resourceType: "xhr_pagination", json: { data: { count: 4, list: [{ key: 1 }, { key: 2 }] } } },
    { url: "https://crm.xiaoman.cn/api/customerRead/companyDiffList?curPage=2&pageSize=2", sequence: 2, resourceType: "xhr_pagination", json: { data: { count: 4, list: [{ key: 2 }, { key: 3 }] } } },
    { url: "https://crm.xiaoman.cn/api/customerRead/companyDiffList?curPage=1&pageSize=2", sequence: 3, resourceType: "fetch", json: { data: { count: 4, list: [{ key: 1 }, { key: 2 }] } } }
  ];
  assert.deepEqual(listCountReconciliationSummary(records, ["data.count"]), {
    expected: 4,
    rowsCaptured: 4,
    uniqueRows: 3,
    duplicateRows: 1,
    paginationBasis: true,
    complete: true
  });
});

test("同 URL 的联系人或商机 POST 页按请求正文哈希分别计数", () => {
  const records = [
    {
      url: "https://crm.xiaoman.cn/api/opportunityRead/list", sequence: 1,
      resourceType: "xhr_post_pagination", requestBodySha256: "page-1",
      json: { data: { count: 3, list: [{ key: 1 }, { key: 2 }] } }
    },
    {
      url: "https://crm.xiaoman.cn/api/opportunityRead/list", sequence: 2,
      resourceType: "xhr_post_pagination", requestBodySha256: "page-2",
      json: { data: { count: 3, list: [{ key: 3 }] } }
    }
  ];
  assert.deepEqual(listCountReconciliationSummary(records, ["data.count"]), {
    expected: 3,
    rowsCaptured: 3,
    uniqueRows: 3,
    duplicateRows: 0,
    paginationBasis: true,
    complete: true
  });
});

test("分页以接口实际首屏行数为准，不盲信声明的 pageSize", () => {
  assert.equal(effectivePaginationPageSize(20, 5), 5);
  assert.equal(Math.ceil(12 / effectivePaginationPageSize(20, 5)), 3);
  assert.equal(effectivePaginationPageSize(20, 20), 20);
  assert.equal(effectivePaginationPageSize(20, 0), 20);
  assert.equal(hasTerminalEmptyPage([{ data: { list: [1, 2] } }, { data: { list: [] } }]), true);
  assert.equal(hasTerminalEmptyPage([{ data: { list: [1, 2] } }]), false);
});

test("API 分页按声明总数自动越过旧 500 页上限但保留绝对安全门", () => {
  assert.ok(API_PAGE_CONCURRENCY >= 6 && API_PAGE_CONCURRENCY <= 32);
  assert.ok(MAIL_DETAIL_CONCURRENCY >= 6 && MAIL_DETAIL_CONCURRENCY <= 24);
  assert.ok(RESOURCE_DOWNLOAD_CONCURRENCY >= 3 && RESOURCE_DOWNLOAD_CONCURRENCY <= 10);
  assert.deepEqual(paginationPagePlan(12_423, 20, 500), { requiredPages: 622, pageLimit: 623 });
  assert.deepEqual(paginationPagePlan(100, 20, 5000), { requiredPages: 5, pageLimit: 5000 });
  assert.throws(() => paginationPagePlan(500_000, 20, 500), /绝对安全上限/);
  assert.equal(adapter.dynamic.visible_page_cap, 2);
  assert.equal(adapter.dynamic.safety_page_cap, 5000);
});

test("隐藏或空标签仍主动补取全部只读核心接口第一页", () => {
  const urls = requiredCoreGetUrls("123", adapter).map(raw => new URL(raw));
  assert.equal(urls.length, 8);
  assert.deepEqual(urls.map(url => url.pathname), [
    "/api/customerV2Read/FormFieldList",
    "/api/customerRead/companyDiffList",
    "/api/customerRead/customerOrderList",
    "/api/customerRead/relatedProductList",
    "/api/fileFolderRead/fileList",
    "/api/fileFolderRead/folderList",
    "/api/customerRead/trailList",
    "/api/customerRead/trailList"
  ]);
  assert.equal(urls[4]!.searchParams.get("object_id"), "123");
  assert.equal(urls[4]!.searchParams.get("object_name"), "objCompany");
  assert.equal(urls[5]!.searchParams.get("object_id"), "123");
  assert.equal(urls[5]!.searchParams.get("object_name"), "objCompany");
  assert.equal(urls[3]!.searchParams.get("page"), "1");
  assert.equal(urls[3]!.searchParams.has("page_no"), false);
  assert.equal(urls[2]!.searchParams.get("sort_field"), "create_time");
  assert.deepEqual(urls[7]!.searchParams.getAll("modules[]"), adapter.dynamic.mail_module_values);
  assert.equal(urls[7]!.searchParams.get("stat_info"), "0");
  assert.equal(isConfiguredMailFilterUrl(urls[7]!.href, adapter), true);
});

test("专用邮件筛选遗漏时只从闭合全动态中的配置邮件类型补回 mail_id", () => {
  const dedicated = [
    { trail_id: 1, mail_id: 101, type: 202, node_type: 202 }
  ];
  const broad = [
    { trail_id: 1, mail_id: 101, type: 202, node_type: 202 },
    { trail_id: 2, mail_id: 102, type: 201, node_type: 201 },
    { trail_id: 3, mail_id: 103, type: 9, node_type: 9 },
    { trail_id: 4, type: 9, node_type: 9, nested: { mail_id: 104 } }
  ];
  assert.equal(isConfiguredMailTrailRow(broad[0], adapter), true);
  assert.equal(isConfiguredMailTrailRow(broad[1], adapter), true);
  assert.equal(isConfiguredMailTrailRow(broad[2], adapter), false);
  assert.equal(isConfiguredMailTrailRow(broad[3], adapter), false);
  assert.deepEqual(completeConfiguredMailIds(dedicated, broad, adapter), ["101", "102"]);
});

test("专用与全动态邮件集合可不同但并集必须绑定当前客户且 mail/trail 一一对应", () => {
  const dedicated = [
    { trail_id: 1, company_id: 88, type: 202, node_type: 202, data: { mail_id: 101 } },
    { trail_id: 2, company_id: 88, type: 201, node_type: 201, data: { mail_id: 102 } }
  ];
  const broad = [
    { trail_id: 2, company_id: 88, type: 201, node_type: 201, data: { mail_id: 102 } },
    { trail_id: 3, company_id: 88, type: 202, node_type: 202, data: { mail_id: 103 } }
  ];
  const summary = configuredMailIdentityClosure([...dedicated, ...broad], adapter, "88");
  assert.equal(summary.complete, true);
  assert.equal(summary.uniqueMailIds, 3);
  assert.equal(summary.uniqueTrailIds, 3);
  assert.equal(summary.companyMismatches, 0);
});

test("邮件并集对跨客户或 mail/trail 冲突保持 fail-closed", () => {
  const crossCustomer = configuredMailIdentityClosure([
    { trail_id: 1, company_id: 99, type: 202, node_type: 202, data: { mail_id: 101 } }
  ], adapter, "88");
  assert.equal(crossCustomer.complete, false);
  assert.equal(crossCustomer.companyMismatches, 1);

  const mailConflict = configuredMailIdentityClosure([
    { trail_id: 1, company_id: 88, type: 202, node_type: 202, data: { mail_id: 101 } },
    { trail_id: 2, company_id: 88, type: 202, node_type: 202, data: { mail_id: 101 } }
  ], adapter, "88");
  assert.equal(mailConflict.complete, false);
  assert.equal(mailConflict.mailToTrailConflicts, 1);

  const trailConflict = configuredMailIdentityClosure([
    { trail_id: 1, company_id: 88, type: 202, node_type: 202, data: { mail_id: 101 } },
    { trail_id: 1, company_id: 88, type: 201, node_type: 201, data: { mail_id: 102 } }
  ], adapter, "88");
  assert.equal(trailConflict.complete, false);
  assert.equal(trailConflict.trailToMailConflicts, 1);
});

test("related products pagination removes the conflicting page_no parameter", () => {
  const url = new URL(paginationUrlForPage(
    "https://crm.xiaoman.cn/api/customerRead/relatedProductList?company_id=123&page=1&page_no=1&page_size=10",
    "page",
    2,
    ["page_no"]
  ));
  assert.equal(url.searchParams.get("page"), "2");
  assert.equal(url.searchParams.has("page_no"), false);
  assert.equal(url.searchParams.get("page_size"), "10");
});

test("small result sets never overfetch a full concurrency batch", () => {
  assert.deepEqual(paginationBatchPageNumbers(1, 500, 1, 1), [1]);
  assert.deepEqual(paginationBatchPageNumbers(1, 5000, 100, 20), [1, 2, 3, 4, 5]);
  const largeBatch = paginationBatchPageNumbers(1, 5000, 12_423, 20);
  assert.equal(largeBatch.length, API_PAGE_CONCURRENCY);
  assert.equal(largeBatch[0], 1);
  assert.equal(largeBatch.at(-1), API_PAGE_CONCURRENCY);
  assert.deepEqual(paginationBatchPageNumbers(7, 5000, 0, 20), []);
});

test("动态与邮件主动分页从第一页起固定使用列表模式", () => {
  const trail = "https://crm.xiaoman.cn/api/customerRead/trailList?curPage=1&pageSize=20&stat_info=1";
  const normalized = new URL(normalizeObservedPaginationUrl(trail, adapter.dynamic.trail_endpoint));
  assert.equal(normalized.searchParams.get("stat_info"), "0");
  assert.equal(normalized.searchParams.get("curPage"), "1");

  const withoutFlag = new URL(normalizeObservedPaginationUrl(
    "https://crm.xiaoman.cn/api/customerRead/trailList?curPage=1&pageSize=20",
    adapter.dynamic.trail_endpoint
  ));
  assert.equal(withoutFlag.searchParams.get("stat_info"), "0");

  const other = "https://crm.xiaoman.cn/api/customerRead/companyDiffList?curPage=1&pageSize=20&stat_info=1";
  assert.equal(normalizeObservedPaginationUrl(other, adapter.dynamic.trail_endpoint), other);
});

test("邮件筛选模板必须包含全部配置模块并独立分页", () => {
  const full = "https://crm.xiaoman.cn/api/customerRead/trailList?modules%5B%5D=202&modules%5B%5D=201&modules%5B%5D=3&curPage=1&pageSize=20";
  const partial = "https://crm.xiaoman.cn/api/customerRead/trailList?modules%5B%5D=202&modules%5B%5D=201&curPage=1&pageSize=20";
  assert.equal(isConfiguredMailFilterUrl(full, adapter), true);
  assert.equal(isConfiguredMailFilterUrl(`${full}&begin_time=1`, adapter), false);
  assert.equal(isConfiguredMailFilterUrl(partial, adapter), false);
});

test("邮件筛选按分页行数闭合，无 mail_id 行单独解释而不是误报遗漏", () => {
  const pages = [
    { data: { count: 3, list: [{ trail_id: 1, mail_id: 10 }, { trail_id: 2 }] } },
    { data: { count: 3, list: [{ trail_id: 3, mail_id: 11 }] } }
  ];
  assert.deepEqual(mailPaginationSummary(pages, ["data.count"]), {
    expected: 3,
    rowCount: 3,
    rowsWithMailId: 2,
    rowsWithoutMailId: 1,
    uniqueMailIds: 2,
    duplicateMailIdOccurrences: 0,
    rowsWithTrailId: 3,
    rowsWithoutTrailId: 0,
    uniqueTrailIds: 3,
    duplicateTrailIdRows: 0,
    nonDetailRowsWithoutTrailId: 0,
    nonDetailTypeGroups: [{ module: null, type: null, nodeType: null, rows: 1 }],
    complete: true
  });
});

test("零邮件客户以已观察的总数零响应直接闭合", () => {
  const zero = [{ data: { count: 0, list: [] } }];
  const summary = mailPaginationSummary(zero, ["data.count"]);
  assert.equal(summary.expected, 0);
  assert.equal(summary.rowCount, 0);
  assert.equal(summary.complete, true);
  assert.equal(mailPaginationExpectedMatches(0, null), true);
  assert.equal(mailPaginationExpectedMatches(1, null), false);
  assert.equal(mailPaginationExpectedMatches(1, 1), true);
  assert.equal(zeroCountPaginationClosed(0, null, 0, 0, 0, 0), true);
  assert.equal(zeroCountPaginationClosed(0, 0, 0, 0, 0, 0), true);
  assert.equal(zeroCountPaginationClosed(1, null, 0, 0, 0, 0), false);
  assert.equal(zeroCountPaginationClosed(0, 1, 0, 0, 0, 0), false);
  assert.equal(zeroCountPaginationClosed(0, null, 1, 1, 0, 0), false);
  assert.equal(zeroCountPaginationClosed(0, null, 0, 1, 0, 0), false);
  assert.equal(zeroCountPaginationClosed(0, null, 0, 0, 1, 0), false);
});

test("只读 POST 只有精确 URL 和请求正文哈希一致时才能覆盖读体失败", () => {
  const url = "https://crm.xiaoman.cn/api/customerV2Read/companyCustomerList?page=1";
  assert.equal(
    exactReadOnlyPostKey("POST", url, "abc"),
    `POST\n${url}\nabc`
  );
  assert.notEqual(exactReadOnlyPostKey("POST", url, "abc"), exactReadOnlyPostKey("POST", url, "def"));
  assert.notEqual(
    exactReadOnlyPostKey("POST", url, "abc"),
    exactReadOnlyPostKey("POST", "https://crm.xiaoman.cn/api/customerV2Read/companyCustomerList?page=2", "abc")
  );
  assert.equal(exactReadOnlyPostKey("POST", "https://crm.xiaoman.cn/api/customerWrite/update", "abc"), null);
  assert.equal(isReadOnlyApiPath("/api/userScheduleRead/list"), true);
  assert.equal(exactReadOnlyPostKey("POST", "https://crm.xiaoman.cn/api/userScheduleRead/list", "abc") !== null, true);
  assert.equal(isReadOnlyApiPath("/api/customerWrite/update"), false);
  assert.equal(exactReadOnlyPostKey("GET", url, "abc"), null);
  assert.equal(exactReadOnlyPostKey("POST", "https://crm.xiaoman.cn/api/ciqRead/list", "abc"), null);
  assert.equal(exactReadOnlyPostKey("POST", "https://crm.xiaoman.cn/api/aiWordRead/WordCloud", "abc"), null);
});

test("联系人和商机 POST 分页只改已观察页码并保留筛选条件", () => {
  const jsonInput = {
    url: "https://crm.xiaoman.cn/api/opportunityRead/list",
    body: Buffer.from(JSON.stringify({ curPage: 1, pageSize: 20, company_id: "7", filter: "keep" })),
    headers: { "content-type": "application/json" },
    requestBodySha256: "old"
  };
  const jsonPage = readOnlyPostPaginationInputForPage(jsonInput, 3)!;
  assert.deepEqual(JSON.parse(jsonPage.body.toString("utf8")), {
    curPage: 3, pageSize: 20, company_id: "7", filter: "keep"
  });
  assert.equal(readOnlyPostAdvertisedPageSize(jsonInput), 20);
  assert.notEqual(jsonPage.requestBodySha256, "old");

  const formInput = {
    url: "https://crm.xiaoman.cn/api/customerV2Read/companyCustomerList",
    body: Buffer.from("contact_page_index=1&contact_page_size=10&company_id=7&keyword=keep"),
    headers: { "content-type": "application/x-www-form-urlencoded" },
    requestBodySha256: "old"
  };
  const formPage = readOnlyPostPaginationInputForPage(formInput, 4)!;
  assert.equal(new URLSearchParams(formPage.body.toString("utf8")).get("contact_page_index"), "4");
  assert.equal(new URLSearchParams(formPage.body.toString("utf8")).get("company_id"), "7");
  assert.equal(readOnlyPostAdvertisedPageSize(formInput), 10);
  assert.equal(readOnlyPostPaginationInputForPage({ ...jsonInput, url: "https://crm.xiaoman.cn/api/ciqRead/list" }, 2), null);
});

test("邮件前后关系锚点按窗口覆盖且包含末项", () => {
  const ids = Array.from({ length: 251 }, (_, index) => String(index + 1));
  assert.deepEqual(mailRelationAnchorIds(ids), ["1", "101", "201", "251"]);
  assert.equal(nextUncoveredMailRelationAnchor(ids, new Set(["1", "2"]), new Set(["1", "101"])), "3");
  assert.equal(nextUncoveredMailRelationAnchor(["1"], new Set(["1"]), new Set(["1"])), null);
  assert.deepEqual(mailRelationAnchorIds([]), []);
});

test("read-only POST recovery keeps only replay-safe request headers", () => {
  assert.deepEqual(replayablePostHeaders({
    Accept: "application/json", "Content-Type": "application/json",
    Cookie: "secret", Origin: "https://crm.xiaoman.cn", Referer: "https://crm.xiaoman.cn/",
    "X-CSRF-Token": "csrf", "X-OKKI-Version": "1", Authorization: "secret"
  }), {
    accept: "application/json", "content-type": "application/json",
    "x-csrf-token": "csrf", "x-okki-version": "1"
  });
});

test("native and paginated response union can close a pagination gap", () => {
  assert.equal(unionObjectCoverageComplete(7, 7, 13, 0), true);
  assert.equal(unionObjectCoverageComplete(7, 6, 12, 0), false);
  assert.equal(unionObjectCoverageComplete(7, 7, 13, 1), false);
  assert.equal(unionObjectCoverageComplete(null, 7, 13, 0), false);
});

test("a source visibility gap closes only after an empty page and stable fresh probe", () => {
  assert.equal(stableVisibleGapCoverageComplete(57, 56, 0, true, true), true);
  assert.equal(stableVisibleGapCoverageComplete(57, 56, 0, false, true), false);
  assert.equal(stableVisibleGapCoverageComplete(57, 56, 0, true, false), false);
  assert.equal(stableVisibleGapCoverageComplete(57, 56, 1, true, true), false);
  assert.equal(stableVisibleGapCoverageComplete(57, 57, 0, true, true), false);
});

test("explicit non-retryable discovered-resource HTTP failures are source-unavailable", () => {
  assert.equal(isExplicitlyUnavailableDiscoveredResource("discovered_file", 404), true);
  assert.equal(isExplicitlyUnavailableDiscoveredResource("discovered_file", 410), true);
  assert.equal(isExplicitlyUnavailableDiscoveredResource("discovered_file", 400), true);
  assert.equal(isExplicitlyUnavailableDiscoveredResource("discovered_file", 403), true);
  assert.equal(isExplicitlyUnavailableDiscoveredResource("xhr", 404), false);
  assert.equal(isExplicitlyUnavailableDiscoveredResource("xhr", 403), false);
});

test("动态分页只有唯一 trail_id 精确覆盖声明总数才闭合", () => {
  const pages = [
    { data: { count: 4, list: [{ trail_id: 1 }, { trail_id: 2 }] } },
    { data: { count: 4, list: [{ trail_id: 2 }, { trail_id: 3 }] } }
  ];
  assert.deepEqual(dynamicPaginationSummary(pages, ["data.count"]), {
    expected: 4,
    rowCount: 4,
    rowsWithTrailId: 4,
    rowsWithoutTrailId: 0,
    uniqueTrailIds: 3,
    duplicateTrailIdRows: 1,
    complete: false
  });
});

test("动态和邮件分页对重复或缺失 trail_id 一律 fail-closed", () => {
  const duplicate = [
    { data: { count: 3, list: [{ trail_id: 1, mail_id: 10 }, { trail_id: 1, mail_id: 11 }, { trail_id: 2 }] } }
  ];
  const missing = [
    { data: { count: 2, list: [{ trail_id: 1, mail_id: 10 }, { mail_id: 11 }] } }
  ];
  assert.equal(mailPaginationSummary(duplicate, ["data.count"]).complete, false);
  assert.equal(mailPaginationSummary(missing, ["data.count"]).complete, false);
  assert.equal(dynamicPaginationSummary(duplicate, ["data.count"]).complete, false);
  assert.equal(dynamicPaginationSummary(missing, ["data.count"]).complete, false);
});

test("源端重复映射只有在原始行数精确、无缺 trail_id 且整表 multiset 复跑稳定时闭合", () => {
  const rows = [
    { trail_id: 1, mail_id: 10, module: 2, type: 202, node_type: 202 },
    { trail_id: 2, mail_id: 11, module: 2, type: 201, node_type: 201 },
    { trail_id: 3, mail_id: 11, module: 2, type: 201, node_type: 201 },
    { trail_id: 1, mail_id: 10, module: 2, type: 202, node_type: 202 }
  ];
  const reordered = [rows[2], rows[0], rows[3], rows[1]];
  assert.equal(paginationRowMultisetFingerprint(rows), paginationRowMultisetFingerprint(reordered));
  assert.notEqual(
    paginationRowMultisetFingerprint(rows),
    paginationRowMultisetFingerprint([...rows.slice(0, 3), { ...rows[3], mail_id: 12 }])
  );
  assert.equal(stablePaginatedRowClosure(4, 4, 0, 1, 2, true), true);
  assert.equal(stablePaginatedRowClosure(4, 4, 0, 1, 2, false), false);
  assert.equal(stablePaginatedRowClosure(5, 4, 0, 1, 2, true), false);
  assert.equal(stablePaginatedRowClosure(4, 4, 1, 1, 2, true), false);
  assert.equal(stablePaginatedRowClosure(4, 4, 0, 0, 0, false), true);
});

test("专用邮件筛选唯一 ID 必须与详情、追踪和 manifest ID 集精确一致", () => {
  assert.equal(sameStringSet(["10", "11", "11"], ["11", "10"]), true);
  assert.equal(sameStringSet(["10", "11"], ["10"]), false);
  assert.equal(sameStringSet(["10", "11"], ["10", "12"]), false);
});

test("邮件前后关系允许保留关系源独有 ID，但必须覆盖声明总数和全部已抓详情", () => {
  assert.equal(mailRelationCoverageComplete(4, 4, ["10", "11", "12", "13"], ["10", "11"]), true);
  assert.equal(mailRelationCoverageComplete(4, 4, ["10", "11", "12"], ["10", "11"]), false);
  assert.equal(mailRelationCoverageComplete(4, 5, ["10", "11", "12", "13"], ["10", "11"]), false);
  assert.equal(mailRelationCoverageComplete(4, 4, ["10", "11", "12", "13"], ["10", "99"]), false);
  assert.equal(mailRelationCoverageComplete(null, 0, [], []), false);
  assert.equal(mailRelationCoverageComplete(0, 0, [], []), true);
});

test("邮件详情只把 2xx code=1219 识别为源端已删除，其他非零码仍失败", () => {
  const detail = { code: 0, data: { mail_id: "10", body: "omitted" } };
  assert.deepEqual(validateMailApiResponse(200, null, detail, "10", "detail"), { valid: true, reason: "ok" });
  assert.deepEqual(
    validateMailApiResponse(200, null, { code: 1219, msg: "omitted" }, "10", "detail"),
    { valid: true, reason: "source_deleted" }
  );
  assert.equal(validateMailApiResponse(200, null, { code: 1220 }, "10", "detail").valid, false);
  assert.equal(validateMailApiResponse(200, null, { code: 1219 }, "10", "track").valid, false);
  assert.equal(validateMailApiResponse(503, "HTTP 503", { code: 1219 }, "10", "detail").valid, false);
  assert.equal(validateMailApiResponse(200, null, { data: { mail_id: "10" } }, "10", "detail").valid, false);
  assert.equal(validateMailApiResponse(200, null, { code: "bad", data: { mail_id: "10" } }, "10", "detail").valid, false);
  assert.equal(validateMailApiResponse(200, null, { code: 0, data: { mail_id: "11" } }, "10", "detail").reason, "mail_id_mismatch");
  assert.equal(validateMailApiResponse(200, null, { code: 0, data: {} }, "10", "detail").reason, "mail_id_missing");
  assert.equal(validateMailApiResponse(503, "HTTP 503", detail, "10", "detail").valid, false);
  assert.equal(validateMailApiResponse(200, null, { code: 0, data: [] }, "10", "track").valid, true);
  assert.equal(validateMailApiResponse(200, null, { code: 0, data: "ok" }, "10", "track").reason, "invalid_data_shape");
  assert.equal(validateMailApiResponse(200, null, { code: 0, data: [{ mail_id: "11" }] }, "10", "track").valid, false);
});

test("邮件详情仅在可用加源端已删除精确覆盖全部尝试且无不可用项时闭合", () => {
  assert.equal(mailDetailAttemptsComplete(100, 80, 20, 0), true);
  assert.equal(mailDetailAttemptsComplete(100, 80, 19, 1), false);
  assert.equal(mailDetailAttemptsComplete(100, 100, 0, 0), true);
  assert.equal(mailDetailAttemptsComplete(0, 0, 0, 0), true);
});

test("文档按分页行数闭合，重复 file_id 单独审计而不是误报漏页", () => {
  const pages = [
    { data: { count: 4, list: [{ file_id: 1 }, { file_id: 2 }] } },
    { data: { count: 4, list: [{ file_id: 2 }, { file_id: 3 }] } }
  ];
  assert.deepEqual(documentPaginationSummary(pages, ["data.count"]), {
    expected: 4,
    rowCount: 4,
    rowsWithFileId: 4,
    rowsWithoutFileId: 0,
    uniqueFileIds: 3,
    duplicateFileIdRows: 1,
    complete: true
  });
});

test("文档唯一 file_id 已覆盖接口总数时，整页重复只记告警不误报遗漏", () => {
  const rows = Array.from({ length: 17 }, (_, index) => ({ file_id: index + 1 }));
  const summary = documentPaginationSummary([
    { data: { count: 17, list: rows } },
    { data: { count: 17, list: rows } }
  ], ["data.count"]);
  assert.equal(summary.expected, 17);
  assert.equal(summary.rowCount, 34);
  assert.equal(summary.uniqueFileIds, 17);
  assert.equal(summary.duplicateFileIdRows, 17);
  assert.equal(summary.complete, true);
});

test("同一精确分页 URL 重复响应只采用最新快照参与计数", () => {
  const records = [
    { url: "https://crm.xiaoman.cn/api/fileFolderRead/fileList?page_no=1", sequence: 2, marker: "old" },
    { url: "https://crm.xiaoman.cn/api/fileFolderRead/fileList?page_no=2", sequence: 3, marker: "page2" },
    { url: "https://crm.xiaoman.cn/api/fileFolderRead/fileList?page_no=1", sequence: 9, marker: "new" }
  ];
  assert.deepEqual(latestRecordsPerExactUrl(records).map(item => item.marker), ["page2", "new"]);
});

test("query parameter order does not create a duplicate logical page", () => {
  const first = { url: "https://crm.xiaoman.cn/api/list?page=1&page_size=20", sequence: 1 };
  const reordered = { url: "https://crm.xiaoman.cn/api/list?page_size=20&page=1", sequence: 2 };
  assert.equal(canonicalRequestUrl(first.url), canonicalRequestUrl(reordered.url));
  assert.deepEqual(latestRecordsPerExactUrl([first, reordered]), [reordered]);
  assert.equal(exactGetRecoveryKey("GET", first.url), exactGetRecoveryKey("GET", reordered.url));
});

test("只允许用完全相同的 GET URL 关联主动补取响应", () => {
  const url = "https://crm.xiaoman.cn/crm_web/static/app.js?v=1#fragment";
  assert.equal(exactGetRecoveryKey("GET", url), "https://crm.xiaoman.cn/crm_web/static/app.js?v=1");
  assert.equal(exactGetRecoveryKey("POST", url), null);
  assert.notEqual(exactGetRecoveryKey("GET", url), exactGetRecoveryKey("GET", "https://crm.xiaoman.cn/crm_web/static/app.js?v=2"));
});

test("所有上行正文完整落盘并按用途分类", () => {
  assert.equal(adapter.expected_endpoint_contracts.find(contract => contract.id === "analytics")?.min, 1);
  assert.equal(requestBodyRole("POST", "https://crm.xiaoman.cn/api/reportRead/detail", adapter), "contract_business");
  assert.equal(requestBodyRole("POST", "https://crm.xiaoman.cn/api/opportunityRead/list", adapter), "contract_business");
  assert.equal(requestBodyRole("POST", "https://datasink-sensorsdata.xiaoman.cn/sa?project=production", adapter), "external_service");
  assert.equal(requestBodyRole("POST", "https://example.cn/logstores/trace/track", adapter), "external_service");
  assert.equal(requestBodyRole("POST", "https://crm.xiaoman.cn/api/UserWrite/saveUserSetting", adapter), "same_origin_other");
  assert.equal(isNonEvidenceMutationResponse("POST", "https://crm.xiaoman.cn/api/UserWrite/saveUserSetting", adapter), true);
  assert.equal(isNonEvidenceMutationResponse("POST", "https://crm.xiaoman.cn/api/reportRead/detail", adapter), false);
  assert.equal(isNonEvidenceMutationResponse("GET", "https://crm.xiaoman.cn/api/UserWrite/saveUserSetting", adapter), false);
});

test("客户名从详情契约读取并转换为安全的 Windows 文件夹名", () => {
  assert.equal(customerNameFromDetail({ data: { values: { name: "  Example / Customer  " } } }), "Example / Customer");
  assert.equal(customerNameFromDetail({ data: { values: {} } }), null);
  assert.equal(sanitizeCaseFolderName("T&S / KLARCO", "123"), "T&S ／ KLARCO");
  assert.equal(sanitizeCaseFolderName("CON", "123"), "_CON");
});

test("邮件 HTML 中的实体与编码标签不会污染资源 URL", () => {
  const urls = recursiveUrls({
    html: [
      "<img src=\"https://v4client-oss.xiaoman.cn/55167275/a.png%22%3E%3C/span%3E\">",
      "https://v4client.oss-cn-hangzhou.aliyuncs.com/mail-inline/x..jpg?Expires=1&amp;Signature=abc",
      "https://v4client.oss-cn-hangzhou.aliyuncs.com/mail-attach%2Fdoc%2Fa.pdf?response-content-disposition=attachment%3B%20filename%2A%3Dutf-8%27%27a.pdf&amp;Expires=2&amp;Signature=sig"
    ]
  });
  assert.deepEqual(urls, [
    "https://v4client-oss.xiaoman.cn/55167275/a.png",
    "https://v4client.oss-cn-hangzhou.aliyuncs.com/mail-inline/x..jpg?Expires=1&Signature=abc",
    "https://v4client.oss-cn-hangzhou.aliyuncs.com/mail-attach%2Fdoc%2Fa.pdf?response-content-disposition=attachment%3B%20filename%2A%3Dutf-8%27%27a.pdf&Expires=2&Signature=sig"
  ]);
});

test("源正文坏别名可与已落盘正确资源建立审计对应", () => {
  assert.equal(
    resourceAliasKey("https://v4client-oss.xiaoman.cn/mail-inline/img/a..png"),
    resourceAliasKey("https://v4client.oss-cn-hangzhou.aliyuncs.com/mail-inline%2Fimg%2Fa.png?Signature=x")
  );
  assert.notEqual(
    resourceAliasKey("https://v4client-oss.xiaoman.cn/mail-inline/img/a..png"),
    resourceAliasKey("https://v4client-oss.xiaoman.cn/mail-inline/img/b.png")
  );
  const sameObject = [
    { sequence: 9, bodySha256: "A".repeat(64) },
    { sequence: 3, bodySha256: "A".repeat(64) }
  ];
  assert.equal(selectUniqueAliasArtifact(sameObject)?.sequence, 3);
  assert.equal(selectUniqueAliasArtifact([
    ...sameObject,
    { sequence: 12, bodySha256: "B".repeat(64) }
  ]), null);
});

test("临时签名 URL 只忽略认证字段，所有内容变换参数仍参与对账", () => {
  const first = "https://assets.example.test/mail/a.jpg?Expires=1&OSSAccessKeyId=key&Signature=old&response-content-type=image%2Fjpeg&x-oss-process=image%2Fresize%2Cw_800";
  const rotated = "https://assets.example.test/mail/a.jpg?Expires=2&OSSAccessKeyId=key&Signature=new&response-content-type=image%2Fjpeg&x-oss-process=image%2Fresize%2Cw_800";
  const transformed = "https://assets.example.test/mail/a.jpg?Expires=2&OSSAccessKeyId=key&Signature=new&response-content-type=image%2Fjpeg&x-oss-process=image%2Fresize%2Cw_200";
  assert.equal(signedResourceVariantKey(first), signedResourceVariantKey(rotated));
  assert.notEqual(signedResourceVariantKey(first), signedResourceVariantKey(transformed));
  assert.equal(signedResourceVariantKey("https://assets.example.test/mail/a.jpg?version=1"), null);
});

test("大图片与媒体使用更宽的读体窗口，接口响应仍保持有界等待", () => {
  assert.equal(responseBodyTimeoutMs("image"), 90_000);
  assert.equal(responseBodyTimeoutMs("media"), 90_000);
  assert.equal(responseBodyTimeoutMs("xhr"), 45_000);
});

test("目录名只用于兼容识别，不能再作为自动队列的完成证明", () => {
  const companyId = "123456";
  const customerName = "  Example / Customer  ";
  const preferred = sanitizeCaseFolderName(customerName, companyId);
  const existing = new Set([caseDirectoryNameKey(preferred)]);
  assert.equal(shouldSkipCandidateByCaseDirectory(customerName, companyId, existing), true);
  assert.equal(shouldSkipCandidateByCaseDirectory("Another Customer", "654321", existing), false);
  assert.equal(candidateCaseDirectoryKeys(customerName, companyId).has(caseDirectoryNameKey(preferred)), true);
  assert.equal(shouldSkipCompletedCandidate(companyId, new Set(), new Set(), new Set()), false);
});

test("已完成客户只按显式 ID 刷新一次，不会形成无限循环", () => {
  const completed = new Set(["123", "456"]);
  const refresh = completedCustomerRefreshIds("123, 456;bad 789");
  assert.deepEqual([...refresh], ["123", "456", "789"]);
  assert.equal(shouldSkipCompletedCandidate("123", completed, refresh, new Set()), false);
  assert.equal(shouldSkipCompletedCandidate("123", completed, refresh, new Set(["123"])), true);
  assert.equal(shouldSkipCompletedCandidate("456", completed, new Set(), new Set()), true);
  assert.equal(shouldSkipCompletedCandidate("999", completed, new Set(), new Set()), false);
});

test("case directory dedup handles Unicode, collision suffixes, and legacy company-id folders", () => {
  assert.equal(caseDirectoryNameKey("ＡＢＣ  Customer. "), caseDirectoryNameKey("abc customer"));
  const customerName = "Example Customer";
  const companyId = "123456";
  const collisionFolder = sanitizeCaseFolderName(`${sanitizeCaseFolderName(customerName, companyId)}__${companyId}`, companyId);
  assert.equal(shouldSkipCandidateByCaseDirectory(customerName, companyId, new Set([caseDirectoryNameKey(collisionFolder)])), true);
  assert.equal(shouldSkipCandidateByCaseDirectory(customerName, companyId, new Set([caseDirectoryNameKey(`company_${companyId}`)])), true);
});

