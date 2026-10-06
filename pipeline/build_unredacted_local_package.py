# EN: Build local SQLite, FTS, Parquet and GraphML packages without publishing source content.
# 中文：构建本地 SQLite、FTS、Parquet、GraphML 数据包，不发布源内容。
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence
from xml.sax.saxutils import escape, quoteattr


SCHEMA = "okki.single_customer.unredacted_local_package.v1"
FULL_EXTRACT_SCHEMA = "okki.single_customer_full_extract.v1"
MANUAL_MANIFEST_SCHEMA = "okki.trade.manual_capture_manifest.v1"
MANUAL_RECEIPT_SCHEMA = "okki.trade.manual_capture_receipt.v1"
MIN_SQLITE = (3, 51, 3)
SQLITE_WAL_PATCHED_BACKPORTS = ((3, 44, 6), (3, 50, 7))
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PARQUET_TABLES = (
    "nodes",
    "relation_occurrence",
    "relation_fact",
    "files",
    "tasks",
    "coverage",
    "manual_trade_artifacts",
    "text_documents",
)


class PackageError(RuntimeError):
    pass


class DependencyMissing(PackageError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PackageError(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def canonical_json_bytes(value: Any) -> bytes:
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
    atomic_bytes(path, canonical_json_bytes(value))


def read_json(path: Path, code: str) -> dict[str, Any]:
    require(path.is_file(), f"{code}_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"{code}_INVALID") from exc
    require(isinstance(value, dict), f"{code}_INVALID")
    return value


def parse_nonnegative_int(value: Any, code: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PackageError(code) from exc
    require(parsed >= 0, code)
    return parsed


def normalize_sha(value: Any, code: str) -> str:
    text = str(value or "")
    require(SHA_RE.fullmatch(text) is not None, code)
    return text.upper()


def safe_relative_path(value: Any, code: str) -> str:
    text = str(value or "").replace("\\", "/")
    pure = PurePosixPath(text)
    require(text != "" and not pure.is_absolute() and ".." not in pure.parts, code)
    return pure.as_posix()


def resolve_inside(root: Path, value: Path | str, code: str) -> Path:
    root = root.resolve()
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    require(resolved == root or root in resolved.parents, code)
    return resolved


def relative_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def ensure_sqlite_version(version: Sequence[int] | None = None) -> None:
    """Require a fixed WAL branch and actual SQLite features. 中文：要求修复 WAL 的分支及实际能力。"""
    actual = tuple(version or sqlite3.sqlite_version_info)
    # EN: This is a public corruption fix, not a private runtime requirement.
    # SQLite documents patched backports: https://sqlite.org/wal.html#walresetbug
    # 中文：此门槛对应官方数据库损坏修复，并非私有运行时要求；允许官方回移修复版本。
    patched = actual >= MIN_SQLITE or any(actual[:2] == branch[:2] and actual >= branch for branch in SQLITE_WAL_PATCHED_BACKPORTS)
    require(patched, "SQLITE_VERSION_TOO_OLD")
    ensure_sqlite_capabilities()


def ensure_sqlite_capabilities() -> None:
    """Probe only a temporary synthetic database. 中文：仅使用临时合成数据库探测能力。"""
    with tempfile.TemporaryDirectory(prefix="crm_sqlite_preflight_") as temporary:
        database = sqlite3.connect(Path(temporary) / "probe.sqlite3")
        backup = sqlite3.connect(":memory:")
        try:
            require(database.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal", "SQLITE_WAL_UNAVAILABLE")
            database.execute("PRAGMA foreign_keys=ON")
            require(database.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "SQLITE_FOREIGN_KEYS_UNAVAILABLE")
            database.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            database.execute("CREATE TABLE child(id INTEGER REFERENCES parent(id))")
            try:
                database.execute("INSERT INTO child VALUES(1)")
            except sqlite3.IntegrityError:
                pass
            else:
                raise PackageError("SQLITE_FOREIGN_KEYS_NOT_ENFORCED")
            database.execute("CREATE VIRTUAL TABLE full_text USING fts5(text)")
            database.execute("INSERT INTO full_text VALUES('synthetic probe')")
            require(database.execute("SELECT COUNT(*) FROM full_text WHERE full_text MATCH 'synthetic'").fetchone()[0] == 1, "SQLITE_FTS5_UNAVAILABLE")
            database.execute("INSERT INTO parent VALUES(1) ON CONFLICT(id) DO UPDATE SET id=excluded.id")
            require(database.execute("SELECT ROW_NUMBER() OVER(ORDER BY id) FROM parent").fetchone()[0] == 1, "SQLITE_WINDOW_FUNCTIONS_UNAVAILABLE")
            database.commit()
            database.backup(backup)
            require(backup.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 1, "SQLITE_BACKUP_UNAVAILABLE")
            require(database.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLITE_PREFLIGHT_INTEGRITY_FAILED")
        except sqlite3.Error as error:
            raise PackageError("SQLITE_REQUIRED_CAPABILITY_UNAVAILABLE") from error
        finally:
            backup.close()
            database.close()


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def load_case_identity(case_root: Path, company_id: str) -> dict[str, Any]:
    require(SAFE_ID_RE.fullmatch(company_id) is not None, "COMPANY_ID_INVALID")
    require(case_root.is_dir(), "CASE_ROOT_MISSING")
    identity = read_json(case_root / "case_identity.json", "CASE_IDENTITY")
    require(str(identity.get("company_id") or "") == company_id, "CASE_IDENTITY_MISMATCH")
    require(
        case_root.name == f"company_{company_id}" or case_root.name.endswith(f"_{company_id}"),
        "CASE_DIRECTORY_IDENTITY_MISMATCH",
    )
    return identity


def load_auto7(case_root: Path, company_id: str) -> dict[str, Any]:
    state_path = case_root / "work" / "capture_state.json"
    state = read_json(state_path, "CAPTURE_STATE")
    require(state.get("running") is False, "AUTO7_CAPTURE_STILL_RUNNING")
    require(str(state.get("companyId") or "") == company_id, "CAPTURE_STATE_COMPANY_MISMATCH")

    scope_path = case_root / "manifests" / "processing_scope_latest.json"
    scope = read_json(scope_path, "PROCESSING_SCOPE")
    require(scope.get("schema") == 1, "PROCESSING_SCOPE_SCHEMA_INVALID")
    require(str(scope.get("capture_status") or "").casefold() == "pass", "AUTO7_NOT_PASS")
    require(str(scope.get("company_id") or "") == company_id, "PROCESSING_SCOPE_COMPANY_MISMATCH")
    session = str(scope.get("default_session_id") or "")
    require(SAFE_ID_RE.fullmatch(session) is not None, "PROCESSING_SCOPE_SESSION_INVALID")
    object_filter = scope.get("default_object_filter")
    require(
        isinstance(object_filter, dict)
        and object_filter.get("field") == "session_id"
        and str(object_filter.get("equals") or "") == session,
        "PROCESSING_SCOPE_FILTER_INVALID",
    )
    object_manifest_rel = safe_relative_path(scope.get("object_manifest"), "PROCESSING_SCOPE_OBJECT_MANIFEST_INVALID")
    object_manifest_path = resolve_inside(case_root, object_manifest_rel, "PROCESSING_SCOPE_OBJECT_MANIFEST_ESCAPE")
    require(object_manifest_path.is_file(), "PROCESSING_SCOPE_OBJECT_MANIFEST_MISSING")
    terminal_path = resolve_inside(
        case_root,
        Path("raw") / "sessions" / session / "final_status.json",
        "AUTO7_TERMINAL_ESCAPE",
    )
    terminal = read_json(terminal_path, "AUTO7_TERMINAL")
    errors = terminal.get("errors")
    metrics = terminal.get("metrics")
    require(
        terminal.get("phase") == "complete"
        and isinstance(errors, list)
        and not errors
        and isinstance(metrics, dict)
        and metrics.get("reconciliation_failures") == 0,
        "AUTO7_TERMINAL_NOT_PASS",
    )
    return {
        "session": session,
        "scope_path": scope_path,
        "terminal_path": terminal_path,
        "object_manifest_path": object_manifest_path,
    }


def locate_manual_receipt(case_root: Path) -> Path:
    root = case_root / "evidence" / "manual_trade"
    require(root.is_dir(), "MANUAL_TRADE_ROOT_MISSING")
    candidates = sorted(
        root.glob("*/trade_manual_capture_receipt.json"),
        key=lambda item: (item.stat().st_mtime_ns, item.as_posix().casefold()),
    )
    require(bool(candidates), "MANUAL_TRADE_RECEIPT_MISSING")
    return candidates[-1].resolve()


def resolve_manifest_from_receipt(case_root: Path, receipt_path: Path, raw: Any) -> Path:
    candidate = Path(str(raw or ""))
    require(str(candidate) != "", "MANUAL_TRADE_MANIFEST_PATH_INVALID")
    if candidate.is_absolute():
        return resolve_inside(case_root, candidate, "MANUAL_TRADE_MANIFEST_ESCAPE")
    from_case = resolve_inside(case_root, candidate, "MANUAL_TRADE_MANIFEST_ESCAPE")
    if from_case.is_file():
        return from_case
    return resolve_inside(receipt_path.parent, candidate, "MANUAL_TRADE_MANIFEST_ESCAPE")


def load_manual_trade(case_root: Path, company_id: str) -> dict[str, Any]:
    receipt_path = locate_manual_receipt(case_root)
    receipt = read_json(receipt_path, "MANUAL_TRADE_RECEIPT")
    require(receipt.get("schema") == MANUAL_RECEIPT_SCHEMA, "MANUAL_TRADE_RECEIPT_SCHEMA_INVALID")
    require(str(receipt.get("company_id") or "") == company_id, "MANUAL_TRADE_RECEIPT_COMPANY_MISMATCH")
    require(str(receipt.get("status") or "") == "PASS", "MANUAL_TRADE_RECEIPT_NOT_PASS")
    session = str(receipt.get("session") or "")
    require(SAFE_ID_RE.fullmatch(session) is not None, "MANUAL_TRADE_SESSION_INVALID")
    manifest_path = resolve_manifest_from_receipt(case_root, receipt_path, receipt.get("manifest_path"))
    require(manifest_path.is_file(), "MANUAL_TRADE_MANIFEST_MISSING")
    require(
        manifest_path.stat().st_size
        == parse_nonnegative_int(receipt.get("manifest_bytes"), "MANUAL_TRADE_MANIFEST_BYTES_INVALID"),
        "MANUAL_TRADE_MANIFEST_BYTES_MISMATCH",
    )
    require(
        sha256_file(manifest_path)
        == normalize_sha(receipt.get("manifest_sha256"), "MANUAL_TRADE_MANIFEST_SHA_INVALID"),
        "MANUAL_TRADE_MANIFEST_SHA_MISMATCH",
    )
    manifest = read_json(manifest_path, "MANUAL_TRADE_MANIFEST")
    require(manifest.get("schema") == MANUAL_MANIFEST_SCHEMA, "MANUAL_TRADE_MANIFEST_SCHEMA_INVALID")
    require(str(manifest.get("company_id") or "") == company_id, "MANUAL_TRADE_MANIFEST_COMPANY_MISMATCH")
    require(str(manifest.get("status") or "") == "PASS", "MANUAL_TRADE_MANIFEST_NOT_PASS")
    if manifest.get("session") is not None:
        require(str(manifest.get("session") or "") == session, "MANUAL_TRADE_SESSION_MISMATCH")
    source_gaps = manifest.get("source_gaps")
    require(isinstance(source_gaps, list) and not source_gaps, "MANUAL_TRADE_SOURCE_GAPS_PRESENT")
    require(manifest.get("counts") == receipt.get("counts"), "MANUAL_TRADE_COUNTS_MISMATCH")
    counts = manifest.get("counts")
    artifacts = manifest.get("artifacts")
    require(isinstance(counts, dict) and isinstance(artifacts, list), "MANUAL_TRADE_MANIFEST_INVALID")
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    byte_total = 0
    for item in artifacts:
        require(isinstance(item, dict), "MANUAL_TRADE_ARTIFACT_INVALID")
        rel = safe_relative_path(item.get("relative_path"), "MANUAL_TRADE_ARTIFACT_PATH_INVALID")
        require(rel not in seen, "MANUAL_TRADE_ARTIFACT_DUPLICATE")
        seen.add(rel)
        path = resolve_inside(manifest_path.parent, rel, "MANUAL_TRADE_ARTIFACT_ESCAPE")
        size = parse_nonnegative_int(item.get("bytes"), "MANUAL_TRADE_ARTIFACT_BYTES_INVALID")
        digest = normalize_sha(item.get("sha256"), "MANUAL_TRADE_ARTIFACT_SHA_INVALID")
        require(path.is_file() and path.stat().st_size == size, "MANUAL_TRADE_ARTIFACT_SIZE_MISMATCH")
        byte_total += size
        resolved.append(
            {
                "session": session,
                "relative_path": rel,
                "case_relative_path": relative_posix(path, case_root),
                "path": path,
                "bytes": size,
                "sha256": digest,
                "record_json": json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            }
        )
    require(
        len(resolved) == parse_nonnegative_int(counts.get("artifact_files"), "MANUAL_TRADE_ARTIFACT_COUNT_INVALID"),
        "MANUAL_TRADE_ARTIFACT_COUNT_MISMATCH",
    )
    require(
        byte_total == parse_nonnegative_int(counts.get("artifact_bytes"), "MANUAL_TRADE_ARTIFACT_BYTES_TOTAL_INVALID"),
        "MANUAL_TRADE_ARTIFACT_BYTES_TOTAL_MISMATCH",
    )
    return {
        "session": session,
        "receipt_path": receipt_path,
        "manifest_path": manifest_path,
        "artifacts": resolved,
        "artifact_bytes": byte_total,
    }


def load_full_extract(case_root: Path, company_id: str, auto7: dict[str, Any]) -> dict[str, Any]:
    root = case_root / "derived" / "full_extract"
    require(root.is_dir(), "FULL_EXTRACT_MISSING")
    verification_path = root / "verification.json"
    verification = read_json(verification_path, "FULL_EXTRACT_VERIFICATION")
    require(verification.get("schema") == FULL_EXTRACT_SCHEMA, "FULL_EXTRACT_SCHEMA_INVALID")
    require(str(verification.get("company_id") or "") == company_id, "FULL_EXTRACT_COMPANY_MISMATCH")
    require(verification.get("status") == "PASS", "FULL_EXTRACT_NOT_PASS")
    require(verification.get("auto7_status") == "PASS", "FULL_EXTRACT_AUTO7_NOT_PASS")
    require(str(verification.get("automatic_session_id") or "") == auto7["session"], "FULL_EXTRACT_SESSION_MISMATCH")
    require(verification.get("manual_trade_status") == "PASS", "FULL_EXTRACT_MANUAL_TRADE_NOT_PASS")
    zero_fields = (
        "pending",
        "failed",
        "binding_mismatch",
        "evidence_scope_mismatch",
        "evidence_hash_mismatch",
        "missing_outputs",
        "bad_output_hash",
        "missing_indexes",
        "manual_source_gaps",
    )
    for field in zero_fields:
        require(parse_nonnegative_int(verification.get(field), f"FULL_EXTRACT_{field.upper()}_INVALID") == 0, f"FULL_EXTRACT_{field.upper()}_NONZERO")
    require(verification.get("sqlite_integrity") == "ok", "FULL_EXTRACT_STATE_INTEGRITY_NOT_PASS")
    require(verification.get("fts_integrity") == "ok", "FULL_EXTRACT_FTS_INTEGRITY_NOT_PASS")
    paths = {
        "verification": verification_path,
        "full_text": root / "full_text.sqlite3",
        "relationships": root / "relationship_graph.jsonl",
        "files": root / "file_inventory.csv",
        "tasks": root / "evidence_index.csv",
        "coverage": root / "coverage.csv",
    }
    for name, path in paths.items():
        require(path.is_file(), f"FULL_EXTRACT_{name.upper()}_MISSING")
    source = open_readonly(paths["full_text"])
    try:
        require(str(source.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "FULL_TEXT_SQLITE_INTEGRITY_FAILED")
        document_count = int(source.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        fts_count = int(source.execute("SELECT COUNT(*) FROM full_text").fetchone()[0])
    finally:
        source.close()
    expected_fts = parse_nonnegative_int(verification.get("fts_rows"), "FULL_EXTRACT_FTS_ROWS_INVALID")
    require(document_count == expected_fts and fts_count == expected_fts, "FULL_EXTRACT_FTS_ROWS_MISMATCH")
    return {
        "root": root,
        "verification": verification,
        "paths": paths,
        "document_count": document_count,
    }


def stream_csv(path: Path, required: set[str], code: str) -> Iterator[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            require(reader.fieldnames is not None and required.issubset(set(reader.fieldnames)), code)
            for row in reader:
                yield dict(row)
    except PackageError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PackageError(code) from exc


def _hash_job(job: tuple[str, int, str]) -> tuple[bool, bool]:
    raw_path, expected_size, expected_sha = job
    path = Path(raw_path)
    if not path.is_file() or path.stat().st_size != expected_size:
        return False, False
    return True, sha256_file(path) == expected_sha


def verify_hash_jobs(jobs: Iterable[tuple[Path, int, str]], workers: int, code: str) -> int:
    normalized = [(str(path), size, digest) for path, size, digest in jobs]
    if not normalized:
        return 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        for exists_and_size, digest_ok in executor.map(_hash_job, normalized):
            require(exists_and_size and digest_ok, code)
    return len(normalized)


def _parse_relation_batch(batch: list[tuple[int, str]]) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str, str, str, str, int]]]:
    nodes: list[tuple[str, str, str]] = []
    edges: list[tuple[str, str, str, str, str, str, int]] = []
    try:
        for line_number, line in batch:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError
            record_type = value.get("record_type")
            if record_type == "node":
                node_id = value.get("node_id")
                entity_type = value.get("entity_type")
                node_value = value.get("value")
                if not all(isinstance(item, str) and item != "" for item in (node_id, entity_type, node_value)):
                    raise ValueError
                nodes.append((node_id, entity_type, node_value))
            elif record_type == "edge":
                edge_id = value.get("edge_id")
                from_node = value.get("from")
                to_node = value.get("to")
                relation = value.get("relation")
                source_rel = value.get("source_rel")
                pointer = value.get("json_pointer")
                if not all(isinstance(item, str) and item != "" for item in (edge_id, from_node, to_node, relation, source_rel)) or not isinstance(pointer, str):
                    raise ValueError
                edges.append((edge_id, from_node, to_node, relation, source_rel, pointer, line_number))
            else:
                raise ValueError
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise PackageError("RELATION_RECORD_INVALID") from exc
    return nodes, edges


def _relation_batches(path: Path, batch_size: int) -> Iterator[list[tuple[int, str]]]:
    batch: list[tuple[int, str]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                batch.append((line_number, line))
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
    except (OSError, UnicodeError) as exc:
        raise PackageError("RELATION_GRAPH_READ_FAILED") from exc
    if batch:
        yield batch


def parsed_relation_batches(path: Path, workers: int, batch_size: int = 2048) -> Iterator[tuple[list[tuple[str, str, str]], list[tuple[str, str, str, str, str, str, int]]]]:
    batches = _relation_batches(path, batch_size)
    if workers <= 1:
        for batch in batches:
            yield _parse_relation_batch(batch)
        return
    in_flight: deque[Future[Any]] = deque()
    with ProcessPoolExecutor(max_workers=workers) as executor:
        try:
            for batch in batches:
                in_flight.append(executor.submit(_parse_relation_batch, batch))
                if len(in_flight) >= max(2, workers * 2):
                    yield in_flight.popleft().result()
            while in_flight:
                yield in_flight.popleft().result()
        except Exception as exc:
            for future in in_flight:
                future.cancel()
            if isinstance(exc, PackageError):
                raise
            raise PackageError("RELATION_PARSE_WORKER_FAILED") from exc


def create_package_tables(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE nodes(
          node_id TEXT PRIMARY KEY,
          entity_type TEXT NOT NULL,
          value TEXT NOT NULL,
          UNIQUE(entity_type,value)
        ) WITHOUT ROWID;
        CREATE TABLE relation_occurrence(
          occurrence_id INTEGER PRIMARY KEY,
          edge_id TEXT NOT NULL,
          from_node_id TEXT NOT NULL,
          to_node_id TEXT NOT NULL,
          relation TEXT NOT NULL,
          source_rel TEXT NOT NULL,
          json_pointer TEXT NOT NULL,
          source_line INTEGER NOT NULL
        );
        CREATE TABLE relation_fact(
          from_node_id TEXT NOT NULL,
          to_node_id TEXT NOT NULL,
          relation TEXT NOT NULL,
          evidence_count INTEGER NOT NULL,
          first_source_rel TEXT NOT NULL,
          last_source_rel TEXT NOT NULL,
          PRIMARY KEY(from_node_id,to_node_id,relation)
        ) WITHOUT ROWID;
        CREATE TABLE files(
          source_rel TEXT PRIMARY KEY,
          sha256 TEXT NOT NULL,
          bytes INTEGER NOT NULL,
          magic TEXT NOT NULL,
          category TEXT NOT NULL,
          mime TEXT NOT NULL,
          source_scope TEXT NOT NULL,
          active INTEGER NOT NULL,
          prepared_at TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE tasks(
          task_id TEXT PRIMARY KEY,
          source_rel TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          task_kind TEXT NOT NULL,
          state TEXT NOT NULL,
          output_rel TEXT NOT NULL,
          result_sha256 TEXT NOT NULL,
          attempts INTEGER NOT NULL,
          error_code TEXT NOT NULL,
          updated_at TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE coverage(
          coverage_id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          name TEXT NOT NULL,
          total INTEGER NOT NULL,
          completed INTEGER NOT NULL,
          pending INTEGER NOT NULL,
          failed INTEGER NOT NULL,
          bytes INTEGER
        );
        CREATE TABLE manual_trade_artifacts(
          artifact_id INTEGER PRIMARY KEY,
          session TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          case_relative_path TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          bytes INTEGER NOT NULL,
          record_json TEXT NOT NULL,
          UNIQUE(session,relative_path)
        );
        CREATE TABLE lineage(
          lineage_id INTEGER PRIMARY KEY,
          source_kind TEXT NOT NULL,
          source_path TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          source_bytes INTEGER NOT NULL,
          target_name TEXT NOT NULL,
          row_count INTEGER,
          recorded_at TEXT NOT NULL
        );
        """
    )


def copy_full_text_database(source_path: Path, target_path: Path) -> sqlite3.Connection:
    if target_path.exists():
        target_path.unlink()
    source = open_readonly(source_path)
    target = sqlite3.connect(target_path, timeout=60)
    target.row_factory = sqlite3.Row
    try:
        source.backup(target, pages=8192, sleep=0.01)
    finally:
        source.close()
    target.execute("PRAGMA journal_mode=WAL")
    target.execute("PRAGMA synchronous=FULL")
    target.execute("PRAGMA foreign_keys=ON")
    target.execute("PRAGMA busy_timeout=60000")
    target.execute("PRAGMA temp_store=MEMORY")
    target.execute("PRAGMA cache_size=-524288")
    create_package_tables(target)
    target.commit()
    return target


def import_files(db: sqlite3.Connection, case_root: Path, path: Path, expected: int, workers: int) -> tuple[int, list[tuple[Path, int, str]]]:
    required = {"source_rel", "sha256", "bytes", "magic", "category", "mime", "active", "prepared_at"}
    rows: list[tuple[Any, ...]] = []
    hash_jobs: list[tuple[Path, int, str]] = []
    count = 0
    db.execute("BEGIN IMMEDIATE")
    for row in stream_csv(path, required, "FULL_EXTRACT_FILE_INVENTORY_INVALID"):
        source_rel = safe_relative_path(row.get("source_rel"), "FULL_EXTRACT_FILE_PATH_INVALID")
        digest = normalize_sha(row.get("sha256"), "FULL_EXTRACT_FILE_SHA_INVALID")
        size = parse_nonnegative_int(row.get("bytes"), "FULL_EXTRACT_FILE_BYTES_INVALID")
        active = parse_nonnegative_int(row.get("active"), "FULL_EXTRACT_FILE_ACTIVE_INVALID")
        require(active == 1, "FULL_EXTRACT_INACTIVE_FILE_PRESENT")
        source_path = resolve_inside(case_root, source_rel, "FULL_EXTRACT_FILE_PATH_ESCAPE")
        hash_jobs.append((source_path, size, digest))
        rows.append((source_rel, digest, size, str(row.get("magic") or ""), str(row.get("category") or ""), str(row.get("mime") or ""), str(row.get("source_scope") or "automatic"), active, str(row.get("prepared_at") or "")))
        count += 1
        if len(rows) >= 5000:
            db.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", rows)
            rows.clear()
    if rows:
        db.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    require(count == expected, "FULL_EXTRACT_FILE_COUNT_MISMATCH")
    return verify_hash_jobs(hash_jobs, workers, "FULL_EXTRACT_SOURCE_HASH_MISMATCH"), hash_jobs


def import_tasks(db: sqlite3.Connection, path: Path, expected: int) -> int:
    required = {"task_id", "source_rel", "source_sha256", "task_kind", "state", "output_rel", "result_sha256", "attempts", "error_code", "updated_at"}
    rows: list[tuple[Any, ...]] = []
    count = 0
    db.execute("BEGIN IMMEDIATE")
    for row in stream_csv(path, required, "FULL_EXTRACT_TASK_INDEX_INVALID"):
        state = str(row.get("state") or "")
        require(state == "completed", "FULL_EXTRACT_TASK_NOT_COMPLETED")
        output_rel = str(row.get("output_rel") or "")
        result_sha = str(row.get("result_sha256") or "")
        require(output_rel != "" and SHA_RE.fullmatch(result_sha) is not None, "FULL_EXTRACT_TASK_OUTPUT_INVALID")
        rows.append((str(row.get("task_id") or ""), str(row.get("source_rel") or ""), normalize_sha(row.get("source_sha256"), "FULL_EXTRACT_TASK_SOURCE_SHA_INVALID"), str(row.get("task_kind") or ""), state, output_rel, result_sha.upper(), parse_nonnegative_int(row.get("attempts"), "FULL_EXTRACT_TASK_ATTEMPTS_INVALID"), str(row.get("error_code") or ""), str(row.get("updated_at") or "")))
        count += 1
        if len(rows) >= 5000:
            db.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            rows.clear()
    if rows:
        db.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    require(count == expected, "FULL_EXTRACT_TASK_COUNT_MISMATCH")
    return count


def import_coverage(db: sqlite3.Connection, path: Path) -> int:
    required = {"scope", "name", "total", "completed", "pending", "failed", "bytes"}
    rows: list[tuple[Any, ...]] = []
    count = 0
    for row in stream_csv(path, required, "FULL_EXTRACT_COVERAGE_INVALID"):
        total = parse_nonnegative_int(row.get("total"), "FULL_EXTRACT_COVERAGE_TOTAL_INVALID")
        completed = parse_nonnegative_int(row.get("completed"), "FULL_EXTRACT_COVERAGE_COMPLETED_INVALID")
        pending = parse_nonnegative_int(row.get("pending"), "FULL_EXTRACT_COVERAGE_PENDING_INVALID")
        failed = parse_nonnegative_int(row.get("failed"), "FULL_EXTRACT_COVERAGE_FAILED_INVALID")
        require(total == completed and pending == 0 and failed == 0, "FULL_EXTRACT_COVERAGE_NOT_PASS")
        raw_bytes = str(row.get("bytes") or "")
        byte_count = None if raw_bytes == "" else parse_nonnegative_int(raw_bytes, "FULL_EXTRACT_COVERAGE_BYTES_INVALID")
        rows.append((str(row.get("scope") or ""), str(row.get("name") or ""), total, completed, pending, failed, byte_count))
        count += 1
    db.execute("BEGIN IMMEDIATE")
    db.executemany("INSERT INTO coverage(scope,name,total,completed,pending,failed,bytes) VALUES(?,?,?,?,?,?,?)", rows)
    db.commit()
    return count


def import_manual_artifacts(db: sqlite3.Connection, manual: dict[str, Any], workers: int) -> int:
    jobs = [(row["path"], row["bytes"], row["sha256"]) for row in manual["artifacts"]]
    verify_hash_jobs(jobs, workers, "MANUAL_TRADE_ARTIFACT_HASH_MISMATCH")
    db.execute("BEGIN IMMEDIATE")
    db.executemany(
        "INSERT INTO manual_trade_artifacts(session,relative_path,case_relative_path,sha256,bytes,record_json) VALUES(?,?,?,?,?,?)",
        [
            (row["session"], row["relative_path"], row["case_relative_path"], row["sha256"], row["bytes"], row["record_json"])
            for row in manual["artifacts"]
        ],
    )
    db.commit()
    return len(jobs)


def import_relations(db: sqlite3.Connection, path: Path, expected_rows: int, workers: int) -> dict[str, int]:
    input_node_records = 0
    input_edge_records = 0
    db.execute("BEGIN IMMEDIATE")
    for node_rows, edge_rows in parsed_relation_batches(path, workers):
        if node_rows:
            try:
                db.executemany("INSERT INTO nodes(node_id,entity_type,value) VALUES(?,?,?)", node_rows)
            except sqlite3.IntegrityError as exc:
                raise PackageError("RELATION_NODE_DUPLICATE_OR_CONFLICT") from exc
            input_node_records += len(node_rows)
        if edge_rows:
            db.executemany(
                "INSERT INTO relation_occurrence(edge_id,from_node_id,to_node_id,relation,source_rel,json_pointer,source_line) VALUES(?,?,?,?,?,?,?)",
                edge_rows,
            )
            input_edge_records += len(edge_rows)
        if (input_node_records + input_edge_records) % 100000 < len(node_rows) + len(edge_rows):
            db.commit()
            db.execute("BEGIN IMMEDIATE")
    db.commit()
    require(input_node_records + input_edge_records == expected_rows, "RELATION_GRAPH_ROW_COUNT_MISMATCH")
    db.execute("CREATE INDEX idx_relation_occurrence_fact ON relation_occurrence(from_node_id,to_node_id,relation)")
    db.execute("CREATE INDEX idx_relation_occurrence_source ON relation_occurrence(source_rel)")
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        """INSERT INTO relation_fact(from_node_id,to_node_id,relation,evidence_count,first_source_rel,last_source_rel)
           SELECT from_node_id,to_node_id,relation,COUNT(*),MIN(source_rel),MAX(source_rel)
           FROM relation_occurrence
           GROUP BY from_node_id,to_node_id,relation"""
    )
    db.commit()
    node_count = int(db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    occurrence_count = int(db.execute("SELECT COUNT(*) FROM relation_occurrence").fetchone()[0])
    fact_count = int(db.execute("SELECT COUNT(*) FROM relation_fact").fetchone()[0])
    missing_endpoints = int(
        db.execute(
            """SELECT COUNT(*) FROM relation_occurrence o
               LEFT JOIN nodes a ON a.node_id=o.from_node_id
               LEFT JOIN nodes b ON b.node_id=o.to_node_id
               WHERE a.node_id IS NULL OR b.node_id IS NULL"""
        ).fetchone()[0]
    )
    require(missing_endpoints == 0, "RELATION_ENDPOINT_MISSING")
    require(node_count == input_node_records, "RELATION_NODE_COUNT_MISMATCH")
    require(occurrence_count == input_edge_records, "RELATION_OCCURRENCE_COUNT_MISMATCH")
    return {
        "node_records": input_node_records,
        "nodes": node_count,
        "relation_occurrence": occurrence_count,
        "relation_fact": fact_count,
        "missing_endpoints": missing_endpoints,
    }


def insert_meta_and_lineage(
    db: sqlite3.Connection,
    case_root: Path,
    company_id: str,
    auto7: dict[str, Any],
    full: dict[str, Any],
    manual: dict[str, Any],
    counts: dict[str, int],
    source_hashes: dict[Path, str],
    duckdb_version: str,
) -> None:
    built_at = utc_now()
    meta = {
        "schema": SCHEMA,
        "company_id": company_id,
        "built_at": built_at,
        "sqlite_version": sqlite3.sqlite_version,
        "duckdb_version": duckdb_version,
        "auto7_status": "PASS",
        "full_extract_status": "PASS",
        "manual_trade_status": "PASS",
        "auto7_session": auto7["session"],
        "manual_trade_session": manual["session"],
        **{f"count_{key}": str(value) for key, value in counts.items()},
    }
    lineage_specs = [
        ("auto7_processing_scope", auto7["scope_path"], "meta", 1),
        ("auto7_terminal", auto7["terminal_path"], "meta", 1),
        ("auto7_object_manifest", auto7["object_manifest_path"], "meta", None),
        ("full_extract_verification", full["paths"]["verification"], "meta", 1),
        ("full_text_database", full["paths"]["full_text"], "documents/full_text", full["document_count"]),
        ("relationship_graph", full["paths"]["relationships"], "nodes/relation_occurrence/relation_fact", counts["node_records"] + counts["relation_occurrence"]),
        ("file_inventory", full["paths"]["files"], "files", counts["files"]),
        ("task_index", full["paths"]["tasks"], "tasks", counts["tasks"]),
        ("coverage_index", full["paths"]["coverage"], "coverage", counts["coverage"]),
        ("manual_trade_manifest", manual["manifest_path"], "manual_trade_artifacts", counts["manual_trade_artifacts"]),
        ("manual_trade_receipt", manual["receipt_path"], "manual_trade_artifacts", counts["manual_trade_artifacts"]),
    ]
    db.execute("BEGIN IMMEDIATE")
    db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", sorted(meta.items()))
    db.executemany(
        "INSERT INTO lineage(source_kind,source_path,source_sha256,source_bytes,target_name,row_count,recorded_at) VALUES(?,?,?,?,?,?,?)",
        [
            (
                kind,
                relative_posix(path, case_root),
                source_hashes[path.resolve()],
                path.stat().st_size,
                target,
                row_count,
                built_at,
            )
            for kind, path, target, row_count in lineage_specs
        ],
    )
    db.commit()


def xml_text(value: Any) -> str:
    text = str(value)
    cleaned = "".join(
        character
        if character in "\t\n\r" or 0x20 <= ord(character) <= 0xD7FF or 0xE000 <= ord(character) <= 0xFFFD or 0x10000 <= ord(character) <= 0x10FFFF
        else "\uFFFD"
        for character in text
    )
    return escape(cleaned)


def write_graphml(db_path: Path, target: Path) -> dict[str, int]:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    node_count = edge_count = 0
    db = open_readonly(db_path)
    try:
        raw = temporary.open("wb")
        try:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as out:
                    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                    out.write('<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n')
                    out.write('<key id="d0" for="node" attr.name="entity_type" attr.type="string"/>\n')
                    out.write('<key id="d1" for="node" attr.name="value" attr.type="string"/>\n')
                    out.write('<key id="d2" for="edge" attr.name="relation" attr.type="string"/>\n')
                    out.write('<key id="d3" for="edge" attr.name="evidence_count" attr.type="long"/>\n')
                    out.write('<graph id="customer_relationships" edgedefault="directed">\n')
                    for row in db.execute("SELECT node_id,entity_type,value FROM nodes ORDER BY node_id"):
                        out.write(f'<node id={quoteattr(str(row["node_id"]))}><data key="d0">{xml_text(row["entity_type"])}</data><data key="d1">{xml_text(row["value"])}</data></node>\n')
                        node_count += 1
                    for index, row in enumerate(db.execute("SELECT from_node_id,to_node_id,relation,evidence_count FROM relation_fact ORDER BY from_node_id,to_node_id,relation"), 1):
                        out.write(f'<edge id="f{index}" source={quoteattr(str(row["from_node_id"]))} target={quoteattr(str(row["to_node_id"]))}><data key="d2">{xml_text(row["relation"])}</data><data key="d3">{int(row["evidence_count"])}</data></edge>\n')
                        edge_count += 1
                    out.write("</graph>\n</graphml>\n")
            raw.flush()
            os.fsync(raw.fileno())
        finally:
            raw.close()
    finally:
        db.close()
    os.replace(temporary, target)
    return {"nodes": node_count, "edges": edge_count}


DUCKDB_EXPORT_SPECS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "nodes": ("SELECT node_id,entity_type,value FROM nodes ORDER BY node_id", (("node_id", "VARCHAR"), ("entity_type", "VARCHAR"), ("value", "VARCHAR"))),
    "relation_occurrence": ("SELECT occurrence_id,edge_id,from_node_id,to_node_id,relation,source_rel,json_pointer,source_line FROM relation_occurrence ORDER BY occurrence_id", (("occurrence_id", "BIGINT"), ("edge_id", "VARCHAR"), ("from_node_id", "VARCHAR"), ("to_node_id", "VARCHAR"), ("relation", "VARCHAR"), ("source_rel", "VARCHAR"), ("json_pointer", "VARCHAR"), ("source_line", "BIGINT"))),
    "relation_fact": ("SELECT from_node_id,to_node_id,relation,evidence_count,first_source_rel,last_source_rel FROM relation_fact ORDER BY from_node_id,to_node_id,relation", (("from_node_id", "VARCHAR"), ("to_node_id", "VARCHAR"), ("relation", "VARCHAR"), ("evidence_count", "BIGINT"), ("first_source_rel", "VARCHAR"), ("last_source_rel", "VARCHAR"))),
    "files": ("SELECT source_rel,sha256,bytes,magic,category,mime,source_scope,active,prepared_at FROM files ORDER BY source_rel", (("source_rel", "VARCHAR"), ("sha256", "VARCHAR"), ("bytes", "BIGINT"), ("magic", "VARCHAR"), ("category", "VARCHAR"), ("mime", "VARCHAR"), ("source_scope", "VARCHAR"), ("active", "BIGINT"), ("prepared_at", "VARCHAR"))),
    "tasks": ("SELECT task_id,source_rel,source_sha256,task_kind,state,output_rel,result_sha256,attempts,error_code,updated_at FROM tasks ORDER BY task_id", (("task_id", "VARCHAR"), ("source_rel", "VARCHAR"), ("source_sha256", "VARCHAR"), ("task_kind", "VARCHAR"), ("state", "VARCHAR"), ("output_rel", "VARCHAR"), ("result_sha256", "VARCHAR"), ("attempts", "BIGINT"), ("error_code", "VARCHAR"), ("updated_at", "VARCHAR"))),
    "coverage": ("SELECT coverage_id,scope,name,total,completed,pending,failed,bytes FROM coverage ORDER BY coverage_id", (("coverage_id", "BIGINT"), ("scope", "VARCHAR"), ("name", "VARCHAR"), ("total", "BIGINT"), ("completed", "BIGINT"), ("pending", "BIGINT"), ("failed", "BIGINT"), ("bytes", "BIGINT"))),
    "manual_trade_artifacts": ("SELECT session,relative_path,case_relative_path,sha256,bytes,record_json FROM manual_trade_artifacts ORDER BY session,relative_path", (("session", "VARCHAR"), ("relative_path", "VARCHAR"), ("case_relative_path", "VARCHAR"), ("sha256", "VARCHAR"), ("bytes", "BIGINT"), ("record_json", "VARCHAR"))),
    "text_documents": ("SELECT d.id,d.source_rel,d.source_sha256,d.artifact_rel,d.task_kind,d.text_chars,f.text FROM documents d JOIN full_text f ON f.rowid=d.id ORDER BY d.id", (("id", "BIGINT"), ("source_rel", "VARCHAR"), ("source_sha256", "VARCHAR"), ("artifact_rel", "VARCHAR"), ("task_kind", "VARCHAR"), ("text_chars", "BIGINT"), ("text", "VARCHAR"))),
}


def duckdb_settings() -> tuple[int, str]:
    """Select conservative, configurable local resources. 中文：选择可配置的保守本机资源。"""
    threads = int(os.environ.get("CRM_DUCKDB_THREADS", min(8, os.cpu_count() or 1)))
    memory = os.environ.get("CRM_DUCKDB_MEMORY_LIMIT", "4GB")
    require(1 <= threads <= (os.cpu_count() or 1), "DUCKDB_THREADS_INVALID")
    require(re.fullmatch(r"[1-9]\d*(?:MB|GB)", memory, re.I) is not None, "DUCKDB_MEMORY_LIMIT_INVALID")
    return threads, memory


def _duckdb_export(db_path: Path, output_dir: Path) -> int:
    import duckdb  # type: ignore

    output_dir.mkdir(parents=True, exist_ok=True)
    engine = duckdb.connect()
    exported_counts: dict[str, int] = {}
    threads, memory = duckdb_settings()
    try:
        engine.execute(f"SET threads={threads}")
        engine.execute(f"SET memory_limit='{memory}'")
        engine.execute("SET preserve_insertion_order=false")
        engine.execute("LOAD sqlite")
        database_literal = str(db_path.resolve()).replace("'", "''")
        for table_name in PARQUET_TABLES:
            _, columns = DUCKDB_EXPORT_SPECS[table_name]
            if table_name == "text_documents":
                query = (
                    f"SELECT d.id,d.source_rel,d.source_sha256,d.artifact_rel,d.task_kind,d.text_chars,decode(f.text) AS text "
                    f"FROM sqlite_scan('{database_literal}','documents') d "
                    f"JOIN sqlite_scan('{database_literal}','full_text') f "
                    f"ON decode(f.source_rel)=d.source_rel "
                    f"AND decode(f.source_sha256)=d.source_sha256 "
                    f"AND decode(f.artifact_rel)=d.artifact_rel "
                    f"AND decode(f.task_kind)=d.task_kind"
                )
                document_rows = int(engine.execute(f"SELECT COUNT(*) FROM sqlite_scan('{database_literal}','documents')").fetchone()[0])
                joined_rows = int(engine.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0])
                if joined_rows != document_rows:
                    raise RuntimeError("TEXT_DOCUMENT_JOIN_COUNT_MISMATCH")
            else:
                selected = ",".join(f'"{name}"' for name, _ in columns)
                query = f"SELECT {selected} FROM sqlite_scan('{database_literal}','{table_name}')"
            destination = output_dir / f"{table_name}.parquet"
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            quoted = str(temporary).replace("'", "''")
            engine.execute(f"COPY ({query}) TO '{quoted}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)")
            os.replace(temporary, destination)
            exported_counts[table_name] = int(engine.execute(f"SELECT COUNT(*) FROM read_parquet('{str(destination).replace("'", "''")}')").fetchone()[0])
        atomic_json(
            output_dir / "export_receipt.json",
            {
                "schema": "okki.single_customer.unredacted_parquet_export.v1",
                "status": "PASS",
                "duckdb_version": duckdb.__version__,
                "settings": {"threads": threads, "memory_limit": memory, "preserve_insertion_order": False, "compression": "ZSTD", "row_group_size": 122880},
                "row_counts": exported_counts,
            },
        )
    finally:
        engine.close()
    return 0


def python_candidates(explicit: str | None = None) -> list[str]:
    if os.environ.get("OKKI_DUCKDB_DISABLE_DISCOVERY") == "1":
        return [explicit] if explicit else []
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_python = os.environ.get("OKKI_DUCKDB_PYTHON")
    if env_python:
        candidates.append(env_python)
    candidates.append(sys.executable)
    try:
        result = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                match = re.search(r"([A-Za-z]:\\.*python\.exe)\s*$", line, flags=re.I)
                if match:
                    candidates.append(match.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        result = subprocess.run(["where.exe", "python"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0:
            candidates.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def find_duckdb_python(explicit: str | None = None) -> str:
    for candidate in python_candidates(explicit):
        try:
            result = subprocess.run(
                [candidate, "-c", "import duckdb; assert tuple(map(int, duckdb.__version__.split('.')[:2])) >= (1, 0)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    raise DependencyMissing("DEPENDENCY_MISSING")


def read_duckdb_version(interpreter: str) -> str:
    try:
        result = subprocess.run(
            [interpreter, "-c", "import duckdb; print(duckdb.__version__)"] ,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyMissing("DEPENDENCY_MISSING") from exc
    version = result.stdout.strip()
    require(result.returncode == 0 and re.fullmatch(r"[0-9A-Za-z.+_-]+", version) is not None, "DUCKDB_VERSION_INVALID")
    return version


def export_parquet(db_path: Path, output_dir: Path, duckdb_python: str | None, expected_counts: dict[str, int]) -> dict[str, Any]:
    interpreter = find_duckdb_python(duckdb_python)
    command = [
        interpreter,
        str(Path(__file__).resolve()),
        "--internal-duckdb-export",
        str(db_path),
        str(output_dir),
    ]
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=None,
        check=False,
    )
    require(result.returncode == 0, "PARQUET_EXPORT_FAILED")
    sizes: dict[str, int] = {}
    for name in PARQUET_TABLES:
        path = output_dir / f"{name}.parquet"
        require(path.is_file() and path.stat().st_size >= 8, "PARQUET_EXPORT_MISSING")
        with path.open("rb") as handle:
            require(handle.read(4) == b"PAR1", "PARQUET_HEADER_INVALID")
            handle.seek(-4, os.SEEK_END)
            require(handle.read(4) == b"PAR1", "PARQUET_FOOTER_INVALID")
        sizes[name] = path.stat().st_size
    receipt = read_json(output_dir / "export_receipt.json", "PARQUET_EXPORT_RECEIPT")
    require(receipt.get("schema") == "okki.single_customer.unredacted_parquet_export.v1" and receipt.get("status") == "PASS", "PARQUET_EXPORT_RECEIPT_INVALID")
    row_counts = receipt.get("row_counts")
    require(isinstance(row_counts, dict), "PARQUET_EXPORT_RECEIPT_INVALID")
    for name in PARQUET_TABLES:
        require(parse_nonnegative_int(row_counts.get(name), "PARQUET_ROW_COUNT_INVALID") == expected_counts[name], "PARQUET_ROW_COUNT_MISMATCH")
    return {"sizes": sizes, "receipt": receipt}


def source_hash_map(paths: Iterable[Path], workers: int) -> dict[Path, str]:
    unique = sorted({path.resolve() for path in paths}, key=lambda item: item.as_posix().casefold())
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        hashes = list(executor.map(sha256_file, unique))
    return dict(zip(unique, hashes))


def finalise_database(db: sqlite3.Connection) -> None:
    require(str(db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "OUTPUT_SQLITE_INTEGRITY_FAILED")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.commit()
    db.close()


def verify_staged_package(stage: Path, expected: dict[str, int]) -> dict[str, Any]:
    db_path = stage / "customer_full_unredacted.sqlite3"
    db = open_readonly(db_path)
    try:
        require(str(db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "OUTPUT_SQLITE_INTEGRITY_FAILED")
        actual = {
            "nodes": int(db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]),
            "relation_occurrence": int(db.execute("SELECT COUNT(*) FROM relation_occurrence").fetchone()[0]),
            "relation_fact": int(db.execute("SELECT COUNT(*) FROM relation_fact").fetchone()[0]),
            "files": int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0]),
            "tasks": int(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
            "coverage": int(db.execute("SELECT COUNT(*) FROM coverage").fetchone()[0]),
            "manual_trade_artifacts": int(db.execute("SELECT COUNT(*) FROM manual_trade_artifacts").fetchone()[0]),
            "lineage": int(db.execute("SELECT COUNT(*) FROM lineage").fetchone()[0]),
            "text_documents": int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]),
        }
        require(actual["relation_occurrence"] == int(db.execute("SELECT COALESCE(SUM(evidence_count),0) FROM relation_fact").fetchone()[0]), "RELATION_FACT_EVIDENCE_COUNT_MISMATCH")
        missing = int(db.execute("SELECT COUNT(*) FROM relation_occurrence o LEFT JOIN nodes a ON a.node_id=o.from_node_id LEFT JOIN nodes b ON b.node_id=o.to_node_id WHERE a.node_id IS NULL OR b.node_id IS NULL").fetchone()[0])
        require(missing == 0, "RELATION_ENDPOINT_MISSING")
    finally:
        db.close()
    for key, value in expected.items():
        if key in actual:
            require(actual[key] == value, f"OUTPUT_{key.upper()}_COUNT_MISMATCH")
    graph_path = stage / "relations.graphml.gz"
    require(graph_path.is_file(), "GRAPHML_MISSING")
    graph_nodes = graph_edges = 0
    try:
        with gzip.open(graph_path, "rt", encoding="utf-8") as handle:
            for line in handle:
                graph_nodes += line.count("<node ")
                graph_edges += line.count("<edge ")
    except (OSError, UnicodeError) as exc:
        raise PackageError("GRAPHML_INVALID") from exc
    require(graph_nodes == actual["nodes"] and graph_edges == actual["relation_fact"], "GRAPHML_COUNT_MISMATCH")
    for name in PARQUET_TABLES:
        path = stage / "parquet" / f"{name}.parquet"
        require(path.is_file(), "PARQUET_EXPORT_MISSING")
        with path.open("rb") as handle:
            require(handle.read(4) == b"PAR1", "PARQUET_HEADER_INVALID")
            handle.seek(-4, os.SEEK_END)
            require(handle.read(4) == b"PAR1", "PARQUET_FOOTER_INVALID")
    receipt = read_json(stage / "parquet" / "export_receipt.json", "PARQUET_EXPORT_RECEIPT")
    require(receipt.get("status") == "PASS", "PARQUET_EXPORT_RECEIPT_INVALID")
    return {"counts": actual, "graphml": {"nodes": graph_nodes, "edges": graph_edges}}


def publish_stage(stage: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "parquet").mkdir(parents=True, exist_ok=True)
    published: list[Path] = []
    for name in ("customer_full_unredacted.sqlite3", "relations.graphml.gz", "checksums.sha256"):
        source = stage / name
        target = output_dir / name
        os.replace(source, target)
        published.append(target)
    for name in PARQUET_TABLES:
        source = stage / "parquet" / f"{name}.parquet"
        target = output_dir / "parquet" / f"{name}.parquet"
        os.replace(source, target)
        published.append(target)
    source = stage / "parquet" / "export_receipt.json"
    target = output_dir / "parquet" / "export_receipt.json"
    os.replace(source, target)
    published.append(target)
    # The verification receipt is always the final commit marker.
    source = stage / "verification.json"
    target = output_dir / "verification.json"
    os.replace(source, target)
    published.append(target)
    return published


# EN: Validate inputs, build in a private stage and publish only a verified package.
# 中文：验证输入、隔离构建并仅发布通过验证的数据包。
def build_package(case_root: Path, company_id: str, output_dir: Path, workers: int | None = None, duckdb_python: str | None = None) -> dict[str, Any]:
    ensure_sqlite_version()
    case_root = case_root.resolve()
    output_dir = output_dir.resolve()
    load_case_identity(case_root, company_id)
    auto7 = load_auto7(case_root, company_id)
    manual = load_manual_trade(case_root, company_id)
    full = load_full_extract(case_root, company_id, auto7)
    # Resolve the optional dependency before creating any final artifact.
    selected_duckdb_python = find_duckdb_python(duckdb_python)
    duckdb_version = read_duckdb_version(selected_duckdb_python)
    logical_cpus = os.cpu_count() or 1
    worker_count = workers if workers is not None else min(48, max(1, logical_cpus // 2))
    require(1 <= worker_count <= min(logical_cpus, 61), "WORKER_COUNT_INVALID")

    output_dir.mkdir(parents=True, exist_ok=True)
    owned_targets = [
        output_dir / "customer_full_unredacted.sqlite3",
        output_dir / "relations.graphml.gz",
        output_dir / "checksums.sha256",
        output_dir / "verification.json",
        *[output_dir / "parquet" / f"{name}.parquet" for name in PARQUET_TABLES],
        output_dir / "parquet" / "export_receipt.json",
    ]
    require(not any(path.exists() for path in owned_targets), "OUTPUT_TARGET_EXISTS")
    stage = output_dir / f".unredacted_stage_{uuid.uuid4().hex}"
    stage.mkdir()
    (stage / "parquet").mkdir()
    db_path = stage / "customer_full_unredacted.sqlite3"
    db: sqlite3.Connection | None = None
    try:
        db = copy_full_text_database(full["paths"]["full_text"], db_path)
        verification = full["verification"]
        counts: dict[str, int] = {}
        counts["files"], _ = import_files(
            db,
            case_root,
            full["paths"]["files"],
            parse_nonnegative_int(verification.get("evidence_files"), "FULL_EXTRACT_FILE_COUNT_INVALID"),
            worker_count,
        )
        counts["tasks"] = import_tasks(
            db,
            full["paths"]["tasks"],
            parse_nonnegative_int(verification.get("tasks"), "FULL_EXTRACT_TASK_COUNT_INVALID"),
        )
        counts["coverage"] = import_coverage(db, full["paths"]["coverage"])
        counts["manual_trade_artifacts"] = import_manual_artifacts(db, manual, worker_count)
        relation_counts = import_relations(
            db,
            full["paths"]["relationships"],
            parse_nonnegative_int(verification.get("relation_rows"), "FULL_EXTRACT_RELATION_ROWS_INVALID"),
            worker_count,
        )
        counts.update(relation_counts)
        counts["text_documents"] = full["document_count"]
        source_paths = [
            auto7["scope_path"],
            auto7["terminal_path"],
            auto7["object_manifest_path"],
            *full["paths"].values(),
            manual["manifest_path"],
            manual["receipt_path"],
        ]
        hashes = source_hash_map(source_paths, worker_count)
        insert_meta_and_lineage(db, case_root, company_id, auto7, full, manual, counts, hashes, duckdb_version)
        counts["lineage"] = int(db.execute("SELECT COUNT(*) FROM lineage").fetchone()[0])
        finalise_database(db)
        db = None

        graph_counts = write_graphml(db_path, stage / "relations.graphml.gz")
        require(graph_counts["nodes"] == counts["nodes"] and graph_counts["edges"] == counts["relation_fact"], "GRAPHML_COUNT_MISMATCH")
        parquet_result = export_parquet(db_path, stage / "parquet", selected_duckdb_python, {name: counts[name] for name in PARQUET_TABLES})
        checked = verify_staged_package(stage, counts)

        artifact_paths = [db_path, stage / "relations.graphml.gz", *[stage / "parquet" / f"{name}.parquet" for name in PARQUET_TABLES], stage / "parquet" / "export_receipt.json"]
        output_hashes = source_hash_map(artifact_paths, worker_count)
        checksum_lines = [
            f"{output_hashes[path.resolve()]}  {path.relative_to(stage).as_posix()}"
            for path in sorted(artifact_paths, key=lambda item: item.relative_to(stage).as_posix())
        ]
        atomic_bytes(stage / "checksums.sha256", ("\n".join(checksum_lines) + "\n").encode("utf-8"))
        receipt = {
            "schema": SCHEMA,
            "status": "PASS",
            "built_at": utc_now(),
            "sqlite_version": sqlite3.sqlite_version,
            "minimum_sqlite_version": ".".join(map(str, MIN_SQLITE)),
            "supported_sqlite_wal_backports": [".".join(map(str, value)) for value in SQLITE_WAL_PATCHED_BACKPORTS],
            "duckdb": {"version": duckdb_version, "threads": duckdb_settings()[0], "memory_limit": duckdb_settings()[1], "preserve_insertion_order": False},
            "parquet_export": parquet_result["receipt"],
            "workers": worker_count,
            "counts": checked["counts"],
            "graphml": checked["graphml"],
            "artifacts": [
                {
                    "path": path.relative_to(stage).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": output_hashes[path.resolve()],
                }
                for path in sorted(artifact_paths, key=lambda item: item.relative_to(stage).as_posix())
            ],
            "checksums_sha256": sha256_file(stage / "checksums.sha256"),
            "source_gates": {"auto7": "PASS", "full_extract": "PASS", "manual_trade": "PASS"},
        }
        atomic_json(stage / "verification.json", receipt)
        publish_stage(stage, output_dir)
        return {
            "status": "PASS",
            "counts": checked["counts"],
            "hashes": {
                "database": output_hashes[db_path.resolve()],
                "graphml": output_hashes[(stage / "relations.graphml.gz").resolve()],
                "checksums": receipt["checksums_sha256"],
            },
        }
    finally:
        if db is not None:
            db.close()
        if stage.exists() and stage.parent.resolve() == output_dir and stage.name.startswith(".unredacted_stage_"):
            shutil.rmtree(stage, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Build a local-only unredacted OKKI single-customer package")
    result.add_argument("--case-root", type=Path, required=True)
    result.add_argument("--company-id", required=True)
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--workers", type=int)
    result.add_argument("--duckdb-python")
    return result


def quiet_result(value: dict[str, Any]) -> dict[str, Any]:
    return {"status": value["status"], "counts": value.get("counts", {}), "hashes": value.get("hashes", {})}


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--internal-duckdb-export":
        if len(arguments) != 3:
            return 2
        try:
            return _duckdb_export(Path(arguments[1]), Path(arguments[2]))
        except Exception:
            return 2
    args = parser().parse_args(arguments)
    try:
        result = build_package(args.case_root, args.company_id, args.output_dir, args.workers, args.duckdb_python)
    except DependencyMissing:
        print(json.dumps({"status": "DEPENDENCY_MISSING", "dependency": "duckdb"}, separators=(",", ":")))
        return 4
    except PackageError as exc:
        print(json.dumps({"status": "FAILED", "error_code": str(exc)}, separators=(",", ":")))
        return 2
    except Exception:
        print(json.dumps({"status": "FAILED", "error_code": "UNEXPECTED_ERROR"}, separators=(",", ":")))
        return 2
    print(json.dumps(quiet_result(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
