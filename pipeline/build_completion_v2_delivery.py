#!/usr/bin/env python3
# EN: Stage, verify and atomically seal a successor delivery without changing prior evidence.
# 中文：分阶段验证并原子封存继承交付，不修改原有证据。
"""Assemble and atomically publish the OKKI single-customer completion-v2 delivery.

The assembler is deliberately two phase. ``prepare`` creates a private stage
directory and mirrors verified upstream packages with hard links when possible.
Presentation builders then write the XLSX/DOCX/PDF and their QA receipts into
that stage. ``finalize`` verifies those receipts, writes the checksum seal and
verification marker last, then atomically renames the stage to its immutable
``completion_v2_<run_id>`` destination.

No customer body text is written to stdout.  Detail rows are only emitted to
the local delivery payload and authority database.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence


SCHEMA = "okki.single_customer.completion_v2_delivery.v1"
PAYLOAD_SCHEMA = "okki.single_customer.completion_v2_catalog_payload.v1"
AUTHORITY_SCHEMA = "okki.single_customer.completion_v2_authority.v1"
OLD_PACKAGE_SCHEMA = "okki.single_customer.unredacted_local_package.v1"
RELATIONSHIP_SCHEMA = "okki.single_customer.relation_timeline.v2"
UI_SCHEMA = "okki.ui_gap_revisit.v1"
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_MAX_EXCEL_ROWS = 100_000
REQUIRED_FULL_EXTRACT_FILES = (
    "verification.json",
    "state.sqlite3",
    "full_text.sqlite3",
    "processing_scope.v2.json",
    "prepare_manifest.json",
    "run_manifest.json",
    "file_inventory.csv",
    "content_object_index.csv",
    "occurrence_index.csv",
    "source_gaps.csv",
    "evidence_index.csv",
    "media_inventory.csv",
    "coverage.csv",
    "relationship_graph.jsonl",
)


class DeliveryError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise DeliveryError(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, canonical_bytes(value))


def read_json(path: Path, code: str) -> dict[str, Any]:
    require(path.is_file(), f"{code}_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"{code}_INVALID") from exc
    require(isinstance(value, dict), f"{code}_INVALID")
    return value


def parse_int(value: Any, code: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DeliveryError(code) from exc
    require(parsed >= 0, code)
    return parsed


def normalize_sha(value: Any, code: str) -> str:
    text = str(value or "")
    require(SHA_RE.fullmatch(text) is not None, code)
    return text.upper()


def safe_relative(value: Any, code: str) -> str:
    text = str(value or "").replace("\\", "/")
    pure = PurePosixPath(text)
    require(text != "" and not pure.is_absolute() and ".." not in pure.parts, code)
    return pure.as_posix()


def resolve_inside(root: Path, value: str | Path, code: str) -> Path:
    base = root.resolve()
    candidate = Path(value)
    result = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    require(result == base or base in result.parents, code)
    return result


def relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def open_readonly(path: Path) -> sqlite3.Connection:
    require(path.is_file(), "SQLITE_MISSING")
    # Upstream components are required to be finalized before assembly.  The
    # immutable flag prevents Windows SQLite from retaining sidecar handles on
    # these read-only inputs and makes the no-mutation contract explicit.
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
        timeout=60,
        cached_statements=0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def table_names(db: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def require_columns(db: sqlite3.Connection, table: str, expected: set[str], code: str) -> set[str]:
    """Discover the live schema and fail closed instead of guessing aliases."""
    require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) is not None, code)
    actual = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
    require(expected.issubset(actual), code)
    return actual


def read_checksums(path: Path) -> dict[str, str]:
    require(path.is_file(), "CHECKSUM_FILE_MISSING")
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        require(match is not None, "CHECKSUM_FILE_INVALID")
        rel = safe_relative(match.group(2), "CHECKSUM_PATH_INVALID")
        require(rel not in rows, "CHECKSUM_PATH_DUPLICATE")
        rows[rel] = match.group(1).upper()
    require(bool(rows), "CHECKSUM_FILE_EMPTY")
    return rows


def verify_checksum_map(root: Path, values: dict[str, str], workers: int = 8) -> None:
    def check(item: tuple[str, str]) -> bool:
        rel, digest = item
        path = resolve_inside(root, rel, "CHECKSUM_PATH_ESCAPE")
        return path.is_file() and sha256_file(path) == digest

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        require(all(pool.map(check, values.items())), "CHECKSUM_MISMATCH")


def hardlink_or_copy(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    require(not target.exists(), "DELIVERY_TARGET_COLLISION")
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        shutil.copy2(source, target)
        return "copy"


def mirror_tree(source: Path, target: Path) -> dict[str, int]:
    source = source.resolve()
    require(source.is_dir(), "MIRROR_SOURCE_MISSING")
    linked = copied = files = 0
    for path in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: item.as_posix().casefold()):
        if path.name.endswith((".part", "-wal", "-shm")) or path.name.startswith(".completion_v2_"):
            continue
        rel = path.relative_to(source)
        mode = hardlink_or_copy(path, target / rel)
        files += 1
        linked += mode == "hardlink"
        copied += mode == "copy"
    return {"files": files, "hardlinks": linked, "copies": copied}


def load_identity(case_root: Path, company_id: str) -> dict[str, Any]:
    require(SAFE_ID_RE.fullmatch(company_id) is not None, "COMPANY_ID_INVALID")
    require(case_root.is_dir(), "CASE_ROOT_MISSING")
    identity = read_json(case_root / "case_identity.json", "CASE_IDENTITY")
    require(str(identity.get("company_id") or "") == company_id, "CASE_IDENTITY_MISMATCH")
    require(case_root.name.endswith(f"_{company_id}"), "CASE_DIRECTORY_IDENTITY_MISMATCH")
    return identity


def load_old_package(root: Path, expected_old_files: int) -> dict[str, Any]:
    root = root.resolve()
    verification = read_json(root / "verification.json", "OLD_PACKAGE_VERIFICATION")
    require(verification.get("schema") == OLD_PACKAGE_SCHEMA, "OLD_PACKAGE_SCHEMA_INVALID")
    require(verification.get("status") == "PASS", "OLD_PACKAGE_NOT_PASS")
    source_gates = verification.get("source_gates")
    require(isinstance(source_gates, dict) and all(value == "PASS" for value in source_gates.values()), "OLD_PACKAGE_GATES_NOT_PASS")
    counts = verification.get("counts")
    require(isinstance(counts, dict), "OLD_PACKAGE_COUNTS_INVALID")
    require(parse_int(counts.get("files"), "OLD_PACKAGE_FILES_INVALID") == expected_old_files, "OLD_PACKAGE_FILES_MISMATCH")
    checksums = read_checksums(root / "checksums.sha256")
    verify_checksum_map(root, checksums)
    delivery_manifest_path = root / "DELIVERY_MANIFEST.json"
    if delivery_manifest_path.is_file():
        delivery_manifest = read_json(delivery_manifest_path, "OLD_DELIVERY_MANIFEST")
        require(delivery_manifest.get("status") == "DELIVERY_PASS", "OLD_DELIVERY_NOT_PASS")
    db = open_readonly(root / "customer_full_unredacted.sqlite3")
    try:
        require(str(db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "OLD_PACKAGE_DB_INTEGRITY_FAILED")
        require(not list(db.execute("PRAGMA foreign_key_check")), "OLD_PACKAGE_DB_FOREIGN_KEY_FAILED")
        require("files" in table_names(db), "OLD_PACKAGE_DB_FILES_SCHEMA_INVALID")
        require(int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0]) == expected_old_files, "OLD_PACKAGE_DB_FILES_MISMATCH")
    finally:
        db.close()
        gc.collect()
    return {"root": root, "verification": verification, "counts": {key: int(value) for key, value in counts.items()}, "verification_sha256": sha256_file(root / "verification.json")}


def load_full_extract(root: Path, case_root: Path, company_id: str, expected_historical: int, min_evidence: int) -> dict[str, Any]:
    root = root.resolve()
    for name in REQUIRED_FULL_EXTRACT_FILES:
        require((root / name).is_file(), f"FULL_EXTRACT_REQUIRED_FILE_MISSING_{name.upper().replace('.', '_')}")
    verification = read_json(root / "verification.json", "FULL_EXTRACT_VERIFICATION")
    require(str(verification.get("company_id") or "") == company_id, "FULL_EXTRACT_COMPANY_MISMATCH")
    require(verification.get("status") in {"PASS", "INCOMPLETE_SOURCE_GAPS"}, "FULL_EXTRACT_NOT_TERMINAL")
    require(parse_int(verification.get("pending"), "FULL_EXTRACT_PENDING_INVALID") == 0, "FULL_EXTRACT_PENDING")
    require(parse_int(verification.get("failed"), "FULL_EXTRACT_FAILED_INVALID") == 0, "FULL_EXTRACT_FAILED")
    require(parse_int(verification.get("historical_mail_files"), "FULL_EXTRACT_HISTORICAL_INVALID") == expected_historical, "FULL_EXTRACT_HISTORICAL_MISMATCH")
    require(parse_int(verification.get("evidence_files"), "FULL_EXTRACT_EVIDENCE_INVALID") >= min_evidence, "FULL_EXTRACT_EVIDENCE_BELOW_BASELINE")
    state = open_readonly(root / "state.sqlite3")
    try:
        require(str(state.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "FULL_EXTRACT_DB_INTEGRITY_FAILED")
        require(not list(state.execute("PRAGMA foreign_key_check")), "FULL_EXTRACT_DB_FOREIGN_KEY_FAILED")
        required = {"meta", "files", "tasks", "content_objects", "occurrences", "source_gaps"}
        require(required.issubset(table_names(state)), "FULL_EXTRACT_DB_SCHEMA_INVALID")
        require_columns(state, "meta", {"key", "value"}, "FULL_EXTRACT_META_SCHEMA_INVALID")
        require_columns(state, "files", {"source_rel", "sha256", "bytes", "active"}, "FULL_EXTRACT_FILES_SCHEMA_INVALID")
        require_columns(state, "tasks", {"state", "active"}, "FULL_EXTRACT_TASKS_SCHEMA_INVALID")
        require_columns(state, "content_objects", {"sha256", "active"}, "FULL_EXTRACT_OBJECTS_SCHEMA_INVALID")
        require_columns(state, "occurrences", {"occurrence_id", "content_sha256", "active"}, "FULL_EXTRACT_OCCURRENCES_SCHEMA_INVALID")
        require_columns(state, "source_gaps", {"gap_id", "active"}, "FULL_EXTRACT_GAPS_SCHEMA_INVALID")
        meta = {str(row[0]): str(row[1]) for row in state.execute("SELECT key,value FROM meta")}
        require(meta.get("company_id") == company_id, "FULL_EXTRACT_DB_COMPANY_MISMATCH")
        nonterminal = int(state.execute("SELECT COUNT(*) FROM tasks WHERE active=1 AND state NOT IN ('completed','source_gap')").fetchone()[0])
        require(nonterminal == 0, "FULL_EXTRACT_DB_TASKS_NOT_TERMINAL")
        db_counts = {
            "files": int(state.execute("SELECT COUNT(*) FROM files WHERE active=1").fetchone()[0]),
            "tasks": int(state.execute("SELECT COUNT(*) FROM tasks WHERE active=1").fetchone()[0]),
            "content_objects": int(state.execute("SELECT COUNT(*) FROM content_objects WHERE active=1").fetchone()[0]),
            "occurrences": int(state.execute("SELECT COUNT(*) FROM occurrences WHERE active=1").fetchone()[0]),
            "source_gaps": int(state.execute("SELECT COUNT(*) FROM source_gaps WHERE active=1").fetchone()[0]),
        }
        require(db_counts["files"] == parse_int(verification.get("evidence_files"), "FULL_EXTRACT_EVIDENCE_INVALID"), "FULL_EXTRACT_FILE_COUNT_MISMATCH")
        require(db_counts["tasks"] == parse_int(verification.get("tasks"), "FULL_EXTRACT_TASKS_INVALID"), "FULL_EXTRACT_TASK_COUNT_MISMATCH")
        rows = [dict(row) for row in state.execute("SELECT source_rel,sha256,bytes FROM files WHERE active=1 ORDER BY source_rel")]
    finally:
        state.close()
        gc.collect()
    fts = open_readonly(root / "full_text.sqlite3")
    try:
        require(str(fts.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "FULL_EXTRACT_FTS_INTEGRITY_FAILED")
        require({"documents", "full_text"}.issubset(table_names(fts)), "FULL_EXTRACT_FTS_SCHEMA_INVALID")
        require_columns(fts, "documents", {"id"}, "FULL_EXTRACT_FTS_DOCUMENTS_SCHEMA_INVALID")
        fts_rows = int(fts.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    finally:
        fts.close()
        gc.collect()

    def verify_source(row: dict[str, Any]) -> bool:
        path = resolve_inside(case_root, row["source_rel"], "FULL_EXTRACT_SOURCE_ESCAPE")
        return path.is_file() and path.stat().st_size == int(row["bytes"]) and sha256_file(path) == normalize_sha(row["sha256"], "FULL_EXTRACT_SOURCE_SHA_INVALID")

    with ThreadPoolExecutor(max_workers=8) as pool:
        require(all(pool.map(verify_source, rows)), "FULL_EXTRACT_SOURCE_HASH_MISMATCH")
    return {
        "root": root,
        "verification": verification,
        "verification_sha256": sha256_file(root / "verification.json"),
        "counts": {**db_counts, "fts_documents": fts_rows},
        "source_rows": rows,
    }


def load_relationship(root: Path, company_id: str, expected_occurrences: int, expected_links: int, expected_multiparent: int) -> dict[str, Any]:
    root = root.resolve()
    verification = read_json(root / "verification.json", "RELATIONSHIP_VERIFICATION")
    require(verification.get("schema") == RELATIONSHIP_SCHEMA, "RELATIONSHIP_SCHEMA_INVALID")
    require(verification.get("status") == "PASS", "RELATIONSHIP_NOT_PASS")
    checksums = read_checksums(root / "checksums.sha256")
    verify_checksum_map(root, checksums)
    database = root / "relationship_timeline_v2.sqlite3"
    db = open_readonly(database)
    try:
        require(str(db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "RELATIONSHIP_DB_INTEGRITY_FAILED")
        require(not list(db.execute("PRAGMA foreign_key_check")), "RELATIONSHIP_DB_FOREIGN_KEY_FAILED")
        required = {"meta", "nodes", "explicit_relation", "relation_evidence", "timeline_event", "attachment_occurrence"}
        require(required.issubset(table_names(db)), "RELATIONSHIP_DB_SCHEMA_INVALID")
        require_columns(db, "meta", {"key", "value"}, "RELATIONSHIP_META_SCHEMA_INVALID")
        require_columns(db, "nodes", {"node_id", "node_type", "value"}, "RELATIONSHIP_NODES_SCHEMA_INVALID")
        require_columns(db, "explicit_relation", {"relation_id", "subject_node_id", "predicate", "object_node_id", "relation_kind", "evidence_count"}, "RELATIONSHIP_RELATIONS_SCHEMA_INVALID")
        require_columns(db, "relation_evidence", {"relation_id", "source_rel"}, "RELATIONSHIP_EVIDENCE_SCHEMA_INVALID")
        require_columns(db, "timeline_event", {"event_id", "timeline_axis", "stable_sequence", "stable_order_key", "event_kind", "field_name", "timestamp_raw", "timestamp_utc", "local_sort_key", "normalization_status", "timestamp_precision", "source_rel", "json_pointer"}, "RELATIONSHIP_TIMELINE_SCHEMA_INVALID")
        require_columns(db, "attachment_occurrence", {"mail_node_id", "attachment_node_id", "artifact_source_rel", "artifact_sha256", "source_pointer", "resolution"}, "RELATIONSHIP_ATTACHMENT_SCHEMA_INVALID")
        meta = {str(row[0]): str(row[1]) for row in db.execute("SELECT key,value FROM meta")}
        require(meta.get("schema") == RELATIONSHIP_SCHEMA, "RELATIONSHIP_DB_SCHEMA_MISMATCH")
        if meta.get("company_id"):
            require(meta["company_id"] == company_id, "RELATIONSHIP_COMPANY_MISMATCH")
        attachment_occurrences = int(db.execute("SELECT COUNT(*) FROM attachment_occurrence").fetchone()[0])
        distinct_links = int(db.execute("SELECT COUNT(*) FROM (SELECT DISTINCT mail_node_id,attachment_node_id FROM attachment_occurrence)").fetchone()[0])
        multiparent = int(db.execute("SELECT COUNT(*) FROM (SELECT attachment_node_id FROM attachment_occurrence GROUP BY attachment_node_id HAVING COUNT(DISTINCT mail_node_id)>1)").fetchone()[0])
        require(attachment_occurrences == expected_occurrences, "RELATIONSHIP_ATTACHMENT_OCCURRENCE_MISMATCH")
        require(distinct_links == expected_links, "RELATIONSHIP_MAIL_ATTACHMENT_LINK_MISMATCH")
        require(multiparent == expected_multiparent, "RELATIONSHIP_MULTIPARENT_MISMATCH")
        axes = {str(row[0]): int(row[1]) for row in db.execute("SELECT timeline_axis,COUNT(*) FROM timeline_event GROUP BY timeline_axis")}
        require(axes.get("business", 0) > 0, "RELATIONSHIP_BUSINESS_TIMELINE_EMPTY")
        capture_count = axes.get("capture", 0) + axes.get("processing", 0)
        require(capture_count > 0, "RELATIONSHIP_CAPTURE_TIMELINE_EMPTY")
        counts = {str(name): int(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in required if name != "meta"}
    finally:
        db.close()
        gc.collect()
    return {
        "root": root,
        "database": database,
        "verification": verification,
        "verification_sha256": sha256_file(root / "verification.json"),
        "counts": counts,
        "axes": axes,
        "attachment_occurrences": attachment_occurrences,
        "mail_attachment_relations": distinct_links,
        "multiparent_attachments": multiparent,
    }


def load_ui_manifest(path: Path, case_root: Path, company_id: str) -> dict[str, Any]:
    path = path.resolve()
    manifest = read_json(path, "UI_GAP_MANIFEST")
    require(manifest.get("schema") == UI_SCHEMA, "UI_GAP_SCHEMA_INVALID")
    require(str(manifest.get("company_id") or "") == company_id, "UI_GAP_COMPANY_MISMATCH")
    require(manifest.get("status") == "PASS", "UI_GAP_NOT_PASS")
    require(manifest.get("no_auto_next") is True, "UI_GAP_AUTO_NEXT_NOT_DISABLED")
    require(manifest.get("manual_trade_automatic_capture") is False, "UI_GAP_TRADE_AUTOMATION_PRESENT")
    checks = manifest.get("checks")
    require(isinstance(checks, dict) and checks and all(value is True for value in checks.values()), "UI_GAP_CHECKS_NOT_PASS")
    require(checks.get("other_dynamic_found") is True and checks.get("other_dynamic_terminal_closed") is True, "UI_GAP_OTHER_DYNAMIC_NOT_CLOSED")
    session_id = str(manifest.get("session_id") or "")
    require(SAFE_ID_RE.fullmatch(session_id) is not None, "UI_GAP_SESSION_INVALID")
    session_root = resolve_inside(case_root, Path("raw") / "sessions" / session_id, "UI_GAP_SESSION_ESCAPE")
    require(session_root.is_dir(), "UI_GAP_SESSION_MISSING")
    return {"path": path, "manifest": manifest, "session_id": session_id, "session_root": session_root, "sha256": sha256_file(path)}


def source_gap_rows(full_root: Path) -> list[dict[str, str]]:
    path = full_root / "source_gaps.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    required = {"gap_id", "source_scope", "role", "error_code"}
    require(all(required.issubset(row) for row in rows), "SOURCE_GAP_SCHEMA_INVALID")
    return rows


def build_diff(old: dict[str, Any], full: dict[str, Any], relation: dict[str, Any], gaps: list[dict[str, str]]) -> dict[str, Any]:
    old_counts = old["counts"]
    full_counts = full["counts"]
    rel_counts = relation["counts"]
    rows = [
        {"metric": "evidence_files", "old": old_counts.get("files", 0), "v2": full_counts["files"], "delta": full_counts["files"] - old_counts.get("files", 0)},
        {"metric": "processing_tasks", "old": old_counts.get("tasks", 0), "v2": full_counts["tasks"], "delta": full_counts["tasks"] - old_counts.get("tasks", 0)},
        {"metric": "fts_documents", "old": old_counts.get("text_documents", 0), "v2": full_counts["fts_documents"], "delta": full_counts["fts_documents"] - old_counts.get("text_documents", 0)},
        {"metric": "explicit_relations", "old": old_counts.get("relation_fact", 0), "v2": rel_counts["explicit_relation"], "delta": rel_counts["explicit_relation"] - old_counts.get("relation_fact", 0)},
        {"metric": "timeline_events", "old": 0, "v2": rel_counts["timeline_event"], "delta": rel_counts["timeline_event"]},
        {"metric": "mail_attachment_occurrences", "old": 0, "v2": relation["attachment_occurrences"], "delta": relation["attachment_occurrences"]},
        {"metric": "source_gaps", "old": 0, "v2": len(gaps), "delta": len(gaps)},
    ]
    return {"schema": SCHEMA, "rows": rows}


def query_rows(db: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[list[Any]]:
    return [[value for value in row] for row in db.execute(sql, params)]


def build_payload(company_id: str, old: dict[str, Any], full: dict[str, Any], relationship: dict[str, Any], ui: dict[str, Any], gaps: list[dict[str, str]], diff: dict[str, Any], max_rows: int) -> dict[str, Any]:
    gates = {
        "PAGE_GAP_RECOVERY_PASS": True,
        "MAIL_ATTACHMENT_SCOPE_PASS": True,
        "CONTENT_RECURSION_PASS_WITH_SOURCE_GAPS": True,
        "RELATIONSHIP_V2_PASS": True,
        "TIMELINE_V2_PASS": True,
        "DELIVERY_V2_PASS": False,
    }
    db = open_readonly(relationship["database"])
    try:
        mail_total = relationship["mail_attachment_relations"]
        explicit_total = relationship["counts"]["explicit_relation"]
        business_total = relationship["axes"]["business"]
        capture_total = relationship["axes"].get("capture", 0) + relationship["axes"].get("processing", 0)
        mail_rows = query_rows(
            db,
            """SELECT a.mail_node_id,m.value,a.attachment_node_id,n.value,MIN(a.artifact_source_rel),
                      MIN(a.artifact_sha256),MIN(a.source_pointer),MIN(a.resolution)
                 FROM attachment_occurrence a
                 JOIN nodes m ON m.node_id=a.mail_node_id
                 JOIN nodes n ON n.node_id=a.attachment_node_id
                GROUP BY a.mail_node_id,a.attachment_node_id
                ORDER BY a.mail_node_id,a.attachment_node_id LIMIT ?""",
            (max_rows,),
        )
        explicit_rows = query_rows(
            db,
            """SELECT r.relation_id,s.node_type,s.value,r.predicate,o.node_type,o.value,
                      r.relation_kind,r.evidence_count,
                      COALESCE((SELECT e.source_rel FROM relation_evidence e WHERE e.relation_id=r.relation_id ORDER BY e.evidence_id LIMIT 1),'')
                 FROM explicit_relation r JOIN nodes s ON s.node_id=r.subject_node_id
                 JOIN nodes o ON o.node_id=r.object_node_id
                ORDER BY r.relation_kind,r.predicate,r.relation_id LIMIT ?""",
            (max_rows,),
        )
        timeline_sql = """SELECT stable_sequence,event_id,event_kind,field_name,timestamp_raw,
                                 COALESCE(timestamp_utc,local_sort_key,''),normalization_status,
                                 timestamp_precision,source_rel,json_pointer
                            FROM timeline_event WHERE timeline_axis=?
                           ORDER BY stable_sequence,stable_order_key,event_id LIMIT ?"""
        business_rows = query_rows(db, timeline_sql, ("business", max_rows))
        capture_axis = "capture" if relationship["axes"].get("capture", 0) else "processing"
        capture_rows = query_rows(db, timeline_sql, (capture_axis, max_rows))
    finally:
        db.close()
        gc.collect()
    gap_rows = [[
        row.get("gap_id", ""), row.get("source_scope", ""), row.get("role", ""),
        row.get("error_code", ""), row.get("http_status", ""), row.get("content_sha256", ""),
        row.get("source_identity_sha256", ""), row.get("owner_sha256", ""),
    ] for row in gaps]
    coverage = [
        ["旧版合格成果", old["counts"].get("files", 0), old["counts"].get("files", 0), 0, "PASS", "legacy_v1/verification.json"],
        ["v2证据文件", full["counts"]["files"], full["counts"]["files"], 0, full["verification"]["status"], "full_extract_v2/verification.json"],
        ["邮件历史附件原件", full["verification"]["historical_mail_files"], full["verification"]["historical_mail_files"], 0, "PASS", "full_extract_v2/state.sqlite3"],
        ["邮件附件出现记录", relationship["attachment_occurrences"], relationship["attachment_occurrences"], 0, "PASS", "relationship_v2/relationship_timeline_v2.sqlite3"],
        ["唯一邮件附件关系", mail_total, len(mail_rows), max(0, mail_total-len(mail_rows)), "PASS", "relationship_v2/relationship_timeline_v2.sqlite3"],
        ["显式关系", explicit_total, len(explicit_rows), max(0, explicit_total-len(explicit_rows)), "PASS", "relationship_v2/relationship_timeline_v2.sqlite3"],
        ["业务时间线", business_total, len(business_rows), max(0, business_total-len(business_rows)), "PASS", "relationship_v2/relationship_timeline_v2.sqlite3"],
        ["采集时间线", capture_total, len(capture_rows), max(0, capture_total-len(capture_rows)), "PASS", "relationship_v2/relationship_timeline_v2.sqlite3"],
        ["UI缺口补采", 1, 1, 0, "PASS", "ui_gap/ui_gap_revisit_manifest.json"],
        ["客观源缺口", len(gaps), len(gaps), 0, "SOURCE_GAPS", "source_gaps.csv"],
    ]
    return {
        "schema": PAYLOAD_SCHEMA,
        "company_id": company_id,
        "generated_at_utc": utc_now(),
        "accessible_data_status": "ACCESSIBLE_DATA_PASS",
        "absolute_completeness_status": "ABSOLUTE_COMPLETENESS_SOURCE_GAPS" if gaps else "ABSOLUTE_COMPLETENESS_PASS",
        "gates": gates,
        "counts": {
            "old_files": old["counts"].get("files", 0),
            "v2_files": full["counts"]["files"],
            "historical_mail_files": full["verification"]["historical_mail_files"],
            "content_objects": full["counts"]["content_objects"],
            "occurrences": full["counts"]["occurrences"],
            "tasks": full["counts"]["tasks"],
            "fts_documents": full["counts"]["fts_documents"],
            "attachment_occurrences": relationship["attachment_occurrences"],
            "mail_attachment_relations": mail_total,
            "multiparent_attachments": relationship["multiparent_attachments"],
            "explicit_relations": explicit_total,
            "business_timeline": business_total,
            "capture_timeline": capture_total,
            "source_gaps": len(gaps),
        },
        "components": {
            "legacy_v1_verification_sha256": old["verification_sha256"],
            "full_extract_v2_verification_sha256": full["verification_sha256"],
            "relationship_v2_verification_sha256": relationship["verification_sha256"],
            "ui_gap_manifest_sha256": ui["sha256"],
        },
        "detail_limits": {
            "max_rows_per_sheet": max_rows,
            "mail_attachment_total": mail_total,
            "explicit_relation_total": explicit_total,
            "business_timeline_total": business_total,
            "capture_timeline_total": capture_total,
        },
        "coverage_rows": coverage,
        "mail_attachment_rows": mail_rows,
        "explicit_relation_rows": explicit_rows,
        "business_timeline_rows": business_rows,
        "capture_timeline_rows": capture_rows,
        "source_gap_rows": gap_rows,
        "diff_rows": [[row["metric"], row["old"], row["v2"], row["delta"]] for row in diff["rows"]],
    }


def create_authority(path: Path, company_id: str, components: list[dict[str, Any]], gaps: list[dict[str, str]], diff: dict[str, Any], payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    db = sqlite3.connect(temporary)
    try:
        db.executescript(
            """
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE component(name TEXT PRIMARY KEY,schema_name TEXT NOT NULL,status TEXT NOT NULL,path TEXT NOT NULL,verification_sha256 TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE source_gap(gap_id TEXT PRIMARY KEY,source_scope TEXT NOT NULL,role TEXT NOT NULL,error_code TEXT NOT NULL,http_status TEXT,content_sha256 TEXT,source_identity_sha256 TEXT,owner_sha256 TEXT) WITHOUT ROWID;
            CREATE TABLE old_new_diff(metric TEXT PRIMARY KEY,old_value INTEGER NOT NULL,v2_value INTEGER NOT NULL,delta INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TABLE gate(name TEXT PRIMARY KEY,passed INTEGER NOT NULL CHECK(passed IN (0,1))) WITHOUT ROWID;
            CREATE TABLE delivery_file(path TEXT PRIMARY KEY,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL) WITHOUT ROWID;
            """
        )
        meta = {
            "schema": AUTHORITY_SCHEMA,
            "company_id": company_id,
            "prepared_at_utc": utc_now(),
            "accessible_data_status": payload["accessible_data_status"],
            "absolute_completeness_status": payload["absolute_completeness_status"],
        }
        db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", sorted(meta.items()))
        db.executemany("INSERT INTO component(name,schema_name,status,path,verification_sha256) VALUES(:name,:schema_name,:status,:path,:verification_sha256)", components)
        for index, row in enumerate(gaps, 1):
            values = dict(row)
            values["gap_id"] = values.get("gap_id") or hashlib.sha256(f"gap-{index}".encode()).hexdigest()
            db.execute(
                "INSERT INTO source_gap(gap_id,source_scope,role,error_code,http_status,content_sha256,source_identity_sha256,owner_sha256) VALUES(?,?,?,?,?,?,?,?)",
                (values["gap_id"], values.get("source_scope", ""), values.get("role", ""), values.get("error_code", ""), values.get("http_status"), values.get("content_sha256"), values.get("source_identity_sha256"), values.get("owner_sha256")),
            )
        db.executemany("INSERT INTO old_new_diff(metric,old_value,v2_value,delta) VALUES(:metric,:old,:v2,:delta)", diff["rows"])
        db.executemany("INSERT INTO gate(name,passed) VALUES(?,?)", [(key, int(value)) for key, value in payload["gates"].items()])
        db.commit()
        require(str(db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "AUTHORITY_DB_INTEGRITY_FAILED")
    finally:
        db.close()
        gc.collect()
    os.replace(temporary, path)


def write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


# EN: Validate identity/scope and create resumable work without changing originals.
# 中文：验证身份和范围，创建可续跑任务且不修改原件。
def prepare(args: argparse.Namespace) -> dict[str, Any]:
    case_root = args.case_root.resolve()
    load_identity(case_root, args.company_id)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    require(SAFE_ID_RE.fullmatch(run_id) is not None, "RUN_ID_INVALID")
    parent = (args.output_parent or (case_root / "deliveries" / "full_unredacted_local")).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / f"completion_v2_{run_id}"
    stage = parent / f".completion_v2_{run_id}.stage"
    require(not final.exists(), "FINAL_OUTPUT_EXISTS")
    require(not stage.exists(), "STAGE_OUTPUT_EXISTS")

    old = load_old_package(args.old_package, args.expected_old_files)
    full = load_full_extract(args.full_extract_v2, case_root, args.company_id, args.expected_historical_mail_files, args.expected_min_evidence_files)
    relationship = load_relationship(args.relationship_v2, args.company_id, args.expected_attachment_occurrences, args.expected_mail_attachment_relations, args.expected_multiparent_attachments)
    ui = load_ui_manifest(args.ui_gap_manifest, case_root, args.company_id)
    gaps = source_gap_rows(full["root"])
    require(len(gaps) == parse_int(full["verification"].get("source_gaps"), "FULL_EXTRACT_SOURCE_GAP_COUNT_INVALID"), "FULL_EXTRACT_SOURCE_GAP_COUNT_MISMATCH")
    diff = build_diff(old, full, relationship, gaps)
    payload = build_payload(args.company_id, old, full, relationship, ui, gaps, diff, args.max_excel_rows)

    stage.mkdir()
    success = False
    try:
        mirror_stats = {
            "legacy_v1": mirror_tree(old["root"], stage / "legacy_v1"),
            "full_extract_v2": mirror_tree(full["root"], stage / "full_extract_v2"),
            "relationship_v2": mirror_tree(relationship["root"], stage / "relationship_v2"),
            "ui_gap_session": mirror_tree(ui["session_root"], stage / "ui_gap" / "session"),
        }
        hardlink_or_copy(ui["path"], stage / "ui_gap" / "ui_gap_revisit_manifest.json")
        evidence_linked = evidence_copied = 0
        for row in full["source_rows"]:
            source = resolve_inside(case_root, row["source_rel"], "FULL_EXTRACT_SOURCE_ESCAPE")
            target = stage / "evidence" / "original" / Path(str(row["source_rel"]))
            if target.exists():
                require(target.stat().st_size == int(row["bytes"]) and sha256_file(target) == normalize_sha(row["sha256"], "FULL_EXTRACT_SOURCE_SHA_INVALID"), "EVIDENCE_TARGET_COLLISION")
                continue
            mode = hardlink_or_copy(source, target)
            evidence_linked += mode == "hardlink"
            evidence_copied += mode == "copy"
        mirror_stats["evidence_original"] = {"files": len(full["source_rows"]), "hardlinks": evidence_linked, "copies": evidence_copied}

        atomic_json(stage / "source_gaps.json", {"schema": SCHEMA, "count": len(gaps), "rows": gaps})
        write_csv(stage / "source_gaps.csv", ["gap_id", "source_scope", "role", "error_code", "http_status", "content_sha256", "source_identity_sha256", "owner_sha256"], [[row.get(key, "") for key in ("gap_id", "source_scope", "role", "error_code", "http_status", "content_sha256", "source_identity_sha256", "owner_sha256")] for row in gaps])
        atomic_json(stage / "old_vs_v2_diff.json", diff)
        write_csv(stage / "old_vs_v2_diff.csv", ["metric", "old", "v2", "delta"], [[row["metric"], row["old"], row["v2"], row["delta"]] for row in diff["rows"]])
        atomic_json(stage / "delivery_payload.json", payload)
        components = [
            {"name": "legacy_v1", "schema_name": OLD_PACKAGE_SCHEMA, "status": "PASS", "path": "legacy_v1", "verification_sha256": old["verification_sha256"]},
            {"name": "full_extract_v2", "schema_name": str(full["verification"].get("schema") or "schema-discovered"), "status": full["verification"]["status"], "path": "full_extract_v2", "verification_sha256": full["verification_sha256"]},
            {"name": "relationship_v2", "schema_name": RELATIONSHIP_SCHEMA, "status": "PASS", "path": "relationship_v2", "verification_sha256": relationship["verification_sha256"]},
            {"name": "ui_gap", "schema_name": UI_SCHEMA, "status": "PASS", "path": "ui_gap", "verification_sha256": ui["sha256"]},
        ]
        create_authority(stage / "completion_v2_authority.sqlite3", args.company_id, components, gaps, diff, payload)
        manifest = {
            "schema": SCHEMA,
            "status": "STAGED",
            "company_id": args.company_id,
            "run_id": run_id,
            "prepared_at_utc": utc_now(),
            "stage": str(stage),
            "final": str(final),
            "mirror": mirror_stats,
            "gates": payload["gates"],
            "counts": payload["counts"],
            "source_gaps": len(gaps),
            "privacy": {"local_only": True, "stdout_customer_body": False},
        }
        atomic_json(stage / "prepare_manifest.json", manifest)
        success = True
        return {"status": "STAGED", "stage": str(stage), "final": str(final), "run_id": run_id, "counts": payload["counts"], "source_gaps": len(gaps)}
    finally:
        if not success and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def validate_zip(path: Path, code: str) -> None:
    require(path.is_file() and path.stat().st_size > 0, f"{code}_MISSING")
    try:
        with zipfile.ZipFile(path) as archive:
            require(archive.testzip() is None, f"{code}_ZIP_CORRUPT")
    except zipfile.BadZipFile as exc:
        raise DeliveryError(f"{code}_ZIP_INVALID") from exc


# EN: Require independently bound workbook/report QA receipts.
# 中文：要求工作簿与报告验收回执独立绑定。
def validate_presentations(stage: Path, company_id: str) -> dict[str, Any]:
    workbook = stage / f"客户{company_id}_全案例递归补全目录_v2.xlsx"
    docx = stage / f"客户{company_id}_全案例递归补全研究报告_v2.docx"
    pdf = stage / f"客户{company_id}_全案例递归补全研究报告_v2.pdf"
    workbook_receipt = read_json(stage / "qa" / "workbook_v2_verification.json", "WORKBOOK_RECEIPT")
    report_receipt = read_json(stage / "qa" / "report_v2_verification.json", "REPORT_RECEIPT")
    validate_zip(workbook, "WORKBOOK")
    validate_zip(docx, "DOCX")
    require(pdf.is_file() and pdf.stat().st_size > 8 and pdf.read_bytes()[:5] == b"%PDF-", "PDF_INVALID")
    require(workbook_receipt.get("status") == "PASS", "WORKBOOK_NOT_PASS")
    require(parse_int(workbook_receipt.get("formula_errors"), "WORKBOOK_FORMULA_ERRORS_INVALID") == 0, "WORKBOOK_FORMULA_ERRORS")
    require(workbook_receipt.get("all_sheets_rendered") is True, "WORKBOOK_RENDER_NOT_PASS")
    require(workbook_receipt.get("all_filters_present") is True and workbook_receipt.get("all_freeze_panes_present") is True, "WORKBOOK_OOXML_NOT_PASS")
    output = workbook_receipt.get("output")
    require(isinstance(output, dict) and normalize_sha(output.get("sha256"), "WORKBOOK_SHA_INVALID") == sha256_file(workbook), "WORKBOOK_HASH_MISMATCH")
    # Structural/SVG checks cannot certify native opening or human readability.
    # 结构与 SVG 检查不能替代原生只读打开和人工可读性验收。
    native_qa = validate_workbook_native_qa(stage, company_id, workbook)
    require(report_receipt.get("status") == "PASS" and report_receipt.get("visual_inspection_pass") is True, "REPORT_NOT_PASS")
    require(report_receipt.get("word_com_export") is True and report_receipt.get("opened_read_only") is True, "REPORT_WORD_COM_NOT_PASS")
    require(normalize_sha(report_receipt.get("docx_sha256"), "REPORT_DOCX_SHA_INVALID") == sha256_file(docx), "REPORT_DOCX_HASH_MISMATCH")
    require(normalize_sha(report_receipt.get("pdf_sha256"), "REPORT_PDF_SHA_INVALID") == sha256_file(pdf), "REPORT_PDF_HASH_MISMATCH")
    require(parse_int(report_receipt.get("docx_render_pages"), "REPORT_DOCX_PAGES_INVALID") > 0, "REPORT_DOCX_RENDER_EMPTY")
    require(parse_int(report_receipt.get("pdf_render_pages"), "REPORT_PDF_PAGES_INVALID") > 0, "REPORT_PDF_RENDER_EMPTY")
    return {"workbook": workbook, "docx": docx, "pdf": pdf, "workbook_receipt": workbook_receipt, "workbook_native_qa": native_qa, "report_receipt": report_receipt}


def validate_workbook_native_qa(stage: Path, company_id: str, workbook: Path) -> dict[str, Any]:
    """Require a separate human/native receipt bound to the exact final bytes.

    独立人工/原生回执必须绑定最终工作簿及结构回执的真实字节。
    本函数只验证契约；不能自动证明人的检查真实发生。
    """
    qa = read_json(stage / "qa" / "workbook_native_qa.json", "WORKBOOK_NATIVE_QA")
    require(qa.get("schema") == "evidence_trail.workbook_native_qa.v1", "WORKBOOK_NATIVE_QA_SCHEMA_INVALID")
    require(str(qa.get("company_id") or "") == company_id, "WORKBOOK_NATIVE_QA_IDENTITY_MISMATCH")
    require(qa.get("status") == "PASS", "WORKBOOK_NATIVE_QA_NOT_PASS")
    require(qa.get("opened_read_only") is True and qa.get("repair_prompt_seen") is False, "WORKBOOK_NATIVE_OPEN_NOT_PASS")
    require(qa.get("all_sheets_visual_inspection_pass") is True, "WORKBOOK_HUMAN_VISUAL_NOT_PASS")
    require(parse_int(qa.get("formula_errors"), "WORKBOOK_NATIVE_FORMULA_ERRORS_INVALID") == 0, "WORKBOOK_NATIVE_FORMULA_ERRORS")
    require(normalize_sha(qa.get("workbook_sha256"), "WORKBOOK_NATIVE_SHA_INVALID") == sha256_file(workbook), "WORKBOOK_NATIVE_HASH_MISMATCH")
    structural = stage / "qa" / "workbook_v2_verification.json"
    require(normalize_sha(qa.get("structural_receipt_sha256"), "WORKBOOK_NATIVE_RECEIPT_SHA_INVALID") == sha256_file(structural), "WORKBOOK_NATIVE_RECEIPT_HASH_MISMATCH")
    return qa


def hash_paths(root: Path, paths: Iterable[Path]) -> dict[str, str]:
    ordered = sorted({path.resolve() for path in paths}, key=lambda item: relative(item, root).casefold())
    with ThreadPoolExecutor(max_workers=8) as pool:
        digests = list(pool.map(sha256_file, ordered))
    return {relative(path, root): digest for path, digest in zip(ordered, digests, strict=True)}


# EN: Verify presentation receipts and checksums before immutable atomic publication.
# 中文：验证展示验收回执与哈希后原子发布不可覆盖交付。
def finalize(args: argparse.Namespace) -> dict[str, Any]:
    stage = args.stage.resolve()
    require(stage.is_dir() and stage.name.startswith(".completion_v2_") and stage.name.endswith(".stage"), "STAGE_INVALID")
    manifest = read_json(stage / "prepare_manifest.json", "PREPARE_MANIFEST")
    require(manifest.get("schema") == SCHEMA and manifest.get("status") == "STAGED", "PREPARE_MANIFEST_INVALID")
    company_id = str(manifest.get("company_id") or "")
    presentation = validate_presentations(stage, company_id)
    payload_path = stage / "delivery_payload.json"
    payload = read_json(payload_path, "DELIVERY_PAYLOAD")
    require(payload.get("schema") == PAYLOAD_SCHEMA and payload.get("company_id") == company_id, "DELIVERY_PAYLOAD_INVALID")
    gates = payload.get("gates")
    require(isinstance(gates, dict) and all(gates.get(name) is True for name in ("PAGE_GAP_RECOVERY_PASS", "MAIL_ATTACHMENT_SCOPE_PASS", "CONTENT_RECURSION_PASS_WITH_SOURCE_GAPS", "RELATIONSHIP_V2_PASS", "TIMELINE_V2_PASS")), "DELIVERY_SOURCE_GATES_NOT_PASS")
    gates["DELIVERY_V2_PASS"] = True
    payload["gates"] = gates
    atomic_json(payload_path, payload)

    authority_path = stage / "completion_v2_authority.sqlite3"
    db = sqlite3.connect(authority_path)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("UPDATE gate SET passed=1 WHERE name='DELIVERY_V2_PASS'")
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('finalized_at_utc',?)", (utc_now(),))
        db.commit()
        require(str(db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "AUTHORITY_DB_INTEGRITY_FAILED")
    finally:
        db.close()
        gc.collect()

    transient = {stage / "checksums.sha256", stage / "verification.json"}
    files = [path for path in stage.rglob("*") if path.is_file() and path not in transient and not path.name.endswith(("-wal", "-shm", ".part"))]
    initial_hashes = hash_paths(stage, [path for path in files if path.resolve() != authority_path.resolve()])
    db = sqlite3.connect(authority_path)
    try:
        db.execute("DELETE FROM delivery_file")
        db.executemany("INSERT INTO delivery_file(path,bytes,sha256) VALUES(?,?,?)", [(rel, (stage / rel).stat().st_size, digest) for rel, digest in sorted(initial_hashes.items())])
        db.commit()
        db.execute("VACUUM")
        require(str(db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "AUTHORITY_DB_INTEGRITY_FAILED")
    finally:
        db.close()
        gc.collect()

    files = [path for path in stage.rglob("*") if path.is_file() and path not in transient and not path.name.endswith(("-wal", "-shm", ".part"))]
    hashes = hash_paths(stage, files)
    checksum_text = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(hashes.items(), key=lambda item: item[0].casefold()))
    atomic_bytes(stage / "checksums.sha256", checksum_text.encode("utf-8"))
    gaps = read_json(stage / "source_gaps.json", "SOURCE_GAPS")
    source_gap_count = parse_int(gaps.get("count"), "SOURCE_GAP_COUNT_INVALID")
    final = Path(str(manifest.get("final") or "")).resolve()
    require(final.parent == stage.parent and final.name == stage.name.removeprefix(".").removesuffix(".stage"), "FINAL_PATH_INVALID")
    require(not final.exists(), "FINAL_OUTPUT_EXISTS")
    verification = {
        "schema": SCHEMA,
        "status": "ACCESSIBLE_DATA_PASS",
        "absolute_completeness_status": "ABSOLUTE_COMPLETENESS_SOURCE_GAPS" if source_gap_count else "ABSOLUTE_COMPLETENESS_PASS",
        "company_id": company_id,
        "run_id": manifest["run_id"],
        "finalized_at_utc": utc_now(),
        "gates": gates,
        "counts": payload["counts"],
        "source_gaps": source_gap_count,
        "checksum_entries": len(hashes),
        "checksums_sha256": sha256_file(stage / "checksums.sha256"),
        "artifacts": {
            "workbook": {"path": relative(presentation["workbook"], stage), "bytes": presentation["workbook"].stat().st_size, "sha256": sha256_file(presentation["workbook"])},
            "docx": {"path": relative(presentation["docx"], stage), "bytes": presentation["docx"].stat().st_size, "sha256": sha256_file(presentation["docx"])},
            "pdf": {"path": relative(presentation["pdf"], stage), "bytes": presentation["pdf"].stat().st_size, "sha256": sha256_file(presentation["pdf"])},
        },
    }
    atomic_json(stage / "verification.json", verification)
    os.replace(stage, final)
    return {"status": verification["status"], "absolute_completeness_status": verification["absolute_completeness_status"], "output": str(final), "counts": payload["counts"], "source_gaps": source_gap_count, "verification_sha256": sha256_file(final / "verification.json")}


# EN: Recheck the published checksum seal and authority database.
# 中文：复核已发布的哈希封存与权威数据库。
def verify_final(root: Path) -> dict[str, Any]:
    root = root.resolve()
    verification = read_json(root / "verification.json", "FINAL_VERIFICATION")
    require(verification.get("schema") == SCHEMA and verification.get("status") == "ACCESSIBLE_DATA_PASS", "FINAL_NOT_PASS")
    checksums = read_checksums(root / "checksums.sha256")
    verify_checksum_map(root, checksums)
    require(sha256_file(root / "checksums.sha256") == normalize_sha(verification.get("checksums_sha256"), "FINAL_CHECKSUM_SHA_INVALID"), "FINAL_CHECKSUM_SHA_MISMATCH")
    db = open_readonly(root / "completion_v2_authority.sqlite3")
    try:
        require(str(db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "FINAL_AUTHORITY_INTEGRITY_FAILED")
        require(not list(db.execute("PRAGMA foreign_key_check")), "FINAL_AUTHORITY_FOREIGN_KEY_FAILED")
        require(int(db.execute("SELECT COUNT(*) FROM gate WHERE passed=0").fetchone()[0]) == 0, "FINAL_GATE_NOT_PASS")
    finally:
        db.close()
        gc.collect()
    return {"status": verification["status"], "absolute_completeness_status": verification["absolute_completeness_status"], "checksum_entries": len(checksums), "verification_sha256": sha256_file(root / "verification.json")}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Prepare/finalize the immutable completion-v2 delivery")
    commands = root.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--case-root", type=Path, required=True)
    prepare_parser.add_argument("--company-id", required=True)
    prepare_parser.add_argument("--old-package", type=Path, required=True)
    prepare_parser.add_argument("--full-extract-v2", type=Path, required=True)
    prepare_parser.add_argument("--relationship-v2", type=Path, required=True)
    prepare_parser.add_argument("--ui-gap-manifest", type=Path, required=True)
    prepare_parser.add_argument("--output-parent", type=Path)
    prepare_parser.add_argument("--run-id")
    prepare_parser.add_argument("--expected-old-files", type=int, required=True)
    prepare_parser.add_argument("--expected-historical-mail-files", type=int, required=True)
    prepare_parser.add_argument("--expected-min-evidence-files", type=int, required=True)
    prepare_parser.add_argument("--expected-attachment-occurrences", type=int, required=True)
    prepare_parser.add_argument("--expected-mail-attachment-relations", type=int, required=True)
    prepare_parser.add_argument("--expected-multiparent-attachments", type=int, required=True)
    prepare_parser.add_argument("--max-excel-rows", type=int, default=DEFAULT_MAX_EXCEL_ROWS)
    finalize_parser = commands.add_parser("finalize")
    finalize_parser.add_argument("--stage", type=Path, required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--delivery", type=Path, required=True)
    return root


def quiet(value: dict[str, Any]) -> dict[str, Any]:
    keys = ("status", "absolute_completeness_status", "stage", "final", "output", "run_id", "counts", "source_gaps", "verification_sha256", "checksum_entries")
    return {key: value[key] for key in keys if key in value}


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            for key in (
                "expected_old_files", "expected_historical_mail_files", "expected_min_evidence_files",
                "expected_attachment_occurrences", "expected_mail_attachment_relations", "expected_multiparent_attachments",
            ):
                require(getattr(args, key) >= 0, "EXPECTED_COUNT_INVALID")
            require(1 <= args.max_excel_rows <= 500_000, "MAX_EXCEL_ROWS_INVALID")
            result = prepare(args)
        elif args.command == "finalize":
            result = finalize(args)
        else:
            result = verify_final(args.delivery)
    except DeliveryError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "FAILED", "error_code": str(exc)}, separators=(",", ":")), flush=True)
        return 2
    except Exception:
        print(json.dumps({"schema": SCHEMA, "status": "FAILED", "error_code": "UNEXPECTED_ERROR"}, separators=(",", ":")), flush=True)
        return 3
    print(json.dumps({"schema": SCHEMA, **quiet(result)}, ensure_ascii=True, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
