/**
 * A small, explicit workbook facade backed by ExcelJS and Sharp.
 * 使用 ExcelJS 和 Sharp 实现的精简、明确的工作簿接口。
 *
 * Only the range/style/table/COUNTA operations used by this project are supported.
 * 仅支持本项目实际使用的范围、样式、表格及 COUNTA 操作。
 * PNGs are local SVG layout previews, not Excel/Office rendering or recalculation.
 * PNG 是本地 SVG 布局预览，不代表 Excel/Office 原生渲染或重算验收。
 */
import fs from "node:fs/promises";
import ExcelJS from "exceljs";
import sharp from "sharp";

const xml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[ch]);
const argb = (value) => String(value ?? "000000").replace(/^#/, "").padStart(8, "F");

function columnIndex(value) {
  let result = 0;
  for (const char of value.toUpperCase()) result = result * 26 + char.charCodeAt(0) - 64;
  return result;
}
function bounds(reference) {
  const matched = /^\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$/i.exec(reference);
  if (!matched) throw new Error("WORKBOOK_RANGE_INVALID");
  const box = { left: columnIndex(matched[1]), top: Number(matched[2]), right: columnIndex(matched[3] ?? matched[1]), bottom: Number(matched[4] ?? matched[2]) };
  if (box.left > box.right || box.top > box.bottom || box.bottom > 1048576 || box.right > 16384) throw new Error("WORKBOOK_RANGE_INVALID");
  return box;
}
function font(value) {
  const mapped = { ...value };
  if (value.color) mapped.color = { argb: argb(value.color) };
  return mapped;
}
function border(value) {
  const side = { style: value.style ?? "thin", color: { argb: argb(value.color ?? "E5E7EB") } };
  return { top: side, left: side, right: side, bottom: side };
}

class Range {
  constructor(sheet, reference) {
    this.sheet = sheet;
    this.reference = reference;
    this.box = bounds(reference);
    this.style = {};
    // Style property assignment uses the same checked path as whole-style assignment.
    // 单项样式赋值与整组样式赋值使用同一校验和写入路径。
    this.proxy = new Proxy(this.style, { set: (target, key, value) => { target[key] = value; this.apply({ [key]: value }); return true; } });
  }
  cells(visit) {
    for (let row = this.box.top; row <= this.box.bottom; row += 1) {
      for (let col = this.box.left; col <= this.box.right; col += 1) visit(this.sheet.native.getCell(row, col), row, col);
    }
  }
  merge() { this.sheet.native.mergeCells(this.reference); }
  set values(rows) {
    if (!Array.isArray(rows) || rows.length > this.box.bottom - this.box.top + 1) throw new Error("WORKBOOK_ROW_SHAPE_INVALID");
    rows.forEach((row, r) => {
      if (!Array.isArray(row) || row.length > this.box.right - this.box.left + 1) throw new Error("WORKBOOK_ROW_SHAPE_INVALID");
      row.forEach((value, c) => {
        const cell = this.sheet.native.getCell(this.box.top + r, this.box.left + c);
        // Store user-supplied strings as strings; never promote them to formulas.
        // 用户字符串始终按字符串存储，绝不自动提升为公式。
        cell.value = value ?? "";
        if (typeof value === "string") cell.numFmt = "@";
      });
    });
  }
  get values() {
    const result = [];
    for (let row = this.box.top; row <= this.box.bottom; row += 1) {
      const values = [];
      for (let col = this.box.left; col <= this.box.right; col += 1) values.push(this.sheet.native.getCell(row, col).value);
      result.push(values);
    }
    return result;
  }
  set formulas(rows) {
    rows.forEach((row, r) => row.forEach((value, c) => {
      const formula = String(value).replace(/^=/, "");
      const result = this.sheet.owner.evaluate(formula);
      this.sheet.native.getCell(this.box.top + r, this.box.left + c).value = { formula, result };
    }));
  }
  get format() { return this.proxy; }
  set format(value) { Object.assign(this.style, value); this.apply(value); }
  apply(value) {
    if (value.rowHeight !== undefined) {
      for (let row = this.box.top; row <= this.box.bottom; row += 1) this.sheet.native.getRow(row).height = value.rowHeight;
    }
    if (value.columnWidth !== undefined) {
      for (let col = this.box.left; col <= this.box.right; col += 1) this.sheet.native.getColumn(col).width = value.columnWidth;
    }
    this.cells((cell) => {
      if (value.font) cell.font = { ...cell.font, ...font(value.font) };
      if (value.fill) cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: argb(value.fill) } };
      if (value.numberFormat) cell.numFmt = value.numberFormat;
      const alignment = { ...cell.alignment };
      if (value.wrapText !== undefined) alignment.wrapText = value.wrapText;
      if (value.horizontalAlignment) alignment.horizontal = value.horizontalAlignment;
      if (value.verticalAlignment) alignment.vertical = value.verticalAlignment === "center" ? "middle" : value.verticalAlignment;
      cell.alignment = alignment;
      if (value.borders) cell.border = border(value.borders);
    });
  }
}

class Sheet {
  constructor(owner, native) {
    this.owner = owner;
    this.native = native;
    this.frozenRows = 0;
    this.frozenColumns = 0;
    this.freezePanes = {
      freezeRows: (count) => { this.frozenRows = count; this.updateViews(); },
      freezeColumns: (count) => { this.frozenColumns = count; this.updateViews(); },
    };
    this.tables = { add: (reference, hasHeaders, name) => {
      if (!hasHeaders) throw new Error("WORKBOOK_TABLE_HEADERS_REQUIRED");
      const range = new Range(this, reference);
      const values = range.values;
      const table = native.addTable({ name, ref: `${String(reference).split(":")[0]}`, headerRow: true,
        columns: values[0].map((value) => ({ name: String(value), filterButton: true })),
        rows: values.slice(1), style: { theme: "TableStyleMedium2", showRowStripes: true } });
      return new Proxy({}, { set: (_target, key, value) => {
        if (key === "style") table.table.style.theme = value;
        if (key === "showBandedRows") table.table.style.showRowStripes = value;
        if (key === "showFilterButton") table.table.columns.forEach((col) => { col.filterButton = value; });
        table.commit(); return true;
      } });
    } };
  }
  set showGridLines(value) { this.gridLines = value; this.updateViews(); }
  get showGridLines() { return this.gridLines ?? false; }
  updateViews() {
    this.native.views = [{ state: this.frozenRows || this.frozenColumns ? "frozen" : "normal", xSplit: this.frozenColumns,
      ySplit: this.frozenRows, showGridLines: this.showGridLines, zoomScale: 90 }];
  }
  getRange(reference) { return new Range(this, reference); }
  getRangeByIndexes(row, col, rowCount, colCount) {
    const first = this.native.getCell(row + 1, col + 1).address;
    const last = this.native.getCell(row + rowCount, col + colCount).address;
    return this.getRange(`${first}:${last}`);
  }
}

class LocalWorkbook {
  constructor(native = new ExcelJS.Workbook()) {
    this.native = native;
    this.native.creator = "Evidence Trail contributors";
    this.native.calcProperties = { fullCalcOnLoad: true };
    this.sheets = native.worksheets.map((sheet) => new Sheet(this, sheet));
    this.worksheets = { add: (name) => { const sheet = new Sheet(this, native.addWorksheet(name)); this.sheets.push(sheet); return sheet; } };
  }
  evaluate(formula) {
    // Explicit COUNTA support keeps cached values auditable; unsupported formulas fail closed.
    // 显式支持 COUNTA 并保存可核对缓存值，遇到未支持公式直接停止。
    const matched = /^COUNTA\('((?:[^']|'')+)'!(.+)\)$/i.exec(formula);
    if (!matched) throw new Error("WORKBOOK_FORMULA_UNSUPPORTED");
    const sheet = this.sheets.find((item) => item.native.name === matched[1].replaceAll("''", "'"));
    if (!sheet) throw new Error("WORKBOOK_FORMULA_SHEET_MISSING");
    return sheet.getRange(matched[2]).values.flat().filter((value) => value !== "" && value !== null && value !== undefined).length;
  }
  async inspect(options) {
    let records = [];
    if (options.kind === "sheet") records = this.sheets.map((sheet) => ({ id: sheet.native.id, name: sheet.native.name }));
    else if (options.kind === "table") {
      const [sheetName, ref] = options.range.split("!");
      const sheet = this.sheets.find((item) => item.native.name === sheetName);
      if (!sheet) throw new Error("WORKBOOK_RANGE_SHEET_MISSING");
      records = [{ range: options.range, values: sheet.getRange(ref).values }];
    } else if (options.kind === "match") {
      const expression = options.options?.useRegex ? new RegExp(options.searchTerm) : null;
      for (const sheet of this.sheets) sheet.native.eachRow((row) => row.eachCell((cell) => {
        const text = displayValue(cell);
        if (expression ? expression.test(text) : text.includes(options.searchTerm)) records.push({ sheet: sheet.native.name, cell: cell.address, match: text });
      }));
      records = records.slice(0, options.options?.maxResults ?? 100);
    } else throw new Error("WORKBOOK_INSPECTION_UNSUPPORTED");
    return { ndjson: records.map((value) => JSON.stringify(value)).join("\n") };
  }
  async render({ sheetName, range }) {
    const sheet = this.sheets.find((item) => item.native.name === sheetName);
    if (!sheet) throw new Error("WORKBOOK_RENDER_SHEET_MISSING");
    const box = bounds(range);
    if (box.bottom - box.top > 200 || box.right - box.left > 30) throw new Error("WORKBOOK_PREVIEW_TOO_LARGE");
    const widths = [];
    for (let col = box.left; col <= box.right; col += 1) widths.push(Math.max(70, Math.min(560, (sheet.native.getColumn(col).width ?? 15) * 7)));
    const positions = [0]; widths.forEach((width) => positions.push(positions.at(-1) + width));
    const merges = sheet.native.model.merges.map(bounds);
    let y = 0;
    const fragments = [];
    for (let row = box.top; row <= box.bottom; row += 1) {
      const height = Math.max(30, Math.min(160, (sheet.native.getRow(row).height ?? 28) * 1.33));
      for (let col = box.left; col <= box.right; col += 1) {
        const merge = merges.find((item) => row >= item.top && row <= item.bottom && col >= item.left && col <= item.right);
        if (merge && (row !== merge.top || col !== merge.left)) continue;
        const cell = sheet.native.getCell(row, col);
        const width = merge ? positions[Math.min(merge.right, box.right) - box.left + 1] - positions[col - box.left] : widths[col - box.left];
        const x = positions[col - box.left];
        const fill = cell.fill?.fgColor?.argb?.slice(-6) ?? "FFFFFF";
        const color = cell.font?.color?.argb?.slice(-6) ?? "1F2937";
        const size = Math.min(22, (cell.font?.size ?? 10) * 1.33);
        const charLimit = Math.max(5, Math.floor((width - 16) / size));
        const raw = Array.from(displayValue(cell).replace(/[\r\n]+/g, " "));
        const lines = [];
        const maxLines = Math.max(1, Math.floor((height - 8) / (size + 3)));
        for (let offset = 0; offset < raw.length && lines.length < maxLines; offset += charLimit) lines.push(raw.slice(offset, offset + charLimit).join(""));
        if (raw.length > charLimit * maxLines && lines.length) lines[lines.length - 1] = lines.at(-1).slice(0, -1) + "…";
        const spans = lines.map((line, index) => `<tspan x="${x + 8}" y="${y + size + 5 + index * (size + 3)}">${xml(line)}</tspan>`).join("");
        fragments.push(`<rect x="${x}" y="${y}" width="${width}" height="${height}" fill="#${fill}" stroke="#D9E2F3"/><text font-family="sans-serif" font-size="${size}" font-weight="${cell.font?.bold ? "bold" : "normal"}" fill="#${color}">${spans}</text>`);
      }
      y += height;
    }
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${positions.at(-1)}" height="${y}">${fragments.join("")}</svg>`;
    const png = await sharp(Buffer.from(svg)).png().toBuffer();
    return new Blob([png], { type: "image/png" });
  }
}

function displayValue(cell) {
  const value = cell.value;
  if (value && typeof value === "object" && "formula" in value) return String(value.result ?? "");
  if (value && typeof value === "object" && "error" in value) return value.error;
  return cell.text ?? String(value ?? "");
}

export const Workbook = { create: () => new LocalWorkbook() };
export const FileBlob = { load: (filename) => fs.readFile(filename) };
export const SpreadsheetFile = {
  exportXlsx: async (workbook) => ({ save: (filename) => workbook.native.xlsx.writeFile(filename) }),
  importXlsx: async (bytes) => { const native = new ExcelJS.Workbook(); await native.xlsx.load(bytes); return new LocalWorkbook(native); },
};
