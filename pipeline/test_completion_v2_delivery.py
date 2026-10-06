# EN: Synthetic fixtures only; never read real captures or private customer directories.
# 中文：仅使用合成测试夹具，不读取真实采集或私有客户目录。
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path

import build_completion_v2_delivery as module


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def checksum(path: Path, values: list[Path]) -> None:
    lines = [f"{sha(item)}  {item.relative_to(path).as_posix()}" for item in values]
    (path / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


class CompletionDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.case = root / "company_42"
        self.case.mkdir()
        write_json(self.case / "case_identity.json", {"company_id": "42"})
        self.output_parent = self.case / "deliveries" / "full_unredacted_local"

        evidence = self.case / "raw" / "evidence"
        evidence.mkdir(parents=True)
        self.sources = []
        for index in range(3):
            path = evidence / f"source_{index}.txt"
            path.write_text(f"local-{index}\n", encoding="utf-8")
            self.sources.append(path)

        self.old = root / "old"
        self.old.mkdir()
        with sqlite3.connect(self.old / "customer_full_unredacted.sqlite3") as db:
            db.execute("CREATE TABLE files(id INTEGER PRIMARY KEY)")
            db.executemany("INSERT INTO files(id) VALUES(?)", [(1,), (2,)])
        write_json(self.old / "verification.json", {
            "schema": module.OLD_PACKAGE_SCHEMA, "status": "PASS",
            "source_gates": {"auto7": "PASS", "full_extract": "PASS", "manual_trade": "PASS"},
            "counts": {"files": 2, "tasks": 2, "text_documents": 2, "relation_fact": 1},
        })
        checksum(self.old, [self.old / "customer_full_unredacted.sqlite3"])

        self.full = root / "full"
        self.full.mkdir()
        with sqlite3.connect(self.full / "state.sqlite3") as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE files(source_rel TEXT PRIMARY KEY,sha256 TEXT,bytes INTEGER,active INTEGER);
                CREATE TABLE tasks(task_id TEXT PRIMARY KEY,state TEXT,active INTEGER);
                CREATE TABLE content_objects(sha256 TEXT PRIMARY KEY,active INTEGER);
                CREATE TABLE occurrences(occurrence_id TEXT PRIMARY KEY,content_sha256 TEXT,active INTEGER);
                CREATE TABLE source_gaps(gap_id TEXT PRIMARY KEY,active INTEGER);
            """)
            db.execute("INSERT INTO meta VALUES('company_id','42')")
            for index, source in enumerate(self.sources):
                digest = sha(source)
                rel = source.relative_to(self.case).as_posix()
                db.execute("INSERT INTO files VALUES(?,?,?,1)", (rel, digest, source.stat().st_size))
                db.execute("INSERT INTO tasks VALUES(?,?,1)", (f"t{index}", "completed"))
                db.execute("INSERT INTO content_objects VALUES(?,1)", (digest,))
                db.execute("INSERT INTO occurrences VALUES(?,?,1)", (f"o{index}", digest))
            db.execute("INSERT INTO source_gaps VALUES('gap1',1)")
        with sqlite3.connect(self.full / "full_text.sqlite3") as db:
            db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY)")
            db.execute("CREATE VIRTUAL TABLE full_text USING fts5(text)")
            db.executemany("INSERT INTO documents(id) VALUES(?)", [(1,), (2,), (3,)])
        write_json(self.full / "verification.json", {
            "schema": "okki.single_customer_full_extract.v1", "company_id": "42",
            "status": "INCOMPLETE_SOURCE_GAPS", "pending": 0, "failed": 0,
            "historical_mail_files": 1, "evidence_files": 3, "tasks": 3, "source_gaps": 1,
        })
        for name in ("processing_scope.v2.json", "prepare_manifest.json", "run_manifest.json"):
            write_json(self.full / name, {"schema": "synthetic"})
        for name in ("file_inventory.csv", "content_object_index.csv", "occurrence_index.csv", "evidence_index.csv", "media_inventory.csv", "coverage.csv"):
            (self.full / name).write_text("field\n", encoding="utf-8")
        with (self.full / "source_gaps.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["gap_id", "content_sha256", "source_scope", "role", "source_identity_sha256", "owner_sha256", "error_code", "http_status"])
            writer.writeheader()
            writer.writerow({"gap_id": "gap1", "source_scope": "mail", "role": "deleted_mail", "error_code": "SOURCE_DELETED"})
        (self.full / "relationship_graph.jsonl").write_text("{}\n", encoding="utf-8")

        self.relationship = root / "relationship"
        self.relationship.mkdir()
        database = self.relationship / "relationship_timeline_v2.sqlite3"
        with sqlite3.connect(database) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE nodes(node_id TEXT PRIMARY KEY,node_type TEXT,value TEXT);
                CREATE TABLE explicit_relation(relation_id TEXT PRIMARY KEY,subject_node_id TEXT,predicate TEXT,object_node_id TEXT,relation_kind TEXT,evidence_count INTEGER);
                CREATE TABLE relation_evidence(evidence_id TEXT PRIMARY KEY,relation_id TEXT,source_rel TEXT);
                CREATE TABLE timeline_event(event_id TEXT PRIMARY KEY,timeline_axis TEXT,stable_sequence INTEGER,stable_order_key TEXT,event_kind TEXT,field_name TEXT,timestamp_raw TEXT,timestamp_utc TEXT,local_sort_key TEXT,normalization_status TEXT,timestamp_precision TEXT,source_rel TEXT,json_pointer TEXT);
                CREATE TABLE attachment_occurrence(occurrence_id TEXT PRIMARY KEY,mail_node_id TEXT,attachment_node_id TEXT,artifact_source_rel TEXT,artifact_sha256 TEXT,source_pointer TEXT,resolution TEXT);
            """)
            db.executemany("INSERT INTO meta VALUES(?,?)", [("schema", module.RELATIONSHIP_SCHEMA), ("company_id", "42")])
            db.executemany("INSERT INTO nodes VALUES(?,?,?)", [("m1", "mail", "mail1"), ("m2", "mail", "mail2"), ("a1", "attachment", "attachment")])
            db.execute("INSERT INTO explicit_relation VALUES('r1','m1','mail_has_attachment','a1','business',1)")
            db.execute("INSERT INTO relation_evidence VALUES('e1','r1','raw/evidence/source_0.txt')")
            db.executemany("INSERT INTO timeline_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("tb", "business", 1, "1", "mail_sent", "sent_at", "2020-01-01", "", "2020-01-01", "local_unzoned", "day", "raw/evidence/source_0.txt", "/sent_at"),
                ("tc", "capture", 1, "1", "captured", "captured_at", "2020-01-02Z", "2020-01-02Z", "", "absolute", "day", "raw/evidence/source_0.txt", "/captured_at"),
            ])
            db.executemany("INSERT INTO attachment_occurrence VALUES(?,?,?,?,?,?,?)", [
                ("o1", "m1", "a1", "raw/evidence/source_0.txt", "A" * 64, "/a", "CAPTURED"),
                ("o2", "m2", "a1", "raw/evidence/source_0.txt", "A" * 64, "/b", "CAPTURED"),
            ])
        write_json(self.relationship / "verification.json", {"schema": module.RELATIONSHIP_SCHEMA, "status": "PASS"})
        checksum(self.relationship, [database])

        session = self.case / "raw" / "sessions" / "ui_session"
        session.mkdir(parents=True)
        (session / "page.html").write_text("<html></html>", encoding="utf-8")
        self.ui = self.case / "manifests" / "ui_gap_revisit_latest.json"
        write_json(self.ui, {
            "schema": module.UI_SCHEMA, "company_id": "42", "session_id": "ui_session", "status": "PASS",
            "no_auto_next": True, "manual_trade_automatic_capture": False,
            "checks": {"other_dynamic_found": True, "other_dynamic_terminal_closed": True, "all_found_filters_terminal_closed": True, "documents_terminal_closed": True},
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self) -> Namespace:
        return Namespace(
            case_root=self.case, company_id="42", old_package=self.old, full_extract_v2=self.full,
            relationship_v2=self.relationship, ui_gap_manifest=self.ui, output_parent=self.output_parent,
            run_id="synthetic", expected_old_files=2, expected_historical_mail_files=1,
            expected_min_evidence_files=3, expected_attachment_occurrences=2,
            expected_mail_attachment_relations=2, expected_multiparent_attachments=1, max_excel_rows=100,
        )

    def test_prepare_and_finalize_are_atomic(self) -> None:
        result = module.prepare(self.args())
        stage = Path(result["stage"])
        final = Path(result["final"])
        self.assertTrue(stage.is_dir())
        self.assertFalse(final.exists())
        payload = json.loads((stage / "delivery_payload.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["counts"]["mail_attachment_relations"], 2)
        self.assertEqual(payload["absolute_completeness_status"], "ABSOLUTE_COMPLETENESS_SOURCE_GAPS")
        self.assertTrue((stage / "evidence" / "original" / "raw" / "evidence" / "source_2.txt").is_file())

        workbook = stage / "客户42_全案例递归补全目录_v2.xlsx"
        docx = stage / "客户42_全案例递归补全研究报告_v2.docx"
        for item in (workbook, docx):
            with zipfile.ZipFile(item, "w") as archive:
                archive.writestr("synthetic.xml", "<x/>")
        pdf = stage / "客户42_全案例递归补全研究报告_v2.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        write_json(stage / "qa" / "workbook_v2_verification.json", {
            "status": "PASS", "formula_errors": 0, "all_sheets_rendered": True,
            "all_filters_present": True, "all_freeze_panes_present": True,
            "output": {"sha256": sha(workbook)},
        })
        write_json(stage / "qa" / "report_v2_verification.json", {
            "status": "PASS", "visual_inspection_pass": True, "word_com_export": True,
            "opened_read_only": True, "docx_sha256": sha(docx), "pdf_sha256": sha(pdf),
            "docx_render_pages": 1, "pdf_render_pages": 1,
        })

        # Assert fail-closed before adding a synthetic contract fixture; no Office is run.
        # 先验证缺原生回执时拒绝，再补合成契约夹具；本测试不运行 Office。
        with self.assertRaisesRegex(module.DeliveryError, "WORKBOOK_NATIVE_QA_MISSING"):
            module.finalize(Namespace(stage=stage))
        self.assertTrue(stage.is_dir())
        self.assertFalse(final.exists())
        write_json(stage / "qa" / "workbook_native_qa.json", {
            "schema": "evidence_trail.workbook_native_qa.v1", "company_id": "42", "status": "PASS",
            "opened_read_only": True, "repair_prompt_seen": False,
            "all_sheets_visual_inspection_pass": True, "formula_errors": 0,
            "workbook_sha256": sha(workbook),
            "structural_receipt_sha256": sha(stage / "qa" / "workbook_v2_verification.json"),
        })

        final_result = module.finalize(Namespace(stage=stage))
        self.assertEqual(final_result["status"], "ACCESSIBLE_DATA_PASS")
        self.assertFalse(stage.exists())
        self.assertTrue(final.is_dir())
        verified = module.verify_final(final)
        self.assertEqual(verified["absolute_completeness_status"], "ABSOLUTE_COMPLETENESS_SOURCE_GAPS")

    def test_prepare_rejects_incomplete_ui_gap(self) -> None:
        manifest = json.loads(self.ui.read_text(encoding="utf-8"))
        manifest["status"] = "INCOMPLETE"
        write_json(self.ui, manifest)
        with self.assertRaisesRegex(module.DeliveryError, "UI_GAP_NOT_PASS"):
            module.prepare(self.args())


if __name__ == "__main__":
    unittest.main()
