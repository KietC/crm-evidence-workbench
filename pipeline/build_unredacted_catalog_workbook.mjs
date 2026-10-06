import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

// Local workbook generation never contacts a document or model service.
// 本地工作簿生成不连接文档服务或模型服务。
import { FileBlob, SpreadsheetFile, Workbook } from "./lib/open_workbook.mjs";
import JSZip from "jszip";

const SCHEMA = "okki.single_customer.unredacted_catalog_workbook_payload.v1";
const VERIFY_SCHEMA = "okki.single_customer.unredacted_catalog_workbook_verification.v1";
const DANGEROUS_CELL_PREFIX = /^[\t\r\n ]*[=+\-@]/;
const EXCEL_AUTO_COERCE_TEXT = /^(?:\d{11,}|\d+(?:\.\d+)?[Ee][+\-]?\d+|\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]+)?)$/i;
const MAX_EXCEL_CELL_CHARS = 32760;
const MAX_LOGICAL_RELATIONS = 100000;

const TABLES = [
  {
    key: "coverage_rows", name: "覆盖", tableName: "UnredactedCoverageTable",
    title: "采集与处理覆盖", note: "按数据域核对预期、已入库、未完成与来源；不含客户正文。",
    headers: ["数据域", "预期数量", "已入库", "未完成", "状态", "来源"],
    widths: [26, 14, 14, 14, 20, 54], numeric: [1, 2, 3], wrap: true,
  },
  {
    key: "entity_count_rows", name: "实体计数", tableName: "UnredactedEntityCountsTable",
    title: "实体节点计数", note: "只显示按实体类型聚合后的数量；节点原值在本地 Explorer 中按页查看。",
    headers: ["实体类型", "节点数", "唯一值数", "说明"],
    widths: [28, 16, 16, 56], numeric: [1, 2], wrap: true,
  },
  {
    key: "node_detail_rows", name: "实体节点", tableName: "UnredactedNodesTable",
    title: "不匿名实体节点明细", note: "保留节点 ID、类型和原始值；危险公式前缀写入时自动转成纯文本。",
    headers: ["节点ID", "实体类型", "原始值", "标准化值", "关系证据数", "首个来源"],
    widths: [42, 24, 64, 64, 16, 58], numeric: [4], wrap: false,
  },
  {
    key: "relation_stat_rows", name: "关系统计", tableName: "UnredactedRelationStatsTable",
    title: "关系类型统计", note: "逻辑事实与原始 occurrence 分开计数；完整 occurrence 不进入 Excel。",
    headers: ["关系类型", "逻辑事实数", "发生次数", "独立来源数", "说明"],
    widths: [34, 16, 16, 16, 56], numeric: [1, 2, 3], wrap: true,
  },
  {
    key: "file_catalog_rows", name: "文件目录", tableName: "UnredactedFileCatalogTable",
    title: "文件与证据目录", note: "目录只保存标识、相对路径、字节、哈希、类型与状态；文件正文不复制到工作簿。",
    headers: ["文件ID", "分类", "相对路径", "字节", "SHA256", "MIME", "状态"],
    widths: [30, 22, 64, 16, 68, 28, 20], numeric: [3], wrap: false,
  },
  {
    key: "task_catalog_rows", name: "任务目录", tableName: "UnredactedTaskCatalogTable",
    title: "处理任务目录", note: "用于核对 OCR、ASR、解析和索引任务的输入、状态、输出与回执。",
    headers: ["任务ID", "任务类型", "来源文件", "状态", "输出路径", "结果SHA256", "尝试次数", "错误码"],
    widths: [34, 26, 58, 20, 58, 68, 14, 28], numeric: [6], wrap: false,
  },
  {
    key: "logical_relation_rows", name: "逻辑关系", tableName: "UnredactedLogicalRelationsTable",
    title: "去重逻辑关系事实", note: "每行是一条去重后的逻辑关系事实。发生明细留在 SQLite relation_occurrence 中严格分页查看。",
    headers: ["事实ID", "起点ID", "起点类型", "起点原值", "关系", "终点ID", "终点类型", "终点原值", "证据数", "首个来源", "末个来源"],
    widths: [34, 40, 22, 56, 34, 40, 22, 56, 14, 54, 54], numeric: [8], wrap: false,
  },
];

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) throw new Error("WORKBOOK_ARGUMENTS_INVALID");
    result[key.slice(2)] = value;
  }
  for (const required of ["input", "output", "preview-dir", "verification"]) {
    if (!result[required]) throw new Error(`WORKBOOK_ARGUMENT_MISSING_${required.toUpperCase().replaceAll("-", "_")}`);
  }
  return result;
}

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex").toUpperCase();
}

let escapedCellCount = 0;
let forcedTextCellCount = 0;

function safeCell(value) {
  // Reject unsupported cell values and preserve dangerous prefixes as text.
  // 拒绝不支持的单元格值，将危险前缀保留为纯文本。
  if (value === null || value === undefined) return "";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("WORKBOOK_NONFINITE_NUMBER");
    return value;
  }
  if (typeof value === "boolean" || value instanceof Date) return value;
  if (typeof value === "object") throw new Error("WORKBOOK_CELL_OBJECT_FORBIDDEN");
  const text = String(value);
  if (text.length > MAX_EXCEL_CELL_CHARS) throw new Error("WORKBOOK_CELL_TOO_LONG");
  if (text.startsWith("'")) return text;
  if (DANGEROUS_CELL_PREFIX.test(text)) {
    escapedCellCount += 1;
    return `'${text}`;
  }
  if (EXCEL_AUTO_COERCE_TEXT.test(text)) {
    forcedTextCellCount += 1;
    return `'${text}`;
  }
  return text;
}

function safeRows(rows) {
  return rows.map((row) => row.map(safeCell));
}

function columnName(number) {
  let value = number;
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

function requireInteger(value, code) {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(code);
  return value;
}

function requireRows(payload, key, width) {
  const rows = payload[key];
  if (!Array.isArray(rows)) throw new Error(`WORKBOOK_${key.toUpperCase()}_INVALID`);
  for (const row of rows) {
    if (!Array.isArray(row) || row.length !== width) throw new Error(`WORKBOOK_${key.toUpperCase()}_WIDTH_INVALID`);
  }
  return rows;
}

function validatePayload(payload) {
  // Count equality refers to this supplied payload, not a historical customer.
  // 数量一致性只针对本次输入，不针对历史客户。
  if (!payload || payload.schema !== SCHEMA) throw new Error("WORKBOOK_PAYLOAD_SCHEMA_INVALID");
  if (typeof payload.company_id !== "string" || !payload.company_id.trim()) throw new Error("WORKBOOK_COMPANY_ID_INVALID");
  if (!payload.database || payload.database.integrity !== "ok") throw new Error("WORKBOOK_DATABASE_INTEGRITY_INVALID");
  if (!/^[A-Fa-f0-9]{64}$/.test(String(payload.database.sha256 || ""))) throw new Error("WORKBOOK_DATABASE_SHA256_INVALID");
  if (!payload.counts || typeof payload.counts !== "object") throw new Error("WORKBOOK_COUNTS_INVALID");
  for (const key of ["documents", "nodes", "relation_facts", "relation_occurrences", "files", "tasks"]) {
    requireInteger(payload.counts[key], `WORKBOOK_COUNT_${key.toUpperCase()}_INVALID`);
  }
  for (const forbidden of ["document_rows", "document_text_rows", "full_text_rows", "relation_occurrence_rows", "occurrence_rows"]) {
    if (Object.hasOwn(payload, forbidden)) throw new Error(`WORKBOOK_FORBIDDEN_PAYLOAD_${forbidden.toUpperCase()}`);
  }
  for (const table of TABLES) requireRows(payload, table.key, table.headers.length);
  if (payload.logical_relation_rows.length > MAX_LOGICAL_RELATIONS) throw new Error("WORKBOOK_LOGICAL_RELATIONS_LIMIT_EXCEEDED");
  if (payload.logical_relation_rows.length !== payload.counts.relation_facts) throw new Error("WORKBOOK_LOGICAL_RELATION_COUNT_MISMATCH");
  if (payload.node_detail_rows.length !== payload.counts.nodes) throw new Error("WORKBOOK_NODE_COUNT_MISMATCH");
  if (payload.file_catalog_rows.length !== payload.counts.files) throw new Error("WORKBOOK_FILE_COUNT_MISMATCH");
  if (payload.task_catalog_rows.length !== payload.counts.tasks) throw new Error("WORKBOOK_TASK_COUNT_MISMATCH");
}

function applyTitle(sheet, title, note, width) {
  const last = columnName(width);
  sheet.getRange(`A1:${last}1`).merge();
  sheet.getRange("A1").values = [[safeCell(title)]];
  sheet.getRange(`A1:${last}1`).format = {
    fill: "#0B2545", font: { bold: true, color: "#FFFFFF", size: 16 },
    horizontalAlignment: "left", verticalAlignment: "center",
  };
  sheet.getRange(`A1:${last}1`).format.rowHeight = 30;
  sheet.getRange(`A2:${last}2`).merge();
  sheet.getRange("A2").values = [[safeCell(note)]];
  sheet.getRange(`A2:${last}2`).format = {
    fill: "#E8EEF5", font: { color: "#1F3A5F", size: 10 },
    wrapText: true, verticalAlignment: "center",
  };
  sheet.getRange(`A2:${last}2`).format.rowHeight = 30;
  sheet.showGridLines = false;
}

function applyHeader(range) {
  range.format = {
    fill: "#2E74B5", font: { bold: true, color: "#FFFFFF", size: 10 },
    horizontalAlignment: "center", verticalAlignment: "center", wrapText: true,
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
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
  const chunkSize = 5000;
  for (let start = 0; start < rows.length; start += chunkSize) {
    const chunk = safeRows(rows.slice(start, start + chunkSize));
    sheet.getRangeByIndexes(4 + start, 0, chunk.length, width).values = chunk;
  }
  const endRow = 4 + rows.length;
  const table = sheet.tables.add(`A4:${last}${endRow}`, true, config.tableName);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
  table.showBandedRows = true;
  const body = sheet.getRange(`A5:${last}${endRow}`);
  body.format = {
    font: { color: "#1F2937", size: 10 }, verticalAlignment: "top", wrapText: config.wrap,
  };
  for (const index of config.numeric) {
    const column = columnName(index + 1);
    sheet.getRange(`${column}5:${column}${endRow}`).format.numberFormat = "#,##0";
  }
  config.widths.forEach((widthValue, index) => {
    const column = columnName(index + 1);
    sheet.getRange(`${column}4:${column}${endRow}`).format.columnWidth = widthValue;
  });
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(1);
  return { sheet, endRow, last, records: sourceRows.length };
}

async function renderPreview(workbook, sheetName, range, outputPath) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  const bytes = new Uint8Array(await preview.arrayBuffer());
  await fs.writeFile(outputPath, bytes);
  return { file: path.basename(outputPath), bytes: bytes.byteLength, sha256: sha256(bytes) };
}

function insertFrozenPane(xml) {
  const root = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)worksheet\b/.exec(xml);
  if (!root) throw new Error("WORKBOOK_WORKSHEET_XML_INVALID");
  const prefix = root[1];
  const pane = `<${prefix}pane xSplit="1" ySplit="4" topLeftCell="B5" activePane="bottomRight" state="frozen"/>`;
  let output = xml.replace(/<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?pane\b[^>]*\/\s*>/g, "");
  const selfClosingView = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)sheetView\b([^>]*)\/\s*>/;
  if (selfClosingView.test(output)) {
    return output.replace(selfClosingView, (_match, viewPrefix, attributes) => `<${viewPrefix}sheetView${attributes}>${pane}</${viewPrefix}sheetView>`);
  }
  const openingView = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)sheetView\b[^>]*>/;
  if (openingView.test(output)) return output.replace(openingView, (match) => `${match}${pane}`);
  const views = `<${prefix}sheetViews><${prefix}sheetView workbookViewId="0">${pane}</${prefix}sheetView></${prefix}sheetViews>`;
  const dimension = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)dimension\b[^>]*\/\s*>/;
  if (dimension.test(output)) return output.replace(dimension, (match) => `${match}${views}`);
  const worksheet = /<((?:[A-Za-z_][A-Za-z0-9_.-]*:)?)worksheet\b[^>]*>/;
  if (worksheet.test(output)) return output.replace(worksheet, (match) => `${match}${views}`);
  throw new Error("WORKBOOK_WORKSHEET_XML_INVALID");
}

function stripAutoCoercePrefix(xml) {
  const valuePattern = /(<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?v>)'((?:\d{11,}|\d+(?:\.\d+)?[Ee][+\-]?\d+|\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]+)?))(<\/(?:[A-Za-z_][A-Za-z0-9_.-]*:)?v>)/gi;
  let count = 0;
  const output = xml.replace(valuePattern, (_match, opening, value, closing) => {
    count += 1;
    return `${opening}${value}${closing}`;
  });
  return { output, count };
}

async function patchFrozenPanes(xlsxBytes) {
  // Patch view metadata only; never substitute or trim underlying cell content.
  // 仅补齐视图元数据，绝不替换或裁剪底层单元格正文。
  const zip = await JSZip.loadAsync(xlsxBytes);
  const worksheetNames = Object.keys(zip.files).filter((name) => /^xl\/worksheets\/sheet\d+\.xml$/.test(name));
  if (!worksheetNames.length) throw new Error("WORKBOOK_WORKSHEETS_MISSING");
  let unquoted = 0;
  for (const name of worksheetNames) {
    const xml = await zip.file(name).async("string");
    const stripped = stripAutoCoercePrefix(insertFrozenPane(xml));
    unquoted += stripped.count;
    zip.file(name, stripped.output);
  }
  const output = await zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE", compressionOptions: { level: 6 } });
  return { bytes: output, patched: worksheetNames.length, unquoted };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const inputBytes = await fs.readFile(args.input);
  const payload = JSON.parse(inputBytes.toString("utf8"));
  validatePayload(payload);

  const workbook = Workbook.create();
  const navigationRows = [
    ["概览", "数据库身份、状态与核心规模", 1, "不含客户全文"],
    ["覆盖", "各数据域完成度与缺口", payload.coverage_rows.length, "聚合数据"],
    ["实体计数", "实体类型数量统计", payload.entity_count_rows.length, "不含节点原值"],
    ["实体节点", "实体 ID、类型和不匿名原值", payload.node_detail_rows.length, "不含全文"],
    ["关系统计", "关系类型、事实与发生次数", payload.relation_stat_rows.length, "不含 occurrence 明细"],
    ["文件目录", "原件与派生文件索引", payload.file_catalog_rows.length, "不含文件正文"],
    ["任务目录", "处理任务、状态与输出索引", payload.task_catalog_rows.length, "不含任务正文"],
    ["逻辑关系", "去重后的逻辑关系事实", payload.logical_relation_rows.length, "不含 relation_occurrence 明细"],
    ["本地 Explorer", "全文、节点原值与 occurrence 分页查询", payload.counts.relation_occurrences, safeCell(payload.explorer?.url || "http://127.0.0.1:18765/")],
  ];
  const navigation = writeTableSheet(workbook, {
    name: "导航", tableName: "UnredactedNavigationTable", title: "不匿名本地交付导航",
    note: "Excel 只承担导航、概览、覆盖、计数和目录。全文及大规模发生明细请使用 127.0.0.1 只读 Explorer。",
    headers: ["入口", "用途", "记录数", "内容边界 / 地址"], widths: [24, 54, 16, 58], numeric: [2], wrap: true,
  }, navigationRows);

  const overviewRows = [
    ["公司ID", payload.company_id, "主键；按文本保存"],
    ["生成时间UTC", payload.generated_at_utc || "", "目录工作簿生成时间"],
    ["数据库文件", payload.database.filename || "customer_full_unredacted.sqlite3", "本机不匿名资料库"],
    ["数据库SHA256", payload.database.sha256.toUpperCase(), "用于核对 Explorer 与 Excel 是否绑定同一份库"],
    ["数据库字节", requireInteger(payload.database.bytes, "WORKBOOK_DATABASE_BYTES_INVALID"), "只读交付文件大小"],
    ["SQLite完整性", payload.database.integrity, "必须为 ok"],
    ["全文文档", payload.counts.documents, "正文只在 Explorer 查询"],
    ["实体节点", payload.counts.nodes, "不匿名原值已进入实体节点工作表"],
    ["逻辑关系事实", payload.counts.relation_facts, "已进入逻辑关系工作表"],
    ["关系发生记录", payload.counts.relation_occurrences, "不进入 Excel；Explorer 严格分页"],
    ["文件目录", payload.counts.files, "仅索引和哈希"],
    ["处理任务", payload.counts.tasks, "仅状态和输出索引"],
  ];
  const overview = writeTableSheet(workbook, {
    name: "概览", tableName: "UnredactedOverviewTable", title: "单客户不匿名资料库概览",
    note: "不匿名实体节点和关系两端原值已进入工作簿；全文和完整 occurrence 留在本机 Explorer，避免 Excel 失控。",
    headers: ["指标", "值", "说明"], widths: [28, 72, 64], numeric: [], wrap: true,
  }, overviewRows);
  overview.sheet.getRange("B5:B16").format.numberFormat = "@";
  for (const row of [9, 11, 12, 13, 14, 15, 16]) overview.sheet.getRange(`B${row}`).format.numberFormat = "#,##0";

  const written = { navigation, overview };
  for (const table of TABLES) written[table.key] = writeTableSheet(workbook, table, payload[table.key]);

  await fs.mkdir(path.dirname(args.output), { recursive: true });
  await fs.mkdir(args["preview-dir"], { recursive: true });
  const previewSpecs = [
    ["导航", `A1:D${navigation.endRow}`, "01_navigation.png"],
    ["概览", `A1:C${overview.endRow}`, "02_overview.png"],
    ["覆盖", `A1:F${Math.min(written.coverage_rows.endRow, 28)}`, "03_coverage.png"],
    ["实体计数", `A1:D${Math.min(written.entity_count_rows.endRow, 24)}`, "04_entity_counts.png"],
    ["实体节点", `A1:F${Math.min(written.node_detail_rows.endRow, 24)}`, "05_entity_nodes.png"],
    ["关系统计", `A1:E${Math.min(written.relation_stat_rows.endRow, 24)}`, "06_relation_stats.png"],
    ["文件目录", `A1:G${Math.min(written.file_catalog_rows.endRow, 24)}`, "07_file_catalog.png"],
    ["任务目录", `A1:H${Math.min(written.task_catalog_rows.endRow, 24)}`, "08_task_catalog.png"],
    ["逻辑关系", `A1:K${Math.min(written.logical_relation_rows.endRow, 24)}`, "09_logical_relations.png"],
  ];

  const formulaScan = await workbook.inspect({
    kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 200 }, summary: "final formula error scan", maxChars: 4000,
  });
  const keyInspection = await workbook.inspect({
    kind: "table", range: "概览!A1:C16", include: "values,formulas",
    tableMaxRows: 16, tableMaxCols: 3, maxChars: 4000,
  });
  const sheetInspection = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 4000 });

  const exported = await SpreadsheetFile.exportXlsx(workbook);
  const rawPath = `${args.output}.xlsx.tmp`;
  await exported.save(rawPath);
  let patched;
  try {
    const rawBytes = await fs.readFile(rawPath);
    patched = await patchFrozenPanes(rawBytes);
    await fs.writeFile(args.output, patched.bytes);
  } finally {
    await fs.rm(rawPath, { force: true });
  }

  const finalWorkbook = await SpreadsheetFile.importXlsx(await FileBlob.load(args.output));
  const previews = [];
  for (const [sheetName, range, filename] of previewSpecs) {
    previews.push(await renderPreview(finalWorkbook, sheetName, range, path.join(args["preview-dir"], filename)));
  }

  const verification = {
    schema: VERIFY_SCHEMA,
    status: "PASS",
    input_sha256: sha256(inputBytes),
    output: { file: path.basename(args.output), bytes: patched.bytes.byteLength, sha256: sha256(patched.bytes) },
    sheets: ["导航", "概览", "覆盖", "实体计数", "实体节点", "关系统计", "文件目录", "任务目录", "逻辑关系"],
    counts: {
      coverage_rows: payload.coverage_rows.length,
      entity_count_rows: payload.entity_count_rows.length,
      node_detail_rows: payload.node_detail_rows.length,
      relation_stat_rows: payload.relation_stat_rows.length,
      file_catalog_rows: payload.file_catalog_rows.length,
      task_catalog_rows: payload.task_catalog_rows.length,
      logical_relation_rows: payload.logical_relation_rows.length,
      relation_occurrences_excel_rows: 0,
      previews: previews.length,
      freeze_pane_xml_sheets: patched.patched,
      escaped_dangerous_cells: escapedCellCount,
      forced_text_cells: forcedTextCellCount,
      ooxml_text_prefixes_removed: patched.unquoted,
    },
    controls: {
      open_source_authored: true,
      preview_renderer: "local_svg_sharp_not_office",
      native_office_open_verified: false,
      tables_and_filters: true,
      frozen_rows: 4,
      frozen_columns: 1,
      full_text_exported: false,
      relation_occurrences_exported: false,
      dangerous_formula_prefixes_escaped: true,
    },
    previews,
    inspection_hashes: {
      formula_error_scan: sha256(Buffer.from(formulaScan.ndjson, "utf8")),
      key_range: sha256(Buffer.from(keyInspection.ndjson, "utf8")),
      sheets: sha256(Buffer.from(sheetInspection.ndjson, "utf8")),
    },
  };
  await fs.writeFile(args.verification, `${JSON.stringify(verification, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify({ status: "PASS", sheets: verification.sheets.length, logical_relations: payload.logical_relation_rows.length, previews: previews.length })}\n`);
}

main().catch((error) => {
  const code = error instanceof Error && /^[A-Z0-9_]+$/.test(error.message) ? error.message : "WORKBOOK_BUILD_FAILED";
  process.stderr.write(`${code}\n`);
  if (process.env.OKKI_SYNTHETIC_DEBUG === "1" && error instanceof Error) process.stderr.write(`${error.stack}\n`);
  process.exitCode = 2;
});
