import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

// Keep the original workbook payload contract with an open-source backend.
// 使用开源后端并保留原有工作簿输入契约。
import { FileBlob, SpreadsheetFile, Workbook } from "./lib/open_workbook.mjs";
import JSZip from "jszip";

const SCHEMA = "okki.single_customer.completion_v2_catalog_payload.v1";
const VERIFY_SCHEMA = "okki.single_customer.completion_v2_catalog_verification.v1";
const DANGEROUS_CELL_PREFIX = /^[\t\r\n ]*[=+\-@]/;
const EXCEL_AUTO_COERCE_TEXT = /^(?:\d{11,}|\d+(?:\.\d+)?[Ee][+\-]?\d+|\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]+)?)$/i;
const MAX_CELL_CHARS = 32760;
const ERROR_TOKEN = /^(?:#REF!|#DIV\/0!|#VALUE!|#NAME\?|#N\/A)$/;

const TABLES = [
  {
    key: "coverage_rows", name: "补全覆盖", tableName: "CompletionCoverageTable",
    title: "全案例递归补全覆盖", note: "预期、交付目录展示数和完整权威来源分开列示；完整明细以 SQLite/Parquet 为准。",
    headers: ["数据域", "权威总数", "工作簿展示", "未在工作簿展开", "状态", "权威来源"],
    widths: [30, 16, 16, 20, 30, 58], numeric: [1, 2, 3], wrap: true,
  },
  {
    key: "mail_attachment_rows", name: "邮件附件关系", tableName: "CompletionMailAttachmentTable",
    title: "邮件与附件明确归属", note: "每行是去重后的邮件→附件关系；所有出现记录和多父附件保留在权威库。",
    headers: ["邮件节点ID", "邮件原值", "附件节点ID", "附件原值", "原始来源", "附件SHA256", "JSON指针", "解析状态"],
    widths: [42, 46, 42, 54, 62, 68, 62, 24], numeric: [], wrap: false,
  },
  {
    key: "explicit_relation_rows", name: "显式关系", tableName: "CompletionExplicitRelationTable",
    title: "显式父子与业务关系", note: "只有源字段明确支持的结构、参与者、业务和派生关系；普通共现不升级为父子事实。",
    headers: ["关系ID", "起点类型", "起点原值", "关系", "终点类型", "终点原值", "关系类别", "证据数", "首个来源"],
    widths: [42, 22, 58, 38, 22, 58, 22, 14, 62], numeric: [7], wrap: false,
  },
  {
    key: "business_timeline_rows", name: "业务时间线", tableName: "CompletionBusinessTimelineTable",
    title: "业务发生时间线", note: "保留原始时间、解析值、精度、来源和JSON指针；无时区时间不猜测UTC。",
    headers: ["稳定序号", "事件ID", "事件类型", "时间字段", "原始时间", "解析时间", "解析状态", "精度", "来源", "JSON指针"],
    widths: [14, 42, 30, 30, 34, 34, 24, 18, 62, 64], numeric: [0], wrap: false,
  },
  {
    key: "capture_timeline_rows", name: "采集时间线", tableName: "CompletionCaptureTimelineTable",
    title: "采集证据时间线", note: "按稳定序号保存采集/处理事件；与业务发生时间线分离。",
    headers: ["稳定序号", "事件ID", "事件类型", "时间字段", "原始时间", "解析时间", "解析状态", "精度", "来源", "JSON指针"],
    widths: [14, 42, 30, 30, 34, 34, 24, 18, 62, 64], numeric: [0], wrap: false,
  },
  {
    key: "source_gap_rows", name: "源缺口", tableName: "CompletionSourceGapTable",
    title: "客观不可取得内容清单", note: "删除邮件、失效URL、需密码或源站未返回等缺口单独保留，不能伪装成绝对完整。",
    headers: ["缺口ID", "来源域", "角色", "错误码", "HTTP状态", "内容SHA256", "来源身份SHA256", "父对象SHA256"],
    widths: [42, 28, 34, 38, 14, 68, 68, 68], numeric: [], wrap: false,
  },
  {
    key: "diff_rows", name: "新旧差异", tableName: "CompletionDiffTable",
    title: "旧版与补全版差异", note: "旧版成果原样保留，v2只增加遗漏范围、显式关系、双时间线和源缺口说明。",
    headers: ["指标", "旧版", "补全版", "增量"], widths: [38, 18, 18, 18], numeric: [1, 2, 3], wrap: true,
  },
];

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!key?.startsWith("--")) throw new Error("WORKBOOK_ARGUMENTS_INVALID");
    const name = key.slice(2);
    if (name === "validate-only") result[name] = true;
    else {
      const value = argv[index + 1];
      if (value === undefined || value.startsWith("--")) throw new Error("WORKBOOK_ARGUMENTS_INVALID");
      result[name] = value;
      index += 1;
    }
  }
  if (!result.input) throw new Error("WORKBOOK_ARGUMENT_MISSING_INPUT");
  if (!result["validate-only"]) {
    for (const required of ["output", "preview-dir", "verification"]) {
      if (!result[required]) throw new Error(`WORKBOOK_ARGUMENT_MISSING_${required.toUpperCase().replaceAll("-", "_")}`);
    }
  }
  return result;
}

function sha256(bytes) { return crypto.createHash("sha256").update(bytes).digest("hex").toUpperCase(); }
function requireInteger(value, code) {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(code);
  return value;
}

let escapedCellCount = 0;
let forcedTextCellCount = 0;
function safeCell(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("WORKBOOK_NONFINITE_NUMBER");
    return value;
  }
  if (typeof value === "boolean" || value instanceof Date) return value;
  if (typeof value === "object") throw new Error("WORKBOOK_CELL_OBJECT_FORBIDDEN");
  const text = String(value);
  if (text.length > MAX_CELL_CHARS) throw new Error("WORKBOOK_CELL_TOO_LONG");
  if (text.startsWith("'")) return text;
  if (DANGEROUS_CELL_PREFIX.test(text)) { escapedCellCount += 1; return `'${text}`; }
  if (EXCEL_AUTO_COERCE_TEXT.test(text)) { forcedTextCellCount += 1; return `'${text}`; }
  return text;
}
function safeRows(rows) { return rows.map((row) => row.map(safeCell)); }
function columnName(number) {
  let value = number; let result = "";
  while (value > 0) { value -= 1; result = String.fromCharCode(65 + (value % 26)) + result; value = Math.floor(value / 26); }
  return result;
}
function requireRows(payload, key, width) {
  const rows = payload[key];
  if (!Array.isArray(rows)) throw new Error(`WORKBOOK_${key.toUpperCase()}_INVALID`);
  for (const row of rows) if (!Array.isArray(row) || row.length !== width) throw new Error(`WORKBOOK_${key.toUpperCase()}_WIDTH_INVALID`);
  return rows;
}

function validatePayload(payload) {
  // Do not manufacture source-completeness gates during workbook authoring.
  // 生成工作簿时不能自行制造来源完整性验收状态。
  if (!payload || payload.schema !== SCHEMA) throw new Error("WORKBOOK_PAYLOAD_SCHEMA_INVALID");
  if (typeof payload.company_id !== "string" || !payload.company_id.trim()) throw new Error("WORKBOOK_COMPANY_ID_INVALID");
  if (payload.accessible_data_status !== "ACCESSIBLE_DATA_PASS") throw new Error("WORKBOOK_ACCESSIBLE_STATUS_INVALID");
  if (!["ABSOLUTE_COMPLETENESS_SOURCE_GAPS", "ABSOLUTE_COMPLETENESS_PASS"].includes(payload.absolute_completeness_status)) throw new Error("WORKBOOK_ABSOLUTE_STATUS_INVALID");
  if (!payload.gates || typeof payload.gates !== "object") throw new Error("WORKBOOK_GATES_INVALID");
  for (const gate of ["PAGE_GAP_RECOVERY_PASS", "MAIL_ATTACHMENT_SCOPE_PASS", "CONTENT_RECURSION_PASS_WITH_SOURCE_GAPS", "RELATIONSHIP_V2_PASS", "TIMELINE_V2_PASS"]) {
    if (payload.gates[gate] !== true) throw new Error("WORKBOOK_GATE_NOT_PASS");
  }
  if (!payload.counts || typeof payload.counts !== "object") throw new Error("WORKBOOK_COUNTS_INVALID");
  for (const value of Object.values(payload.counts)) requireInteger(value, "WORKBOOK_COUNT_INVALID");
  if (!payload.detail_limits || typeof payload.detail_limits !== "object") throw new Error("WORKBOOK_LIMITS_INVALID");
  for (const table of TABLES) requireRows(payload, table.key, table.headers.length);
  if (payload.mail_attachment_rows.length > payload.counts.mail_attachment_relations) throw new Error("WORKBOOK_MAIL_ROWS_OVERFLOW");
  if (payload.explicit_relation_rows.length > payload.counts.explicit_relations) throw new Error("WORKBOOK_RELATION_ROWS_OVERFLOW");
  if (payload.business_timeline_rows.length > payload.counts.business_timeline) throw new Error("WORKBOOK_BUSINESS_ROWS_OVERFLOW");
  if (payload.capture_timeline_rows.length > payload.counts.capture_timeline) throw new Error("WORKBOOK_CAPTURE_ROWS_OVERFLOW");
  if (payload.source_gap_rows.length !== payload.counts.source_gaps) throw new Error("WORKBOOK_SOURCE_GAP_COUNT_MISMATCH");
}

function applyRowCap(payload, rawCap) {
  // Display limits must not be confused with the authoritative full dataset.
  // 展示上限不能与完整权威数据集混为一谈。
  if (rawCap === undefined) return;
  const cap = Number(rawCap);
  if (!Number.isSafeInteger(cap) || cap < 1000) throw new Error("WORKBOOK_ROW_CAP_INVALID");
  const keys = ["explicit_relation_rows", "business_timeline_rows", "capture_timeline_rows"];
  for (const key of keys) payload[key] = payload[key].slice(0, cap);
  payload.detail_limits.max_rows_per_sheet = cap;
  const coverageByLabel = new Map([
    ["显式关系", "explicit_relation_rows"],
    ["业务时间线", "business_timeline_rows"],
    ["采集时间线", "capture_timeline_rows"],
  ]);
  for (const row of payload.coverage_rows) {
    const key = coverageByLabel.get(row[0]);
    if (!key) continue;
    row[2] = payload[key].length;
    row[3] = Math.max(0, Number(row[1]) - row[2]);
  }
}

function applyTitle(sheet, title, note, width) {
  const last = columnName(width);
  sheet.getRange(`A1:${last}1`).merge();
  sheet.getRange("A1").values = [[safeCell(title)]];
  sheet.getRange(`A1:${last}1`).format = { fill: "#0B2545", font: { bold: true, color: "#FFFFFF", size: 16 }, horizontalAlignment: "left", verticalAlignment: "center" };
  sheet.getRange(`A1:${last}1`).format.rowHeight = 30;
  sheet.getRange(`A2:${last}2`).merge();
  sheet.getRange("A2").values = [[safeCell(note)]];
  sheet.getRange(`A2:${last}2`).format = { fill: "#E8EEF5", font: { color: "#1F3A5F", size: 10 }, wrapText: true, verticalAlignment: "center" };
  sheet.getRange(`A2:${last}2`).format.rowHeight = 32;
  sheet.showGridLines = false;
}
function applyHeader(range) {
  range.format = { fill: "#2E74B5", font: { bold: true, color: "#FFFFFF", size: 10 }, horizontalAlignment: "center", verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: "#D9E2F3" } };
  range.format.rowHeight = 30;
}
function writeTableSheet(workbook, config, sourceRows) {
  const sheet = workbook.worksheets.add(config.name);
  const width = config.headers.length;
  const last = columnName(width);
  applyTitle(sheet, config.title, config.note, width);
  sheet.getRange(`A4:${last}4`).values = [config.headers];
  applyHeader(sheet.getRange(`A4:${last}4`));
  const rows = sourceRows.length ? sourceRows : [config.headers.map((_, index) => index === 0 ? "无记录" : "")];
  for (let start = 0; start < rows.length; start += 4000) {
    const chunk = safeRows(rows.slice(start, start + 4000));
    sheet.getRangeByIndexes(4 + start, 0, chunk.length, width).values = chunk;
  }
  const endRow = 4 + rows.length;
  const table = sheet.tables.add(`A4:${last}${endRow}`, true, config.tableName);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
  table.showBandedRows = true;
  sheet.getRange(`A5:${last}${endRow}`).format = { font: { color: "#1F2937", size: 10 }, verticalAlignment: "top", wrapText: config.wrap };
  for (const index of config.numeric) sheet.getRange(`${columnName(index + 1)}5:${columnName(index + 1)}${endRow}`).format.numberFormat = "#,##0";
  config.widths.forEach((value, index) => { sheet.getRange(`${columnName(index + 1)}4:${columnName(index + 1)}${endRow}`).format.columnWidth = value; });
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(1);
  return { sheet, endRow, last, records: sourceRows.length };
}

function insertFrozenPane(xml) {
  const root = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)worksheet\b/.exec(xml);
  if (!root) throw new Error("WORKBOOK_WORKSHEET_XML_INVALID");
  const prefix = root[1];
  const pane = `<${prefix}pane xSplit="1" ySplit="4" topLeftCell="B5" activePane="bottomRight" state="frozen"/>`;
  let output = xml.replace(/<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?pane\b[^>]*\/\s*>/g, "");
  const selfClosingView = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)sheetView\b([^>]*)\/\s*>/;
  if (selfClosingView.test(output)) return output.replace(selfClosingView, (_m, p, a) => `<${p}sheetView${a}>${pane}</${p}sheetView>`);
  const openingView = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)sheetView\b[^>]*>/;
  if (openingView.test(output)) return output.replace(openingView, (match) => `${match}${pane}`);
  const views = `<${prefix}sheetViews><${prefix}sheetView workbookViewId="0">${pane}</${prefix}sheetView></${prefix}sheetViews>`;
  const dimension = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)dimension\b[^>]*\/\s*>/;
  if (dimension.test(output)) return output.replace(dimension, (match) => `${match}${views}`);
  return output.replace(/<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)worksheet\b[^>]*>/, (match) => `${match}${views}`);
}
function insertAutoFilter(xml, reference) {
  const root = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)worksheet\b/.exec(xml);
  if (!root) throw new Error("WORKBOOK_WORKSHEET_XML_INVALID");
  const prefix = root[1];
  let output = xml
    .replace(/<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?autoFilter\b[^>]*\/\s*>/g, "")
    .replace(/<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?autoFilter\b[^>]*>[\s\S]*?<\/(?:[A-Za-z_][A-Za-z0-9_.-]*:)?autoFilter>/g, "");
  const closingSheetData = /<\/((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)sheetData>/;
  if (!closingSheetData.test(output)) throw new Error("WORKBOOK_SHEET_DATA_MISSING");
  return output.replace(closingSheetData, (match) => `${match}<${prefix}autoFilter ref="${reference}"/>`);
}
function stripAutoCoercePrefix(xml) {
  const pattern = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)c\b([^>]*?)\bt="str"([^>]*)>\s*<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)v>'((?:\d{11,}|\d+(?:\.\d+)?[Ee][+\-]?\d+|\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]+)?))<\/\4v>\s*<\/\1c>/gi;
  let count = 0;
  return { output: xml.replace(pattern, (_m, prefix, before, after, _valuePrefix, value) => {
    count += 1;
    return `<${prefix}c${before}t="inlineStr"${after}><${prefix}is><${prefix}t>\u200B${value}</${prefix}t></${prefix}is></${prefix}c>`;
  }), count: () => count };
}
async function patchAndInspectXlsx(rawBytes, filterRefs) {
  // Verify exported XML rather than trusting in-memory filter/freeze settings.
  // 检查导出的 XML，而非仅相信内存中的筛选和冻结配置。
  const zip = await JSZip.loadAsync(rawBytes);
  const worksheetNames = Object.keys(zip.files).filter((name) => /^xl\/worksheets\/sheet\d+\.xml$/.test(name));
  if (!worksheetNames.length) throw new Error("WORKBOOK_WORKSHEETS_MISSING");
  let removed = 0;
  if (!Array.isArray(filterRefs) || filterRefs.length !== worksheetNames.length) throw new Error("WORKBOOK_FILTER_REFS_INVALID");
  for (const [index, name] of worksheetNames.entries()) {
    const xml = await zip.file(name).async("string");
    const stripped = stripAutoCoercePrefix(insertAutoFilter(insertFrozenPane(xml), filterRefs[index]));
    removed += stripped.count();
    zip.file(name, stripped.output);
  }
  const bytes = await zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE", compressionOptions: { level: 6 } });
  const checked = await JSZip.loadAsync(bytes);
  let filterCount = 0; let paneCount = 0;
  for (const name of worksheetNames) {
    const xml = await checked.file(name).async("string");
    filterCount += /<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?autoFilter\b/.test(xml) ? 1 : 0;
    paneCount += /<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?pane\b[^>]*state="frozen"/.test(xml) ? 1 : 0;
  }
  return { bytes, sheets: worksheetNames.length, filters: filterCount, panes: paneCount, removed };
}
function formulaErrorCount(ndjson) {
  let count = 0;
  for (const line of String(ndjson || "").split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const root = JSON.parse(line);
      const walk = (value) => {
        if (typeof value === "string" && ERROR_TOKEN.test(value)) count += 1;
        else if (Array.isArray(value)) value.forEach(walk);
        else if (value && typeof value === "object") Object.values(value).forEach(walk);
      };
      walk(root);
    } catch { /* inspect may include a compact non-JSON summary */ }
  }
  return count;
}
async function renderPreview(workbook, sheetName, range, outputPath) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  const bytes = new Uint8Array(await preview.arrayBuffer());
  await fs.writeFile(outputPath, bytes);
  return { file: path.basename(outputPath), bytes: bytes.byteLength, sha256: sha256(bytes) };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const inputBytes = await fs.readFile(args.input);
  const payload = JSON.parse(inputBytes.toString("utf8"));
  applyRowCap(payload, args["row-cap"]);
  validatePayload(payload);
  if (args["validate-only"]) {
    process.stdout.write(`${JSON.stringify({ status: "VALID", schema: payload.schema, sheets: TABLES.length + 1, rows: Object.fromEntries(TABLES.map((item) => [item.key, payload[item.key].length])) })}\n`);
    return;
  }

  const workbook = Workbook.create();
  const overviewRows = [
    ["客户ID", payload.company_id, "按文本保存；单案例硬绑定"],
    ["生成时间UTC", payload.generated_at_utc || "", "交付目录生成时间"],
    ["可取得数据状态", payload.accessible_data_status, "当前可访问内容全部处理、关联并验收"],
    ["绝对完整状态", payload.absolute_completeness_status, "删除、失效、需密码等客观源缺口单列"],
    ["旧版文件", payload.counts.old_files, "原合格成果未覆盖"],
    ["v2证据文件", payload.counts.v2_files, "含历史邮件附件及新补采"],
    ["历史邮件附件原件", payload.counts.historical_mail_files, "字节与SHA绑定"],
    ["邮件附件出现记录", payload.counts.attachment_occurrences, "多次出现不因SHA去重丢失"],
    ["唯一邮件附件关系", payload.counts.mail_attachment_relations, "邮件→附件明确边"],
    ["多父附件", payload.counts.multiparent_attachments, "一件附件对应多封邮件"],
    ["显式关系", payload.counts.explicit_relations, "完整列表在SQLite/Parquet"],
    ["业务时间事件", payload.counts.business_timeline, "业务发生顺序"],
    ["采集时间事件", payload.counts.capture_timeline, "证据采集/处理顺序"],
    ["客观源缺口", payload.counts.source_gaps, "不冒充绝对完整"],
  ];
  const overview = writeTableSheet(workbook, {
    name: "概览", tableName: "CompletionOverviewTable", title: "单客户全案例递归补全 v2",
    note: "深蓝风格延续旧版。Excel承担审计导航和可控明细；超出工作簿展示上限的完整数据保留在本地SQLite/Parquet。",
    headers: ["指标", "值", "说明"], widths: [30, 74, 70], numeric: [], wrap: true,
  }, overviewRows);
  const written = { overview };
  for (const config of TABLES) written[config.key] = writeTableSheet(workbook, config, payload[config.key]);

  const formulaScan = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 300 }, summary: "completion v2 formula error scan", maxChars: 8000 });
  const formulaErrors = formulaErrorCount(formulaScan.ndjson);
  if (formulaErrors !== 0) throw new Error("WORKBOOK_FORMULA_ERRORS_PRESENT");

  await fs.mkdir(path.dirname(args.output), { recursive: true });
  await fs.mkdir(args["preview-dir"], { recursive: true });
  const exported = await SpreadsheetFile.exportXlsx(workbook);
  const rawPath = `${args.output}.xlsx.tmp`;
  await exported.save(rawPath);
  let patched;
  const filterRefs = [`A4:C${overview.endRow}`, ...TABLES.map((config) => `A4:${written[config.key].last}${written[config.key].endRow}`)];
  try { patched = await patchAndInspectXlsx(await fs.readFile(rawPath), filterRefs); await fs.writeFile(args.output, patched.bytes); }
  finally { await fs.rm(rawPath, { force: true }); }
  if (patched.filters !== patched.sheets || patched.panes !== patched.sheets) throw new Error("WORKBOOK_OOXML_CONTROLS_MISSING");

  const finalWorkbook = await SpreadsheetFile.importXlsx(await FileBlob.load(args.output));
  const previewSpecs = [["概览", `A1:C${overview.endRow}`]];
  TABLES.forEach((config, index) => previewSpecs.push([config.name, `A1:${written[config.key].last}${Math.min(written[config.key].endRow, 28)}`, `${String(index + 2).padStart(2, "0")}_${config.key}.png`]));
  previewSpecs[0].push("01_overview.png");
  const previews = [];
  for (const [sheetName, range, filename] of previewSpecs) previews.push(await renderPreview(finalWorkbook, sheetName, range, path.join(args["preview-dir"], filename)));

  const verification = {
    schema: VERIFY_SCHEMA,
    status: "PASS",
    input_sha256: sha256(inputBytes),
    output: { file: path.basename(args.output), bytes: patched.bytes.byteLength, sha256: sha256(patched.bytes) },
    sheets: ["概览", ...TABLES.map((item) => item.name)],
    formula_errors: formulaErrors,
    all_sheets_rendered: previews.length === TABLES.length + 1,
    all_filters_present: patched.filters === patched.sheets,
    all_freeze_panes_present: patched.panes === patched.sheets,
    counts: Object.fromEntries(TABLES.map((item) => [item.key, payload[item.key].length])),
    controls: { open_source_authored: true, preview_renderer: "local_svg_sharp_not_office", native_office_open_verified: false, dangerous_formula_prefixes_escaped: true, escaped_dangerous_cells: escapedCellCount, forced_text_cells: forcedTextCellCount, ooxml_text_prefixes_removed: patched.removed },
    previews,
    inspection_hash: sha256(Buffer.from(formulaScan.ndjson, "utf8")),
  };
  await fs.mkdir(path.dirname(args.verification), { recursive: true });
  await fs.writeFile(args.verification, `${JSON.stringify(verification, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify({ status: "PASS", sheets: verification.sheets.length, previews: previews.length, formula_errors: formulaErrors })}\n`);
}

main().catch((error) => {
  const code = error instanceof Error && /^[A-Z0-9_]+$/.test(error.message) ? error.message : "WORKBOOK_BUILD_FAILED";
  process.stderr.write(`${code}\n`);
  if (process.env.OKKI_SYNTHETIC_DEBUG === "1" && error instanceof Error) process.stderr.write(`${error.stack}\n`);
  process.exitCode = 2;
});
