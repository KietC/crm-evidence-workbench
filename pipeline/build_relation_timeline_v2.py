#!/usr/bin/env python3
# EN: Build explicit provenance-backed relations and independent business/capture timelines.
# 中文：构建有来源支持的显式关系及独立业务/采集时间线。
"""Build and verify a local-only explicit relationship and dual-timeline package.

The v2 package is supplemental.  It never changes the capture, full-extract, or
v1 unredacted package.  Source values stay in the local SQLite/Parquet/GraphML
artifacts; stdout contains only status, counts, paths, and hashes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
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
import uuid
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, time as datetime_time, timezone, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence
from xml.sax.saxutils import escape, quoteattr


SCHEMA = "okki.single_customer.relation_timeline.v2"
SOURCE_PACKAGE_SCHEMA = "okki.single_customer.unredacted_local_package.v1"
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
JSON_SUFFIXES = {".json", ".jsonl"}
PARQUET_TABLES = (
    "artifacts",
    "source_manifest",
    "record_occurrence",
    "nodes",
    "explicit_relation",
    "relation_evidence",
    "timeline_event",
    "attachment_occurrence",
    "derived_artifacts",
    "source_gap_task",
    "archive_member",
    "trade_entity",
    "lineage",
)


ID_TYPE_TOKENS = {
    "company": "company",
    "customer": "company",
    "buyer": "company",
    "seller": "company",
    "supplier": "company",
    "contact": "contact",
    "mail": "mail",
    "message": "mail",
    "email_message": "mail",
    "opportunity": "opportunity",
    "business": "opportunity",
    "order": "order",
    "product": "product",
    "sku": "product",
    "file": "file",
    "document": "document",
    "attachment": "attachment",
    "folder": "folder",
    "activity": "activity",
    "event": "activity",
    "trade": "trade_record",
    "customs": "trade_record",
    "shipment": "trade_record",
}

TYPE_PRIORITY = (
    "mail",
    "order",
    "opportunity",
    "contact",
    "trade_record",
    "activity",
    "product",
    "attachment",
    "document",
    "file",
    "company",
)

TIME_EXCLUDED = {
    "timezone",
    "time_zone",
    "time_flag",
    "times_flag",
    "send_status",
    "sender",
    "receive_origin_sender",
    "delay_send_flag",
    "stat_open_times_flag",
    "stat_open_times_unreply_flag",
}


class BuildError(RuntimeError):
    pass


class DependencyMissing(BuildError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise BuildError(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_tree_hash(path: Path) -> str:
    if path.is_file():
        return sha256_file(path)
    require(path.is_dir(), "TASK_OUTPUT_MISSING")
    digest = hashlib.sha256()
    for item in sorted((candidate for candidate in path.rglob("*") if candidate.is_file()), key=lambda candidate: candidate.as_posix().casefold()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(item)))
    return digest.hexdigest()


def output_stats(path: Path) -> tuple[str, int, int]:
    if path.is_file():
        return "file", path.stat().st_size, 1
    require(path.is_dir(), "TASK_OUTPUT_MISSING")
    files = [candidate for candidate in path.rglob("*") if candidate.is_file()]
    return "directory", sum(candidate.stat().st_size for candidate in files), len(files)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_json_hash(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def read_json(path: Path, code: str) -> dict[str, Any]:
    require(path.is_file(), f"{code}_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"{code}_INVALID") from exc
    require(isinstance(value, dict), f"{code}_INVALID")
    return value


def safe_relative(value: Any, code: str) -> str:
    text = str(value or "").replace("\\", "/")
    pure = PurePosixPath(text)
    require(text != "" and not pure.is_absolute() and ".." not in pure.parts, code)
    return pure.as_posix()


def resolve_inside(root: Path, value: str | Path, code: str) -> Path:
    base = root.resolve()
    candidate = Path(value)
    path = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    require(path == base or base in path.parents, code)
    return path


def normalize_key(value: Any) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", str(value)).casefold()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def pointer_escape(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def pointer_join(pointer: str, part: Any) -> str:
    encoded = pointer_escape(part)
    return f"{pointer}/{encoded}" if pointer else f"/{encoded}"


def normalize_sha(value: Any, code: str) -> str:
    text = str(value or "")
    require(SHA_RE.fullmatch(text) is not None, code)
    return text.lower()


def parse_int(value: Any, code: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BuildError(code) from exc
    require(parsed >= 0, code)
    return parsed


def open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def read_checksum_file(path: Path) -> dict[str, str]:
    require(path.is_file(), "SOURCE_CHECKSUMS_MISSING")
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        require(match is not None, "SOURCE_CHECKSUMS_INVALID")
        rel = safe_relative(match.group(2), "SOURCE_CHECKSUM_PATH_INVALID")
        require(rel not in result, "SOURCE_CHECKSUM_DUPLICATE")
        result[rel] = match.group(1).lower()
    return result


def load_source_package(case_root: Path, company_id: str, package_root: Path) -> dict[str, Any]:
    require(SAFE_ID_RE.fullmatch(company_id) is not None, "COMPANY_ID_INVALID")
    identity = read_json(case_root / "case_identity.json", "CASE_IDENTITY")
    require(str(identity.get("company_id") or "") == company_id, "CASE_IDENTITY_MISMATCH")
    require(case_root.name == f"company_{company_id}" or case_root.name.endswith(f"_{company_id}"), "CASE_DIRECTORY_IDENTITY_MISMATCH")
    package_root = package_root.resolve()
    database = package_root / "customer_full_unredacted.sqlite3"
    verification_path = package_root / "verification.json"
    require(database.is_file(), "SOURCE_DATABASE_MISSING")
    verification = read_json(verification_path, "SOURCE_VERIFICATION")
    require(verification.get("status") == "PASS", "SOURCE_PACKAGE_NOT_PASS")
    checksums = read_checksum_file(package_root / "checksums.sha256")
    expected = checksums.get("customer_full_unredacted.sqlite3")
    require(expected is not None and sha256_file(database) == expected, "SOURCE_DATABASE_HASH_MISMATCH")
    db = open_readonly(database)
    try:
        require(str(db.execute("PRAGMA quick_check(1)").fetchone()[0]) == "ok", "SOURCE_DATABASE_INTEGRITY_FAILED")
        tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({"meta", "files", "tasks", "manual_trade_artifacts"}.issubset(tables), "SOURCE_DATABASE_SCHEMA_INVALID")
        meta = {str(row[0]): str(row[1]) for row in db.execute("SELECT key,value FROM meta")}
        require(meta.get("schema") == SOURCE_PACKAGE_SCHEMA, "SOURCE_PACKAGE_SCHEMA_INVALID")
        require(meta.get("company_id") == company_id, "SOURCE_PACKAGE_COMPANY_MISMATCH")
    finally:
        db.close()
    return {
        "root": package_root,
        "database": database,
        "database_sha256": expected,
        "verification": verification,
        "verification_path": verification_path,
    }


def load_full_extract_v2(case_root: Path, company_id: str, root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir() and (root == case_root.resolve() or case_root.resolve() in root.parents), "FULL_EXTRACT_V2_SCOPE_INVALID")
    state = root / "state.sqlite3"
    verification_path = root / "verification.json"
    require(state.is_file(), "FULL_EXTRACT_V2_STATE_MISSING")
    verification = read_json(verification_path, "FULL_EXTRACT_V2_VERIFICATION")
    require(str(verification.get("company_id") or "") == company_id, "FULL_EXTRACT_V2_COMPANY_MISMATCH")
    require(str(verification.get("status") or "") in {"PASS", "INCOMPLETE_SOURCE_GAPS"}, "FULL_EXTRACT_V2_NOT_TERMINAL")
    database = open_readonly(state)
    try:
        require(str(database.execute("PRAGMA quick_check(1)").fetchone()[0]) == "ok", "FULL_EXTRACT_V2_DATABASE_INVALID")
        tables = {str(row[0]) for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({"meta", "files", "tasks", "source_gaps"}.issubset(tables), "FULL_EXTRACT_V2_SCHEMA_INVALID")
        file_count = int(database.execute("SELECT COUNT(*) FROM files WHERE active=1").fetchone()[0])
        task_count = int(database.execute("SELECT COUNT(*) FROM tasks WHERE active=1").fetchone()[0])
        completed = int(database.execute("SELECT COUNT(*) FROM tasks WHERE active=1 AND state='completed'").fetchone()[0])
        source_gap = int(database.execute("SELECT COUNT(*) FROM tasks WHERE active=1 AND state='source_gap'").fetchone()[0])
        nonterminal = int(database.execute("SELECT COUNT(*) FROM tasks WHERE active=1 AND state NOT IN ('completed','source_gap')").fetchone()[0])
        require(nonterminal == 0 and completed + source_gap == task_count, "FULL_EXTRACT_V2_TASKS_NOT_TERMINAL")
        require(file_count == parse_int(verification.get("evidence_files"), "FULL_EXTRACT_V2_FILE_COUNT_INVALID"), "FULL_EXTRACT_V2_FILE_COUNT_MISMATCH")
        require(task_count == parse_int(verification.get("tasks"), "FULL_EXTRACT_V2_TASK_COUNT_INVALID"), "FULL_EXTRACT_V2_TASK_COUNT_MISMATCH")
        require(source_gap == parse_int(verification.get("task_source_gaps"), "FULL_EXTRACT_V2_GAP_COUNT_INVALID"), "FULL_EXTRACT_V2_GAP_COUNT_MISMATCH")
    finally:
        database.close()
    return {
        "root": root,
        "database": state,
        "database_sha256": sha256_file(state),
        "verification_path": verification_path,
        "verification_sha256": sha256_file(verification_path),
        "verification": verification,
        "file_count": file_count,
        "task_count": task_count,
        "completed": completed,
        "source_gap": source_gap,
    }


def load_mail_capture_baseline(case_root: Path, completion_run_dir: Path, company_id: str) -> dict[str, Any]:
    baseline = completion_run_dir / "baseline"
    freeze_path = baseline / "freeze_manifest.json"
    manifest_path = baseline / "mail_capture_manifest.json"
    freeze = read_json(freeze_path, "FREEZE_MANIFEST")
    require(str(freeze.get("company_id") or "") == company_id, "FREEZE_MANIFEST_COMPANY_MISMATCH")
    authority = freeze.get("authority_files")
    require(isinstance(authority, list), "FREEZE_MANIFEST_AUTHORITY_INVALID")
    selected = next(
        (
            row
            for row in authority
            if isinstance(row, dict)
            and str(row.get("copied_rel") or "").replace("\\", "/") == "baseline/mail_capture_manifest.json"
        ),
        None,
    )
    require(isinstance(selected, dict), "MAIL_CAPTURE_AUTHORITY_MISSING")
    expected_sha = normalize_sha(selected.get("sha256"), "MAIL_CAPTURE_AUTHORITY_SHA_INVALID")
    expected_bytes = parse_int(selected.get("bytes"), "MAIL_CAPTURE_AUTHORITY_BYTES_INVALID")
    require(manifest_path.is_file(), "MAIL_CAPTURE_MANIFEST_MISSING")
    require(manifest_path.stat().st_size == expected_bytes, "MAIL_CAPTURE_MANIFEST_SIZE_MISMATCH")
    require(sha256_file(manifest_path) == expected_sha, "MAIL_CAPTURE_MANIFEST_HASH_MISMATCH")
    manifest = read_json(manifest_path, "MAIL_CAPTURE_MANIFEST")
    require(str(manifest.get("company_id") or "") == company_id, "MAIL_CAPTURE_MANIFEST_COMPANY_MISMATCH")
    require(str(manifest.get("status") or "").upper() in {"PASS", "COMPLETE", "COMPLETED"}, "MAIL_CAPTURE_MANIFEST_NOT_COMPLETE")
    mails = manifest.get("mails")
    require(isinstance(mails, list), "MAIL_CAPTURE_MANIFEST_MAILS_INVALID")
    source_rel = manifest_path.relative_to(case_root).as_posix()
    return {
        "path": manifest_path,
        "source_rel": source_rel,
        "sha256": expected_sha,
        "bytes": expected_bytes,
        "schema": str(manifest.get("schema") or "unknown"),
        "status": str(manifest.get("status") or ""),
        "generated_at": str(manifest.get("generated_at") or ""),
        "mails": mails,
    }


def supplemental_mail_artifacts(
    case_root: Path,
    manifest: dict[str, Any],
    existing_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing = {str(row["source_rel"]): row for row in existing_rows}
    discovered: dict[str, dict[str, Any]] = {}
    for mail in manifest["mails"]:
        require(isinstance(mail, dict), "MAIL_CAPTURE_MAIL_INVALID")
        resources = mail.get("related_resources")
        require(isinstance(resources, list), "MAIL_CAPTURE_RESOURCES_INVALID")
        for resource in resources:
            require(isinstance(resource, dict), "MAIL_CAPTURE_RESOURCE_INVALID")
            artifact = resource.get("artifact")
            require(isinstance(artifact, dict), "MAIL_CAPTURE_RESOURCE_ARTIFACT_MISSING")
            source_rel = safe_relative(artifact.get("relative_path"), "MAIL_ATTACHMENT_PATH_INVALID")
            source_sha = normalize_sha(artifact.get("sha256"), "MAIL_ATTACHMENT_SHA_INVALID")
            source_bytes = parse_int(artifact.get("bytes"), "MAIL_ATTACHMENT_BYTES_INVALID")
            if source_rel in existing:
                require(existing[source_rel]["sha256"] == source_sha, "MAIL_ATTACHMENT_EXISTING_HASH_MISMATCH")
                require(existing[source_rel]["bytes"] == source_bytes, "MAIL_ATTACHMENT_EXISTING_SIZE_MISMATCH")
                continue
            previous = discovered.get(source_rel)
            if previous is not None:
                require(previous["sha256"] == source_sha and previous["bytes"] == source_bytes, "MAIL_ATTACHMENT_MANIFEST_CONFLICT")
                continue
            path = resolve_inside(case_root, source_rel, "MAIL_ATTACHMENT_PATH_ESCAPE")
            require(path.is_file(), "MAIL_ATTACHMENT_FILE_MISSING")
            require(path.stat().st_size == source_bytes, "MAIL_ATTACHMENT_FILE_SIZE_MISMATCH")
            require(sha256_file(path) == source_sha, "MAIL_ATTACHMENT_FILE_HASH_MISMATCH")
            discovered[source_rel] = {
                "source_rel": source_rel,
                "source_scope": "mail_related_resource",
                "sha256": source_sha,
                "bytes": source_bytes,
                "magic": "mail_manifest_attachment",
                "category": path.suffix.casefold().lstrip(".") or "binary",
                "mime": str(artifact.get("mime_type") or "application/octet-stream"),
                "prepared_at": manifest.get("generated_at") or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "manifest_record_json": None,
                "path": path,
            }
    return [discovered[key] for key in sorted(discovered)]


def parse_timezone_hint(value: Any) -> timezone | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if text in {"Z", "UTC", "GMT", "+00:00", "+0000"}:
        return timezone.utc
    match = re.fullmatch(r"([+\-])(\d{2}):?(\d{2})", text)
    if not match:
        return None
    minutes = int(match.group(2)) * 60 + int(match.group(3))
    if minutes > 14 * 60:
        return None
    if match.group(1) == "-":
        minutes = -minutes
    return timezone(timedelta(minutes=minutes))


def time_field_candidate(key: Any) -> bool:
    normalized = normalize_key(key)
    if normalized in TIME_EXCLUDED or normalized.endswith(("_flag", "_status", "_timezone", "_times")):
        return False
    return (
        normalized in {"date", "time", "timestamp", "datetime"}
        or normalized.endswith(("_time", "_date", "_timestamp", "_datetime", "_at"))
    )


def timezone_hint(record: dict[str, Any]) -> str | None:
    for key, value in record.items():
        normalized = normalize_key(key)
        if normalized in {"timezone", "time_zone", "utc_offset", "timezone_offset"} and isinstance(value, (str, int, float)):
            return str(value)
    return None


def raw_scalar(value: Any) -> tuple[str, str]:
    if value is None:
        return "null", "null"
    if isinstance(value, bool):
        return "boolean", "true" if value else "false"
    if isinstance(value, str):
        return "string", value
    if isinstance(value, (int, float)):
        return "number", str(value)
    return "json", canonical_json(value)


def epoch_datetime(number: float) -> tuple[datetime, str] | None:
    magnitude = abs(number)
    if 946684800 <= magnitude <= 4102444800:
        seconds, unit = number, "epoch_seconds"
    elif 946684800000 <= magnitude <= 4102444800000:
        seconds, unit = number / 1_000, "epoch_milliseconds"
    elif 946684800000000 <= magnitude <= 4102444800000000:
        seconds, unit = number / 1_000_000, "epoch_microseconds"
    elif 946684800000000000 <= magnitude <= 4102444800000000000:
        seconds, unit = number / 1_000_000_000, "epoch_nanoseconds"
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc), unit
    except (OverflowError, OSError, ValueError):
        return None


def normalize_timestamp(value: Any, zone_hint: str | None = None) -> dict[str, Any]:
    raw_type, raw = raw_scalar(value)
    utc_value: datetime | None = None
    local_value: str | None = None
    status = "unparsed"
    basis = "unknown"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = epoch_datetime(float(value))
        if parsed:
            utc_value, basis = parsed
            status = "absolute"
    elif isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d{10,19}(?:\.\d+)?", text):
            parsed = epoch_datetime(float(text))
            if parsed:
                utc_value, basis = parsed
                status = "absolute"
        if utc_value is None and text:
            normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            parsed_dt: datetime | None = None
            try:
                parsed_dt = datetime.fromisoformat(normalized)
            except ValueError:
                for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
                    try:
                        parsed_dt = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
            if parsed_dt is not None:
                if parsed_dt.tzinfo is not None:
                    utc_value = parsed_dt.astimezone(timezone.utc)
                    status, basis = "absolute", "iso_with_offset"
                else:
                    hint = parse_timezone_hint(zone_hint)
                    if hint is not None:
                        utc_value = parsed_dt.replace(tzinfo=hint).astimezone(timezone.utc)
                        status, basis = "absolute", "local_with_sibling_offset"
                    else:
                        local_value = parsed_dt.isoformat(timespec="microseconds")
                        status, basis = "local_unzoned", "local_datetime"
    utc_text = utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z") if utc_value else None
    sort_key = int(utc_value.timestamp() * 1_000_000) if utc_value else None
    precision = "unknown"
    if basis.startswith("epoch_"):
        precision = basis.removeprefix("epoch_").removesuffix("s")
    elif isinstance(value, str):
        text = value.strip()
        if re.search(r"[.,]\d+(?:Z|[+\-]\d\d:?\d\d)?$", text, re.I):
            precision = "fractional_second"
        elif re.search(r"\d{1,2}:\d{2}:\d{2}", text):
            precision = "second"
        elif re.search(r"\d{1,2}:\d{2}", text):
            precision = "minute"
        elif re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text):
            precision = "day"
    return {
        "timestamp_raw": raw,
        "timestamp_raw_type": raw_type,
        "timestamp_utc": utc_text,
        "timestamp_sort_key": sort_key,
        "local_sort_key": local_value,
        "normalization_status": status,
        "time_basis": basis,
        "timezone_raw": zone_hint,
        "timestamp_precision": precision,
    }


def stable_time_key(axis: str, normalized: dict[str, Any], event_id: str) -> str:
    if normalized["timestamp_sort_key"] is not None:
        return f"A:{int(normalized['timestamp_sort_key']) + 10**19:020d}:{axis}:{event_id}"
    if normalized["local_sort_key"]:
        return f"L:{normalized['local_sort_key']}:{axis}:{event_id}"
    return f"Z:{axis}:{event_id}"


def stable_sequence_basis(normalized: dict[str, Any]) -> str:
    if normalized["timestamp_sort_key"] is not None:
        return "timestamp_utc_then_event_id"
    if normalized["local_sort_key"]:
        return "local_time_then_event_id"
    return "source_pointer_hash_only_not_time"


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE artifacts(
          artifact_id TEXT PRIMARY KEY,
          source_rel TEXT NOT NULL UNIQUE,
          source_scope TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          source_bytes INTEGER NOT NULL,
          magic TEXT NOT NULL,
          category TEXT NOT NULL,
          mime TEXT NOT NULL,
          prepared_at_raw TEXT NOT NULL,
          file_mtime_utc TEXT NOT NULL,
          manifest_record_json TEXT,
          current_hash_verified INTEGER NOT NULL CHECK(current_hash_verified=1)
        ) WITHOUT ROWID;
        CREATE TABLE source_manifest(
          manifest_id TEXT PRIMARY KEY,
          source_rel TEXT NOT NULL UNIQUE,
          source_sha256 TEXT NOT NULL,
          source_bytes INTEGER NOT NULL,
          manifest_schema TEXT NOT NULL,
          manifest_status TEXT NOT NULL,
          current_hash_verified INTEGER NOT NULL CHECK(current_hash_verified=1)
        ) WITHOUT ROWID;
        CREATE TABLE nodes(
          node_id TEXT PRIMARY KEY,
          node_type TEXT NOT NULL,
          value TEXT NOT NULL,
          UNIQUE(node_type,value)
        ) WITHOUT ROWID;
        CREATE TABLE record_occurrence(
          record_id TEXT PRIMARY KEY,
          artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
          parent_record_id TEXT REFERENCES record_occurrence(record_id),
          record_node_id TEXT NOT NULL REFERENCES nodes(node_id),
          primary_entity_node_id TEXT REFERENCES nodes(node_id),
          source_rel TEXT NOT NULL,
          source_line INTEGER,
          pointer_kind TEXT NOT NULL,
          json_pointer TEXT NOT NULL,
          depth INTEGER NOT NULL,
          record_type TEXT NOT NULL,
          record_sha256 TEXT NOT NULL,
          parse_status TEXT NOT NULL,
          first_event_sort_key INTEGER,
          last_event_sort_key INTEGER,
          UNIQUE(artifact_id,pointer_kind,json_pointer)
        ) WITHOUT ROWID;
        CREATE TABLE explicit_relation(
          relation_id TEXT PRIMARY KEY,
          subject_node_id TEXT NOT NULL REFERENCES nodes(node_id),
          predicate TEXT NOT NULL,
          object_node_id TEXT NOT NULL REFERENCES nodes(node_id),
          relation_kind TEXT NOT NULL,
          evidence_count INTEGER NOT NULL DEFAULT 0,
          first_event_sort_key INTEGER,
          last_event_sort_key INTEGER,
          UNIQUE(subject_node_id,predicate,object_node_id)
        ) WITHOUT ROWID;
        CREATE TABLE timeline_event(
          event_id TEXT PRIMARY KEY,
          artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
          record_id TEXT REFERENCES record_occurrence(record_id),
          timeline_axis TEXT NOT NULL CHECK(timeline_axis IN ('business','capture')),
          event_kind TEXT NOT NULL,
          field_name TEXT NOT NULL,
          source_rel TEXT NOT NULL,
          source_line INTEGER,
          json_pointer TEXT NOT NULL,
          timestamp_raw TEXT NOT NULL,
          timestamp_raw_type TEXT NOT NULL,
          timestamp_utc TEXT,
          timestamp_sort_key INTEGER,
          local_sort_key TEXT,
          stable_order_key TEXT NOT NULL,
          stable_sequence INTEGER NOT NULL DEFAULT 0,
          stable_sequence_basis TEXT NOT NULL,
          normalization_status TEXT NOT NULL,
          time_basis TEXT NOT NULL,
          timezone_raw TEXT,
          timestamp_precision TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE relation_evidence(
          evidence_id TEXT PRIMARY KEY,
          relation_id TEXT NOT NULL REFERENCES explicit_relation(relation_id),
          artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
          record_id TEXT NOT NULL REFERENCES record_occurrence(record_id),
          event_id TEXT REFERENCES timeline_event(event_id),
          source_rel TEXT NOT NULL,
          source_line INTEGER,
          json_pointer TEXT NOT NULL,
          evidence_role TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE lineage(
          lineage_id TEXT PRIMARY KEY,
          source_kind TEXT NOT NULL,
          source_path TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          source_bytes INTEGER NOT NULL,
          source_artifact_id TEXT REFERENCES artifacts(artifact_id),
          target_table TEXT NOT NULL,
          target_key TEXT NOT NULL,
          derivation TEXT NOT NULL,
          recorded_at_utc TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE attachment_occurrence(
          occurrence_id TEXT PRIMARY KEY,
          manifest_id TEXT NOT NULL REFERENCES source_manifest(manifest_id),
          mail_node_id TEXT NOT NULL REFERENCES nodes(node_id),
          attachment_node_id TEXT NOT NULL REFERENCES nodes(node_id),
          artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
          source_mail_ordinal INTEGER NOT NULL,
          source_resource_ordinal INTEGER NOT NULL,
          source_pointer TEXT NOT NULL,
          artifact_source_rel TEXT NOT NULL,
          artifact_sha256 TEXT NOT NULL,
          resolution TEXT NOT NULL,
          UNIQUE(manifest_id,source_mail_ordinal,source_resource_ordinal)
        ) WITHOUT ROWID;
        CREATE TABLE derived_artifacts(
          derived_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL,
          source_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
          source_rel TEXT NOT NULL,
          output_rel TEXT NOT NULL,
          output_sha256 TEXT NOT NULL,
          output_kind TEXT NOT NULL CHECK(output_kind IN ('file','directory')),
          output_bytes INTEGER NOT NULL,
          output_file_count INTEGER NOT NULL,
          task_kind TEXT NOT NULL,
          result_hash_verified INTEGER NOT NULL CHECK(result_hash_verified=1),
          UNIQUE(task_id,output_rel)
        ) WITHOUT ROWID;
        CREATE TABLE source_gap_task(
          task_id TEXT PRIMARY KEY,
          source_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
          gap_node_id TEXT NOT NULL REFERENCES nodes(node_id),
          source_rel TEXT NOT NULL,
          output_rel TEXT NOT NULL,
          output_sha256 TEXT NOT NULL,
          output_kind TEXT NOT NULL CHECK(output_kind IN ('file','directory')),
          output_bytes INTEGER NOT NULL,
          output_file_count INTEGER NOT NULL,
          task_kind TEXT NOT NULL,
          error_code TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          result_hash_verified INTEGER NOT NULL CHECK(result_hash_verified=1)
        ) WITHOUT ROWID;
        CREATE TABLE archive_member(
          member_id TEXT PRIMARY KEY,
          artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
          member_path TEXT NOT NULL,
          member_index INTEGER NOT NULL,
          compressed_bytes INTEGER NOT NULL,
          uncompressed_bytes INTEGER NOT NULL,
          crc32 INTEGER NOT NULL,
          encrypted INTEGER NOT NULL CHECK(encrypted IN (0,1)),
          is_directory INTEGER NOT NULL CHECK(is_directory IN (0,1)),
          UNIQUE(artifact_id,member_index)
        ) WITHOUT ROWID;
        CREATE TABLE trade_entity(
          entity_node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
          entity_type TEXT NOT NULL CHECK(entity_type IN ('trade_record','trade_candidate')),
          unbound_to_customer INTEGER NOT NULL CHECK(unbound_to_customer IN (0,1)),
          binding_status TEXT NOT NULL,
          source_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
          source_record_id TEXT NOT NULL REFERENCES record_occurrence(record_id),
          source_rel TEXT NOT NULL,
          source_line INTEGER,
          json_pointer TEXT NOT NULL,
          binding_evidence_pointer TEXT
        ) WITHOUT ROWID;
        CREATE INDEX idx_record_artifact ON record_occurrence(artifact_id,source_line,json_pointer);
        CREATE INDEX idx_record_parent ON record_occurrence(parent_record_id);
        CREATE INDEX idx_relation_subject ON explicit_relation(subject_node_id,predicate);
        CREATE INDEX idx_relation_object ON explicit_relation(object_node_id,predicate);
        CREATE INDEX idx_evidence_relation ON relation_evidence(relation_id);
        CREATE INDEX idx_evidence_artifact ON relation_evidence(artifact_id,record_id);
        CREATE INDEX idx_timeline_absolute ON timeline_event(timeline_axis,timestamp_sort_key,event_id);
        CREATE INDEX idx_timeline_stable ON timeline_event(timeline_axis,stable_order_key);
        CREATE INDEX idx_timeline_record ON timeline_event(record_id);
        CREATE INDEX idx_timeline_artifact_axis_event ON timeline_event(artifact_id,timeline_axis,event_id);
        CREATE UNIQUE INDEX idx_timeline_sequence ON timeline_event(timeline_axis,stable_sequence) WHERE stable_sequence>0;
        CREATE INDEX idx_attachment_mail ON attachment_occurrence(mail_node_id,attachment_node_id);
        CREATE INDEX idx_attachment_object ON attachment_occurrence(attachment_node_id,mail_node_id);
        CREATE INDEX idx_derived_source ON derived_artifacts(source_artifact_id);
        CREATE INDEX idx_gap_source ON source_gap_task(source_artifact_id);
        CREATE INDEX idx_archive_artifact ON archive_member(artifact_id,member_index);
        """
    )


def infer_type_from_context(pointer: str) -> str | None:
    normalized = normalize_key(pointer)
    tokens = normalized.split("_")
    for token in reversed(tokens):
        singular = token[:-1] if token.endswith("s") else token
        if singular in ID_TYPE_TOKENS:
            return ID_TYPE_TOKENS[singular]
    return None


def infer_id_type(key: Any, pointer: str, record_type: str | None = None) -> str | None:
    normalized = normalize_key(key)
    if normalized == "id":
        return record_type or infer_type_from_context(pointer)
    if not (normalized.endswith("_id") or normalized.endswith("id")):
        return None
    base = normalized[:-3] if normalized.endswith("_id") else normalized[:-2]
    base = base.rstrip("_")
    tokens = base.split("_")
    for width in range(min(3, len(tokens)), 0, -1):
        candidate = "_".join(tokens[-width:])
        if candidate in ID_TYPE_TOKENS:
            return ID_TYPE_TOKENS[candidate]
    for token in reversed(tokens):
        if token in ID_TYPE_TOKENS:
            return ID_TYPE_TOKENS[token]
    return None


def scalar_identity(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or len(text) > 4096:
        return None
    return text


def infer_record_type(value: dict[str, Any], pointer: str) -> str:
    candidates: set[str] = set()
    context = infer_type_from_context(pointer)
    if context:
        candidates.add(context)
    for key, item in value.items():
        kind = infer_id_type(key, pointer, context)
        if kind and scalar_identity(item) is not None:
            candidates.add(kind)
    for item in TYPE_PRIORITY:
        if item in candidates:
            return item
    return context or "record"


def relation_predicate(subject_type: str, object_type: str, role: str | None = None) -> str:
    if object_type == "email" and role:
        if role in {"sender", "from"}:
            return f"{subject_type}_has_sender"
        if role in {"receiver", "recipient", "to", "cc", "bcc"}:
            return f"{subject_type}_has_recipient"
    mappings = {
        ("company", "contact"): "company_has_contact",
        ("company", "mail"): "company_has_mail",
        ("company", "opportunity"): "company_has_opportunity",
        ("company", "order"): "company_has_order",
        ("company", "product"): "company_has_product",
        ("company", "activity"): "company_has_activity",
        ("company", "trade_record"): "company_has_trade_record",
        ("mail", "attachment"): "mail_references_attachment_id",
        ("mail", "file"): "mail_has_file",
        ("mail", "contact"): "mail_has_contact",
        ("order", "product"): "order_has_product",
        ("order", "contact"): "order_has_contact",
        ("order", "company"): "order_has_company",
        ("opportunity", "product"): "opportunity_has_product",
        ("opportunity", "contact"): "opportunity_has_contact",
        ("opportunity", "company"): "opportunity_has_company",
        ("trade_record", "company"): "trade_record_has_company",
        ("trade_record", "product"): "trade_record_has_product",
        ("activity", "contact"): "activity_has_contact",
    }
    return mappings.get((subject_type, object_type), f"{subject_type}_references_{object_type}")


class PackageBuilder:
    def __init__(self, case_root: Path, source: dict[str, Any], database: Path) -> None:
        self.case_root = case_root.resolve()
        self.source = source
        self.db = sqlite3.connect(database, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=60000")
        self.db.execute("PRAGMA temp_store=MEMORY")
        create_schema(self.db)
        self.counts: Counter[str] = Counter()
        self.record_type_counts: Counter[str] = Counter()
        self.relation_kind_counts: Counter[str] = Counter()
        self.timeline_axis_counts: Counter[str] = Counter()
        self.timeline_status_counts: Counter[str] = Counter()

    def close(self) -> None:
        self.db.close()

    def insert_node(self, node_type: str, value: str) -> str:
        node_id = f"{node_type}:{sha256_bytes(value.encode('utf-8'))}"
        self.db.execute("INSERT OR IGNORE INTO nodes(node_id,node_type,value) VALUES(?,?,?)", (node_id, node_type, value))
        return node_id

    def insert_event(
        self,
        *,
        artifact_id: str,
        record_id: str | None,
        axis: str,
        event_kind: str,
        field_name: str,
        source_rel: str,
        source_line: int | None,
        pointer: str,
        value: Any,
        zone_hint: str | None = None,
    ) -> str:
        normalized = normalize_timestamp(value, zone_hint)
        raw = normalized["timestamp_raw"]
        event_id = sha256_bytes(
            f"{artifact_id}\0{record_id or ''}\0{axis}\0{event_kind}\0{pointer}\0{normalized['timestamp_raw_type']}\0{raw}".encode("utf-8")
        )
        stable = stable_time_key(axis, normalized, event_id)
        self.db.execute(
            """INSERT OR IGNORE INTO timeline_event(
                 event_id,artifact_id,record_id,timeline_axis,event_kind,field_name,source_rel,source_line,
                 json_pointer,timestamp_raw,timestamp_raw_type,timestamp_utc,timestamp_sort_key,
                 local_sort_key,stable_order_key,stable_sequence,stable_sequence_basis,
                 normalization_status,time_basis,timezone_raw,timestamp_precision)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                artifact_id,
                record_id,
                axis,
                event_kind,
                field_name,
                source_rel,
                source_line,
                pointer,
                raw,
                normalized["timestamp_raw_type"],
                normalized["timestamp_utc"],
                normalized["timestamp_sort_key"],
                normalized["local_sort_key"],
                stable,
                0,
                stable_sequence_basis(normalized),
                normalized["normalization_status"],
                normalized["time_basis"],
                normalized["timezone_raw"],
                normalized["timestamp_precision"],
            ),
        )
        self.timeline_axis_counts[axis] += 1
        self.timeline_status_counts[normalized["normalization_status"]] += 1
        return event_id

    def add_relation(
        self,
        *,
        subject_node_id: str,
        predicate: str,
        object_node_id: str,
        relation_kind: str,
        artifact_id: str,
        record_id: str,
        source_rel: str,
        source_line: int | None,
        pointer: str,
        evidence_role: str,
        event_id: str | None,
    ) -> str | None:
        if subject_node_id == object_node_id:
            return None
        relation_id = sha256_bytes(f"{subject_node_id}\0{predicate}\0{object_node_id}".encode("utf-8"))
        self.db.execute(
            "INSERT OR IGNORE INTO explicit_relation(relation_id,subject_node_id,predicate,object_node_id,relation_kind) VALUES(?,?,?,?,?)",
            (relation_id, subject_node_id, predicate, object_node_id, relation_kind),
        )
        evidence_id = sha256_bytes(
            f"{relation_id}\0{artifact_id}\0{record_id}\0{source_line or 0}\0{pointer}\0{evidence_role}".encode("utf-8")
        )
        self.db.execute(
            """INSERT OR IGNORE INTO relation_evidence(
                 evidence_id,relation_id,artifact_id,record_id,event_id,source_rel,source_line,json_pointer,evidence_role)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (evidence_id, relation_id, artifact_id, record_id, event_id, source_rel, source_line, pointer, evidence_role),
        )
        self.relation_kind_counts[relation_kind] += 1
        return relation_id

    def insert_record_row(
        self,
        *,
        artifact_id: str,
        parent: dict[str, Any] | None,
        source_rel: str,
        source_line: int | None,
        pointer_kind: str,
        pointer: str,
        depth: int,
        record_type: str,
        record_sha256: str,
        parse_status: str,
        primary_entity_node_id: str | None,
    ) -> dict[str, Any]:
        record_id = sha256_bytes(f"{artifact_id}\0{pointer_kind}\0{pointer}".encode("utf-8"))
        locator = f"{source_rel}#{pointer_kind}:{pointer}"
        record_node_id = self.insert_node("record", locator)
        self.db.execute(
            """INSERT INTO record_occurrence(
                 record_id,artifact_id,parent_record_id,record_node_id,primary_entity_node_id,source_rel,
                 source_line,pointer_kind,json_pointer,depth,record_type,record_sha256,parse_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record_id,
                artifact_id,
                parent["record_id"] if parent else None,
                record_node_id,
                primary_entity_node_id,
                source_rel,
                source_line,
                pointer_kind,
                pointer,
                depth,
                record_type,
                record_sha256,
                parse_status,
            ),
        )
        self.counts["record_occurrence"] += 1
        self.record_type_counts[record_type] += 1
        return {
            "record_id": record_id,
            "record_node_id": record_node_id,
            "primary_node_id": primary_entity_node_id,
            "primary_type": None,
            "primary_event_id": None,
        }

    @staticmethod
    def event_priority(field_name: str) -> int:
        normalized = normalize_key(field_name)
        if any(token in normalized for token in ("create", "created", "start")):
            return 0
        if any(token in normalized for token in ("send", "receive", "reply", "mail", "order", "trade", "date")):
            return 1
        if any(token in normalized for token in ("update", "modified", "change")):
            return 2
        if any(token in normalized for token in ("end", "expire", "close")):
            return 3
        return 4

    def direct_entities(self, value: dict[str, Any], pointer: str, record_type: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for key, item in value.items():
            kind = infer_id_type(key, pointer, record_type)
            identity = scalar_identity(item)
            if not kind or identity is None or (kind, identity) in seen:
                continue
            seen.add((kind, identity))
            result.append(
                {
                    "type": kind,
                    "value": identity,
                    "node_id": self.insert_node(kind, identity),
                    "field": str(key),
                    "pointer": pointer_join(pointer, key),
                }
            )
        return result

    @staticmethod
    def choose_anchor(entities: list[dict[str, str]], record_type: str) -> dict[str, str] | None:
        for entity in entities:
            if entity["type"] == record_type:
                return entity
        for kind in TYPE_PRIORITY:
            for entity in entities:
                if entity["type"] == kind:
                    return entity
        return entities[0] if entities else None

    def update_record_time_bounds(self, record_id: str) -> None:
        row = self.db.execute(
            "SELECT MIN(timestamp_sort_key),MAX(timestamp_sort_key) FROM timeline_event WHERE record_id=? AND timestamp_sort_key IS NOT NULL",
            (record_id,),
        ).fetchone()
        self.db.execute(
            "UPDATE record_occurrence SET first_event_sort_key=?,last_event_sort_key=? WHERE record_id=?",
            (row[0], row[1], record_id),
        )

    def process_value(
        self,
        *,
        artifact_id: str,
        source_rel: str,
        value: Any,
        pointer: str,
        pointer_kind: str,
        source_line: int | None,
        parent: dict[str, Any],
        depth: int,
    ) -> dict[str, Any] | None:
        if isinstance(value, list):
            info = self.insert_record_row(
                artifact_id=artifact_id,
                parent=parent,
                source_rel=source_rel,
                source_line=source_line,
                pointer_kind=pointer_kind,
                pointer=pointer,
                depth=depth,
                record_type="array",
                record_sha256=canonical_json_hash(value),
                parse_status="parsed_json_array",
                primary_entity_node_id=parent.get("primary_node_id"),
            )
            info["primary_node_id"] = parent.get("primary_node_id")
            info["primary_type"] = parent.get("primary_type")
            self.add_relation(
                subject_node_id=parent["record_node_id"],
                predicate="record_contains_record",
                object_node_id=info["record_node_id"],
                relation_kind="structural",
                artifact_id=artifact_id,
                record_id=info["record_id"],
                source_rel=source_rel,
                source_line=source_line,
                pointer=pointer,
                evidence_role="parent_child",
                event_id=None,
            )
            for index, item in enumerate(value):
                if isinstance(item, (dict, list)):
                    child = self.process_value(
                        artifact_id=artifact_id,
                        source_rel=source_rel,
                        value=item,
                        pointer=pointer_join(pointer, index),
                        pointer_kind=pointer_kind,
                        source_line=source_line,
                        parent=info,
                        depth=depth + 1,
                    )
                    if child and info.get("primary_node_id") and child.get("primary_node_id"):
                        container_role = normalize_key(pointer.rsplit("/", 1)[-1])
                        if info["primary_type"] == "mail" and container_role in {"attachment_list", "attachments"}:
                            predicate = "mail_lists_attachment"
                        elif info["primary_type"] == "mail" and container_role in {"trade_document_list", "document_list", "documents"}:
                            predicate = "mail_has_document"
                        else:
                            predicate = relation_predicate(str(info["primary_type"]), str(child["primary_type"]))
                        self.add_relation(
                            subject_node_id=str(info["primary_node_id"]),
                            predicate=predicate,
                            object_node_id=str(child["primary_node_id"]),
                            relation_kind="business",
                            artifact_id=artifact_id,
                            record_id=child["record_id"],
                            source_rel=source_rel,
                            source_line=source_line,
                            pointer=child["pointer"],
                            evidence_role="nested_record",
                            event_id=child.get("primary_event_id"),
                        )
            info["pointer"] = pointer
            return info

        if not isinstance(value, dict):
            return None

        record_type = infer_record_type(value, pointer)
        entities = self.direct_entities(value, pointer, record_type)
        anchor = self.choose_anchor(entities, record_type)
        info = self.insert_record_row(
            artifact_id=artifact_id,
            parent=parent,
            source_rel=source_rel,
            source_line=source_line,
            pointer_kind=pointer_kind,
            pointer=pointer,
            depth=depth,
            record_type=record_type,
            record_sha256=canonical_json_hash(value),
            parse_status="parsed_json_object",
            primary_entity_node_id=anchor["node_id"] if anchor else None,
        )
        info["primary_node_id"] = anchor["node_id"] if anchor else None
        info["primary_type"] = anchor["type"] if anchor else None
        info["pointer"] = pointer
        if anchor and anchor["type"] == "trade_record":
            scope_row = self.db.execute("SELECT source_scope FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if scope_row and str(scope_row[0]) == "manual_trade":
                self.db.execute(
                    """INSERT OR IGNORE INTO trade_entity(
                         entity_node_id,entity_type,unbound_to_customer,binding_status,source_artifact_id,
                         source_record_id,source_rel,source_line,json_pointer,binding_evidence_pointer)
                       VALUES(?, 'trade_record', 1, 'UNBOUND', ?, ?, ?, ?, ?, NULL)""",
                    (anchor["node_id"], artifact_id, info["record_id"], source_rel, source_line, pointer),
                )

        zone = timezone_hint(value)
        direct_events: list[tuple[int, str]] = []
        for key, item in value.items():
            if not time_field_candidate(key):
                continue
            event_pointer = pointer_join(pointer, key)
            event_id = self.insert_event(
                artifact_id=artifact_id,
                record_id=info["record_id"],
                axis="business",
                event_kind=normalize_key(key) or "time_field",
                field_name=str(key),
                source_rel=source_rel,
                source_line=source_line,
                pointer=event_pointer,
                value=item,
                zone_hint=zone,
            )
            direct_events.append((self.event_priority(str(key)), event_id))
        if direct_events:
            direct_events.sort(key=lambda item: (item[0], item[1]))
            info["primary_event_id"] = direct_events[0][1]
            self.update_record_time_bounds(info["record_id"])

        self.add_relation(
            subject_node_id=parent["record_node_id"],
            predicate="record_contains_record",
            object_node_id=info["record_node_id"],
            relation_kind="structural",
            artifact_id=artifact_id,
            record_id=info["record_id"],
            source_rel=source_rel,
            source_line=source_line,
            pointer=pointer,
            evidence_role="parent_child",
            event_id=info.get("primary_event_id"),
        )
        for entity in entities:
            self.add_relation(
                subject_node_id=info["record_node_id"],
                predicate=f"record_identifies_{entity['type']}",
                object_node_id=entity["node_id"],
                relation_kind="identity",
                artifact_id=artifact_id,
                record_id=info["record_id"],
                source_rel=source_rel,
                source_line=source_line,
                pointer=entity["pointer"],
                evidence_role="identity_field",
                event_id=info.get("primary_event_id"),
            )
        if anchor:
            for entity in entities:
                if entity["node_id"] == anchor["node_id"]:
                    continue
                subject_node_id = anchor["node_id"]
                object_node_id = entity["node_id"]
                predicate = relation_predicate(anchor["type"], entity["type"])
                evidence_role = "direct_id_field"
                field = normalize_key(entity["field"])
                if anchor["type"] == "mail":
                    if field in {"root_mail_id", "thread_root_mail_id"} and entity["type"] == "mail":
                        predicate, evidence_role = "mail_has_thread_root", "mail_thread_root_field"
                    elif field in {"reply_to", "reply_mail_id", "reply_to_mail_id", "in_reply_to_mail_id"} and entity["type"] == "mail":
                        predicate, evidence_role = "mail_replies_to", "mail_reply_field"
                    elif field == "source_mail_id" and entity["type"] == "mail":
                        subject_node_id, object_node_id = entity["node_id"], anchor["node_id"]
                        predicate, evidence_role = "mail_source_to_derived", "mail_source_field"
                    elif field == "folder_id" and entity["type"] == "folder":
                        predicate, evidence_role = "mail_in_folder", "mail_folder_field"
                self.add_relation(
                    subject_node_id=subject_node_id,
                    predicate=predicate,
                    object_node_id=object_node_id,
                    relation_kind="business",
                    artifact_id=artifact_id,
                    record_id=info["record_id"],
                    source_rel=source_rel,
                    source_line=source_line,
                    pointer=entity["pointer"],
                    evidence_role=evidence_role,
                    event_id=info.get("primary_event_id"),
                )

        for key, item in value.items():
            normalized = normalize_key(key)
            if isinstance(item, str) and normalized in {"sender", "from", "receiver", "recipient", "to", "cc", "bcc"}:
                subject = anchor["node_id"] if anchor else info["record_node_id"]
                subject_type = anchor["type"] if anchor else "record"
                for address in EMAIL_RE.findall(item):
                    email_node = self.insert_node("email", address)
                    self.add_relation(
                        subject_node_id=subject,
                        predicate=relation_predicate(subject_type, "email", normalized),
                        object_node_id=email_node,
                        relation_kind="participant",
                        artifact_id=artifact_id,
                        record_id=info["record_id"],
                        source_rel=source_rel,
                        source_line=source_line,
                        pointer=pointer_join(pointer, key),
                        evidence_role=f"email_{normalized}",
                        event_id=info.get("primary_event_id"),
                    )

        for key, item in value.items():
            if not isinstance(item, (dict, list)):
                continue
            child = self.process_value(
                artifact_id=artifact_id,
                source_rel=source_rel,
                value=item,
                pointer=pointer_join(pointer, key),
                pointer_kind=pointer_kind,
                source_line=source_line,
                parent=info,
                depth=depth + 1,
            )
            if child and info.get("primary_node_id") and child.get("primary_node_id"):
                container_role = normalize_key(key)
                if info["primary_type"] == "mail" and container_role in {"attachment_list", "attachments"}:
                    predicate = "mail_lists_attachment"
                elif info["primary_type"] == "mail" and container_role in {"trade_document_list", "document_list", "documents"}:
                    predicate = "mail_has_document"
                else:
                    predicate = relation_predicate(str(info["primary_type"]), str(child["primary_type"]))
                self.add_relation(
                    subject_node_id=str(info["primary_node_id"]),
                    predicate=predicate,
                    object_node_id=str(child["primary_node_id"]),
                    relation_kind="business",
                    artifact_id=artifact_id,
                    record_id=child["record_id"],
                    source_rel=source_rel,
                    source_line=source_line,
                    pointer=child["pointer"],
                    evidence_role="nested_record",
                    event_id=child.get("primary_event_id"),
                )
        return info

    def invalid_record(
        self,
        *,
        artifact_id: str,
        source_rel: str,
        source_line: int | None,
        pointer_kind: str,
        pointer: str,
        parent: dict[str, Any],
        depth: int,
        raw_hash: str,
        status: str,
    ) -> dict[str, Any]:
        info = self.insert_record_row(
            artifact_id=artifact_id,
            parent=parent,
            source_rel=source_rel,
            source_line=source_line,
            pointer_kind=pointer_kind,
            pointer=pointer,
            depth=depth,
            record_type="unparsed_record",
            record_sha256=raw_hash,
            parse_status=status,
            primary_entity_node_id=None,
        )
        info["pointer"] = pointer
        self.add_relation(
            subject_node_id=parent["record_node_id"],
            predicate="record_contains_record",
            object_node_id=info["record_node_id"],
            relation_kind="structural",
            artifact_id=artifact_id,
            record_id=info["record_id"],
            source_rel=source_rel,
            source_line=source_line,
            pointer=pointer,
            evidence_role="unparsed_child",
            event_id=None,
        )
        return info

    def capture_event(
        self,
        *,
        artifact_id: str,
        record_id: str,
        source_rel: str,
        event_kind: str,
        field_name: str,
        pointer: str,
        value: Any,
    ) -> str | None:
        if value is None or str(value) == "":
            return None
        return self.insert_event(
            artifact_id=artifact_id,
            record_id=record_id,
            axis="capture",
            event_kind=event_kind,
            field_name=field_name,
            source_rel=source_rel,
            source_line=None,
            pointer=pointer,
            value=value,
        )

    def add_archive_members(
        self,
        *,
        path: Path,
        artifact_id: str,
        artifact_node: str,
        root: dict[str, Any],
        source_rel: str,
    ) -> None:
        try:
            is_archive = zipfile.is_zipfile(path)
        except OSError:
            is_archive = False
        if not is_archive:
            return
        try:
            with zipfile.ZipFile(path) as archive:
                for index, member in enumerate(archive.infolist()):
                    member_path = str(member.filename)
                    member_id = sha256_bytes(f"{artifact_id}\0{index}\0{member_path}".encode("utf-8"))
                    self.db.execute(
                        """INSERT INTO archive_member(
                             member_id,artifact_id,member_path,member_index,compressed_bytes,uncompressed_bytes,
                             crc32,encrypted,is_directory) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            member_id,
                            artifact_id,
                            member_path,
                            index,
                            int(member.compress_size),
                            int(member.file_size),
                            int(member.CRC),
                            1 if member.flag_bits & 0x1 else 0,
                            1 if member.is_dir() else 0,
                        ),
                    )
                    member_node = self.insert_node("archive_member", f"{source_rel}!/{member_path}")
                    self.add_relation(
                        subject_node_id=artifact_node,
                        predicate="archive_contains_member",
                        object_node_id=member_node,
                        relation_kind="structural",
                        artifact_id=artifact_id,
                        record_id=root["record_id"],
                        source_rel=source_rel,
                        source_line=None,
                        pointer=f"$archive/members/{index}",
                        evidence_role="zip_central_directory",
                        event_id=None,
                    )
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise BuildError("ARCHIVE_MEMBER_READ_FAILED") from exc

    def add_manual_trade_candidate(
        self,
        *,
        artifact_id: str,
        artifact_node: str,
        root: dict[str, Any],
        source_rel: str,
        source_scope: str,
    ) -> None:
        if source_scope != "manual_trade":
            return
        match = re.search(r"(?:^|[/_])candidate[_-]?(\d+)(?:[/_.-]|$)", source_rel, re.I)
        if not match:
            return
        parts = PurePosixPath(source_rel).parts
        session = next((part for index, part in enumerate(parts) if part == "manual_trade" and index + 1 < len(parts) for part in [parts[index + 1]]), "manual_trade")
        candidate_key = f"{session}:candidate_{int(match.group(1)):04d}"
        candidate_node = self.insert_node("trade_candidate", candidate_key)
        self.db.execute(
            """INSERT OR IGNORE INTO trade_entity(
                 entity_node_id,entity_type,unbound_to_customer,binding_status,source_artifact_id,
                 source_record_id,source_rel,source_line,json_pointer,binding_evidence_pointer)
               VALUES(?, 'trade_candidate', 1, 'UNBOUND', ?, ?, ?, NULL, '$artifact', NULL)""",
            (candidate_node, artifact_id, root["record_id"], source_rel),
        )
        self.add_relation(
            subject_node_id=candidate_node,
            predicate="trade_candidate_has_artifact",
            object_node_id=artifact_node,
            relation_kind="structural",
            artifact_id=artifact_id,
            record_id=root["record_id"],
            source_rel=source_rel,
            source_line=None,
            pointer="$artifact",
            evidence_role="manual_trade_candidate_path",
            event_id=None,
        )

    def add_derived_artifact(
        self,
        *,
        task: dict[str, Any],
        artifact_id: str,
        artifact_node: str,
        root: dict[str, Any],
        source_rel: str,
    ) -> None:
        if str(task.get("state") or "completed") != "completed":
            return
        output_rel = task.get("output_rel")
        output_sha = task.get("result_sha256")
        output_bytes = task.get("output_bytes")
        output_kind = task.get("output_kind")
        output_file_count = task.get("output_file_count")
        if not output_rel or not output_sha or output_bytes is None or output_kind not in {"file", "directory"} or output_file_count is None:
            return
        derived_id = sha256_bytes(f"{task['task_id']}\0{output_rel}\0{output_sha}".encode("utf-8"))
        self.db.execute(
            """INSERT INTO derived_artifacts(
                 derived_id,task_id,source_artifact_id,source_rel,output_rel,output_sha256,
                 output_kind,output_bytes,output_file_count,task_kind,result_hash_verified)
               VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
            (
                derived_id,
                str(task["task_id"]),
                artifact_id,
                source_rel,
                output_rel,
                output_sha,
                output_kind,
                int(output_bytes),
                int(output_file_count),
                str(task["task_kind"]),
            ),
        )
        derived_node = self.insert_node("derived_artifact", output_rel)
        self.add_relation(
            subject_node_id=artifact_node,
            predicate="source_artifact_derived_to_output",
            object_node_id=derived_node,
            relation_kind="lineage",
            artifact_id=artifact_id,
            record_id=root["record_id"],
            source_rel=source_rel,
            source_line=None,
            pointer=f"$task/{pointer_escape(task['task_id'])}/output_rel",
            evidence_role="verified_task_output",
            event_id=None,
        )

    def add_source_gap_task(
        self,
        *,
        task: dict[str, Any],
        artifact_id: str,
        artifact_node: str,
        root: dict[str, Any],
        source_rel: str,
    ) -> None:
        if str(task.get("state") or "") != "source_gap":
            return
        output_rel = str(task.get("output_rel") or "")
        output_sha = str(task.get("result_sha256") or "")
        output_kind = str(task.get("output_kind") or "")
        error_code = str(task.get("error_code") or "SOURCE_GAP")
        require(output_rel != "" and SHA_RE.fullmatch(output_sha) is not None, "SOURCE_GAP_OUTPUT_INVALID")
        require(output_kind in {"file", "directory"}, "SOURCE_GAP_OUTPUT_KIND_INVALID")
        gap_node = self.insert_node("source_gap", str(task["task_id"]))
        self.db.execute(
            """INSERT INTO source_gap_task(
                 task_id,source_artifact_id,gap_node_id,source_rel,output_rel,output_sha256,output_kind,
                 output_bytes,output_file_count,task_kind,error_code,updated_at,result_hash_verified)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                str(task["task_id"]),
                artifact_id,
                gap_node,
                source_rel,
                output_rel,
                output_sha,
                output_kind,
                int(task["output_bytes"]),
                int(task["output_file_count"]),
                str(task["task_kind"]),
                error_code,
                str(task["updated_at"]),
            ),
        )
        self.add_relation(
            subject_node_id=artifact_node,
            predicate="source_artifact_has_source_gap",
            object_node_id=gap_node,
            relation_kind="gap",
            artifact_id=artifact_id,
            record_id=root["record_id"],
            source_rel=source_rel,
            source_line=None,
            pointer=f"$task/{pointer_escape(task['task_id'])}/error_code",
            evidence_role="terminal_source_gap",
            event_id=None,
        )
    def build_artifact(self, row: dict[str, Any], task_rows: list[dict[str, Any]]) -> None:
        path: Path = row["path"]
        source_rel = row["source_rel"]
        artifact_id = sha256_bytes(f"{source_rel}\0{row['sha256']}".encode("utf-8"))
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.db.execute(
            """INSERT INTO artifacts(
                 artifact_id,source_rel,source_scope,source_sha256,source_bytes,magic,category,mime,
                 prepared_at_raw,file_mtime_utc,manifest_record_json,current_hash_verified)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                artifact_id,
                source_rel,
                row["source_scope"],
                row["sha256"],
                row["bytes"],
                row["magic"],
                row["category"],
                row["mime"],
                row["prepared_at"],
                mtime,
                row.get("manifest_record_json"),
            ),
        )
        self.counts["artifacts"] += 1
        artifact_node = self.insert_node("artifact", source_rel)
        root = self.insert_record_row(
            artifact_id=artifact_id,
            parent=None,
            source_rel=source_rel,
            source_line=None,
            pointer_kind="artifact",
            pointer="$artifact",
            depth=0,
            record_type="artifact_document",
            record_sha256=row["sha256"],
            parse_status="artifact_hash_verified",
            primary_entity_node_id=artifact_node,
        )
        root["primary_node_id"] = artifact_node
        root["primary_type"] = "artifact"
        root["pointer"] = "$artifact"
        self.add_archive_members(
            path=path,
            artifact_id=artifact_id,
            artifact_node=artifact_node,
            root=root,
            source_rel=source_rel,
        )
        self.add_manual_trade_candidate(
            artifact_id=artifact_id,
            artifact_node=artifact_node,
            root=root,
            source_rel=source_rel,
            source_scope=str(row["source_scope"]),
        )
        prepared_event = self.capture_event(
            artifact_id=artifact_id,
            record_id=root["record_id"],
            source_rel=source_rel,
            event_kind="artifact_prepared",
            field_name="prepared_at",
            pointer="$artifact/prepared_at",
            value=row["prepared_at"],
        )
        self.capture_event(
            artifact_id=artifact_id,
            record_id=root["record_id"],
            source_rel=source_rel,
            event_kind="source_file_mtime",
            field_name="file_mtime_utc",
            pointer="$artifact/file_mtime_utc",
            value=mtime,
        )
        for task in task_rows:
            self.capture_event(
                artifact_id=artifact_id,
                record_id=root["record_id"],
                source_rel=source_rel,
                event_kind="task_updated",
                field_name="updated_at",
                pointer=f"$task/{pointer_escape(task['task_id'])}/updated_at",
                value=task["updated_at"],
            )
            self.add_derived_artifact(
                task=task,
                artifact_id=artifact_id,
                artifact_node=artifact_node,
                root=root,
                source_rel=source_rel,
            )
            self.add_source_gap_task(
                task=task,
                artifact_id=artifact_id,
                artifact_node=artifact_node,
                root=root,
                source_rel=source_rel,
            )
        self.update_record_time_bounds(root["record_id"])
        self.add_relation(
            subject_node_id=artifact_node,
            predicate="artifact_contains_record",
            object_node_id=root["record_node_id"],
            relation_kind="structural",
            artifact_id=artifact_id,
            record_id=root["record_id"],
            source_rel=source_rel,
            source_line=None,
            pointer="$artifact",
            evidence_role="artifact_root",
            event_id=prepared_event,
        )
        lineage_id = sha256_bytes(f"artifact\0{artifact_id}".encode("utf-8"))
        self.db.execute(
            """INSERT INTO lineage(
                 lineage_id,source_kind,source_path,source_sha256,source_bytes,source_artifact_id,
                 target_table,target_key,derivation,recorded_at_utc)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                lineage_id,
                row["source_scope"],
                source_rel,
                row["sha256"],
                row["bytes"],
                artifact_id,
                "artifacts/record_occurrence/timeline_event/relation_evidence",
                artifact_id,
                "sha256-verified local parse",
                utc_now(),
            ),
        )

        mode = "opaque"
        if path.suffix.casefold() == ".jsonl":
            mode = "jsonl"
        elif path.suffix.casefold() == ".json":
            mode = "json"
        else:
            with path.open("rb") as handle:
                sample = handle.read(4096).lstrip(b"\xef\xbb\xbf \t\r\n")
            if sample.startswith((b"{", b"[")):
                mode = "json"
        if mode == "jsonl":
            try:
                with path.open("r", encoding="utf-8-sig") as handle:
                    for physical_line, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        pointer = f"/line/{physical_line}"
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError:
                            self.invalid_record(
                                artifact_id=artifact_id,
                                source_rel=source_rel,
                                source_line=physical_line,
                                pointer_kind="jsonl_pointer",
                                pointer=pointer,
                                parent=root,
                                depth=1,
                                raw_hash=sha256_bytes(line.encode("utf-8")),
                                status="invalid_jsonl_line",
                            )
                            continue
                        if isinstance(value, (dict, list)):
                            self.process_value(
                                artifact_id=artifact_id,
                                source_rel=source_rel,
                                value=value,
                                pointer=pointer,
                                pointer_kind="jsonl_pointer",
                                source_line=physical_line,
                                parent=root,
                                depth=1,
                            )
                        else:
                            self.invalid_record(
                                artifact_id=artifact_id,
                                source_rel=source_rel,
                                source_line=physical_line,
                                pointer_kind="jsonl_pointer",
                                pointer=pointer,
                                parent=root,
                                depth=1,
                                raw_hash=canonical_json_hash(value),
                                status="parsed_json_scalar",
                            )
            except (OSError, UnicodeError) as exc:
                raise BuildError("JSONL_READ_FAILED") from exc
        elif mode == "json":
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                self.invalid_record(
                    artifact_id=artifact_id,
                    source_rel=source_rel,
                    source_line=None,
                    pointer_kind="json_pointer",
                    pointer="",
                    parent=root,
                    depth=1,
                    raw_hash=row["sha256"],
                    status="invalid_json_document",
                )
            else:
                if isinstance(value, (dict, list)):
                    self.process_value(
                        artifact_id=artifact_id,
                        source_rel=source_rel,
                        value=value,
                        pointer="",
                        pointer_kind="json_pointer",
                        source_line=None,
                        parent=root,
                        depth=1,
                    )
                else:
                    self.invalid_record(
                        artifact_id=artifact_id,
                        source_rel=source_rel,
                        source_line=None,
                        pointer_kind="json_pointer",
                        pointer="",
                        parent=root,
                        depth=1,
                        raw_hash=canonical_json_hash(value),
                        status="parsed_json_scalar",
                    )

    def import_mail_capture_manifest(self, manifest: dict[str, Any]) -> dict[str, int]:
        manifest_id = sha256_bytes(f"mail-capture\0{manifest['sha256']}".encode("utf-8"))
        # The relationship is asserted by the frozen manifest, not by the binary attachment.
        manifest_artifact_id = sha256_bytes(f"{manifest['source_rel']}\0{manifest['sha256']}".encode("utf-8"))
        self.db.execute(
            """INSERT INTO artifacts(artifact_id,source_rel,source_scope,source_sha256,source_bytes,
                 magic,category,mime,prepared_at_raw,file_mtime_utc,manifest_record_json,current_hash_verified)
               VALUES(?,?,?,?,?,'json','json','application/json','','',NULL,1)""",
            (manifest_artifact_id, manifest["source_rel"], "relation_provenance_manifest", manifest["sha256"], manifest["bytes"]),
        )
        self.counts["artifacts"] += 1
        self.db.execute(
            """INSERT INTO lineage(lineage_id,source_kind,source_path,source_sha256,source_bytes,
                 source_artifact_id,target_table,target_key,derivation,recorded_at_utc)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sha256_bytes(f"manifest-artifact\0{manifest['sha256']}".encode("utf-8")),
             "frozen_mail_capture_manifest_artifact", manifest["source_rel"], manifest["sha256"], manifest["bytes"],
             manifest_artifact_id, "artifacts/record_occurrence", manifest_artifact_id,
             "sha256-verified manifest provenance artifact", utc_now()),
        )
        manifest_node = self.insert_node("artifact", manifest["source_rel"])
        manifest_root = self.insert_record_row(
            artifact_id=manifest_artifact_id, parent=None, source_rel=manifest["source_rel"],
            source_line=None, pointer_kind="artifact", pointer="$artifact", depth=0,
            record_type="artifact_document", record_sha256=manifest["sha256"],
            parse_status="artifact_hash_verified", primary_entity_node_id=manifest_node,
        )
        self.add_relation(subject_node_id=manifest_node, predicate="artifact_contains_record",
                          object_node_id=manifest_root["record_node_id"], relation_kind="structural",
                          artifact_id=manifest_artifact_id, record_id=manifest_root["record_id"],
                          source_rel=manifest["source_rel"], source_line=None, pointer="$artifact",
                          evidence_role="manifest_document", event_id=None)
        self.db.execute(
            """INSERT INTO source_manifest(
                 manifest_id,source_rel,source_sha256,source_bytes,manifest_schema,manifest_status,current_hash_verified)
               VALUES(?,?,?,?,?,?,1)""",
            (
                manifest_id,
                manifest["source_rel"],
                manifest["sha256"],
                int(manifest["bytes"]),
                manifest["schema"],
                manifest["status"],
            ),
        )
        occurrences = 0
        for mail_ordinal, mail in enumerate(manifest["mails"], 1):
            require(isinstance(mail, dict), "MAIL_CAPTURE_MAIL_INVALID")
            mail_identity = scalar_identity(mail.get("mail_id"))
            require(mail_identity is not None, "MAIL_CAPTURE_MAIL_ID_MISSING")
            mail_node = self.insert_node("mail", mail_identity)
            resources = mail.get("related_resources")
            require(isinstance(resources, list), "MAIL_CAPTURE_RESOURCES_INVALID")
            for resource_ordinal, resource in enumerate(resources, 1):
                require(isinstance(resource, dict), "MAIL_CAPTURE_RESOURCE_INVALID")
                artifact = resource.get("artifact")
                require(isinstance(artifact, dict), "MAIL_CAPTURE_RESOURCE_ARTIFACT_MISSING")
                source_rel = safe_relative(artifact.get("relative_path"), "MAIL_ATTACHMENT_PATH_INVALID")
                artifact_sha = normalize_sha(artifact.get("sha256"), "MAIL_ATTACHMENT_SHA_INVALID")
                artifact_row = self.db.execute(
                    "SELECT artifact_id,source_sha256 FROM artifacts WHERE source_rel=?",
                    (source_rel,),
                ).fetchone()
                require(artifact_row is not None, "MAIL_ATTACHMENT_ARTIFACT_NOT_IMPORTED")
                require(str(artifact_row["source_sha256"]) == artifact_sha, "MAIL_ATTACHMENT_ARTIFACT_HASH_MISMATCH")
                artifact_id = str(artifact_row["artifact_id"])
                root = self.db.execute(
                    "SELECT record_id FROM record_occurrence WHERE artifact_id=? AND pointer_kind='artifact'",
                    (artifact_id,),
                ).fetchone()
                require(root is not None, "MAIL_ATTACHMENT_ROOT_RECORD_MISSING")
                attachment_node = self.insert_node("attachment", artifact_sha)
                pointer = f"/mails/{mail_ordinal - 1}/related_resources/{resource_ordinal - 1}"
                provenance = self.insert_record_row(
                    artifact_id=manifest_artifact_id, parent=manifest_root,
                    source_rel=manifest["source_rel"], source_line=None,
                    pointer_kind="json_pointer", pointer=pointer, depth=1,
                    record_type="mail_attachment_manifest_occurrence", record_sha256=canonical_json_hash(resource),
                    parse_status="parsed_json_object", primary_entity_node_id=mail_node,
                )
                self.add_relation(subject_node_id=manifest_node, predicate="artifact_contains_record",
                                  object_node_id=provenance["record_node_id"], relation_kind="structural",
                                  artifact_id=manifest_artifact_id, record_id=provenance["record_id"],
                                  source_rel=manifest["source_rel"], source_line=None, pointer=pointer,
                                  evidence_role="manifest_occurrence", event_id=None)
                occurrence_id = sha256_bytes(f"{manifest_id}\0{mail_ordinal}\0{resource_ordinal}".encode("utf-8"))
                resolution = str(resource.get("resolution") or "")
                self.db.execute(
                    """INSERT INTO attachment_occurrence(
                         occurrence_id,manifest_id,mail_node_id,attachment_node_id,artifact_id,
                         source_mail_ordinal,source_resource_ordinal,source_pointer,artifact_source_rel,
                         artifact_sha256,resolution) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        occurrence_id,
                        manifest_id,
                        mail_node,
                        attachment_node,
                        artifact_id,
                        mail_ordinal,
                        resource_ordinal,
                        pointer,
                        source_rel,
                        artifact_sha,
                        resolution,
                    ),
                )
                self.add_relation(
                    subject_node_id=mail_node,
                    predicate="mail_has_attachment",
                    object_node_id=attachment_node,
                    relation_kind="business",
                    artifact_id=manifest_artifact_id,
                    record_id=provenance["record_id"],
                    source_rel=manifest["source_rel"],
                    source_line=None,
                    pointer=pointer,
                    evidence_role="mail_capture_manifest_occurrence",
                    event_id=None,
                )
                occurrences += 1
        self.db.execute(
            """INSERT INTO lineage(
                 lineage_id,source_kind,source_path,source_sha256,source_bytes,source_artifact_id,
                 target_table,target_key,derivation,recorded_at_utc)
               VALUES(?,?,?,?,?,NULL,?,?,?,?)""",
            (
                sha256_bytes(f"mail-manifest\0{manifest['sha256']}".encode("utf-8")),
                "frozen_mail_capture_manifest",
                manifest["source_rel"],
                manifest["sha256"],
                int(manifest["bytes"]),
                "attachment_occurrence/explicit_relation/relation_evidence",
                manifest_id,
                "sha256-verified frozen manifest import",
                utc_now(),
            ),
        )
        unique_relations = int(
            self.db.execute("SELECT COUNT(*) FROM explicit_relation WHERE predicate='mail_has_attachment'").fetchone()[0]
        )
        multiparent = int(
            self.db.execute(
                """SELECT COUNT(*) FROM (
                     SELECT attachment_node_id FROM attachment_occurrence
                     GROUP BY attachment_node_id HAVING COUNT(DISTINCT mail_node_id)>1)"""
            ).fetchone()[0]
        )
        return {"occurrences": occurrences, "unique_relations": unique_relations, "multiparent": multiparent}

    def finalize(
        self,
        company_id: str,
        *,
        expected_attachment_occurrences: int,
        expected_mail_attachment_relations: int,
        expected_multiparent_attachments: int,
    ) -> dict[str, Any]:
        self.db.execute(
            """WITH ranked AS (
                 SELECT event_id,ROW_NUMBER() OVER(PARTITION BY timeline_axis ORDER BY stable_order_key,event_id) AS seq
                 FROM timeline_event)
               UPDATE timeline_event SET stable_sequence=(SELECT seq FROM ranked WHERE ranked.event_id=timeline_event.event_id)"""
        )
        self.db.execute(
            """UPDATE explicit_relation AS r SET
                 evidence_count=(SELECT COUNT(*) FROM relation_evidence e WHERE e.relation_id=r.relation_id),
                 first_event_sort_key=(SELECT MIN(t.timestamp_sort_key) FROM relation_evidence e JOIN timeline_event t ON t.event_id=e.event_id WHERE e.relation_id=r.relation_id),
                 last_event_sort_key=(SELECT MAX(t.timestamp_sort_key) FROM relation_evidence e JOIN timeline_event t ON t.event_id=e.event_id WHERE e.relation_id=r.relation_id)"""
        )
        source_database = self.source["database"]
        self.db.execute(
            """INSERT INTO lineage(
                 lineage_id,source_kind,source_path,source_sha256,source_bytes,source_artifact_id,
                 target_table,target_key,derivation,recorded_at_utc)
               VALUES(?,?,?,?,?,NULL,?,?,?,?)""",
            (
                sha256_bytes(f"source-package\0{self.source['database_sha256']}".encode("utf-8")),
                "unredacted_v1_database",
                str(source_database),
                self.source["database_sha256"],
                source_database.stat().st_size,
                "all",
                SCHEMA,
                "v2 supplemental transformation",
                utc_now(),
            ),
        )
        counts = {
            name: int(self.db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in PARQUET_TABLES
        }
        counts["business_relations"] = int(self.db.execute("SELECT COUNT(*) FROM explicit_relation WHERE relation_kind='business'").fetchone()[0])
        counts["structural_relations"] = int(self.db.execute("SELECT COUNT(*) FROM explicit_relation WHERE relation_kind='structural'").fetchone()[0])
        counts["business_timeline"] = int(self.db.execute("SELECT COUNT(*) FROM timeline_event WHERE timeline_axis='business'").fetchone()[0])
        counts["capture_timeline"] = int(self.db.execute("SELECT COUNT(*) FROM timeline_event WHERE timeline_axis='capture'").fetchone()[0])
        counts["mail_attachment_occurrences"] = int(self.db.execute("SELECT COUNT(*) FROM attachment_occurrence").fetchone()[0])
        counts["mail_attachment_relations"] = int(self.db.execute("SELECT COUNT(*) FROM explicit_relation WHERE predicate='mail_has_attachment'").fetchone()[0])
        counts["multiparent_attachments"] = int(
            self.db.execute(
                "SELECT COUNT(*) FROM (SELECT attachment_node_id FROM attachment_occurrence GROUP BY attachment_node_id HAVING COUNT(DISTINCT mail_node_id)>1)"
            ).fetchone()[0]
        )
        require(counts["artifacts"] > 0, "NO_ARTIFACTS")
        require(counts["record_occurrence"] >= counts["artifacts"], "RECORD_COVERAGE_EMPTY")
        require(counts["structural_relations"] > 0, "STRUCTURAL_RELATIONS_EMPTY")
        require(counts["business_relations"] > 0, "BUSINESS_RELATIONS_EMPTY")
        require(counts["business_timeline"] > 0 and counts["capture_timeline"] > 0, "DUAL_TIMELINE_INCOMPLETE")
        require(counts["mail_attachment_occurrences"] == expected_attachment_occurrences, "ATTACHMENT_OCCURRENCE_GATE_FAILED")
        require(counts["mail_attachment_relations"] == expected_mail_attachment_relations, "MAIL_ATTACHMENT_RELATION_GATE_FAILED")
        require(counts["multiparent_attachments"] == expected_multiparent_attachments, "MULTIPARENT_ATTACHMENT_GATE_FAILED")
        meta = {
            "schema": SCHEMA,
            "company_id": company_id,
            "built_at_utc": utc_now(),
            "source_package_sha256": self.source["database_sha256"],
            **{f"count_{key}": str(value) for key, value in counts.items()},
        }
        self.db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", sorted(meta.items()))
        self.db.commit()
        require(str(self.db.execute("PRAGMA integrity_check").fetchone()[0]) == "ok", "OUTPUT_SQLITE_INTEGRITY_FAILED")
        require(not list(self.db.execute("PRAGMA foreign_key_check")), "OUTPUT_FOREIGN_KEY_FAILED")
        return counts


def load_artifact_inputs(
    case_root: Path,
    source_database: Path,
    workers: int,
    authoritative_database: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    manual_source = open_readonly(source_database)
    try:
        manual = {
            str(row[0]): str(row[1])
            for row in manual_source.execute("SELECT case_relative_path,record_json FROM manual_trade_artifacts")
        }
    finally:
        manual_source.close()
    source = open_readonly(authoritative_database or source_database)
    try:
        file_columns = {str(row[1]) for row in source.execute("PRAGMA table_info(files)")}
        active_filter = " WHERE active=1" if "active" in file_columns else ""
        rows: list[dict[str, Any]] = []
        for row in source.execute(
            f"SELECT source_rel,source_scope,sha256,bytes,magic,category,mime,prepared_at FROM files{active_filter} ORDER BY source_rel"
        ):
            rel = safe_relative(row["source_rel"], "SOURCE_ARTIFACT_PATH_INVALID")
            path = resolve_inside(case_root, rel, "SOURCE_ARTIFACT_PATH_ESCAPE")
            rows.append(
                {
                    "source_rel": rel,
                    "source_scope": str(row["source_scope"]),
                    "sha256": normalize_sha(row["sha256"], "SOURCE_ARTIFACT_SHA_INVALID"),
                    "bytes": parse_int(row["bytes"], "SOURCE_ARTIFACT_BYTES_INVALID"),
                    "magic": str(row["magic"]),
                    "category": str(row["category"]),
                    "mime": str(row["mime"]),
                    "prepared_at": str(row["prepared_at"]),
                    "manifest_record_json": manual.get(rel),
                    "path": path,
                }
            )
        tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)
        task_columns = {str(row[1]) for row in source.execute("PRAGMA table_info(tasks)")}
        require({"task_id", "source_rel", "task_kind", "updated_at", "output_rel", "result_sha256"}.issubset(task_columns), "SOURCE_TASK_SCHEMA_INCOMPLETE")
        state_expr = "state" if "state" in task_columns else "'completed' AS state"
        error_expr = "error_code" if "error_code" in task_columns else "'' AS error_code"
        task_filter = " WHERE active=1" if "active" in task_columns else ""
        for row in source.execute(
            f"SELECT task_id,source_rel,task_kind,updated_at,output_rel,result_sha256,{state_expr},{error_expr} FROM tasks{task_filter} ORDER BY source_rel,task_id"
        ):
            item = dict(row)
            require(str(item["state"]) in {"completed", "source_gap"}, "SOURCE_TASK_NOT_TERMINAL")
            output_rel = safe_relative(item["output_rel"], "TASK_OUTPUT_PATH_INVALID")
            output_path = resolve_inside(case_root, output_rel, "TASK_OUTPUT_PATH_ESCAPE")
            output_sha = normalize_sha(item["result_sha256"], "TASK_OUTPUT_SHA_INVALID")
            require(output_path.exists(), "TASK_OUTPUT_MISSING")
            require(stable_tree_hash(output_path) == output_sha, "TASK_OUTPUT_HASH_MISMATCH")
            output_kind, output_bytes, output_file_count = output_stats(output_path)
            item["output_rel"] = output_rel
            item["result_sha256"] = output_sha
            item["output_kind"] = output_kind
            item["output_bytes"] = output_bytes
            item["output_file_count"] = output_file_count
            tasks[str(row["source_rel"])].append(item)
    finally:
        source.close()

    def verify(row: dict[str, Any]) -> str:
        path: Path = row["path"]
        if not path.is_file():
            return "SOURCE_ARTIFACT_MISSING"
        if path.stat().st_size != row["bytes"]:
            return "SOURCE_ARTIFACT_SIZE_MISMATCH"
        if sha256_file(path) != row["sha256"]:
            return "SOURCE_ARTIFACT_HASH_MISMATCH"
        return "PASS"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        results = list(executor.map(verify, rows))
    for result in results:
        require(result == "PASS", result)
    return rows, tasks


def xml_text(value: Any) -> str:
    text = str(value)
    cleaned = "".join(
        character
        if character in "\t\n\r" or 0x20 <= ord(character) <= 0xD7FF or 0xE000 <= ord(character) <= 0xFFFD or 0x10000 <= ord(character) <= 0x10FFFF
        else "\uFFFD"
        for character in text
    )
    return escape(cleaned)


def write_graphml(database: Path, target: Path) -> dict[str, int]:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    db = open_readonly(database)
    nodes = edges = 0
    try:
        raw = temporary.open("wb")
        try:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as output:
                    output.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                    output.write('<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n')
                    output.write('<key id="n0" for="node" attr.name="node_type" attr.type="string"/>\n')
                    output.write('<key id="n1" for="node" attr.name="value" attr.type="string"/>\n')
                    output.write('<key id="e0" for="edge" attr.name="predicate" attr.type="string"/>\n')
                    output.write('<key id="e1" for="edge" attr.name="relation_kind" attr.type="string"/>\n')
                    output.write('<key id="e2" for="edge" attr.name="evidence_count" attr.type="long"/>\n')
                    output.write('<key id="e3" for="edge" attr.name="first_event_sort_key" attr.type="long"/>\n')
                    output.write('<key id="e4" for="edge" attr.name="last_event_sort_key" attr.type="long"/>\n')
                    output.write('<graph id="explicit_relationships_v2" edgedefault="directed">\n')
                    for row in db.execute("SELECT node_id,node_type,value FROM nodes ORDER BY node_id"):
                        output.write(
                            f'<node id={quoteattr(str(row["node_id"]))}><data key="n0">{xml_text(row["node_type"])}</data>'
                            f'<data key="n1">{xml_text(row["value"])}</data></node>\n'
                        )
                        nodes += 1
                    for row in db.execute(
                        "SELECT relation_id,subject_node_id,object_node_id,predicate,relation_kind,evidence_count,first_event_sort_key,last_event_sort_key FROM explicit_relation ORDER BY relation_id"
                    ):
                        first = "" if row["first_event_sort_key"] is None else f'<data key="e3">{int(row["first_event_sort_key"])}</data>'
                        last = "" if row["last_event_sort_key"] is None else f'<data key="e4">{int(row["last_event_sort_key"])}</data>'
                        output.write(
                            f'<edge id={quoteattr(str(row["relation_id"]))} source={quoteattr(str(row["subject_node_id"]))} target={quoteattr(str(row["object_node_id"]))}>'
                            f'<data key="e0">{xml_text(row["predicate"])}</data><data key="e1">{xml_text(row["relation_kind"])}</data>'
                            f'<data key="e2">{int(row["evidence_count"])}</data>{first}{last}</edge>\n'
                        )
                        edges += 1
                    output.write("</graph>\n</graphml>\n")
            raw.flush()
            os.fsync(raw.fileno())
        finally:
            raw.close()
    finally:
        db.close()
    os.replace(temporary, target)
    return {"nodes": nodes, "edges": edges}


def python_candidates(explicit: str | None = None) -> list[str]:
    result: list[str] = []
    if explicit:
        result.append(explicit)
    environment = os.environ.get("OKKI_DUCKDB_PYTHON")
    if environment:
        result.append(environment)
    # EN: Use a caller-selected interpreter or current environment, never private runtimes.
    # 中文：只使用调用者选定解释器或当前环境，不发现私有运行时。
    result.append(sys.executable)
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in result:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def find_duckdb_python(explicit: str | None = None) -> str:
    for candidate in python_candidates(explicit):
        if not Path(candidate).is_file():
            continue
        try:
            process = subprocess.run(
                [candidate, "-c", "import duckdb; print(duckdb.__version__)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if process.returncode == 0:
            return candidate
    raise DependencyMissing("DUCKDB_DEPENDENCY_MISSING")


def internal_export(database: Path, output_dir: Path) -> int:
    import duckdb  # type: ignore

    output_dir.mkdir(parents=True, exist_ok=True)
    engine = duckdb.connect()
    counts: dict[str, int] = {}
    # EN: Resource caps are installation configuration, not a specific workstation profile.
    # 中文：资源上限是安装配置，不沿用特定生产工作站的配置。
    threads = int(os.environ.get("CRM_DUCKDB_THREADS", min(8, os.cpu_count() or 1)))
    memory = os.environ.get("CRM_DUCKDB_MEMORY_LIMIT", "4GB")
    require(1 <= threads <= (os.cpu_count() or 1), "DUCKDB_THREADS_INVALID")
    require(re.fullmatch(r"[1-9]\d*(?:MB|GB)", memory, re.I) is not None, "DUCKDB_MEMORY_LIMIT_INVALID")
    try:
        engine.execute(f"SET threads={threads}")
        engine.execute(f"SET memory_limit='{memory}'")
        engine.execute("SET preserve_insertion_order=false")
        engine.execute("LOAD sqlite")
        database_literal = str(database.resolve()).replace("'", "''")
        for table in PARQUET_TABLES:
            destination = output_dir / f"{table}.parquet"
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            query = f"SELECT * FROM sqlite_scan('{database_literal}','{table}')"
            engine.execute(
                f"COPY ({query}) TO '{str(temporary).replace("'", "''")}' (FORMAT PARQUET,COMPRESSION ZSTD,ROW_GROUP_SIZE 122880)"
            )
            os.replace(temporary, destination)
            counts[table] = int(engine.execute(f"SELECT COUNT(*) FROM read_parquet('{str(destination).replace("'", "''")}')").fetchone()[0])
    finally:
        engine.close()
    atomic_json(
        output_dir / "export_receipt.json",
        {
            "schema": "okki.single_customer.relation_timeline.parquet.v2",
            "status": "PASS",
            "duckdb_version": duckdb.__version__,
            "settings": {"threads": threads, "memory_limit": memory},
            "row_counts": counts,
        },
    )
    return 0


def export_parquet(database: Path, output_dir: Path, expected: dict[str, int], explicit_python: str | None) -> str:
    interpreter = find_duckdb_python(explicit_python)
    process = subprocess.run(
        [interpreter, str(Path(__file__).resolve()), "internal-export", "--database", str(database), "--output-dir", str(output_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=None,
        check=False,
    )
    require(process.returncode == 0, "PARQUET_EXPORT_FAILED")
    receipt = read_json(output_dir / "export_receipt.json", "PARQUET_RECEIPT")
    require(receipt.get("status") == "PASS", "PARQUET_RECEIPT_NOT_PASS")
    row_counts = receipt.get("row_counts")
    require(isinstance(row_counts, dict), "PARQUET_RECEIPT_INVALID")
    for table in PARQUET_TABLES:
        require(parse_int(row_counts.get(table), "PARQUET_ROW_COUNT_INVALID") == expected[table], "PARQUET_ROW_COUNT_MISMATCH")
        path = output_dir / f"{table}.parquet"
        require(path.is_file() and path.stat().st_size >= 8, "PARQUET_FILE_MISSING")
        with path.open("rb") as handle:
            require(handle.read(4) == b"PAR1", "PARQUET_HEADER_INVALID")
            handle.seek(-4, os.SEEK_END)
            require(handle.read(4) == b"PAR1", "PARQUET_FOOTER_INVALID")
    return interpreter


def resolve_json_pointer(value: Any, pointer: str, pointer_kind: str, jsonl_rows: dict[int, Any] | None = None) -> Any:
    if pointer_kind == "artifact":
        raise KeyError(pointer)
    parts = pointer.split("/")[1:] if pointer.startswith("/") else ([] if pointer == "" else ["__invalid__"])
    current = value
    if pointer_kind == "jsonl_pointer":
        require(len(parts) >= 2 and parts[0] == "line" and parts[1].isdigit(), "JSONL_POINTER_INVALID")
        line_number = int(parts[1])
        if jsonl_rows is None or line_number not in jsonl_rows:
            raise KeyError(pointer)
        current = jsonl_rows[line_number]
        parts = parts[2:]
    for encoded in parts:
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and 0 <= int(token) < len(current):
            current = current[int(token)]
        else:
            raise KeyError(pointer)
    return current


def load_source_structure(path: Path, pointer_kinds: set[str]) -> tuple[Any, dict[int, Any] | None]:
    plain: Any = None
    lines: dict[int, Any] | None = None
    if "json_pointer" in pointer_kinds:
        plain = json.loads(path.read_text(encoding="utf-8-sig"))
    if "jsonl_pointer" in pointer_kinds:
        lines = {}
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if line.strip():
                    try:
                        lines[line_number] = json.loads(line)
                    except json.JSONDecodeError:
                        continue
    return plain, lines


def checksum_paths(root: Path, paths: Iterable[Path]) -> dict[str, str]:
    ordered = sorted({path.resolve() for path in paths}, key=lambda item: item.relative_to(root.resolve()).as_posix())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        digests = list(executor.map(sha256_file, ordered))
    return {path.relative_to(root.resolve()).as_posix(): digest for path, digest in zip(ordered, digests)}


def write_checksums(root: Path, paths: Iterable[Path]) -> dict[str, str]:
    hashes = checksum_paths(root, paths)
    atomic_bytes(root / "checksums.sha256", ("".join(f"{digest}  {rel}\n" for rel, digest in sorted(hashes.items()))).encode("utf-8"))
    return hashes


def graphml_counts(path: Path) -> dict[str, Any]:
    nodes = edges = 0
    keys: set[str] = set()
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                nodes += line.count("<node ")
                edges += line.count("<edge ")
                if "<key " in line:
                    match = re.search(r'attr\.name="([^"]+)"', line)
                    if match:
                        keys.add(match.group(1))
    except (OSError, UnicodeError) as exc:
        raise BuildError("GRAPHML_INVALID") from exc
    return {"nodes": nodes, "edges": edges, "keys": sorted(keys)}


def verify_artifact_chunk(arguments: tuple[str, str, list[str]]) -> dict[str, int]:
    database_text, case_root_text, artifact_ids = arguments
    case_root = Path(case_root_text)
    db = open_readonly(Path(database_text))
    pointer_checked = pointer_failed = time_raw_checked = time_raw_failed = 0
    artifact_hash_checked = artifact_hash_failed = 0
    try:
        for artifact_id in artifact_ids:
            artifact = db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            require(artifact is not None, "ARTIFACT_ROW_MISSING")
            source_path = resolve_inside(case_root, artifact["source_rel"], "ARTIFACT_PATH_ESCAPE")
            artifact_hash_checked += 1
            if not source_path.is_file() or source_path.stat().st_size != artifact["source_bytes"] or sha256_file(source_path) != artifact["source_sha256"]:
                artifact_hash_failed += 1
                continue
            records = list(db.execute("SELECT * FROM record_occurrence WHERE artifact_id=? ORDER BY depth,json_pointer", (artifact_id,)))
            structured = [row for row in records if row["parse_status"].startswith("parsed_json")]
            kinds = {str(row["pointer_kind"]) for row in structured}
            plain: Any = None
            jsonl_rows: dict[int, Any] | None = None
            if kinds:
                try:
                    plain, jsonl_rows = load_source_structure(source_path, kinds)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    pointer_failed += len(structured)
                    structured = []
            for row in records:
                if row["pointer_kind"] == "artifact":
                    pointer_checked += 1
                    pointer_failed += row["record_sha256"] != artifact["source_sha256"]
                    continue
                if not row["parse_status"].startswith("parsed_json"):
                    continue
                pointer_checked += 1
                try:
                    resolved = resolve_json_pointer(plain, row["json_pointer"], row["pointer_kind"], jsonl_rows)
                except (KeyError, BuildError):
                    pointer_failed += 1
                    continue
                if canonical_json_hash(resolved) != row["record_sha256"]:
                    pointer_failed += 1
                if row["pointer_kind"] == "jsonl_pointer":
                    parts = str(row["json_pointer"]).split("/")
                    if len(parts) < 3 or not parts[2].isdigit() or int(parts[2]) != row["source_line"]:
                        pointer_failed += 1
                elif row["source_line"] is not None:
                    pointer_failed += 1
            for event in db.execute(
                """SELECT t.*,r.pointer_kind FROM timeline_event t JOIN record_occurrence r ON r.record_id=t.record_id
                   WHERE t.artifact_id=? AND t.timeline_axis='business' ORDER BY t.event_id""",
                (artifact_id,),
            ):
                time_raw_checked += 1
                try:
                    resolved = resolve_json_pointer(plain, event["json_pointer"], event["pointer_kind"], jsonl_rows)
                    raw_type, raw = raw_scalar(resolved)
                    if raw_type != event["timestamp_raw_type"] or raw != event["timestamp_raw"]:
                        time_raw_failed += 1
                except (KeyError, BuildError):
                    time_raw_failed += 1
    finally:
        db.close()
    return {
        "artifact_hash_checked": artifact_hash_checked,
        "artifact_hash_failed": artifact_hash_failed,
        "pointer_checked": pointer_checked,
        "pointer_failed": pointer_failed,
        "time_raw_checked": time_raw_checked,
        "time_raw_failed": time_raw_failed,
    }


def verify_relation_provenance(db: sqlite3.Connection, case_root: Path) -> int:
    """Validate locators without exposing source content in diagnostics."""
    checked = 0
    structures: dict[str, tuple[Any, dict[int, Any] | None]] = {}
    for row in db.execute("""SELECT e.*,a.source_rel AS artifact_source_rel,
          r.artifact_id AS record_artifact_id,r.source_rel AS record_source_rel,
          r.source_line AS record_source_line,r.pointer_kind,r.json_pointer AS record_pointer,
          r.parse_status FROM relation_evidence e
          JOIN artifacts a ON a.artifact_id=e.artifact_id
          JOIN record_occurrence r ON r.record_id=e.record_id"""):
        locator = str(row["evidence_id"])
        require(row["artifact_id"] == row["record_artifact_id"]
                and row["source_rel"] == row["artifact_source_rel"] == row["record_source_rel"]
                and row["source_line"] == row["record_source_line"],
                f"RELATION_PROVENANCE_MISMATCH:{locator}")
        pointer = str(row["json_pointer"])
        if row["pointer_kind"] in {"json_pointer", "jsonl_pointer"}:
            require(pointer == row["record_pointer"] or pointer.startswith(str(row["record_pointer"]).rstrip("/") + "/"),
                    f"RELATION_POINTER_RECORD_MISMATCH:{locator}")
            try:
                rel = str(row["source_rel"])
                cache_key = rel + "\0" + str(row["pointer_kind"])
                if cache_key not in structures:
                    structures[cache_key] = load_source_structure(resolve_inside(case_root, rel, "RELATION_PATH_ESCAPE"), {row["pointer_kind"]})
                plain, jsonl_rows = structures[cache_key]
                resolve_json_pointer(plain, pointer, row["pointer_kind"], jsonl_rows)
            except (OSError, UnicodeError, ValueError, KeyError, IndexError, TypeError, BuildError):
                raise BuildError(f"RELATION_POINTER_INVALID:{locator}") from None
        else:
            # Artifact/task/archive virtual locators are separately verified by their tables.
            require(pointer.startswith("$"), f"RELATION_VIRTUAL_POINTER_INVALID:{locator}")
        checked += 1
    return checked


# EN: Independently verify package structure, count gates and source provenance.
# 中文：独立检查包结构、数量门槛与来源血缘。
def verify_package(
    case_root: Path,
    package_root: Path,
    write_receipt: bool = True,
    workers: int | None = None,
) -> dict[str, Any]:
    case_root = case_root.resolve()
    package_root = package_root.resolve()
    manifest = read_json(package_root / "build_manifest.json", "BUILD_MANIFEST")
    require(manifest.get("schema") == SCHEMA and manifest.get("status") == "BUILT", "BUILD_MANIFEST_INVALID")
    expected_counts = manifest.get("counts")
    require(isinstance(expected_counts, dict), "BUILD_MANIFEST_COUNTS_INVALID")
    gates = manifest.get("gates")
    require(isinstance(gates, dict), "BUILD_MANIFEST_GATES_INVALID")
    expected_attachment_occurrences = parse_int(gates.get("expected_attachment_occurrences"), "ATTACHMENT_GATE_INVALID")
    expected_mail_attachment_relations = parse_int(gates.get("expected_mail_attachment_relations"), "MAIL_ATTACHMENT_GATE_INVALID")
    expected_multiparent = parse_int(gates.get("expected_multiparent_attachments"), "MULTIPARENT_GATE_INVALID")
    full_extract_info = manifest.get("full_extract_v2")
    if full_extract_info is not None:
        require(isinstance(full_extract_info, dict), "FULL_EXTRACT_V2_MANIFEST_INVALID")
        full_root = resolve_inside(case_root, str(full_extract_info.get("path") or ""), "FULL_EXTRACT_V2_MANIFEST_PATH_ESCAPE")
        full_state = full_root / "state.sqlite3"
        require(full_state.is_file(), "FULL_EXTRACT_V2_STATE_MISSING")
        require(sha256_file(full_state) == normalize_sha(full_extract_info.get("state_sha256"), "FULL_EXTRACT_V2_STATE_SHA_INVALID"), "FULL_EXTRACT_V2_STATE_HASH_MISMATCH")
    checksums = read_checksum_file(package_root / "checksums.sha256")

    def check_checksum(item: tuple[str, str]) -> str:
        rel, digest = item
        path = resolve_inside(package_root, rel, "OUTPUT_CHECKSUM_PATH_ESCAPE")
        if not path.is_file():
            return "missing"
        return "ok" if sha256_file(path) == digest else "mismatch"

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        checksum_results = list(executor.map(check_checksum, checksums.items()))
    require(all(item == "ok" for item in checksum_results), "OUTPUT_CHECKSUM_MISMATCH")
    database = package_root / "relationship_timeline_v2.sqlite3"
    require(database.is_file(), "OUTPUT_DATABASE_MISSING")
    db = open_readonly(database)
    pointer_checked = pointer_failed = time_raw_checked = time_raw_failed = 0
    artifact_hash_checked = artifact_hash_failed = 0
    try:
        require(str(db.execute("PRAGMA quick_check(1)").fetchone()[0]) == "ok", "OUTPUT_DATABASE_INTEGRITY_FAILED")
        require(not list(db.execute("PRAGMA foreign_key_check")), "OUTPUT_FOREIGN_KEY_FAILED")
        relation_provenance_checked = verify_relation_provenance(db, case_root)
        tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({"meta", *PARQUET_TABLES}.issubset(tables), "OUTPUT_DATABASE_SCHEMA_INVALID")
        meta = {str(row[0]): str(row[1]) for row in db.execute("SELECT key,value FROM meta")}
        require(meta.get("schema") == SCHEMA, "OUTPUT_META_SCHEMA_INVALID")
        actual_counts = {table: int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in PARQUET_TABLES}
        for table in PARQUET_TABLES:
            require(actual_counts[table] == parse_int(expected_counts.get(table), "OUTPUT_COUNT_INVALID"), "OUTPUT_COUNT_MISMATCH")
        provenance_count = int(db.execute("SELECT COUNT(*) FROM artifacts WHERE source_scope='relation_provenance_manifest'").fetchone()[0])
        require(provenance_count == 1 == parse_int(manifest.get("provenance_artifact_count"), "PROVENANCE_ARTIFACT_COUNT_INVALID"), "PROVENANCE_ARTIFACT_COUNT_MISMATCH")
        require(int(db.execute("""SELECT COUNT(*) FROM artifacts a JOIN source_manifest m
                    ON a.source_rel=m.source_rel AND a.source_sha256=m.source_sha256 AND a.source_bytes=m.source_bytes
                    WHERE a.source_scope='relation_provenance_manifest'""").fetchone()[0]) == 1,
                "PROVENANCE_MANIFEST_ARTIFACT_MISMATCH")
        require(actual_counts["artifacts"] - provenance_count == parse_int(manifest.get("source_artifact_count"), "SOURCE_ARTIFACT_COUNT_INVALID"), "SOURCE_ARTIFACT_COUNT_MISMATCH")
        if isinstance(full_extract_info, dict):
            require(int(db.execute("SELECT COUNT(*) FROM artifacts WHERE source_scope<>'relation_provenance_manifest'").fetchone()[0]) == parse_int(full_extract_info.get("files"), "FULL_EXTRACT_V2_FILE_GATE_INVALID"), "FULL_EXTRACT_V2_FILE_GATE_FAILED")
            require(actual_counts["derived_artifacts"] == parse_int(full_extract_info.get("completed"), "FULL_EXTRACT_V2_COMPLETED_GATE_INVALID"), "FULL_EXTRACT_V2_DERIVED_GATE_FAILED")
            require(actual_counts["source_gap_task"] == parse_int(full_extract_info.get("source_gap"), "FULL_EXTRACT_V2_SOURCE_GAP_GATE_INVALID"), "FULL_EXTRACT_V2_SOURCE_GAP_GATE_FAILED")
        require(
            int(db.execute("SELECT COUNT(*) FROM artifacts a WHERE NOT EXISTS(SELECT 1 FROM record_occurrence r WHERE r.artifact_id=a.artifact_id AND r.pointer_kind='artifact')").fetchone()[0]) == 0,
            "ARTIFACT_RECORD_COVERAGE_MISMATCH",
        )
        require(
            int(
                db.execute(
                    """SELECT COUNT(*) FROM explicit_relation r
                       LEFT JOIN (SELECT relation_id,COUNT(*) AS actual_count FROM relation_evidence GROUP BY relation_id) e
                         ON e.relation_id=r.relation_id
                       WHERE r.evidence_count<>COALESCE(e.actual_count,0)"""
                ).fetchone()[0]
            ) == 0,
            "RELATION_EVIDENCE_COUNT_MISMATCH",
        )
        require(int(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE evidence_count=0").fetchone()[0]) == 0, "RELATION_WITHOUT_EVIDENCE")
        require(int(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE predicate IN ('cooccurs_in_record','observed_in_customer_case')").fetchone()[0]) == 0, "COARSE_RELATION_PRESENT")
        require(int(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE relation_kind='business'").fetchone()[0]) > 0, "BUSINESS_RELATIONS_EMPTY")
        require(int(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE relation_kind='structural'").fetchone()[0]) > 0, "STRUCTURAL_RELATIONS_EMPTY")
        require(
            int(db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
            == int(
                db.execute(
                    "SELECT COUNT(*) FROM (SELECT subject_node_id AS node_id FROM explicit_relation UNION SELECT object_node_id FROM explicit_relation)"
                ).fetchone()[0]
            ),
            "UNREFERENCED_NODE_PRESENT",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM timeline_event WHERE stable_order_key IS NULL OR stable_order_key='' ").fetchone()[0]) == 0,
            "TIMELINE_STABLE_KEY_MISSING",
        )
        require(
            int(db.execute("SELECT COUNT(*)-COUNT(DISTINCT stable_order_key) FROM timeline_event").fetchone()[0]) == 0,
            "TIMELINE_STABLE_KEY_DUPLICATE",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM timeline_event WHERE stable_sequence<=0 OR stable_sequence_basis='' OR timestamp_precision='' ").fetchone()[0]) == 0,
            "TIMELINE_SEQUENCE_OR_PRECISION_MISSING",
        )
        for axis, count in db.execute("SELECT timeline_axis,COUNT(*) FROM timeline_event GROUP BY timeline_axis"):
            bounds = db.execute(
                "SELECT MIN(stable_sequence),MAX(stable_sequence),COUNT(DISTINCT stable_sequence) FROM timeline_event WHERE timeline_axis=?",
                (axis,),
            ).fetchone()
            require((int(bounds[0]), int(bounds[1]), int(bounds[2])) == (1, int(count), int(count)), "TIMELINE_SEQUENCE_NOT_CONTIGUOUS")
        require(
            int(db.execute("SELECT COUNT(*) FROM timeline_event WHERE stable_sequence_basis LIKE '%path%' OR stable_sequence_basis LIKE '%source_rel%'").fetchone()[0]) == 0,
            "PATH_ORDER_MASQUERADING_AS_TIME",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM timeline_event WHERE normalization_status='absolute' AND (timestamp_utc IS NULL OR timestamp_sort_key IS NULL)").fetchone()[0]) == 0,
            "TIMELINE_ABSOLUTE_INCOMPLETE",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM timeline_event WHERE normalization_status='local_unzoned' AND (local_sort_key IS NULL OR timestamp_sort_key IS NOT NULL)").fetchone()[0]) == 0,
            "TIMELINE_LOCAL_INCONSISTENT",
        )
        axes = {str(row[0]): int(row[1]) for row in db.execute("SELECT timeline_axis,COUNT(*) FROM timeline_event GROUP BY timeline_axis")}
        require(axes.get("business", 0) > 0 and axes.get("capture", 0) > 0, "DUAL_TIMELINE_INCOMPLETE")
        require(
            int(db.execute("SELECT COUNT(*) FROM artifacts a WHERE a.source_scope='manual_trade' AND NOT EXISTS(SELECT 1 FROM record_occurrence r WHERE r.artifact_id=a.artifact_id)").fetchone()[0]) == 0,
            "MANUAL_ARTIFACT_RECORD_COVERAGE_MISMATCH",
        )
        require(actual_counts["lineage"] >= actual_counts["artifacts"] + 2, "LINEAGE_COUNT_MISMATCH")
        require(
            int(db.execute("SELECT COUNT(*) FROM artifacts a WHERE NOT EXISTS(SELECT 1 FROM lineage l WHERE l.source_artifact_id=a.artifact_id)").fetchone()[0]) == 0,
            "ARTIFACT_LINEAGE_MISSING",
        )
        require(actual_counts["attachment_occurrence"] == expected_attachment_occurrences, "ATTACHMENT_OCCURRENCE_GATE_FAILED")
        require(
            int(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE predicate='mail_has_attachment'").fetchone()[0]) == expected_mail_attachment_relations,
            "MAIL_ATTACHMENT_RELATION_GATE_FAILED",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM (SELECT attachment_node_id FROM attachment_occurrence GROUP BY attachment_node_id HAVING COUNT(DISTINCT mail_node_id)>1)").fetchone()[0]) == expected_multiparent,
            "MULTIPARENT_ATTACHMENT_GATE_FAILED",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM attachment_occurrence o JOIN artifacts a ON a.artifact_id=o.artifact_id WHERE o.artifact_source_rel<>a.source_rel OR o.artifact_sha256<>a.source_sha256").fetchone()[0]) == 0,
            "ATTACHMENT_OCCURRENCE_ARTIFACT_MISMATCH",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM derived_artifacts").fetchone()[0])
            == int(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE predicate='source_artifact_derived_to_output'").fetchone()[0]),
            "SOURCE_DERIVED_RELATION_MISMATCH",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM source_gap_task").fetchone()[0])
            == int(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE predicate='source_artifact_has_source_gap'").fetchone()[0]),
            "SOURCE_GAP_RELATION_MISMATCH",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM source_gap_task g JOIN derived_artifacts d ON d.task_id=g.task_id").fetchone()[0]) == 0,
            "SOURCE_GAP_PRETENDED_DERIVED",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM archive_member").fetchone()[0])
            == int(db.execute("SELECT COUNT(*) FROM explicit_relation WHERE predicate='archive_contains_member'").fetchone()[0]),
            "ARCHIVE_MEMBER_RELATION_MISMATCH",
        )
        require(int(db.execute("SELECT COUNT(*) FROM trade_entity").fetchone()[0]) > 0, "TRADE_ENTITY_EMPTY")
        require(
            int(db.execute("SELECT COUNT(*) FROM trade_entity WHERE unbound_to_customer<>1 OR binding_status<>'UNBOUND' OR binding_evidence_pointer IS NOT NULL").fetchone()[0]) == 0,
            "TRADE_UNBOUND_FLAG_INVALID",
        )
        require(
            int(db.execute("SELECT COUNT(*) FROM explicit_relation r WHERE r.predicate='order_has_product' AND NOT EXISTS(SELECT 1 FROM relation_evidence e JOIN record_occurrence o ON o.record_id=e.record_id WHERE e.relation_id=r.relation_id AND o.parse_status LIKE 'parsed_json%')").fetchone()[0]) == 0,
            "ORDER_PRODUCT_WITHOUT_SOURCE_POINTER",
        )

        source_manifests = list(db.execute("SELECT * FROM source_manifest"))
        require(len(source_manifests) == 1, "SOURCE_MANIFEST_COUNT_INVALID")
        for source_manifest in source_manifests:
            manifest_path = resolve_inside(case_root, source_manifest["source_rel"], "SOURCE_MANIFEST_PATH_ESCAPE")
            require(manifest_path.is_file(), "SOURCE_MANIFEST_MISSING")
            require(manifest_path.stat().st_size == source_manifest["source_bytes"], "SOURCE_MANIFEST_SIZE_MISMATCH")
            require(sha256_file(manifest_path) == source_manifest["source_sha256"], "SOURCE_MANIFEST_HASH_MISMATCH")

        derived_rows = [dict(row) for row in db.execute("SELECT output_rel,output_sha256,output_kind,output_bytes,output_file_count FROM derived_artifacts")]
        gap_rows = [dict(row) for row in db.execute("SELECT output_rel,output_sha256,output_kind,output_bytes,output_file_count FROM source_gap_task")]

        def verify_derived(derived: dict[str, Any]) -> str:
            output_path = resolve_inside(case_root, derived["output_rel"], "DERIVED_OUTPUT_PATH_ESCAPE")
            if not output_path.exists():
                return "DERIVED_OUTPUT_MISSING"
            output_kind, output_bytes, output_file_count = output_stats(output_path)
            if output_kind != derived["output_kind"]:
                return "DERIVED_OUTPUT_KIND_MISMATCH"
            if output_bytes != derived["output_bytes"] or output_file_count != derived["output_file_count"]:
                return "DERIVED_OUTPUT_SIZE_MISMATCH"
            if stable_tree_hash(output_path) != derived["output_sha256"]:
                return "DERIVED_OUTPUT_HASH_MISMATCH"
            return "PASS"

        verification_workers = workers if workers is not None else min(32, max(1, (os.cpu_count() or 1) // 2))
        require(1 <= verification_workers <= min(32, os.cpu_count() or 1), "VERIFY_WORKER_COUNT_INVALID")
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(verification_workers, 16)) as executor:
            for derived_status in executor.map(verify_derived, [*derived_rows, *gap_rows]):
                require(derived_status == "PASS", derived_status)

        artifact_ids = [str(row[0]) for row in db.execute("SELECT artifact_id FROM artifacts ORDER BY artifact_id")]
        chunk_count = min(verification_workers, len(artifact_ids))
        chunks = [artifact_ids[index::chunk_count] for index in range(chunk_count)]
        arguments = [(str(database), str(case_root), chunk) for chunk in chunks if chunk]
        with concurrent.futures.ProcessPoolExecutor(max_workers=chunk_count) as executor:
            chunk_results = list(executor.map(verify_artifact_chunk, arguments))
        for chunk_result in chunk_results:
            artifact_hash_checked += chunk_result["artifact_hash_checked"]
            artifact_hash_failed += chunk_result["artifact_hash_failed"]
            pointer_checked += chunk_result["pointer_checked"]
            pointer_failed += chunk_result["pointer_failed"]
            time_raw_checked += chunk_result["time_raw_checked"]
            time_raw_failed += chunk_result["time_raw_failed"]
        require(artifact_hash_failed == 0, "ARTIFACT_HASH_VALIDATION_FAILED")
        require(pointer_failed == 0, "RECORD_POINTER_VALIDATION_FAILED")
        require(time_raw_failed == 0, "TIMELINE_RAW_VALIDATION_FAILED")
    finally:
        db.close()

    graph = graphml_counts(package_root / "relations_v2.graphml.gz")
    require(graph["nodes"] == expected_counts["nodes"] and graph["edges"] == expected_counts["explicit_relation"], "GRAPHML_COUNT_MISMATCH")
    require({"predicate", "relation_kind", "evidence_count", "first_event_sort_key", "last_event_sort_key"}.issubset(set(graph["keys"])), "GRAPHML_SCHEMA_INCOMPLETE")
    parquet_receipt = read_json(package_root / "parquet" / "export_receipt.json", "PARQUET_RECEIPT")
    require(parquet_receipt.get("status") == "PASS", "PARQUET_RECEIPT_NOT_PASS")
    parquet_counts = parquet_receipt.get("row_counts")
    require(isinstance(parquet_counts, dict), "PARQUET_RECEIPT_INVALID")
    for table in PARQUET_TABLES:
        require(parse_int(parquet_counts.get(table), "PARQUET_COUNT_INVALID") == expected_counts[table], "PARQUET_COUNT_MISMATCH")
    result = {
        "schema": SCHEMA,
        "status": "PASS",
        "verified_at_utc": utc_now(),
        "counts": expected_counts,
        "artifact_hash_checked": artifact_hash_checked,
        "artifact_hash_failed": artifact_hash_failed,
        "record_pointer_checked": pointer_checked,
        "record_pointer_failed": pointer_failed,
        "relation_provenance_checked": relation_provenance_checked,
        "timeline_raw_checked": time_raw_checked,
        "timeline_raw_failed": time_raw_failed,
        "checksum_entries": len(checksums),
        "graphml": graph,
    }
    if write_receipt:
        atomic_json(package_root / "verification.json", result)
    return result


def completion_run(case_root: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        path = explicit.resolve()
        work = (case_root / "work").resolve()
        require(path.is_dir() and work in path.parents, "COMPLETION_RUN_INVALID")
        return path
    candidates = sorted(
        (path for path in (case_root / "work").glob("completion_v2_*") if path.is_dir()),
        key=lambda path: path.name,
    )
    return candidates[-1].resolve() if candidates else None


def publish(stage: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "parquet").mkdir(parents=True, exist_ok=True)
    relative_paths = [
        Path("relationship_timeline_v2.sqlite3"),
        Path("relations_v2.graphml.gz"),
        Path("build_manifest.json"),
        Path("checksums.sha256"),
        *[Path("parquet") / f"{table}.parquet" for table in PARQUET_TABLES],
        Path("parquet/export_receipt.json"),
        Path("verification.json"),
    ]
    for relative in relative_paths:
        source = stage / relative
        target = output / relative
        require(source.is_file() and not target.exists(), "PUBLISH_TARGET_CONFLICT")
        os.replace(source, target)


# EN: Validate inputs, build in a private stage and publish only a verified package.
# 中文：验证输入、隔离构建并仅发布通过验证的数据包。
def build_package(
    case_root: Path,
    company_id: str,
    source_package: Path,
    output_dir: Path | None = None,
    completion_run_dir: Path | None = None,
    workers: int | None = None,
    duckdb_python: str | None = None,
    expected_attachment_occurrences: int | None = None,
    expected_mail_attachment_relations: int | None = None,
    expected_multiparent_attachments: int | None = None,
    full_extract_v2_dir: Path | None = None,
) -> dict[str, Any]:
    case_root = case_root.resolve()
    output = (output_dir or (case_root / "derived" / "relationship_v2")).resolve()
    # EN: Count expectations are an explicit input contract, not production history.
    # 中文：期望数量属于显式输入契约，不能采用历史生产数量。
    require(all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (
        expected_attachment_occurrences, expected_mail_attachment_relations, expected_multiparent_attachments
    )), "EXPLICIT_ATTACHMENT_EXPECTATIONS_REQUIRED")
    source = load_source_package(case_root, company_id, source_package)
    full_extract = load_full_extract_v2(case_root, company_id, full_extract_v2_dir) if full_extract_v2_dir is not None else None
    run_dir = completion_run(case_root, completion_run_dir)
    require(run_dir is not None, "COMPLETION_RUN_REQUIRED")
    mail_manifest = load_mail_capture_baseline(case_root, run_dir, company_id)
    cpu_count = os.cpu_count() or 1
    worker_count = workers if workers is not None else min(32, max(1, cpu_count // 2))
    require(1 <= worker_count <= min(61, cpu_count), "WORKER_COUNT_INVALID")
    owned = [
        output / "relationship_timeline_v2.sqlite3",
        output / "relations_v2.graphml.gz",
        output / "build_manifest.json",
        output / "checksums.sha256",
        output / "verification.json",
        output / "parquet" / "export_receipt.json",
        *[output / "parquet" / f"{table}.parquet" for table in PARQUET_TABLES],
    ]
    require(not any(path.exists() for path in owned), "OUTPUT_TARGET_EXISTS")
    output.mkdir(parents=True, exist_ok=True)
    stage = output / f".relationship_v2_stage_{uuid.uuid4().hex}"
    stage.mkdir()
    (stage / "parquet").mkdir()
    builder: PackageBuilder | None = None
    success = False
    try:
        rows, tasks = load_artifact_inputs(
            case_root,
            source["database"],
            worker_count,
            full_extract["database"] if full_extract is not None else None,
        )
        if full_extract is None:
            rows.extend(supplemental_mail_artifacts(case_root, mail_manifest, rows))
        else:
            require(len(rows) == full_extract["file_count"], "FULL_EXTRACT_V2_ARTIFACT_AUTHORITY_MISMATCH")
            require(sum(len(value) for value in tasks.values()) == full_extract["task_count"], "FULL_EXTRACT_V2_TASK_AUTHORITY_MISMATCH")
        rows.sort(key=lambda row: str(row["source_rel"]))
        database = stage / "relationship_timeline_v2.sqlite3"
        builder = PackageBuilder(case_root, source, database)
        for index, row in enumerate(rows, 1):
            builder.build_artifact(row, tasks.get(row["source_rel"], []))
            if index % 50 == 0:
                builder.db.commit()
        attachment_import = builder.import_mail_capture_manifest(mail_manifest)
        if full_extract is not None:
            builder.db.execute(
                """INSERT INTO lineage(
                     lineage_id,source_kind,source_path,source_sha256,source_bytes,source_artifact_id,
                     target_table,target_key,derivation,recorded_at_utc)
                   VALUES(?,?,?,?,?,NULL,?,?,?,?)""",
                (
                    sha256_bytes(f"full-extract-v2\0{full_extract['database_sha256']}".encode("utf-8")),
                    "full_extract_v2_state_database",
                    str(full_extract["database"]),
                    full_extract["database_sha256"],
                    full_extract["database"].stat().st_size,
                    "artifacts/derived_artifacts/source_gap_task/timeline_event/relation_evidence",
                    "all_active_rows",
                    "active files and terminal tasks authoritative import",
                    utc_now(),
                ),
            )
        counts = builder.finalize(
            company_id,
            expected_attachment_occurrences=expected_attachment_occurrences,
            expected_mail_attachment_relations=expected_mail_attachment_relations,
            expected_multiparent_attachments=expected_multiparent_attachments,
        )
        if full_extract is not None:
            require(int(builder.db.execute("SELECT COUNT(*) FROM artifacts WHERE source_scope<>'relation_provenance_manifest'").fetchone()[0]) == full_extract["file_count"], "FULL_EXTRACT_V2_ARTIFACT_GATE_FAILED")
            require(counts["derived_artifacts"] == full_extract["completed"], "FULL_EXTRACT_V2_DERIVED_GATE_FAILED")
            require(counts["source_gap_task"] == full_extract["source_gap"], "FULL_EXTRACT_V2_SOURCE_GAP_GATE_FAILED")
        record_types = {
            str(row[0]): int(row[1])
            for row in builder.db.execute("SELECT record_type,COUNT(*) FROM record_occurrence GROUP BY record_type ORDER BY record_type")
        }
        node_types = {
            str(row[0]): int(row[1])
            for row in builder.db.execute("SELECT node_type,COUNT(*) FROM nodes GROUP BY node_type ORDER BY node_type")
        }
        relation_kinds = {
            str(row[0]): int(row[1])
            for row in builder.db.execute("SELECT relation_kind,COUNT(*) FROM explicit_relation GROUP BY relation_kind ORDER BY relation_kind")
        }
        timeline_axes = {
            str(row[0]): int(row[1])
            for row in builder.db.execute("SELECT timeline_axis,COUNT(*) FROM timeline_event GROUP BY timeline_axis ORDER BY timeline_axis")
        }
        timeline_status = {
            str(row[0]): int(row[1])
            for row in builder.db.execute("SELECT normalization_status,COUNT(*) FROM timeline_event GROUP BY normalization_status ORDER BY normalization_status")
        }
        builder.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        builder.db.execute("PRAGMA journal_mode=DELETE")
        builder.db.execute("PRAGMA synchronous=FULL")
        builder.db.commit()
        builder.close()
        builder = None
        graph = write_graphml(database, stage / "relations_v2.graphml.gz")
        require(graph["nodes"] == counts["nodes"] and graph["edges"] == counts["explicit_relation"], "GRAPHML_COUNT_MISMATCH")
        selected_duckdb = export_parquet(database, stage / "parquet", counts, duckdb_python)
        run_relative = None
        if run_dir is not None:
            run_relative = run_dir.relative_to(case_root).as_posix()
        manifest = {
            "schema": SCHEMA,
            "status": "BUILT",
            "built_at_utc": utc_now(),
            "company_id": company_id,
            "source_package": {
                "path": str(source["root"]),
                "database_sha256": source["database_sha256"],
                "verification_sha256": sha256_file(source["verification_path"]),
            },
            "full_extract_v2": None
            if full_extract is None
            else {
                "path": str(full_extract["root"]),
                "state_sha256": full_extract["database_sha256"],
                "verification_sha256": full_extract["verification_sha256"],
                "files": full_extract["file_count"],
                "tasks": full_extract["task_count"],
                "completed": full_extract["completed"],
                "source_gap": full_extract["source_gap"],
            },
            "completion_run": run_relative,
            "gates": {
                "expected_attachment_occurrences": expected_attachment_occurrences,
                "expected_mail_attachment_relations": expected_mail_attachment_relations,
                "expected_multiparent_attachments": expected_multiparent_attachments,
                "imported_attachment_occurrences": attachment_import["occurrences"],
                "imported_mail_attachment_relations": attachment_import["unique_relations"],
                "imported_multiparent_attachments": attachment_import["multiparent"],
                "mail_capture_manifest_sha256": mail_manifest["sha256"],
            },
            "counts": counts,
            "source_artifact_count": counts["artifacts"] - 1,
            "provenance_artifact_count": 1,
            "record_types": record_types,
            "node_types": node_types,
            "relation_kinds": relation_kinds,
            "timeline_axes": timeline_axes,
            "timeline_normalization": timeline_status,
            "graphml": graph,
            "runtime": {
                "python": sys.version.split()[0],
                "sqlite": sqlite3.sqlite_version,
                "duckdb_python": selected_duckdb,
                "workers": worker_count,
            },
            "privacy": {"stdout_customer_values": False, "online_services": False},
        }
        atomic_json(stage / "build_manifest.json", manifest)
        checksum_inputs = [
            database,
            stage / "relations_v2.graphml.gz",
            stage / "build_manifest.json",
            stage / "parquet" / "export_receipt.json",
            *[stage / "parquet" / f"{table}.parquet" for table in PARQUET_TABLES],
        ]
        hashes = write_checksums(stage, checksum_inputs)
        verification = verify_package(case_root, stage, write_receipt=True, workers=worker_count)
        publish(stage, output)
        success = True
        return {
            "status": "PASS",
            "output_dir": str(output),
            "database_sha256": hashes["relationship_timeline_v2.sqlite3"],
            "graphml_sha256": hashes["relations_v2.graphml.gz"],
            "counts": counts,
            "record_pointer_checked": verification["record_pointer_checked"],
            "timeline_raw_checked": verification["timeline_raw_checked"],
        }
    finally:
        if builder is not None:
            builder.close()
        if stage.exists():
            shutil.rmtree(stage)
        if not success:
            # Only remove an empty directory created by this invocation.  Never
            # touch pre-existing or published v2 artifacts.
            try:
                if output.is_dir() and not any(output.iterdir()):
                    output.rmdir()
            except OSError:
                pass


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Build or verify the local explicit-relation and dual-timeline v2 package")
    commands = root.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--case-root", type=Path, required=True)
    build.add_argument("--company-id", required=True)
    build.add_argument("--source-package", type=Path, required=True)
    build.add_argument("--full-extract-v2", type=Path)
    build.add_argument("--output-dir", type=Path)
    build.add_argument("--completion-run-dir", type=Path)
    build.add_argument("--workers", type=int)
    build.add_argument("--duckdb-python")
    build.add_argument("--expected-attachment-occurrences", type=int, required=True)
    build.add_argument("--expected-mail-attachment-relations", type=int, required=True)
    build.add_argument("--expected-multiparent-attachments", type=int, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--case-root", type=Path, required=True)
    verify.add_argument("--package-dir", type=Path, required=True)
    verify.add_argument("--workers", type=int)
    resume = commands.add_parser("resume-stage")
    resume.add_argument("--case-root", type=Path, required=True)
    resume.add_argument("--stage-dir", type=Path, required=True)
    resume.add_argument("--output-dir", type=Path, required=True)
    resume.add_argument("--workers", type=int, default=min(32, max(1, (os.cpu_count() or 1) // 2)))
    internal = commands.add_parser("internal-export")
    internal.add_argument("--database", type=Path, required=True)
    internal.add_argument("--output-dir", type=Path, required=True)
    return root


def output_status(value: dict[str, Any]) -> None:
    print(json.dumps({"schema": SCHEMA, **value}, ensure_ascii=True, sort_keys=True, separators=(",", ":")), flush=True)


def resume_stage(case_root: Path, stage: Path, output: Path, workers: int) -> dict[str, Any]:
    case_root = case_root.resolve()
    stage = stage.resolve()
    output = output.resolve()
    require(stage.is_dir(), "RESUME_STAGE_MISSING")
    require(stage.parent == output and stage.name.startswith(".relationship_v2_stage_"), "RESUME_STAGE_SCOPE_INVALID")
    database = stage / "relationship_timeline_v2.sqlite3"
    require(database.is_file(), "OUTPUT_DATABASE_MISSING")
    writable = sqlite3.connect(database, timeout=60)
    try:
        writable.execute("PRAGMA busy_timeout=60000")
        writable.execute(
            "CREATE INDEX IF NOT EXISTS idx_timeline_artifact_axis_event ON timeline_event(artifact_id,timeline_axis,event_id)"
        )
        writable.commit()
    finally:
        writable.close()
    checksum_map = read_checksum_file(stage / "checksums.sha256")
    checksum_map["relationship_timeline_v2.sqlite3"] = sha256_file(database)
    atomic_bytes(
        stage / "checksums.sha256",
        "".join(f"{digest}  {rel}\n" for rel, digest in sorted(checksum_map.items())).encode("utf-8"),
    )
    verification = verify_package(case_root, stage, write_receipt=True, workers=workers)
    checksums = read_checksum_file(stage / "checksums.sha256")
    publish(stage, output)
    shutil.rmtree(stage)
    return {
        "status": "PASS",
        "output_dir": str(output),
        "database_sha256": checksums["relationship_timeline_v2.sqlite3"],
        "graphml_sha256": checksums["relations_v2.graphml.gz"],
        "counts": verification["counts"],
        "artifact_hash_checked": verification["artifact_hash_checked"],
        "record_pointer_checked": verification["record_pointer_checked"],
        "timeline_raw_checked": verification["timeline_raw_checked"],
    }


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    args = parser().parse_args()
    if args.command == "internal-export":
        return internal_export(args.database.resolve(), args.output_dir.resolve())
    try:
        if args.command == "build":
            result = build_package(
                args.case_root,
                args.company_id,
                args.source_package,
                args.output_dir,
                args.completion_run_dir,
                args.workers,
                args.duckdb_python,
                args.expected_attachment_occurrences,
                args.expected_mail_attachment_relations,
                args.expected_multiparent_attachments,
                args.full_extract_v2,
            )
        elif args.command == "resume-stage":
            result = resume_stage(args.case_root, args.stage_dir, args.output_dir, args.workers)
        else:
            result = verify_package(args.case_root.resolve(), args.package_dir.resolve(), write_receipt=False, workers=args.workers)
            result = {
                "status": result["status"],
                "package_dir": str(args.package_dir.resolve()),
                "counts": result["counts"],
                "artifact_hash_checked": result["artifact_hash_checked"],
                "record_pointer_checked": result["record_pointer_checked"],
                "timeline_raw_checked": result["timeline_raw_checked"],
            }
        output_status(result)
        return 0
    except DependencyMissing as exc:
        output_status({"status": "DEPENDENCY_MISSING", "error_code": str(exc)})
        return 4
    except BuildError as exc:
        output_status({"status": "FAILED_CLOSED", "error_code": str(exc)})
        return 2
    except Exception:
        output_status({"status": "FAILED_CLOSED", "error_code": "UNEXPECTED_LOCAL_ERROR"})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
