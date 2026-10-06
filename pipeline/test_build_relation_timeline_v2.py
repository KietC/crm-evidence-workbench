# EN: Synthetic fixtures only; never read real captures or private customer directories.
# 中文：仅使用合成测试夹具，不读取真实采集或私有客户目录。
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("build_relation_timeline_v2.py")
SPEC = importlib.util.spec_from_file_location("build_relation_timeline_v2", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

COMPANY_ID = "123456789"
SECRET_COMPANY = "SYNTHETIC_COMPANY_SECRET"
SECRET_CONTACT = "SYNTHETIC_CONTACT_SECRET"
SECRET_ORDER = "SYNTHETIC_ORDER_SECRET"
SECRET_PRODUCT = "SYNTHETIC_PRODUCT_SECRET"
SECRET_MAIL = "SYNTHETIC_MAIL_SECRET"
SECRET_MAIL_2 = "SYNTHETIC_MAIL_2_SECRET"
SECRET_ROOT_MAIL = "SYNTHETIC_ROOT_MAIL_SECRET"
SECRET_REPLY_MAIL = "SYNTHETIC_REPLY_MAIL_SECRET"
SECRET_SOURCE_MAIL = "SYNTHETIC_SOURCE_MAIL_SECRET"
SECRET_FOLDER = "SYNTHETIC_FOLDER_SECRET"
SECRET_TRADE = "SYNTHETIC_TRADE_SECRET"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_case(root: Path) -> tuple[Path, Path, dict[str, Path]]:
    case = root / f"company_{COMPANY_ID}"
    case.mkdir()
    write_json(case / "case_identity.json", {"company_id": COMPANY_ID})
    completion = case / "work" / "completion_v2_test"
    baseline = completion / "baseline"
    baseline.mkdir(parents=True)
    auto = case / "raw" / "sessions" / "capture_test" / "record.json"
    write_json(
        auto,
        {
            "company_id": SECRET_COMPANY,
            "create_time": "2026-01-02T01:02:03Z",
            "contacts": [
                {
                    "contact_id": SECRET_CONTACT,
                    "created_at": "2026-01-02T09:02:03+08:00",
                }
            ],
            "orders": [
                {
                    "order_id": SECRET_ORDER,
                    "company_id": SECRET_COMPANY,
                    "order_time": 1767315723000,
                    "products": [{"product_id": SECRET_PRODUCT, "created_at": "2026-01-02 10:00:00"}],
                }
            ],
            "mails": [
                {
                    "mail_id": SECRET_MAIL,
                    "root_mail_id": SECRET_ROOT_MAIL,
                    "reply_mail_id": SECRET_REPLY_MAIL,
                    "source_mail_id": SECRET_SOURCE_MAIL,
                    "folder_id": SECRET_FOLDER,
                    "sender": "sender@example.test",
                    "receiver": "receiver@example.test",
                    "send_time": "2026-01-02T01:03:03Z",
                    "attachment_list": [{"attachment_id": "SYNTHETIC_ATTACHMENT_ID"}],
                    "trade_document_list": [{"document_id": "SYNTHETIC_DOCUMENT_ID"}],
                }
            ],
        },
    )
    manual = case / "evidence" / "manual_trade" / "manual_trade_test" / "states" / "0002_candidate_0001_base" / "candidate_page_0001.jsonl"
    manual.parent.mkdir(parents=True)
    manual.write_text(
        json.dumps({"trade_id": SECRET_TRADE, "buyer_company_id": SECRET_COMPANY, "trade_time": "2026-01-02T23:00:00Z"})
        + "\n\n"
        + json.dumps({"trade_id": SECRET_TRADE + "_2", "seller_company_id": SECRET_COMPANY, "trade_time": "not-a-time"})
        + "\n",
        encoding="utf-8",
    )
    opaque = case / "raw" / "sessions" / "capture_test" / "note.txt"
    opaque.write_text("synthetic opaque evidence", encoding="utf-8")
    attachment_one = case / "raw" / "sessions" / "capture_test" / "attachments" / "one.png"
    attachment_one.parent.mkdir(parents=True)
    attachment_one.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-one")
    attachment_two = attachment_one.with_name("two.png")
    attachment_two.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-two")
    archive = case / "raw" / "sessions" / "capture_test" / "bundle.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("nested/member.txt", "synthetic archive member")
    files = {
        "auto": auto,
        "manual": manual,
        "opaque": opaque,
        "attachment_one": attachment_one,
        "attachment_two": attachment_two,
        "archive": archive,
    }

    def resource(path: Path) -> dict[str, object]:
        return {
            "source_identity_sha256": sha(path),
            "resolution": "EXACT_SOURCE_IDENTITY",
            "artifact_source_identity_sha256": sha(path),
            "source_unavailable_http_status": None,
            "artifact": {
                "sequence": 1,
                "relative_path": path.relative_to(case).as_posix(),
                "sha256": sha(path),
                "bytes": path.stat().st_size,
                "http_status": 200,
                "mime_type": "image/png",
                "source_identity_sha256": sha(path),
            },
        }

    mail_manifest = {
        "schema": "synthetic.mail.capture.v1",
        "status": "PASS",
        "company_id": COMPANY_ID,
        "mails": [
            {"mail_id": SECRET_MAIL, "related_resources": [resource(attachment_one), resource(attachment_two)]},
            {"mail_id": SECRET_MAIL_2, "related_resources": [resource(attachment_one)]},
        ],
    }
    mail_manifest_path = baseline / "mail_capture_manifest.json"
    write_json(mail_manifest_path, mail_manifest)
    write_json(
        baseline / "freeze_manifest.json",
        {
            "company_id": COMPANY_ID,
            "authority_files": [
                {
                    "label": "mail_capture_manifest",
                    "copied_rel": "baseline/mail_capture_manifest.json",
                    "sha256": sha(mail_manifest_path),
                    "bytes": mail_manifest_path.stat().st_size,
                }
            ],
        },
    )

    source_package = root / "source_v1"
    source_package.mkdir()
    database = source_package / "customer_full_unredacted.sqlite3"
    db = sqlite3.connect(database)
    try:
        db.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE files(source_rel TEXT PRIMARY KEY,source_scope TEXT,sha256 TEXT,bytes INTEGER,magic TEXT,category TEXT,mime TEXT,prepared_at TEXT);
            CREATE TABLE tasks(task_id TEXT PRIMARY KEY,source_rel TEXT,task_kind TEXT,updated_at TEXT,output_rel TEXT,result_sha256 TEXT);
            CREATE TABLE manual_trade_artifacts(case_relative_path TEXT PRIMARY KEY,record_json TEXT);
            """
        )
        db.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [("schema", mod.SOURCE_PACKAGE_SCHEMA), ("company_id", COMPANY_ID)],
        )
        for name, path in files.items():
            rel = path.relative_to(case).as_posix()
            scope = "manual_trade" if name == "manual" else "automatic"
            suffix = path.suffix.casefold()
            magic = "json" if suffix == ".json" else "text"
            mime = "application/json" if suffix in {".json", ".jsonl"} else "text/plain"
            db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?)",
                (rel, scope, sha(path).upper(), path.stat().st_size, magic, "text", mime, "2026-01-02T04:00:00Z"),
            )
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                (
                    f"task_{name}",
                    rel,
                    "extract_text",
                    "2026-01-02T04:05:00Z",
                    (output_path := case / "derived" / "synthetic_tasks" / f"{name}.txt").relative_to(case).as_posix(),
                    "",
                ),
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(f"derived-{name}", encoding="utf-8")
            db.execute("UPDATE tasks SET result_sha256=? WHERE task_id=?", (sha(output_path), f"task_{name}"))
        db.execute(
            "INSERT INTO manual_trade_artifacts VALUES(?,?)",
            (manual.relative_to(case).as_posix(), json.dumps({"kind": "candidate_state"})),
        )
        db.commit()
    finally:
        db.close()
    write_json(source_package / "verification.json", {"status": "PASS"})
    (source_package / "checksums.sha256").write_text(
        f"{sha(database)}  customer_full_unredacted.sqlite3\n", encoding="utf-8"
    )
    return case, source_package, files


class RelationTimelineV2Tests(unittest.TestCase):
    def build(self, root: Path) -> tuple[Path, subprocess.CompletedProcess[str], dict[str, Path]]:
        case, source, files = make_case(root)
        output = root / "v2"
        process = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "build",
                "--case-root",
                str(case),
                "--company-id",
                COMPANY_ID,
                "--source-package",
                str(source),
                "--output-dir",
                str(output),
                "--workers",
                "2",
                "--completion-run-dir",
                str(root / f"company_{COMPANY_ID}" / "work" / "completion_v2_test"),
                "--expected-attachment-occurrences",
                "3",
                "--expected-mail-attachment-relations",
                "3",
                "--expected-multiparent-attachments",
                "1",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        return output, process, files

    def test_builds_explicit_relations_dual_timeline_and_exact_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output, process, _ = self.build(Path(temp))
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            for secret in (SECRET_COMPANY, SECRET_CONTACT, SECRET_ORDER, SECRET_PRODUCT, SECRET_MAIL, SECRET_TRADE):
                self.assertNotIn(secret, process.stdout + process.stderr)
            status = json.loads(process.stdout)
            self.assertEqual(status["status"], "PASS")
            database = output / "relationship_timeline_v2.sqlite3"
            db = sqlite3.connect(database)
            try:
                predicates = {row[0] for row in db.execute("SELECT predicate FROM explicit_relation")}
                self.assertIn("company_has_contact", predicates)
                self.assertIn("company_has_order", predicates)
                self.assertIn("order_has_product", predicates)
                self.assertIn("mail_has_sender", predicates)
                self.assertIn("mail_has_thread_root", predicates)
                self.assertIn("mail_replies_to", predicates)
                self.assertIn("mail_source_to_derived", predicates)
                self.assertIn("mail_in_folder", predicates)
                self.assertIn("mail_has_document", predicates)
                self.assertIn("mail_has_attachment", predicates)
                self.assertIn("trade_record_has_company", predicates)
                self.assertIn("archive_contains_member", predicates)
                self.assertIn("source_artifact_derived_to_output", predicates)
                self.assertNotIn("cooccurs_in_record", predicates)
                axes = dict(db.execute("SELECT timeline_axis,COUNT(*) FROM timeline_event GROUP BY timeline_axis"))
                self.assertGreater(axes["business"], 0)
                self.assertGreater(axes["capture"], 0)
                statuses = dict(db.execute("SELECT normalization_status,COUNT(*) FROM timeline_event GROUP BY normalization_status"))
                self.assertGreater(statuses["absolute"], 0)
                self.assertGreater(statuses["local_unzoned"], 0)
                self.assertGreater(statuses["unparsed"], 0)
                self.assertEqual(
                    db.execute("SELECT timestamp_raw FROM timeline_event WHERE field_name='trade_time' AND normalization_status='unparsed'").fetchone()[0],
                    "not-a-time",
                )
                manual_rel = next(path for path in (Path(temp) / f"company_{COMPANY_ID}" / "evidence").rglob("candidate_page_0001.jsonl")).relative_to(Path(temp) / f"company_{COMPANY_ID}").as_posix()
                self.assertGreater(
                    db.execute("SELECT COUNT(*) FROM record_occurrence WHERE source_rel=? AND source_line=3", (manual_rel,)).fetchone()[0],
                    0,
                )
                self.assertEqual(db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 7)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM artifacts WHERE source_scope='relation_provenance_manifest'").fetchone()[0], 1)
                db.row_factory = sqlite3.Row
                provenance = db.execute("""SELECT e.*,a.source_rel AS artifact_rel,r.source_rel AS record_rel,
                    r.artifact_id AS record_artifact,r.pointer_kind FROM relation_evidence e
                    JOIN artifacts a ON e.artifact_id=a.artifact_id JOIN record_occurrence r ON e.record_id=r.record_id
                    WHERE e.evidence_role='mail_capture_manifest_occurrence'""").fetchall()
                self.assertEqual(len(provenance), 3)
                for evidence in provenance:
                    self.assertEqual(evidence["artifact_id"], evidence["record_artifact"])
                    self.assertEqual(evidence["source_rel"], evidence["artifact_rel"])
                    self.assertEqual(evidence["source_rel"], evidence["record_rel"])
                    value = json.loads((Path(temp) / f"company_{COMPANY_ID}" / evidence["source_rel"]).read_text(encoding="utf-8"))
                    self.assertIn("artifact", mod.resolve_json_pointer(value, evidence["json_pointer"], evidence["pointer_kind"]))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM lineage").fetchone()[0], 9)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attachment_occurrence").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE predicate='mail_has_attachment'").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_member").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM derived_artifacts").fetchone()[0], 6)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM trade_entity WHERE unbound_to_customer=1 AND binding_status='UNBOUND'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT MIN(stable_sequence) FROM timeline_event WHERE timeline_axis='capture'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM timeline_event WHERE timestamp_precision='' OR stable_sequence_basis LIKE '%path%'").fetchone()[0], 0)
                self.assertFalse(list(db.execute("PRAGMA foreign_key_check")))
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                db.close()
            verification = json.loads((output / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "PASS")
            self.assertEqual(verification["artifact_hash_failed"], 0)
            self.assertEqual(verification["record_pointer_failed"], 0)
            self.assertEqual(verification["timeline_raw_failed"], 0)
            with gzip.open(output / "relations_v2.graphml.gz", "rt", encoding="utf-8") as handle:
                graph = handle.read()
            self.assertIn('attr.name="first_event_sort_key"', graph)
            for table in mod.PARQUET_TABLES:
                path = output / "parquet" / f"{table}.parquet"
                self.assertTrue(path.is_file())
                self.assertEqual(path.read_bytes()[:4], b"PAR1")

    def test_standalone_verify_is_content_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output, process, _ = self.build(root)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            case = root / f"company_{COMPANY_ID}"
            verify = subprocess.run(
                [sys.executable, str(MODULE_PATH), "verify", "--case-root", str(case), "--package-dir", str(output)],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertEqual(json.loads(verify.stdout)["status"], "PASS")
            self.assertNotIn(SECRET_CONTACT, verify.stdout + verify.stderr)

    def test_original_provenance_bug_and_invalid_relation_pointer_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output, process, _ = self.build(root)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            case = root / f"company_{COMPANY_ID}"
            db = sqlite3.connect(output / "relationship_timeline_v2.sqlite3")
            db.row_factory = sqlite3.Row
            try:
                self.assertGreater(mod.verify_relation_provenance(db, case), 0)
                row = db.execute("SELECT * FROM relation_evidence WHERE evidence_role='mail_capture_manifest_occurrence' LIMIT 1").fetchone()
                attachment = db.execute("SELECT artifact_id FROM attachment_occurrence LIMIT 1").fetchone()[0]
                record = db.execute("SELECT record_id FROM record_occurrence WHERE artifact_id=? AND pointer_kind='artifact'", (attachment,)).fetchone()[0]
                db.execute("UPDATE relation_evidence SET artifact_id=?,record_id=? WHERE evidence_id=?", (attachment, record, row["evidence_id"]))
                with self.assertRaisesRegex(mod.BuildError, "RELATION_PROVENANCE_MISMATCH:" + row["evidence_id"]):
                    mod.verify_relation_provenance(db, case)
                db.execute("UPDATE relation_evidence SET artifact_id=?,record_id=?,json_pointer=? WHERE evidence_id=?", (row["artifact_id"], row["record_id"], row["json_pointer"] + "/does_not_exist", row["evidence_id"]))
                with self.assertRaisesRegex(mod.BuildError, "RELATION_POINTER_INVALID:" + row["evidence_id"]):
                    mod.verify_relation_provenance(db, case)
            finally:
                db.close()

    def test_tampered_source_fails_closed_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case, source, files = make_case(root)
            files["manual"].write_text("tampered", encoding="utf-8")
            output = root / "v2"
            process = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "build",
                    "--expected-attachment-occurrences", "3",
                    "--expected-mail-attachment-relations", "3",
                    "--expected-multiparent-attachments", "1",
                    "--case-root",
                    str(case),
                    "--company-id",
                    COMPANY_ID,
                    "--source-package",
                    str(source),
                    "--output-dir",
                    str(output),
                    "--workers",
                    "2",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)["error_code"], "SOURCE_ARTIFACT_SIZE_MISMATCH")
            self.assertFalse((output / "relationship_timeline_v2.sqlite3").exists())
            self.assertNotIn(SECRET_TRADE, process.stdout + process.stderr)

    def test_existing_v2_target_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case, source, _ = make_case(root)
            output = root / "v2"
            output.mkdir()
            sentinel = output / "verification.json"
            sentinel.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
            process = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "build",
                    "--expected-attachment-occurrences", "3",
                    "--expected-mail-attachment-relations", "3",
                    "--expected-multiparent-attachments", "1",
                    "--case-root",
                    str(case),
                    "--company-id",
                    COMPANY_ID,
                    "--source-package",
                    str(source),
                    "--output-dir",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)["error_code"], "OUTPUT_TARGET_EXISTS")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "DO_NOT_OVERWRITE")


if __name__ == "__main__":
    unittest.main()

