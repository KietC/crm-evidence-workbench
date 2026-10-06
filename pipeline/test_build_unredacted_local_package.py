# EN: Synthetic fixtures only; never read real captures or private customer directories.
# 中文：仅使用合成测试夹具，不读取真实采集或私有客户目录。
from __future__ import annotations

import csv
import gzip
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("build_unredacted_local_package.py")
SPEC = importlib.util.spec_from_file_location("build_unredacted_local_package", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

COMPANY_ID = "123456789"
SECRET_NODE = "UNREDACTED CUSTOMER NODE VALUE"
SECRET_TEXT = "UNREDACTED CUSTOMER FULL TEXT TOKEN"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    value.update(path.read_bytes())
    return value.hexdigest().upper()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, headers: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def make_case(root: Path, edge_repetitions: int = 5) -> Path:
    case = root / f"company_{COMPANY_ID}"
    session = case / "raw" / "sessions" / "capture_test"
    session.mkdir(parents=True)
    (case / "work").mkdir()
    (case / "manifests").mkdir()
    write_json(case / "case_identity.json", {"company_id": COMPANY_ID})
    write_json(case / "work" / "capture_state.json", {"running": False, "companyId": COMPANY_ID})
    object_manifest = case / "manifests" / "objects.jsonl"
    object_manifest.write_text("{}\n", encoding="utf-8")
    write_json(
        case / "manifests" / "processing_scope_latest.json",
        {
            "schema": 1,
            "company_id": COMPANY_ID,
            "capture_status": "pass",
            "default_session_id": "capture_test",
            "object_manifest": "manifests/objects.jsonl",
            "default_object_filter": {"field": "session_id", "equals": "capture_test"},
        },
    )
    write_json(session / "final_status.json", {"phase": "complete", "errors": [], "metrics": {"reconciliation_failures": 0}})
    source = session / "record.json"
    source.write_text(json.dumps({"body": SECRET_TEXT}), encoding="utf-8")

    derived = case / "derived" / "full_extract"
    derived.mkdir(parents=True)
    full_text = derived / "full_text.sqlite3"
    db = sqlite3.connect(full_text)
    try:
        db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY,source_rel TEXT,source_sha256 TEXT,artifact_rel TEXT,task_kind TEXT,text_chars INTEGER)")
        db.execute("CREATE VIRTUAL TABLE full_text USING fts5(source_rel UNINDEXED,source_sha256 UNINDEXED,artifact_rel UNINDEXED,task_kind UNINDEXED,text)")
        source_sha = digest(source)
        db.execute("INSERT INTO documents VALUES(1,?,?,?,?,?)", ("raw/sessions/capture_test/record.json", source_sha, "derived/text/record.txt", "extract_text", len(SECRET_TEXT)))
        db.execute("INSERT INTO full_text(rowid,source_rel,source_sha256,artifact_rel,task_kind,text) VALUES(1,?,?,?,?,?)", ("raw/sessions/capture_test/record.json", source_sha, "derived/text/record.txt", "extract_text", SECRET_TEXT))
        db.commit()
    finally:
        db.close()

    nodes = [
        {"record_type": "node", "node_id": "company:1", "entity_type": "company", "value": COMPANY_ID},
        {"record_type": "node", "node_id": "contact:1", "entity_type": "contact", "value": SECRET_NODE},
    ]
    edges = [
        {
            "record_type": "edge",
            "edge_id": f"edge:{index}",
            "from": "company:1",
            "to": "contact:1",
            "relation": "observed_in_customer_case",
            "source_rel": "raw/sessions/capture_test/record.json",
            "json_pointer": f"/contact/{index}",
        }
        for index in range(edge_repetitions)
    ]
    relation_path = derived / "relationship_graph.jsonl"
    relation_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [*nodes, *edges]), encoding="utf-8")
    source_sha = digest(source)
    write_csv(
        derived / "file_inventory.csv",
        ["source_rel", "sha256", "bytes", "magic", "category", "mime", "source_scope", "active", "prepared_at"],
        [{"source_rel": "raw/sessions/capture_test/record.json", "sha256": source_sha, "bytes": source.stat().st_size, "magic": "json", "category": "text", "mime": "application/json", "source_scope": "automatic", "active": 1, "prepared_at": "2026-01-02T00:00:00Z"}],
    )
    write_csv(
        derived / "evidence_index.csv",
        ["task_id", "source_rel", "source_sha256", "task_kind", "state", "output_rel", "result_sha256", "attempts", "error_code", "updated_at"],
        [{"task_id": "task1", "source_rel": "raw/sessions/capture_test/record.json", "source_sha256": source_sha, "task_kind": "extract_text", "state": "completed", "output_rel": "derived/text/record.txt", "result_sha256": "A" * 64, "attempts": 1, "error_code": "", "updated_at": "2026-01-02T00:00:00Z"}],
    )
    write_csv(
        derived / "coverage.csv",
        ["scope", "name", "total", "completed", "pending", "failed", "bytes"],
        [{"scope": "file_category", "name": "text", "total": 1, "completed": 1, "pending": 0, "failed": 0, "bytes": source.stat().st_size}],
    )
    write_json(
        derived / "verification.json",
        {
            "schema": mod.FULL_EXTRACT_SCHEMA,
            "company_id": COMPANY_ID,
            "status": "PASS",
            "auto7_status": "PASS",
            "automatic_session_id": "capture_test",
            "manual_trade_status": "PASS",
            "manual_source_gaps": 0,
            "pending": 0,
            "failed": 0,
            "binding_mismatch": 0,
            "evidence_scope_mismatch": 0,
            "evidence_hash_mismatch": 0,
            "missing_outputs": 0,
            "bad_output_hash": 0,
            "missing_indexes": 0,
            "sqlite_integrity": "ok",
            "fts_integrity": "ok",
            "evidence_files": 1,
            "tasks": 1,
            "fts_rows": 1,
            "relation_rows": len(nodes) + len(edges),
        },
    )

    manual = case / "evidence" / "manual_trade" / "manual_trade_test"
    manual.mkdir(parents=True)
    artifact = manual / "state.json"
    artifact.write_text(json.dumps({"trade": SECRET_TEXT}), encoding="utf-8")
    artifact_row = {"relative_path": "state.json", "bytes": artifact.stat().st_size, "sha256": digest(artifact), "kind": "stable_state"}
    manifest = manual / "trade_manual_capture_manifest.json"
    write_json(
        manifest,
        {
            "schema": mod.MANUAL_MANIFEST_SCHEMA,
            "status": "PASS",
            "company_id": COMPANY_ID,
            "session": "manual_trade_test",
            "source_gaps": [],
            "counts": {"artifact_files": 1, "artifact_bytes": artifact.stat().st_size},
            "artifacts": [artifact_row],
        },
    )
    write_json(
        manual / "trade_manual_capture_receipt.json",
        {
            "schema": mod.MANUAL_RECEIPT_SCHEMA,
            "status": "PASS",
            "company_id": COMPANY_ID,
            "session": "manual_trade_test",
            "manifest_path": relative_path(manifest, case),
            "manifest_bytes": manifest.stat().st_size,
            "manifest_sha256": digest(manifest),
            "counts": {"artifact_files": 1, "artifact_bytes": artifact.stat().st_size},
        },
    )
    return case


def relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def sqlite_runtime() -> str:
    candidates = [
        sys.executable,
        os.environ.get("CRM_SQLITE_PYTHON", sys.executable),
    ]
    for candidate in candidates:
        if not Path(candidate).is_file():
            continue
        probe = subprocess.run(
            [candidate, "-c", f"import sys; sys.path.insert(0,{str(MODULE_PATH.parent)!r}); import build_unredacted_local_package as p; p.ensure_sqlite_version()"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise unittest.SkipTest("A patched-WAL SQLite runtime with required capabilities is unavailable")


class UnredactedPackageTests(unittest.TestCase):
    def test_sqlite_version_gate(self) -> None:
        with self.assertRaisesRegex(mod.PackageError, "SQLITE_VERSION_TOO_OLD"):
            mod.ensure_sqlite_version((3, 51, 2))
        mod.ensure_sqlite_version((3, 51, 3))

    def test_dependency_missing_is_stable_and_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            output = Path(temp) / "output"
            environment = os.environ.copy()
            environment["OKKI_DUCKDB_DISABLE_DISCOVERY"] = "1"
            process = subprocess.run(
                [sqlite_runtime(), str(MODULE_PATH), "--case-root", str(case), "--company-id", COMPANY_ID, "--output-dir", str(output), "--workers", "1"],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(process.returncode, 4)
            self.assertEqual(json.loads(process.stdout)["status"], "DEPENDENCY_MISSING")
            self.assertNotIn(SECRET_NODE, process.stdout + process.stderr)
            self.assertFalse((output / "customer_full_unredacted.sqlite3").exists())

    def test_synthetic_build_preserves_values_and_aggregates_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp), edge_repetitions=5000)
            output = Path(temp) / "output"
            process = subprocess.run(
                [sqlite_runtime(), str(MODULE_PATH), "--case-root", str(case), "--company-id", COMPANY_ID, "--output-dir", str(output), "--workers", "2"],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            self.assertEqual(process.returncode, 0, process.stdout[-500:] + process.stderr[-500:])
            self.assertNotIn(SECRET_NODE, process.stdout + process.stderr)
            self.assertNotIn(SECRET_TEXT, process.stdout + process.stderr)
            console = json.loads(process.stdout)
            self.assertEqual(console["status"], "PASS")
            self.assertEqual(console["counts"]["relation_occurrence"], 5000)

            package_db = output / "customer_full_unredacted.sqlite3"
            db = sqlite3.connect(package_db)
            try:
                self.assertEqual(db.execute("SELECT value FROM nodes WHERE node_id='contact:1'").fetchone()[0], SECRET_NODE)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM relation_occurrence").fetchone()[0], 5000)
                self.assertEqual(db.execute("SELECT evidence_count FROM relation_fact").fetchone()[0], 5000)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM full_text WHERE full_text MATCH ?", ("UNREDACTED",)).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM manual_trade_artifacts").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                db.close()
            with gzip.open(output / "relations.graphml.gz", "rt", encoding="utf-8") as handle:
                graph = handle.read()
            self.assertIn(SECRET_NODE, graph)
            for name in mod.PARQUET_TABLES:
                parquet = output / "parquet" / f"{name}.parquet"
                self.assertTrue(parquet.is_file())
                with parquet.open("rb") as handle:
                    self.assertEqual(handle.read(4), b"PAR1")
            verification = json.loads((output / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "PASS")
            self.assertEqual(verification["source_gates"], {"auto7": "PASS", "full_extract": "PASS", "manual_trade": "PASS"})
            checksums = (output / "checksums.sha256").read_text(encoding="utf-8")
            self.assertIn("customer_full_unredacted.sqlite3", checksums)
            self.assertNotIn(SECRET_NODE, checksums)

    def test_identity_mismatch_fails_closed_without_value_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            output = Path(temp) / "output"
            process = subprocess.run(
                [sqlite_runtime(), str(MODULE_PATH), "--case-root", str(case), "--company-id", "999", "--output-dir", str(output), "--workers", "1"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)["error_code"], "CASE_IDENTITY_MISMATCH")
            self.assertNotIn(SECRET_NODE, process.stdout + process.stderr)
            self.assertFalse((output / "verification.json").exists())

    def test_existing_final_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            output = Path(temp) / "output"
            output.mkdir()
            sentinel = output / "verification.json"
            sentinel.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
            process = subprocess.run(
                [sqlite_runtime(), str(MODULE_PATH), "--case-root", str(case), "--company-id", COMPANY_ID, "--output-dir", str(output), "--workers", "1"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)["error_code"], "OUTPUT_TARGET_EXISTS")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "DO_NOT_OVERWRITE")

    def test_duplicate_node_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            relation_path = case / "derived" / "full_extract" / "relationship_graph.jsonl"
            with relation_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"record_type": "node", "node_id": "contact:1", "entity_type": "contact", "value": SECRET_NODE}) + "\n")
            verification_path = case / "derived" / "full_extract" / "verification.json"
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
            verification["relation_rows"] += 1
            write_json(verification_path, verification)
            process = subprocess.run(
                [sqlite_runtime(), str(MODULE_PATH), "--case-root", str(case), "--company-id", COMPANY_ID, "--output-dir", str(Path(temp) / "output"), "--workers", "1"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)["error_code"], "RELATION_NODE_DUPLICATE_OR_CONFLICT")
            self.assertNotIn(SECRET_NODE, process.stdout + process.stderr)

    def test_worker_limit_respects_windows_process_pool_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            with self.assertRaisesRegex(mod.PackageError, "WORKER_COUNT_INVALID"):
                mod.build_package(case, COMPANY_ID, Path(temp) / "output", workers=62)


if __name__ == "__main__":
    unittest.main()

