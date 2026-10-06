# EN: Synthetic fixtures only; never read real captures or private customer directories.
# 中文：仅使用合成测试夹具，不读取真实采集或私有客户目录。
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("build_single_customer_final_delivery.py")
SPEC = importlib.util.spec_from_file_location("build_single_customer_final_delivery", MODULE_PATH)
assert SPEC and SPEC.loader
target = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = target
SPEC.loader.exec_module(target)


COMPANY_ID = "123456789"
SECRET = "CUSTOMER_BODY_SECRET_MUST_NOT_LEAK"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, headers: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def make_case(root: Path) -> tuple[Path, Path, Path]:
    case = root / f"company_{COMPANY_ID}"
    write_json(case / "case_identity.json", {"company_id": COMPANY_ID})
    session = "capture_auto7_synthetic"
    write_json(case / "manifests" / "objects.jsonl", {})
    write_json(
        case / "manifests" / "processing_scope_latest.json",
        {
            "schema": 1,
            "company_id": COMPANY_ID,
            "capture_status": "pass",
            "default_session_id": session,
            "object_manifest": "manifests/objects.jsonl",
            "default_object_filter": {"field": "session_id", "equals": session},
        },
    )
    write_json(
        case / "raw" / "sessions" / session / "final_status.json",
        {"phase": "complete", "errors": [], "metrics": {"reconciliation_failures": 0}},
    )

    full = case / "derived" / "full_extract"
    source_sha = "A" * 64
    result_sha = "B" * 64
    dangerous_source = "=1+1.txt"
    write_csv(
        full / "file_inventory.csv",
        ["source_rel", "sha256", "bytes", "magic", "category", "mime", "active", "prepared_at"],
        [{"source_rel": dangerous_source, "sha256": source_sha, "bytes": 12, "magic": "text", "category": "text", "mime": "text/plain", "active": 1, "prepared_at": "2026-01-02T00:00:00Z"}],
    )
    write_csv(
        full / "evidence_index.csv",
        ["task_id", "source_rel", "source_sha256", "task_kind", "state", "output_rel", "result_sha256", "attempts", "error_code", "updated_at"],
        [{"task_id": "task-1", "source_rel": dangerous_source, "source_sha256": source_sha, "task_kind": "extract_text", "state": "completed", "output_rel": "derived/full_extract/artifacts/extract_text/task-1.json", "result_sha256": result_sha, "attempts": 1, "error_code": "", "updated_at": "2026-01-02T00:00:00Z"}],
    )
    write_csv(
        full / "media_inventory.csv",
        ["source_rel", "sha256", "bytes", "category", "ffprobe", "dual_asr_diarization", "video_frame_ocr"],
        [],
    )
    write_csv(
        full / "coverage.csv",
        ["scope", "name", "total", "completed", "pending", "failed", "bytes"],
        [{"scope": "file_category", "name": "text", "total": 1, "completed": 1, "pending": 0, "failed": 0, "bytes": 12}],
    )
    full.mkdir(parents=True, exist_ok=True)
    relations = [
        {"record_type": "node", "node_id": "company:" + "C" * 64, "entity_type": "company", "value": COMPANY_ID},
        {"record_type": "node", "node_id": "email:" + "D" * 64, "entity_type": "email", "value": SECRET},
        {"record_type": "edge", "edge_id": "E" * 64, "from": "company:" + "C" * 64, "to": "email:" + "D" * 64, "relation": "email_participant", "source_rel": dangerous_source, "json_pointer": "/sender"},
    ]
    (full / "relationship_graph.jsonl").write_text("".join(json.dumps(row) + "\n" for row in relations), encoding="utf-8")
    (full / "full_text.sqlite3").write_bytes((SECRET + " only in unopened synthetic FTS").encode("utf-8"))
    write_json(
        full / "verification.json",
        {
            "schema": target.FULL_EXTRACT_SCHEMA,
            "company_id": COMPANY_ID,
            "status": "PASS",
            "evidence_files": 1,
            "tasks": 1,
            "pending": 0,
            "failed": 0,
            "fts_rows": 1,
            "relation_rows": 3,
        },
    )

    manual = case / "evidence" / "manual_trade" / "manual_trade_synthetic"
    artifact = manual / "=2+2.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"metadata-only")
    manifest = {
        "schema": target.MANUAL_MANIFEST_SCHEMA,
        "status": "PASS_WITH_SOURCE_GAPS",
        "company_id": COMPANY_ID,
        "session": "manual_trade_synthetic",
        "counts": {
            "candidate_list_pages": 1,
            "candidates_visible": 1,
            "state_snapshots": 1,
            "artifact_files": 1,
            "artifact_bytes": artifact.stat().st_size,
            "customs_record_pages_captured": 0,
        },
        "source_gaps": [{"candidate_index": 1, "code": "SOURCE_PAGE_GAP", "observed_numeric_tokens": [1], "action": SECRET}],
        "artifacts": [{"relative_path": artifact.name, "bytes": artifact.stat().st_size, "sha256": target.sha256_file(artifact)}],
    }
    manifest_path = manual / "trade_manual_capture_manifest.json"
    write_json(manifest_path, manifest)
    receipt_path = manual / "trade_manual_capture_receipt.json"
    write_json(
        receipt_path,
        {
            "schema": target.MANUAL_RECEIPT_SCHEMA,
            "status": manifest["status"],
            "company_id": COMPANY_ID,
            "session": manifest["session"],
            "manifest_path": str(manifest_path),
            "manifest_bytes": manifest_path.stat().st_size,
            "manifest_sha256": target.sha256_file(manifest_path),
            "counts": manifest["counts"],
        },
    )
    return case, manifest_path, receipt_path


def build_args(case: Path, output: Path) -> argparse.Namespace:
    return argparse.Namespace(
        case_root=case,
        company_id=COMPANY_ID,
        processing_scope=None,
        trade_receipt=None,
        trade_manifest=None,
        output_dir=output,
        generated_at="2026-01-02T00:00:00Z",
        verify_manual_artifact_hashes=True,
        export_pdf=False,
        word_timeout_seconds=600,
        node_exe=None,
        node_modules=None,
    )


def zip_text(path: Path) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                chunks.append(archive.read(name).decode("utf-8", errors="ignore"))
    return "\n".join(chunks)


def workbook_text_cells(path: Path) -> list[tuple[str, bool]]:
    """Resolve OOXML string encodings and detect formulas. 中文：解析 OOXML 文字并检测公式。"""
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cells: list[tuple[str, bool]] = []
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in strings.findall("s:si", namespace)]
        for name in archive.namelist():
            if not re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
                continue
            worksheet = ET.fromstring(archive.read(name))
            for cell in worksheet.findall(".//s:c", namespace):
                value = cell.find("s:v", namespace)
                inline = cell.find("s:is", namespace)
                text = value.text if value is not None and value.text else ""
                if cell.get("t") == "s":
                    text = shared[int(text)]
                elif inline is not None:
                    text = "".join(inline.itertext())
                cells.append((text, cell.find("s:f", namespace) is not None))
    return cells


class FinalDeliveryTests(unittest.TestCase):
    def test_builds_privacy_bounded_delivery_with_safe_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            case, _manifest, _receipt = make_case(root)
            output = root / "delivery"
            result = target.build_delivery(build_args(case, output))
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["scope_status"], "PASS_WITH_SOURCE_GAPS")
            for name in target.GENERATED_FILENAMES - {"客户技术研究报告.pdf"}:
                self.assertTrue((output / name).is_file(), name)
            self.assertEqual(len(list((output / "qa" / "workbook").glob("*.png"))), 4)

            scope = json.loads((output / "combined_scope.v1.json").read_text(encoding="utf-8"))
            self.assertEqual(scope["schema"], target.SCHEMA)
            self.assertFalse(scope["privacy_boundary"]["full_text_fts_opened"])
            self.assertEqual(scope["manual_trade"]["source_gaps"][0]["code"], "SOURCE_PAGE_GAP")
            self.assertNotIn("action", scope["manual_trade"]["source_gaps"][0])

            xlsx_text = zip_text(output / "customer_research_evidence.xlsx")
            docx_text = zip_text(output / "客户技术研究报告.docx")
            combined_text = (output / "combined_scope.v1.json").read_text(encoding="utf-8")
            self.assertNotIn(SECRET, xlsx_text)
            self.assertNotIn(SECRET, docx_text)
            self.assertNotIn(SECRET, combined_text)
            dangerous_cells = [(text, formula) for text, formula in workbook_text_cells(output / "customer_research_evidence.xlsx")
                               if text in {"=1+1.txt", "'=1+1.txt"}]
            self.assertTrue(dangerous_cells, "Literal dangerous filename must remain present.")
            self.assertTrue(all(not formula for _text, formula in dangerous_cells), "Dangerous filename must never become a formula.")
            formulas = re.findall(r"<(?:x:)?f>([^<]+)</(?:x:)?f>", xlsx_text)
            self.assertEqual(len(formulas), 2)
            self.assertEqual(sum(formula.startswith("COUNTA(") for formula in formulas), 2)
            self.assertGreaterEqual(xlsx_text.count("autoFilter"), 3)
            self.assertGreaterEqual(xlsx_text.count("pane"), 4)
            self.assertIn('w:w="9360"', docx_text)
            self.assertIn('w:top="1440"', docx_text)

            verification = json.loads((output / "delivery_verification.json").read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "PASS")
            self.assertFalse(verification["checks"]["customer_body_read"])
            self.assertEqual(target.verify_delivery_output(output)["status"], "PASS")

    def test_identity_mismatch_fails_closed_before_artifact_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            case, _manifest, _receipt = make_case(Path(temp_name))
            with self.assertRaisesRegex(target.DeliveryError, "CASE_IDENTITY_MISMATCH"):
                target.build_source_bundle(case, "999", None, None, None, False)

    def test_manual_receipt_manifest_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            case, _manifest, receipt = make_case(Path(temp_name))
            value = json.loads(receipt.read_text(encoding="utf-8"))
            value["manifest_sha256"] = "0" * 64
            write_json(receipt, value)
            with self.assertRaisesRegex(target.DeliveryError, "MANUAL_TRADE_MANIFEST_SHA_MISMATCH"):
                target.load_manual_trade(case, COMPANY_ID, receipt, None, False)


if __name__ == "__main__":
    unittest.main()

