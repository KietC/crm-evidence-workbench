# EN: Synthetic fixtures only; never read real captures or private customer directories.
# 中文：仅使用合成测试夹具，不读取真实采集或私有客户目录。
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_unredacted_delivery_report as mod


COMPANY_VALUE = "SYNTHETIC PRIVATE COMPANY 7Q9"
COMPANY_ID = "123456789"
PRIVATE_BODY = "PRIVATE BODY MUST NEVER ENTER REPORT OR STDOUT 7Q9"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest().upper()


# EN: Validate inputs, build in a private stage and publish only a verified package.
# 中文：验证输入、隔离构建并仅发布通过验证的数据包。
def build_package(root: Path, *, gate_status: str = "PASS") -> Path:
    package = root / "package"
    package.mkdir()
    db_path = package / mod.PACKAGE_DB
    db = sqlite3.connect(db_path)
    try:
        db.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE nodes(node_id TEXT PRIMARY KEY,entity_type TEXT NOT NULL,value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE relation_occurrence(occurrence_id INTEGER PRIMARY KEY,edge_id TEXT,from_node_id TEXT,to_node_id TEXT,relation TEXT,source_rel TEXT,json_pointer TEXT,source_line INTEGER);
            CREATE TABLE relation_fact(from_node_id TEXT,to_node_id TEXT,relation TEXT,evidence_count INTEGER,first_source_rel TEXT,last_source_rel TEXT,PRIMARY KEY(from_node_id,to_node_id,relation)) WITHOUT ROWID;
            CREATE TABLE files(source_rel TEXT PRIMARY KEY,sha256 TEXT,bytes INTEGER,magic TEXT,category TEXT,mime TEXT,source_scope TEXT,active INTEGER,prepared_at TEXT) WITHOUT ROWID;
            CREATE TABLE tasks(task_id TEXT PRIMARY KEY,source_rel TEXT,source_sha256 TEXT,task_kind TEXT,state TEXT,output_rel TEXT,result_sha256 TEXT,attempts INTEGER,error_code TEXT,updated_at TEXT) WITHOUT ROWID;
            CREATE TABLE coverage(coverage_id INTEGER PRIMARY KEY,scope TEXT,name TEXT,total INTEGER,completed INTEGER,pending INTEGER,failed INTEGER,bytes INTEGER);
            CREATE TABLE manual_trade_artifacts(artifact_id INTEGER PRIMARY KEY,session TEXT,relative_path TEXT,case_relative_path TEXT,sha256 TEXT,bytes INTEGER,record_json TEXT);
            CREATE TABLE lineage(lineage_id INTEGER PRIMARY KEY,source_kind TEXT,source_path TEXT,source_sha256 TEXT,source_bytes INTEGER,target_name TEXT,row_count INTEGER,recorded_at TEXT);
            CREATE TABLE documents(id INTEGER PRIMARY KEY,source_rel TEXT,source_sha256 TEXT,artifact_rel TEXT,task_kind TEXT,text_chars INTEGER);
            CREATE VIRTUAL TABLE full_text USING fts5(source_rel UNINDEXED,source_sha256 UNINDEXED,artifact_rel UNINDEXED,task_kind UNINDEXED,text);
            """
        )
        db.executemany(
            "INSERT INTO meta(key,value) VALUES(?,?)",
            (
                ("company_id", COMPANY_ID),
                ("built_at", "2026-01-02T00:00:00Z"),
                ("auto7_status", "PASS"),
                ("full_extract_status", "PASS"),
                ("manual_trade_status", "PASS"),
            ),
        )
        db.executemany(
            "INSERT INTO nodes VALUES(?,?,?)",
            (
                ("company:primary", "company", COMPANY_VALUE),
                ("contact:one", "contact", "PRIVATE CONTACT VALUE 7Q9"),
            ),
        )
        db.execute(
            "INSERT INTO relation_occurrence(edge_id,from_node_id,to_node_id,relation,source_rel,json_pointer,source_line) VALUES(?,?,?,?,?,?,?)",
            ("edge:1", "company:primary", "contact:one", "observed_in_customer_case", "raw/record.json", "/contact", 1),
        )
        db.execute(
            "INSERT INTO relation_fact VALUES(?,?,?,?,?,?)",
            ("company:primary", "contact:one", "observed_in_customer_case", 1, "raw/record.json", "raw/record.json"),
        )
        db.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
            ("raw/record.json", "A" * 64, 128, "json", "text", "application/json", "automatic", 1, "2026-01-02T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("task:1", "raw/record.json", "A" * 64, "extract_text", "completed", "derived/record.txt", "B" * 64, 1, "", "2026-01-02T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO coverage(scope,name,total,completed,pending,failed,bytes) VALUES(?,?,?,?,?,?,?)",
            ("file_category", "text", 1, 1, 0, 0, 128),
        )
        db.execute(
            "INSERT INTO manual_trade_artifacts(session,relative_path,case_relative_path,sha256,bytes,record_json) VALUES(?,?,?,?,?,?)",
            ("manual", "page.html", "evidence/page.html", "C" * 64, 32, "{}"),
        )
        db.execute(
            "INSERT INTO lineage(source_kind,source_path,source_sha256,source_bytes,target_name,row_count,recorded_at) VALUES(?,?,?,?,?,?,?)",
            ("source", "raw/record.json", "A" * 64, 128, "documents", 1, "2026-01-02T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO documents VALUES(1,?,?,?,?,?)",
            ("raw/record.json", "A" * 64, "derived/record.txt", "extract_text", len(PRIVATE_BODY)),
        )
        db.execute(
            "INSERT INTO full_text(rowid,source_rel,source_sha256,artifact_rel,task_kind,text) VALUES(1,?,?,?,?,?)",
            ("raw/record.json", "A" * 64, "derived/record.txt", "extract_text", PRIVATE_BODY),
        )
        db.commit()
    finally:
        db.close()

    graph = package / "relations.graphml.gz"
    graph.write_bytes(b"synthetic graph")
    parquet = package / "parquet"
    parquet.mkdir()
    table_file = parquet / "nodes.parquet"
    table_file.write_bytes(b"PAR1syntheticPAR1")
    artifacts = []
    for path in (db_path, graph, table_file):
        artifacts.append(
            {
                "path": path.relative_to(package).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
        )
    verification = {
        "schema": "okki.single_customer.unredacted_local_package.v1",
        "status": "PASS",
        "built_at": "2026-01-02T00:00:00Z",
        "workers": 2,
        "duckdb": {"version": "1.5.3", "threads": 2, "memory_limit": "4GB", "preserve_insertion_order": False},
        "source_gates": {"auto7": "PASS", "full_extract": "PASS", "manual_trade": gate_status},
        "counts": {
            "text_documents": 1,
            "nodes": 2,
            "relation_fact": 1,
            "relation_occurrence": 1,
            "files": 1,
            "tasks": 1,
            "coverage": 1,
            "manual_trade_artifacts": 1,
            "lineage": 1,
        },
        "artifacts": artifacts,
        "checksums_sha256": "D" * 64,
    }
    (package / mod.PACKAGE_VERIFICATION).write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return package


class UnredactedDeliveryReportTests(unittest.TestCase):
    def test_unredacted_identity_is_in_docx_but_body_is_not_logged_or_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = build_package(root)
            output = root / "report.docx"
            before = digest(package / mod.PACKAGE_DB)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = mod.main(["--package-dir", str(package), "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertRegex(stdout.getvalue(), r"^PASS sections=9 tables=\d+ paragraphs=\d+\n$")
            self.assertNotIn(COMPANY_VALUE, stdout.getvalue())
            self.assertNotIn(PRIVATE_BODY, stdout.getvalue())
            self.assertEqual(before, digest(package / mod.PACKAGE_DB))
            self.assertTrue(output.is_file())

            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                relations_xml = archive.read("word/_rels/document.xml.rels").decode("utf-8")
            self.assertIn(COMPANY_VALUE, document_xml)
            self.assertIn(COMPANY_ID, document_xml)
            self.assertNotIn(PRIVATE_BODY, document_xml)
            self.assertIn("https://github.com/duckdb/duckdb", relations_xml)
            self.assertIn("https://www.sqlite.org/fts5.html", relations_xml)

    def test_source_gate_fails_closed_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = build_package(root, gate_status="FAIL")
            output = root / "blocked.docx"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = mod.main(["--package-dir", str(package), "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "ERROR SOURCE_GATE_NOT_PASS\n")
            self.assertNotIn(COMPANY_VALUE, stderr.getvalue())
            self.assertNotIn(PRIVATE_BODY, stderr.getvalue())
            self.assertFalse(output.exists())

    def test_letter_geometry_fonts_tables_and_borderless_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = build_package(root)
            output = root / "report.docx"
            mod.build_report(package, output)
            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                styles_xml = archive.read("word/styles.xml").decode("utf-8")
                header_name = next(name for name in archive.namelist() if name.startswith("word/header") and name.endswith(".xml"))
                header_xml = archive.read(header_name).decode("utf-8")
            self.assertIn('w:w="12240"', document_xml)
            self.assertIn('w:h="15840"', document_xml)
            self.assertGreaterEqual(document_xml.count('w:top="1440"'), 1)
            self.assertRegex(document_xml, r'<w:tblW\b(?=[^>]*w:w="9360")(?=[^>]*w:type="dxa")[^>]*/>')
            self.assertRegex(document_xml, r'<w:tblInd\b(?=[^>]*w:w="120")(?=[^>]*w:type="dxa")[^>]*/>')
            self.assertIn('w:eastAsia="Microsoft YaHei"', styles_xml)
            self.assertNotIn("w:pBdr", header_xml)
            self.assertIn("<w:numPr>", document_xml)
            self.assertNotIn("1. 先在本地全文检索器", document_xml)

    def test_count_mismatch_is_rejected_without_record_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = build_package(root)
            verification_path = package / mod.PACKAGE_VERIFICATION
            payload = json.loads(verification_path.read_text(encoding="utf-8"))
            payload["counts"]["nodes"] = 99
            verification_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(mod.ReportError, "PACKAGE_COUNT_MISMATCH"):
                mod.build_report(package, root / "report.docx")


if __name__ == "__main__":
    unittest.main()
