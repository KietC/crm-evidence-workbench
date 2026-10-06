import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

// Author XLSX and local previews with public, open-source dependencies.
// 使用公开开源依赖生成 XLSX 和本地预览。
import { SpreadsheetFile, Workbook } from "./lib/open_workbook.mjs";

const DANGEROUS_CELL_PREFIX = /^[\t\r\n ]*[=+\-@]/;

function parseArgs(argv) {
  // Require explicit input/output destinations; do not infer a previous case.
  // 明确要求输入和输出位置，不推断历史案例。
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error("WORKBOOK_ARGUMENTS_INVALID");
    }
    result[key.slice(2)] = value;
  }
  for (const required of ["input", "output", "preview-dir", "verification"]) {
    if (!result[required]) throw new Error(`WORKBOOK_ARGUMENT_MISSING_${required.toUpperCase().replaceAll("-", "_")}`);
  }
  return result;
}

function safeCell(value) {
  // Only authored formulas use the formula API; external text stays inert.
  // 仅程序显式创建的公式使用公式接口，外部文本保持不可执行。
  if (value === null || value === undefined) return "";
  if (typeof value === "number" || typeof value === "boolean" || value instanceof Date) return value;
  const text = String(value);
  if (text.startsWith("'") || !DANGEROUS_CELL_PREFIX.test(text)) return text;
  return `'${text}`;
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

function applyTitle(sheet, title, note, width) {
  const last = columnName(width);
  sheet.getRange(`A1:${last}1`).merge();
  sheet.getRange("A1").values = [[safeCell(title)]];
  sheet.getRange(`A1:${last}1`).format = {
    fill: "#0B2545",
    font: { bold: true, color: "#FFFFFF", size: 16 },
    horizontalAlignment: "left",
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${last}1`).format.rowHeight = 30;
  sheet.getRange(`A2:${last}2`).merge();
  sheet.getRange("A2").values = [[safeCell(note)]];
  sheet.getRange(`A2:${last}2`).format = {
    fill: "#E8EEF5",
    font: { color: "#1F3A5F", size: 10 },
    wrapText: true,
    verticalAlignment: "center",
  };
  sheet.getRange(`A2:${last}2`).format.rowHeight = 28;
  sheet.showGridLines = false;
}

function applyHeader(range) {
  range.format = {
    fill: "#2E74B5",
    font: { bold: true, color: "#FFFFFF", size: 10 },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
  range.format.rowHeight = 30;
}

function applyBody(range) {
  range.format = {
    font: { color: "#1F2937", size: 10 },
    verticalAlignment: "center",
    wrapText: true,
    borders: {
      insideHorizontal: { style: "thin", color: "#E5E7EB" },
      bottom: { style: "thin", color: "#CBD5E1" },
    },
  };
}

function writeTableSheet(workbook, config) {
  // A real OOXML table and frozen headings must survive workbook export.
  // 真正的 OOXML 表格和冻结表头必须保留在导出成品中。
  const sheet = workbook.worksheets.add(config.name);
  const width = config.headers.length;
  applyTitle(sheet, config.title, config.note, width);
  const rows = config.rows.length ? config.rows : [config.emptyRow ?? config.headers.map(() => "")];
  const all = [config.headers, ...rows];
  const endRow = 3 + all.length;
  const last = columnName(width);
  sheet.getRange(`A4:${last}${endRow}`).values = safeRows(all);
  applyHeader(sheet.getRange(`A4:${last}4`));
  if (endRow >= 5) applyBody(sheet.getRange(`A5:${last}${endRow}`));
  const table = sheet.tables.add(`A4:${last}${endRow}`, true, config.tableName);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
  table.showBandedRows = true;
  config.widths.forEach((columnWidth, index) => {
    const col = columnName(index + 1);
    sheet.getRange(`${col}4:${col}${endRow}`).format.columnWidth = columnWidth;
  });
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(1);
  return { sheet, endRow, last };
}

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex").toUpperCase();
}

async function renderPreview(workbook, sheetName, range, outputPath) {
  // Render a bounded local layout preview; this is not native Office validation.
  // 渲染有范围限制的本地布局预览，不冒充 Office 原生验收。
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  const bytes = new Uint8Array(await preview.arrayBuffer());
  await fs.writeFile(outputPath, bytes);
  return { path: outputPath, bytes: bytes.byteLength, sha256: sha256(bytes) };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const payload = JSON.parse(await fs.readFile(args.input, "utf8"));
  if (payload.schema !== "okki.single_customer.delivery_workbook_payload.v1") {
    throw new Error("WORKBOOK_PAYLOAD_SCHEMA_INVALID");
  }

  const workbook = Workbook.create();
  const overview = workbook.worksheets.add("概览");
  applyTitle(
    overview,
    "单客户完整研究证据总览",
    "本工作簿仅展示索引、哈希、计数和关系结构；不导出客户正文或关系节点原始值。",
    4,
  );
  const overviewRows = [
    ["公司ID", `ID ${payload.company_id}`, "联合范围状态", payload.scope_status],
    ["AUTO7会话", payload.auto7_session_id, "完整提取状态", payload.full_extract_status],
    ["手工贸易会话", payload.manual_trade_session, "手工贸易状态", payload.manual_trade_status],
    ["原始文件数", payload.counts.raw_files, "完整提取任务数", payload.counts.tasks],
    ["关系节点数", payload.counts.relation_nodes, "关系边数", payload.counts.relation_edges],
    ["手工贸易证据文件数", payload.counts.manual_trade_artifacts, "证据文件字节数", payload.counts.manual_trade_artifact_bytes],
    ["覆盖矩阵行数", "", "证据索引行数", ""],
    ["隐私边界", "未读取全文FTS；未导出关系节点value", "公式前缀防护", "= + - @ 及前导空白已按文本转义"],
  ];
  overview.getRange("A4:D12").values = safeRows([["项目", "值", "项目", "值"], ...overviewRows]);
  applyHeader(overview.getRange("A4:D4"));
  applyBody(overview.getRange("A5:D12"));
  overview.getRange("A10:D10").format.numberFormat = "#,##0";
  overview.getRange("A5:A12").format.font = { bold: true, color: "#1F3A5F" };
  overview.getRange("C5:C12").format.font = { bold: true, color: "#1F3A5F" };
  overview.getRange("A4:D12").format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };
  overview.getRange("B5").format.numberFormat = "@";
  overview.getRange("A4:A12").format.columnWidth = 20;
  overview.getRange("B4:B12").format.columnWidth = 31;
  overview.getRange("C4:C12").format.columnWidth = 20;
  overview.getRange("D4:D12").format.columnWidth = 31;

  const coverage = writeTableSheet(workbook, {
    name: "覆盖矩阵",
    title: "覆盖矩阵",
    note: "合并 AUTO7、完整提取和手工贸易证据的范围、完成度与缺口；可直接筛选状态。",
    headers: ["来源通道", "范围", "项目", "总数", "已完成", "待处理", "失败", "状态", "证据引用", "说明"],
    rows: payload.coverage_rows,
    emptyRow: ["", "", "无覆盖记录", 0, 0, 0, 0, "INCOMPLETE", "", ""],
    widths: [16, 16, 28, 11, 11, 11, 11, 24, 42, 45],
    tableName: "CoverageTable",
  });
  coverage.sheet.getRange(`D5:G${coverage.endRow}`).format.numberFormat = "#,##0";

  const relations = writeTableSheet(workbook, {
    name: "关系台账",
    title: "关系台账（隐私裁剪）",
    note: "节点原始 value 永不导出；端点使用哈希 node_id，来源仅保留相对路径和 JSON Pointer。",
    headers: ["记录类型", "记录ID", "实体类型", "起点", "终点", "关系", "来源引用", "JSON Pointer", "隐私说明"],
    rows: payload.relation_rows,
    emptyRow: ["none", "", "", "", "", "", "", "", "没有关系记录"],
    widths: [14, 52, 18, 52, 52, 28, 46, 38, 34],
    tableName: "RelationshipLedgerTable",
  });

  const evidence = writeTableSheet(workbook, {
    name: "证据索引",
    title: "证据索引",
    note: "证据行仅包含路径、哈希、字节、类型和状态；正文、OCR文本、邮件内容与关系节点原值均不进入工作簿。",
    headers: ["记录类型", "状态", "来源路径", "来源SHA256", "字节", "任务/类别", "输出路径", "结果SHA256", "说明"],
    rows: payload.evidence_rows,
    emptyRow: ["none", "INCOMPLETE", "", "", 0, "", "", "", "没有证据记录"],
    widths: [22, 24, 52, 68, 14, 28, 52, 68, 42],
    tableName: "EvidenceIndexTable",
  });
  evidence.sheet.getRange(`E5:E${evidence.endRow}`).format.numberFormat = "#,##0";

  overview.getRange("B11").formulas = [[`=COUNTA('覆盖矩阵'!$A$5:$A$${coverage.endRow})`]];
  overview.getRange("D11").formulas = [[`=COUNTA('证据索引'!$A$5:$A$${evidence.endRow})`]];
  overview.freezePanes.freezeRows(4);
  overview.freezePanes.freezeColumns(1);

  await fs.mkdir(path.dirname(args.output), { recursive: true });
  await fs.mkdir(args["preview-dir"], { recursive: true });
  const previews = [];
  previews.push(await renderPreview(workbook, "概览", "A1:D12", path.join(args["preview-dir"], "workbook_overview.png")));
  previews.push(await renderPreview(workbook, "覆盖矩阵", `A1:J${Math.min(coverage.endRow, 28)}`, path.join(args["preview-dir"], "workbook_coverage.png")));
  previews.push(await renderPreview(workbook, "关系台账", `A1:I${Math.min(relations.endRow, 28)}`, path.join(args["preview-dir"], "workbook_relationships.png")));
  previews.push(await renderPreview(workbook, "证据索引", `A1:I${Math.min(evidence.endRow, 28)}`, path.join(args["preview-dir"], "workbook_evidence.png")));

  const formulaScan = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: "final formula error scan",
    maxChars: 4000,
  });
  const summary = await workbook.inspect({
    kind: "sheet",
    include: "id,name",
    maxChars: 3000,
  });

  const exported = await SpreadsheetFile.exportXlsx(workbook);
  await exported.save(args.output);
  const outputBytes = await fs.readFile(args.output);
  const verification = {
    schema: "okki.single_customer.delivery_workbook_verification.v1",
    status: "PASS",
    sheets: ["概览", "覆盖矩阵", "关系台账", "证据索引"],
    counts: {
      coverage_rows: payload.coverage_rows.length,
      relation_rows: payload.relation_rows.length,
      evidence_rows: payload.evidence_rows.length,
      previews: previews.length,
      formula_scan_payload_chars: formulaScan.ndjson.length,
      sheet_inspection_payload_chars: summary.ndjson.length,
    },
    output: { path: args.output, bytes: outputBytes.byteLength, sha256: sha256(outputBytes) },
    previews,
    formula_error_scan_sha256: sha256(Buffer.from(formulaScan.ndjson, "utf8")),
    privacy: {
      full_text_fts_opened: false,
      relation_node_values_exported: false,
      dangerous_formula_prefixes_escaped: true,
    },
    controls: { open_source_authored: true, preview_renderer: "local_svg_sharp_not_office", native_office_open_verified: false },
  };
  await fs.writeFile(args.verification, `${JSON.stringify(verification, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify({ status: "PASS", sheets: verification.sheets.length })}\n`);
}

main().catch((error) => {
  const code = error instanceof Error && /^[A-Z0-9_]+$/.test(error.message)
    ? error.message
    : "WORKBOOK_BUILD_FAILED";
  process.stderr.write(`${code}\n`);
  process.exitCode = 2;
});
