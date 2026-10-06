/**
 * Synthetic workbook integration tests: no CRM, credentials or business records.
 * 合成工作簿集成测试：不连接 CRM，不使用凭据或真实业务记录。
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import ExcelJS from "exceljs";
import JSZip from "jszip";
import sharp from "sharp";
import { Workbook, SpreadsheetFile } from "../../pipeline/lib/open_workbook.mjs";

const root = fileURLToPath(new URL("../../", import.meta.url));
const dangerous = "=SYNTHETIC_NOT_A_FORMULA()";
const counts = { old_files: 1, v2_files: 2, historical_mail_files: 1, attachment_occurrences: 1, mail_attachment_relations: 1,
  multiparent_attachments: 0, explicit_relations: 1, business_timeline: 1, capture_timeline: 1, source_gaps: 0 };

function payloadFor(kind) {
  if (kind === "single_customer_delivery") return {
    schema: "okki.single_customer.delivery_workbook_payload.v1", company_id: "123456789", scope_status: "SYNTHETIC",
    counts: { raw_files: 1, tasks: 1, relation_nodes: 1, relation_edges: 0, manual_trade_artifacts: 0, manual_trade_artifact_bytes: 0 },
    coverage_rows: [["synthetic", "fixture", "sample", 1, 1, 0, 0, "PASS", "sample.txt", "not production"]],
    relation_rows: [], evidence_rows: [["fixture", "PASS", dangerous, "a".repeat(64), 1, "text", "result.txt", "b".repeat(64), "synthetic"]],
  };
  if (kind === "unredacted_catalog") return {
    schema: "okki.single_customer.unredacted_catalog_workbook_payload.v1", company_id: "123456789",
    database: { integrity: "ok", sha256: "a".repeat(64), bytes: 0 }, counts: { documents: 0, nodes: 0, relation_facts: 0, relation_occurrences: 0, files: 1, tasks: 0 },
    coverage_rows: [], entity_count_rows: [], node_detail_rows: [], relation_stat_rows: [],
    file_catalog_rows: [["fixture", "text", dangerous, 1, "a".repeat(64), "text/plain", "PASS"]], task_catalog_rows: [], logical_relation_rows: [],
  };
  return {
    schema: "okki.single_customer.completion_v2_catalog_payload.v1", company_id: "123456789", counts,
    accessible_data_status: "ACCESSIBLE_DATA_PASS", absolute_completeness_status: "ABSOLUTE_COMPLETENESS_PASS",
    detail_limits: { max_rows_per_sheet: 1000 }, gates: Object.fromEntries(["PAGE_GAP_RECOVERY_PASS", "MAIL_ATTACHMENT_SCOPE_PASS",
      "CONTENT_RECURSION_PASS_WITH_SOURCE_GAPS", "RELATIONSHIP_V2_PASS", "TIMELINE_V2_PASS"].map((gate) => [gate, true])),
    coverage_rows: [], mail_attachment_rows: [["mail1", "fixture", "file1", dangerous, "sample.json", "a".repeat(64), "/attachments/0", "VALID"]],
    explicit_relation_rows: [], business_timeline_rows: [], capture_timeline_rows: [], source_gap_rows: [], diff_rows: [],
  };
}

for (const [kind, expectedSheets] of [["single_customer_delivery", 4], ["unredacted_catalog", 9], ["completion_v2_catalog", 8]]) {
  test(`${kind}: open-source XLSX, controls, escaped text and every preview`, async () => {
    const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "evidence-trail-synthetic-"));
    try {
      const input = path.join(temporary, "input.json"); const output = path.join(temporary, "output.xlsx");
      const receiptPath = path.join(temporary, "verification.json");
      await fs.writeFile(input, JSON.stringify(payloadFor(kind)));
      const script = kind === "single_customer_delivery" ? "build_single_customer_delivery_workbook.mjs" : `build_${kind === "completion_v2_catalog" ? kind : "unredacted_catalog_workbook"}.mjs`;
      const result = spawnSync(process.execPath, [path.join(root, "pipeline", script), "--input", input, "--output", output,
        "--preview-dir", path.join(temporary, "previews"), "--verification", receiptPath], { encoding: "utf8", timeout: 60000 });
      assert.equal(result.status, 0, result.stderr);
      const receipt = JSON.parse(await fs.readFile(receiptPath, "utf8"));
      assert.equal(receipt.status, "PASS"); assert.equal(receipt.sheets.length, expectedSheets);
      assert.equal(receipt.previews.length, expectedSheets);
      const workbook = new ExcelJS.Workbook(); await workbook.xlsx.readFile(output);
      assert.equal(workbook.worksheets.length, expectedSheets);
      assert.deepEqual(workbook.worksheets.map((sheet) => sheet.name), receipt.sheets);
      assert.ok(workbook.worksheets.some((sheet) => /[\u4e00-\u9fff]/.test(sheet.name)));
      let found = false;
      for (const sheet of workbook.worksheets) {
        assert.ok(sheet.views.some((view) => view.state === "frozen"));
        sheet.eachRow((row) => row.eachCell((cell) => {
          if (String(cell.value).includes(dangerous)) { found = true; assert.equal(typeof cell.value, "string"); }
        }));
      }
      assert.ok(found, "synthetic escaped text must survive round-trip");
      const zip = await JSZip.loadAsync(await fs.readFile(output));
      const tables = Object.keys(zip.files).filter((name) => /^xl\/tables\/table\d+\.xml$/.test(name));
      assert.ok(tables.length >= expectedSheets - 1);
      for (const table of tables) assert.match(await zip.file(table).async("string"), /<autoFilter\b/);
      for (const preview of receipt.previews) {
        const file = preview.file ? path.join(temporary, "previews", preview.file) : preview.path;
        const metadata = await sharp(file).metadata();
        assert.equal(metadata.format, "png"); assert.ok(metadata.width > 100 && metadata.height > 100);
      }
    } finally { await fs.rm(temporary, { recursive: true, force: true }); }
  });
}

test("COUNTA cached value matches source; unsupported formulas fail closed", async () => {
  const workbook = Workbook.create();
  const source = workbook.worksheets.add("Source"); source.getRange("A1:A3").values = [["a"], [""], ["b"]];
  const summary = workbook.worksheets.add("Summary"); summary.getRange("A1").formulas = [["=COUNTA('Source'!$A$1:$A$3)"]];
  assert.equal(summary.getRange("A1").values[0][0].result, 2);
  assert.throws(() => { summary.getRange("A2").formulas = [["=UNSUPPORTED(1)"]]; }, /WORKBOOK_FORMULA_UNSUPPORTED/);
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "evidence-trail-formula-"));
  try {
    const target = path.join(temporary, "formula.xlsx"); await (await SpreadsheetFile.exportXlsx(workbook)).save(target);
    const reloaded = new ExcelJS.Workbook(); await reloaded.xlsx.readFile(target);
    assert.equal(reloaded.getWorksheet("Summary").getCell("A1").result, 2);
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
});

test("completion builder refuses an unverified payload without writing artifacts", async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "evidence-trail-invalid-"));
  try {
    const payload = payloadFor("completion_v2_catalog"); payload.gates.PAGE_GAP_RECOVERY_PASS = false;
    const input = path.join(temporary, "input.json"); await fs.writeFile(input, JSON.stringify(payload));
    const result = spawnSync(process.execPath, [path.join(root, "pipeline/build_completion_v2_catalog.mjs"), "--input", input, "--validate-only"], { encoding: "utf8", timeout: 10000 });
    assert.equal(result.status, 2); assert.equal(result.stderr.trim(), "WORKBOOK_GATE_NOT_PASS");
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
});
