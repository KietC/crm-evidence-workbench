#!/usr/bin/env python3
# EN: Extract text, OCR, containers and optional local ASR with resumable case-isolated tasks.
# 中文：以可续跑、案例隔离任务处理文字、OCR、容器及可选本地 ASR。
"""Local-only, resumable full-text post-processing for one OKKI case.

The program intentionally keeps customer text out of stdout/stderr.  Raw evidence
is never changed.  Derived text, OCR, ASR and relation values stay below the case
directory and are referenced by hashes from the public status output.
"""

from __future__ import annotations

import argparse
import contextlib
import concurrent.futures
import csv
import hashlib
import html
import importlib.util
import json
import mimetypes
import multiprocessing
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree


SCHEMA = "okki.single_customer_full_extract.v1"
PROCESSING_SCOPE_V2_SCHEMA = "okki.processing_scope.v2"
PROCESSING_SCOPE_V2_NAME = "processing_scope.v2.json"
DEFAULT_ASR = os.environ.get("CRM_ASR_SCRIPT")
DEFAULT_TESSDATA = Path(os.environ["TESSDATA_PREFIX"]).resolve() if os.environ.get("TESSDATA_PREFIX") else None
TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".log", ".srt", ".vtt", ".eml",
    ".jsonl", ".mhtml", ".rawhtml", ".js", ".css", ".svg", ".yaml", ".yml",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv"}
ID_TYPES = {
    "company": "company", "customer": "company", "contact": "contact",
    "mail": "mail", "message": "mail", "opportunity": "opportunity",
    "order": "order", "product": "product", "file": "file",
    "document": "document", "attachment": "attachment", "activity": "activity",
    "trade": "trade_record", "customs": "trade_record",
}
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
SESSION_ID = re.compile(r"^capture_[A-Za-z0-9_-]+$")
TERMINAL_TASK_STATES = {"completed", "source_gap"}
DEFAULT_ARCHIVE_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_MEMBERS = 10000


@contextlib.contextmanager
# EN: Hold a kernel-owned lock; process death releases ownership automatically.
# 中文：持有内核所有权锁；进程退出自动释放所有权。
def case_extraction_lock(case_root: Path):
    """OS-owned exclusive lock; process death releases it without PID heuristics."""
    lock_path = case_root.resolve() / ".full_extract.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("CASE_EXTRACTION_LOCK_BUSY") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ExpansionBudget:
    """Counts actual streamed bytes, including duplicate content, across containers."""
    def __init__(self, max_bytes=DEFAULT_ARCHIVE_TOTAL_BYTES, max_members=DEFAULT_ARCHIVE_MAX_MEMBERS):
        if max_bytes < 1 or max_members < 1:
            raise ValueError("ARCHIVE_BUDGET_MUST_BE_POSITIVE")
        self.max_bytes, self.max_members = max_bytes, max_members
        self.bytes = self.members = 0
        self.exhausted_code = None

    def member(self):
        if self.exhausted_code:
            raise SourceGap(self.exhausted_code)
        if self.members >= self.max_members:
            self.exhausted_code = "ARCHIVE_TOTAL_MEMBER_LIMIT"
            raise SourceGap(self.exhausted_code)
        self.members += 1

    def consume(self, count):
        self.bytes += count
        if self.bytes > self.max_bytes:
            self.exhausted_code = "ARCHIVE_TOTAL_BYTE_LIMIT"
            raise SourceGap(self.exhausted_code)


class SourceGap(RuntimeError):
    """A terminal, evidenced source/tool limitation that retries cannot fix."""

    def __init__(self, code: str, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.metadata = metadata or {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_tree_hash(path: Path) -> str:
    if path.is_file():
        return sha256_file(path)
    digest = hashlib.sha256()
    for item in sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: p.as_posix().casefold()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(item)))
    return digest.hexdigest()


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def atomic_text(path: Path, value: str) -> None:
    atomic_bytes(path, value.encode("utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def safe_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def full_extract_root(case_root: Path, output_dir: Path | str | None = None) -> Path:
    root = Path(output_dir) if output_dir is not None else Path("derived/full_extract_v2")
    resolved = root.resolve() if root.is_absolute() else (case_root / root).resolve()
    case = case_root.resolve()
    if resolved == case or case not in resolved.parents:
        raise RuntimeError("OUTPUT_DIRECTORY_OUTSIDE_CASE")
    return resolved


def output_status(stage: str, status: str, **counts: Any) -> None:
    # Values accepted here must be metadata only.  Never pass source text.
    payload = {"schema": SCHEMA, "stage": stage, "status": status, **counts}
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")), flush=True)


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored and data.strip():
            self.parts.append(data.strip())


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def looks_like_text(data: bytes) -> bool:
    if not data or b"\0" in data:
        return False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError:
            return False
    if not text:
        return False
    printable = sum(character.isprintable() or character in "\r\n\t" for character in text)
    return printable / len(text) >= 0.92


def json_scalar_text(value: Any) -> list[str]:
    rows: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                rows.extend(json_scalar_text(item))
            elif item is not None:
                rows.append(f"{key}: {item}")
    elif isinstance(value, list):
        for item in value:
            rows.extend(json_scalar_text(item))
    elif value is not None:
        rows.append(str(value))
    return rows


def identify(path: Path) -> tuple[str, str, str]:
    with path.open("rb") as handle:
        sample = handle.read(4096)
    head = sample[:64]
    suffix = path.suffix.casefold()
    magic = "binary"
    category = "other"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if head.startswith(b"%PDF-"):
        return "pdf", "document", "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image", "image/jpeg"
    if head[:6] in {b"GIF87a", b"GIF89a"}:
        return "gif", "image", "image/gif"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff", "image", "image/tiff"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav", "audio", "audio/wav"
    if head.startswith(b"fLaC"):
        return "flac", "audio", "audio/flac"
    if head.startswith(b"OggS"):
        return "ogg", "audio", "audio/ogg"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3", "audio", "audio/mpeg"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "matroska", "video", "video/x-matroska"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        category = "video" if suffix in VIDEO_SUFFIXES else "audio" if suffix in AUDIO_SUFFIXES else "media"
        return "iso-bmff", category, mime
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
            if any(name.startswith("word/") for name in names):
                return "ooxml-word", "office", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if any(name.startswith("xl/") for name in names):
                return "ooxml-excel", "office", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if any(name.startswith("ppt/") for name in names):
                return "ooxml-powerpoint", "office", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            return "zip", "archive", "application/zip"
        except zipfile.BadZipFile:
            return "bad-zip", "other", mime
    if head.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "rar", "archive", "application/vnd.rar"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole-compound", "legacy-office", "application/x-ole-storage"
    if head.startswith(b"%!PS-Adobe") or suffix == ".eps":
        return "eps", "eps", "application/postscript"
    if suffix == ".json":
        return "json", "text", "application/json"
    if suffix in {".html", ".htm", ".xhtml"}:
        return "html", "text", "text/html"
    if suffix in TEXT_SUFFIXES or suffix in {".xml", ".rels"}:
        return "text", "text", mime
    if looks_like_text(sample):
        stripped = sample.lstrip(b"\xef\xbb\xbf \t\r\n")
        if stripped.startswith((b"{", b"[")):
            return "json-text", "text", "application/json"
        if stripped.startswith(b"<"):
            return "markup-text", "text", "text/plain"
        return "text-sniffed", "text", "text/plain"
    if suffix in IMAGE_SUFFIXES:
        category = "image"
    elif suffix in AUDIO_SUFFIXES:
        category = "audio"
    elif suffix in VIDEO_SUFFIXES:
        category = "video"
    return magic, category, mime


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
          source_rel TEXT PRIMARY KEY, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL, magic TEXT NOT NULL, category TEXT NOT NULL,
          mime TEXT NOT NULL, source_scope TEXT NOT NULL DEFAULT 'automatic',
          active INTEGER NOT NULL DEFAULT 1, prepared_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks(
          task_id TEXT PRIMARY KEY, source_rel TEXT NOT NULL, source_sha256 TEXT NOT NULL,
          content_sha256 TEXT,
          task_kind TEXT NOT NULL, state TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          output_rel TEXT, result_sha256 TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          error_code TEXT, updated_at TEXT NOT NULL,
          UNIQUE(source_rel, source_sha256, task_kind)
        );
        CREATE TABLE IF NOT EXISTS content_objects(
          sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL, magic TEXT NOT NULL,
          category TEXT NOT NULL, mime TEXT NOT NULL, representative_rel TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, prepared_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS occurrences(
          occurrence_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL,
          source_scope TEXT NOT NULL, role TEXT NOT NULL, source_rel TEXT NOT NULL,
          source_identity_sha256 TEXT, owner_sha256 TEXT, resolution TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, prepared_at TEXT NOT NULL,
          FOREIGN KEY(content_sha256) REFERENCES content_objects(sha256)
        );
        CREATE TABLE IF NOT EXISTS source_gaps(
          gap_id TEXT PRIMARY KEY, content_sha256 TEXT, source_scope TEXT NOT NULL,
          role TEXT NOT NULL, source_identity_sha256 TEXT, owner_sha256 TEXT,
          error_code TEXT NOT NULL, http_status INTEGER, active INTEGER NOT NULL DEFAULT 1,
          recorded_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(active,state,task_kind);
        CREATE INDEX IF NOT EXISTS idx_occurrences_content ON occurrences(active,content_sha256);
        CREATE INDEX IF NOT EXISTS idx_occurrences_scope ON occurrences(active,source_scope,role);
        CREATE INDEX IF NOT EXISTS idx_source_gaps_active ON source_gaps(active,source_scope,error_code);
        """
    )
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(files)")}
    if "source_scope" not in columns:
        db.execute("ALTER TABLE files ADD COLUMN source_scope TEXT NOT NULL DEFAULT 'automatic'")
    task_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(tasks)")}
    if "content_sha256" not in task_columns:
        db.execute("ALTER TABLE tasks ADD COLUMN content_sha256 TEXT")
    db.execute("UPDATE tasks SET content_sha256=source_sha256 WHERE content_sha256 IS NULL")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_content_kind "
        "ON tasks(content_sha256,task_kind,active)"
    )
    db.commit()
    return db


def load_identity(case_root: Path, expected_company_id: str) -> dict[str, Any]:
    identity_path = case_root / "case_identity.json"
    if not identity_path.is_file():
        raise RuntimeError("CASE_IDENTITY_MISSING")
    identity = json.loads(identity_path.read_text(encoding="utf-8-sig"))
    if str(identity.get("company_id") or "") != expected_company_id:
        raise RuntimeError("CASE_IDENTITY_MISMATCH")
    if case_root.name != f"company_{expected_company_id}" and not case_root.name.endswith(f"_{expected_company_id}"):
        raise RuntimeError("CASE_DIRECTORY_IDENTITY_MISMATCH")
    return identity


def read_json_object(path: Path, error_code: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"{error_code}_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{error_code}_INVALID") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{error_code}_INVALID")
    return value


def load_automatic_scope(case_root: Path, company_id: str) -> dict[str, Any]:
    state_path = case_root / "work" / "capture_state.json"
    state = read_json_object(state_path, "CAPTURE_STATE")
    if state.get("running") is not False:
        raise RuntimeError("AUTO_CAPTURE_RUNNING")
    if str(state.get("companyId") or "") != company_id:
        raise RuntimeError("CAPTURE_STATE_COMPANY_MISMATCH")

    scope_path = case_root / "manifests" / "processing_scope_latest.json"
    scope = read_json_object(scope_path, "PROCESSING_SCOPE")
    if scope.get("schema") != 1 or str(scope.get("capture_status") or "").casefold() != "pass":
        raise RuntimeError("PROCESSING_SCOPE_NOT_PASS")
    if str(scope.get("company_id") or "") != company_id:
        raise RuntimeError("PROCESSING_SCOPE_COMPANY_MISMATCH")
    session_id = str(scope.get("default_session_id") or "")
    if SESSION_ID.fullmatch(session_id) is None:
        raise RuntimeError("PROCESSING_SCOPE_SESSION_INVALID")
    object_filter = scope.get("default_object_filter")
    if not isinstance(object_filter, dict) or object_filter.get("field") != "session_id" or str(object_filter.get("equals") or "") != session_id:
        raise RuntimeError("PROCESSING_SCOPE_FILTER_INVALID")
    session_root = (case_root / "raw" / "sessions" / session_id).resolve()
    sessions_root = (case_root / "raw" / "sessions").resolve()
    if sessions_root not in session_root.parents or not session_root.is_dir():
        raise RuntimeError("PROCESSING_SCOPE_SESSION_MISSING")
    final_path = session_root / "final_status.json"
    final = read_json_object(final_path, "PASS_SESSION_TERMINAL")
    errors = final.get("errors")
    metrics = final.get("metrics")
    if final.get("phase") != "complete" or not isinstance(errors, list) or errors or not isinstance(metrics, dict) or metrics.get("reconciliation_failures") != 0:
        raise RuntimeError("PASS_SESSION_TERMINAL_NOT_PASS")
    return {
        "session_id": session_id,
        "session_root": session_root,
        "scope": scope,
        "processing_scope_path": scope_path,
        "processing_scope_sha256": sha256_file(scope_path),
        "final_status_path": final_path,
        "final_status_sha256": sha256_file(final_path),
    }


def load_ui_gap_scope(case_root: Path, company_id: str) -> dict[str, Any]:
    manifest_path = case_root / "manifests" / "ui_gap_revisit_latest.json"
    if not manifest_path.is_file():
        return {"status": "NOT_PRESENT", "session_root": None, "manifest_path": None, "manifest_sha256": None}
    manifest = read_json_object(manifest_path, "UI_GAP_REVISIT_MANIFEST")
    if manifest.get("schema") != "okki.ui_gap_revisit.v1" or str(manifest.get("status") or "").upper() != "PASS":
        raise RuntimeError("UI_GAP_REVISIT_NOT_PASS")
    if str(manifest.get("company_id") or "") != company_id:
        raise RuntimeError("UI_GAP_REVISIT_COMPANY_MISMATCH")
    if manifest.get("no_auto_next") is not True or manifest.get("manual_trade_automatic_capture") is not False or manifest.get("processing_scope_updated") is not False:
        raise RuntimeError("UI_GAP_REVISIT_SCOPE_INVALID")
    tabs = manifest.get("automatic_tabs")
    if not isinstance(tabs, list) or set(map(str, tabs)) != {"dynamic", "relational"}:
        raise RuntimeError("UI_GAP_REVISIT_TABS_INVALID")
    checks = manifest.get("checks")
    required_checks = (
        "other_dynamic_found", "other_dynamic_terminal_closed",
        "all_found_filters_terminal_closed", "documents_terminal_closed",
    )
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
        raise RuntimeError("UI_GAP_REVISIT_CHECKS_NOT_PASS")
    session_id = str(manifest.get("session_id") or "")
    if SESSION_ID.fullmatch(session_id) is None:
        raise RuntimeError("UI_GAP_REVISIT_SESSION_INVALID")
    sessions_root = (case_root / "raw" / "sessions").resolve()
    session_root = (sessions_root / session_id).resolve()
    if sessions_root not in session_root.parents or not session_root.is_dir():
        raise RuntimeError("UI_GAP_REVISIT_SESSION_MISSING")
    final_path = session_root / "final_status.json"
    final = read_json_object(final_path, "UI_GAP_REVISIT_TERMINAL")
    if final.get("phase") != "complete" or final.get("running") is not False or final.get("errors") not in ([], None):
        raise RuntimeError("UI_GAP_REVISIT_TERMINAL_NOT_PASS")
    return {
        "status": "PASS", "session_id": session_id, "session_root": session_root,
        "manifest_path": manifest_path, "manifest_sha256": sha256_file(manifest_path),
        "final_status_path": final_path, "final_status_sha256": sha256_file(final_path),
    }


def load_automatic_source_gaps(case_root: Path, company_id: str, session_id: str) -> dict[str, Any]:
    path = case_root / "verification" / "capture_reconciliation_latest.json"
    if not path.is_file():
        return {"path": None, "sha256": None, "source_gaps": []}
    value = read_json_object(path, "CAPTURE_RECONCILIATION")
    if str(value.get("company_id") or "") not in {"", company_id} or str(value.get("session_id") or "") not in {"", session_id}:
        raise RuntimeError("CAPTURE_RECONCILIATION_BINDING_MISMATCH")
    checks = value.get("checks")
    if not isinstance(checks, list):
        raise RuntimeError("CAPTURE_RECONCILIATION_INVALID")
    source_gaps: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict) or check.get("id") != "external_passive_source_unavailable_after_retry":
            continue
        actual = check.get("actual")
        if not isinstance(actual, dict):
            raise RuntimeError("CAPTURE_RECONCILIATION_SOURCE_GAP_INVALID")
        try:
            unique_urls = int(actual.get("unique_urls", -1))
            failure_rows = int(actual.get("failure_rows", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("CAPTURE_RECONCILIATION_SOURCE_GAP_INVALID") from exc
        if unique_urls < 0 or failure_rows < unique_urls:
            raise RuntimeError("CAPTURE_RECONCILIATION_SOURCE_GAP_INVALID")
        for index in range(unique_urls):
            source_gaps.append({
                "gap_id": sha256_bytes(f"automatic\0external_passive_source_unavailable\0{index + 1}".encode("utf-8")),
                "content_sha256": None, "source_scope": "automatic", "role": "external_passive_resource",
                "source_identity_sha256": None, "owner_sha256": None,
                "error_code": "EXTERNAL_PASSIVE_SOURCE_UNAVAILABLE_AFTER_RETRY", "http_status": None,
            })
    return {"path": path, "sha256": sha256_file(path), "source_gaps": source_gaps}


def load_manual_trade_scope(case_root: Path, company_id: str, verify_artifacts: bool = True) -> dict[str, Any]:
    roots = sorted(
        (case_root / "evidence" / "manual_trade").glob("*/trade_manual_capture_manifest.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.as_posix().casefold()),
    )
    if not roots:
        raise RuntimeError("MANUAL_TRADE_MANIFEST_MISSING")
    manifest_path = roots[-1].resolve()
    manifest = read_json_object(manifest_path, "MANUAL_TRADE_MANIFEST")
    status = str(manifest.get("status") or "")
    if manifest.get("schema") != "okki.trade.manual_capture_manifest.v1" or status not in {"PASS", "PASS_WITH_SOURCE_GAPS"}:
        raise RuntimeError("MANUAL_TRADE_MANIFEST_NOT_PASS")
    if str(manifest.get("company_id") or "") != company_id:
        raise RuntimeError("MANUAL_TRADE_COMPANY_MISMATCH")
    source_gaps = manifest.get("source_gaps")
    artifacts = manifest.get("artifacts")
    if not isinstance(source_gaps, list) or not isinstance(artifacts, list):
        raise RuntimeError("MANUAL_TRADE_MANIFEST_INVALID")
    if status == "PASS" and source_gaps:
        raise RuntimeError("MANUAL_TRADE_STATUS_GAP_CONTRADICTION")
    if status == "PASS_WITH_SOURCE_GAPS" and not source_gaps:
        raise RuntimeError("MANUAL_TRADE_STATUS_GAP_CONTRADICTION")
    resolved: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    byte_total = 0
    for row in artifacts:
        if not isinstance(row, dict):
            raise RuntimeError("MANUAL_TRADE_ARTIFACT_INVALID")
        relative = str(row.get("relative_path") or "").replace("\\", "/")
        digest = str(row.get("sha256") or "")
        size = row.get("bytes")
        if not relative or Path(relative).is_absolute() or relative in seen_paths or HEX64.fullmatch(digest) is None or not isinstance(size, int) or size < 0:
            raise RuntimeError("MANUAL_TRADE_ARTIFACT_INVALID")
        path = (manifest_path.parent / Path(relative)).resolve()
        if manifest_path.parent not in path.parents:
            raise RuntimeError("MANUAL_TRADE_ARTIFACT_PATH_ESCAPE")
        if not path.is_file():
            raise RuntimeError("MANUAL_TRADE_ARTIFACT_MISSING")
        if path.stat().st_size != size:
            raise RuntimeError("MANUAL_TRADE_ARTIFACT_SIZE_MISMATCH")
        if verify_artifacts and sha256_file(path).casefold() != digest.casefold():
            raise RuntimeError("MANUAL_TRADE_ARTIFACT_HASH_MISMATCH")
        seen_paths.add(relative)
        byte_total += size
        resolved.append({"path": path, "relative_path": relative, "bytes": size, "sha256": digest.casefold()})
    counts = manifest.get("counts")
    if isinstance(counts, dict):
        if counts.get("artifact_files") is not None and int(counts["artifact_files"]) != len(resolved):
            raise RuntimeError("MANUAL_TRADE_ARTIFACT_COUNT_MISMATCH")
        if counts.get("artifact_bytes") is not None and int(counts["artifact_bytes"]) != byte_total:
            raise RuntimeError("MANUAL_TRADE_ARTIFACT_BYTES_MISMATCH")
    return {
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_file(manifest_path),
        "status": status,
        "source_gap_count": len(source_gaps),
        "source_gaps": source_gaps,
        "artifacts": resolved,
        "artifact_bytes": byte_total,
    }


def resolve_case_artifact(case_root: Path, relative_or_absolute: str, error_code: str) -> Path:
    raw = Path(relative_or_absolute)
    path = raw.resolve() if raw.is_absolute() else (case_root / raw).resolve()
    resolved_root = case_root.resolve()
    if path != resolved_root and resolved_root not in path.parents:
        raise RuntimeError(f"{error_code}_PATH_ESCAPE")
    return path


def validate_artifact_ref(
    case_root: Path,
    value: Any,
    error_code: str,
    verify_artifacts: bool,
    verified: dict[str, tuple[int, str]],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{error_code}_INVALID")
    relative = str(value.get("relative_path") or value.get("path") or "").replace("\\", "/")
    digest = str(value.get("sha256") or "").casefold()
    size = value.get("bytes")
    if not relative or HEX64.fullmatch(digest) is None or not isinstance(size, int) or size < 0:
        raise RuntimeError(f"{error_code}_INVALID")
    path = resolve_case_artifact(case_root, relative, error_code)
    if not path.is_file():
        raise RuntimeError(f"{error_code}_MISSING")
    if path.stat().st_size != size:
        raise RuntimeError(f"{error_code}_SIZE_MISMATCH")
    cache_key = path.as_posix().casefold()
    cached = verified.get(cache_key)
    if cached is not None and cached != (size, digest):
        raise RuntimeError(f"{error_code}_DECLARATION_CONFLICT")
    if cached is None:
        if verify_artifacts and sha256_file(path).casefold() != digest:
            raise RuntimeError(f"{error_code}_HASH_MISMATCH")
        verified[cache_key] = (size, digest)
    return {
        "path": path,
        "relative_path": safe_relative(path, case_root),
        "bytes": size,
        "sha256": digest,
        "source_identity_sha256": str(value.get("source_identity_sha256") or "").casefold() or None,
    }


def load_mail_capture_scope(
    case_root: Path,
    company_id: str,
    automatic: dict[str, Any],
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    declared = automatic["scope"].get("mail_capture_manifest")
    declared_path = declared.get("path") if isinstance(declared, dict) else declared
    if declared_path:
        manifest_path = resolve_case_artifact(case_root, str(declared_path), "MAIL_CAPTURE_MANIFEST")
    else:
        manifest_path = case_root / "manifests" / "mail_capture_manifest_latest.json"
        if not manifest_path.is_file():
            return {
                "manifest_path": None, "manifest_sha256": None, "manifest_bytes": 0,
                "status": "NOT_PRESENT", "counts": {}, "artifacts": [], "occurrences": [],
                "source_gaps": [], "historical_related_files": 0,
            }
    manifest = read_json_object(manifest_path, "MAIL_CAPTURE_MANIFEST")
    if (
        manifest.get("schema") != "okki.crm.mail_capture_manifest.v2"
        or manifest.get("schema_version") != 2
        or manifest.get("artifact_type") != "mail_capture_manifest.v2"
        or str(manifest.get("status") or "").upper() != "PASS"
    ):
        raise RuntimeError("MAIL_CAPTURE_MANIFEST_NOT_PASS")
    if str(manifest.get("company_id") or "") not in {"", company_id}:
        raise RuntimeError("MAIL_CAPTURE_MANIFEST_COMPANY_MISMATCH")
    if str(manifest.get("session_id") or "") != automatic["session_id"]:
        raise RuntimeError("MAIL_CAPTURE_MANIFEST_SESSION_MISMATCH")
    manifest_digest = sha256_file(manifest_path)
    if isinstance(declared, dict):
        if declared.get("bytes") != manifest_path.stat().st_size or str(declared.get("sha256") or "").casefold() != manifest_digest:
            raise RuntimeError("MAIL_CAPTURE_MANIFEST_BINDING_MISMATCH")

    counts = manifest.get("counts")
    mails = manifest.get("mails")
    if not isinstance(counts, dict) or not isinstance(mails, list):
        raise RuntimeError("MAIL_CAPTURE_MANIFEST_INVALID")
    verified: dict[str, tuple[int, str]] = {}
    occurrences: list[dict[str, Any]] = []
    source_gaps: list[dict[str, Any]] = []
    unique_artifacts: dict[str, dict[str, Any]] = {}
    mail_ids: set[str] = set()
    detail_available = detail_deleted = related_candidates = related_artifacts = related_unavailable = related_unresolved = 0

    def add_occurrence(role: str, owner_sha: str, ref: Any, resolution: str) -> None:
        artifact = validate_artifact_ref(case_root, ref, "MAIL_CAPTURE_ARTIFACT", verify_artifacts, verified)
        rel = artifact["relative_path"]
        prior = unique_artifacts.get(rel)
        if prior is not None and (prior["sha256"], prior["bytes"]) != (artifact["sha256"], artifact["bytes"]):
            raise RuntimeError("MAIL_CAPTURE_ARTIFACT_DECLARATION_CONFLICT")
        unique_artifacts[rel] = artifact
        identity = artifact.get("source_identity_sha256")
        ordinal = len(occurrences) + 1
        occurrence_id = sha256_bytes(
            f"mail_capture\0{role}\0{owner_sha}\0{identity or ''}\0{rel}\0{artifact['sha256']}\0{ordinal}".encode("utf-8")
        )
        occurrences.append({
            "occurrence_id": occurrence_id,
            "content_sha256": artifact["sha256"],
            "source_scope": "mail_capture",
            "role": role,
            "source_rel": rel,
            "source_identity_sha256": identity,
            "owner_sha256": owner_sha,
            "resolution": resolution,
        })

    for mail in mails:
        if not isinstance(mail, dict):
            raise RuntimeError("MAIL_CAPTURE_MANIFEST_MAIL_INVALID")
        mail_id = str(mail.get("mail_id") or "")
        if not mail_id or mail_id in mail_ids:
            raise RuntimeError("MAIL_CAPTURE_MANIFEST_MAIL_INVALID")
        mail_ids.add(mail_id)
        owner_sha = sha256_bytes(mail_id.encode("utf-8"))
        status = str(mail.get("detail_status") or "AVAILABLE")
        if status not in {"AVAILABLE", "SOURCE_DELETED"}:
            raise RuntimeError("MAIL_CAPTURE_MANIFEST_DETAIL_STATUS_INVALID")
        add_occurrence("mail_detail", owner_sha, mail.get("detail"), status)
        add_occurrence("mail_track", owner_sha, mail.get("track"), "CAPTURED")
        if status == "AVAILABLE":
            detail_available += 1
        else:
            detail_deleted += 1
            detail_ref = mail.get("detail") if isinstance(mail.get("detail"), dict) else {}
            identity = str(detail_ref.get("source_identity_sha256") or "").casefold() or None
            gap_id = sha256_bytes(f"mail_capture\0mail_detail\0{owner_sha}\0SOURCE_DELETED".encode("utf-8"))
            source_gaps.append({
                "gap_id": gap_id, "content_sha256": None, "source_scope": "mail_capture",
                "role": "mail_detail", "source_identity_sha256": identity, "owner_sha256": owner_sha,
                "error_code": "SOURCE_DELETED", "http_status": None,
            })
        related = mail.get("related_resources")
        if not isinstance(related, list):
            raise RuntimeError("MAIL_CAPTURE_MANIFEST_RESOURCE_INVALID")
        related_candidates += len(related)
        for resource in related:
            if not isinstance(resource, dict):
                raise RuntimeError("MAIL_CAPTURE_MANIFEST_RESOURCE_INVALID")
            resolution = str(resource.get("resolution") or "UNRESOLVED")
            artifact = resource.get("artifact")
            identity = str(resource.get("source_identity_sha256") or "").casefold() or None
            if artifact is not None:
                if resolution not in {"EXACT_SOURCE_IDENTITY", "RECOVERED_BROKEN_ALIAS"}:
                    raise RuntimeError("MAIL_CAPTURE_MANIFEST_RESOURCE_RESOLUTION_INVALID")
                add_occurrence("mail_related_resource", owner_sha, artifact, resolution)
                related_artifacts += 1
                continue
            if resolution == "SOURCE_UNAVAILABLE":
                try:
                    http_status = int(resource.get("source_unavailable_http_status"))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("MAIL_CAPTURE_MANIFEST_RESOURCE_SOURCE_UNAVAILABLE_INVALID") from exc
                if http_status not in {400, 403, 404, 410}:
                    raise RuntimeError("MAIL_CAPTURE_MANIFEST_RESOURCE_SOURCE_UNAVAILABLE_INVALID")
                related_unavailable += 1
                error_code = "SOURCE_UNAVAILABLE"
            elif resolution == "UNRESOLVED":
                http_status = None
                related_unresolved += 1
                error_code = "UNRESOLVED"
            else:
                raise RuntimeError("MAIL_CAPTURE_MANIFEST_RESOURCE_RESOLUTION_INVALID")
            gap_id = sha256_bytes(
                f"mail_capture\0mail_related_resource\0{owner_sha}\0{identity or ''}\0{error_code}".encode("utf-8")
            )
            source_gaps.append({
                "gap_id": gap_id, "content_sha256": None, "source_scope": "mail_capture",
                "role": "mail_related_resource", "source_identity_sha256": identity,
                "owner_sha256": owner_sha, "error_code": error_code, "http_status": http_status,
            })

    expected = {
        "mails": len(mails),
        "detail_artifacts": len(mails),
        "track_artifacts": len(mails),
        "detail_available": detail_available,
        "detail_source_deleted": detail_deleted,
        "related_resource_candidates": related_candidates,
        "related_resource_artifacts": related_artifacts,
        "related_resource_source_unavailable": related_unavailable,
        "related_resource_unresolved": related_unresolved,
    }
    try:
        valid_counts = all(int(counts.get(key, -1)) == value for key, value in expected.items())
    except (TypeError, ValueError):
        valid_counts = False
    if not valid_counts:
        raise RuntimeError("MAIL_CAPTURE_MANIFEST_COUNTS_INVALID")
    historical_related_paths = {
        row["source_rel"] for row in occurrences
        if row["role"] == "mail_related_resource"
        and not Path(row["source_rel"]).is_relative_to(safe_relative(automatic["session_root"], case_root))
    }
    return {
        "manifest_path": manifest_path,
        "manifest_sha256": manifest_digest,
        "manifest_bytes": manifest_path.stat().st_size,
        "status": "PASS",
        "counts": expected,
        "artifacts": list(unique_artifacts.values()),
        "occurrences": occurrences,
        "source_gaps": source_gaps,
        "historical_related_files": len(historical_related_paths),
    }


def tasks_for(category: str, magic: str) -> list[str]:
    if magic == "bad-zip":
        return ["inspect_archive"]
    if category == "text":
        return ["extract_text"]
    if magic == "pdf":
        return ["extract_pdf_native", "ocr_pdf_all_pages"]
    if category == "office":
        return ["extract_office_xml"]
    if category == "legacy-office":
        return ["extract_legacy_office"]
    if category == "archive":
        return ["inspect_archive"]
    if category == "eps":
        return ["extract_eps_text", "ocr_eps_rendered"]
    if category == "image":
        return ["ocr_image"]
    if category in {"audio", "video", "media"}:
        result = ["ffprobe_media", "asr_dual_diarization"]
        if category in {"video", "media"}:
            result.append("video_frame_ocr")
        return result
    return ["classify_binary"]


def task_output_rel(task_id: str, kind: str, output_base: str = "derived/full_extract_v2") -> str:
    suffix = ".json" if kind not in {"asr_dual_diarization", "video_frame_ocr", "ocr_image", "ocr_pdf_all_pages", "ocr_eps_rendered", "extract_legacy_office"} else ""
    return f"{output_base}/artifacts/{kind}/{task_id}{suffix}"


def safe_archive_member_name(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    path = Path(normalized)
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return normalized


def sevenzip_executable() -> str | None:
    candidates = [
        shutil.which("7z"),
        shutil.which("7zz"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def parse_sevenzip_slt(text: str, archive: Path) -> list[dict[str, str]]:
    """Parse 7-Zip's stable ``-slt`` key/value listing without trusting paths."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in [*text.splitlines(), ""]:
        line = raw.rstrip("\r\n")
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key.strip()] = value
    archive_resolved = str(archive.resolve()).casefold()
    return [
        row for row in records
        if row.get("Path")
        and row.get("Path", "").casefold() != archive_resolved
        and ("Size" in row or row.get("Folder") == "+")
    ]


@contextlib.contextmanager
def open_sevenzip_member(executable: str, archive: Path, member: str) -> Any:
    """Stream one listed member to stdout so 7-Zip never writes its path."""
    process = subprocess.Popen(
        [executable, "x", "-so", "-y", "-bb0", "-sccUTF-8", "-spd", str(archive), member],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise SourceGap("RAR_EXTRACTION_FAILED")
    try:
        yield process.stdout
        process.stdout.close()
        stderr = process.stderr.read() if process.stderr is not None else b""
        return_code = process.wait()
        if return_code:
            raise SourceGap("RAR_MEMBER_EXTRACTION_FAILED", {"exit_code": return_code, "stderr_bytes": len(stderr)})
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stderr is not None:
            process.stderr.close()


def persist_content_stream(stream: Any, output_root: Path, suffix: str, cache: dict[str, Path], budget: ExpansionBudget | None = None) -> tuple[Path, str, int]:
    object_root = output_root / "expanded_objects" / "sha256"
    object_root.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".member.", suffix=".part", dir=object_root)
    os.close(descriptor)
    temp = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with temp.open("wb") as handle:
            for chunk in iter(lambda: stream.read(min(4 * 1024 * 1024, max(1, budget.max_bytes - budget.bytes + 1)) if budget else 4 * 1024 * 1024), b""):
                size += len(chunk)
                if budget:
                    budget.consume(len(chunk))
                if size > 2 * 1024 * 1024 * 1024:
                    raise SourceGap("ARCHIVE_MEMBER_TOO_LARGE")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        hexdigest = digest.hexdigest()
        existing = cache.get(hexdigest)
        if existing is not None:
            temp.unlink(missing_ok=True)
            return existing, hexdigest, size
        safe_suffix = suffix.casefold() if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix.casefold()) else ""
        directory = object_root / hexdigest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        matches = sorted(directory.glob(f"{hexdigest}.*")) + ([directory / hexdigest] if (directory / hexdigest).is_file() else [])
        if matches:
            target = matches[0]
            if target.stat().st_size != size or sha256_file(target) != hexdigest:
                raise RuntimeError("EXPANDED_OBJECT_HASH_CONFLICT")
            temp.unlink(missing_ok=True)
        else:
            target = directory / f"{hexdigest}{safe_suffix}"
            os.replace(temp, target)
        cache[hexdigest] = target
        return target, hexdigest, size
    finally:
        temp.unlink(missing_ok=True)


def expand_container_members(
    source: Path,
    container_sha256: str,
    category: str,
    output_root: Path,
    depth: int,
    object_cache: dict[str, Path],
    budget: ExpansionBudget | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    budget = budget or ExpansionBudget()
    members: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    if budget.exhausted_code:
        return members, [{"source_identity_sha256": None, "error_code": budget.exhausted_code, "http_status": None}]
    if depth >= 4:
        return members, [{
            "source_identity_sha256": None, "error_code": "ARCHIVE_RECURSION_LIMIT", "http_status": None,
        }]

    def gap(identity: str | None, code: str) -> None:
        gaps.append({"source_identity_sha256": identity, "error_code": code, "http_status": None})

    def accept(name: str, is_directory: bool, is_symlink: bool, encrypted: bool, declared_bytes: int, compressed_bytes: int, opener: Any) -> None:
        identity = sha256_bytes(name.encode("utf-8", errors="replace"))
        # Bound inspected entries too: rejected names and directories must not
        # evade the run-wide member cap or grow an unbounded gap inventory.
        try:
            budget.member()
        except SourceGap as exc:
            gap(identity, exc.code)
            return
        normalized = safe_archive_member_name(name)
        if normalized is None:
            gap(identity, "ARCHIVE_MEMBER_PATH_TRAVERSAL_BLOCKED")
            return
        if is_directory:
            return
        if is_symlink:
            gap(identity, "ARCHIVE_MEMBER_SYMLINK_BLOCKED")
            return
        if encrypted:
            gap(identity, "ARCHIVE_MEMBER_PASSWORD_REQUIRED")
            return
        if declared_bytes > 2 * 1024 * 1024 * 1024 or (declared_bytes > 100 * 1024 * 1024 and compressed_bytes > 0 and declared_bytes / compressed_bytes > 10000):
            gap(identity, "ARCHIVE_MEMBER_SAFETY_LIMIT")
            return
        if category == "office" and normalized.casefold().endswith((".xml", ".rels")):
            return
        if category == "office" and normalized.casefold() == "[content_types].xml":
            return
        try:
            with opener() as stream:
                path, digest, size = persist_content_stream(stream, output_root, Path(normalized).suffix, object_cache, budget)
        except SourceGap as exc:
            gap(identity, exc.code)
            return
        members.append({
            "path": path, "sha256": digest, "bytes": size,
            "source_identity_sha256": identity,
            "role": "office_embedded_member" if category == "office" else "archive_member",
            "source_scope": "office_embedded" if category == "office" else "archive_member",
            "depth": depth + 1,
        })

    magic, _, _ = identify(source)
    if magic in {"zip", "ooxml-word", "ooxml-excel", "ooxml-powerpoint"}:
        try:
            with zipfile.ZipFile(source) as archive:
                for info in sorted(archive.infolist(), key=lambda item: item.filename.casefold()):
                    mode = (info.external_attr >> 16) & 0xFFFF
                    accept(
                        info.filename, info.is_dir(), stat.S_ISLNK(mode), bool(info.flag_bits & 0x1),
                        int(info.file_size), int(info.compress_size),
                        lambda info=info, archive=archive: archive.open(info, "r"),
                    )
                    if budget.exhausted_code:
                        break
        except zipfile.BadZipFile:
            gap(None, "ARCHIVE_CORRUPT")
    elif magic == "rar":
        executable = sevenzip_executable()
        if executable is None:
            gap(None, "RAR_EXTRACTOR_UNAVAILABLE")
        else:
            listing = subprocess.run(
                [executable, "l", "-slt", "-sccUTF-8", "-bb0", str(source)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if listing.returncode:
                gap(None, "RAR_LISTING_FAILED")
            else:
                records = parse_sevenzip_slt(listing.stdout, source)
                if not records:
                    gap(None, "RAR_LISTING_EMPTY")
                for info in sorted(records, key=lambda item: item.get("Path", "").casefold()):
                    name = info.get("Path", "")
                    attributes = info.get("Attributes", "").upper()
                    symbolic_target = info.get("Symbolic Link") or info.get("Hard Link")
                    accept(
                        name,
                        info.get("Folder") == "+",
                        bool(symbolic_target) or attributes.startswith("L"),
                        info.get("Encrypted") == "+",
                        int(info.get("Size") or 0),
                        int(info.get("Packed Size") or 0),
                        lambda name=name: open_sevenzip_member(executable, source, name),
                    )
                    if budget.exhausted_code:
                        break
    return members, gaps


# EN: Validate identity/scope and create resumable work without changing originals.
# 中文：验证身份和范围，创建可续跑任务且不修改原件。
def prepare(case_root: Path, company_id: str, output_dir: Path | str | None = None, *, archive_total_bytes: int = DEFAULT_ARCHIVE_TOTAL_BYTES, archive_max_members: int = DEFAULT_ARCHIVE_MAX_MEMBERS) -> dict[str, Any]:
    with case_extraction_lock(case_root):
        return _prepare_locked(case_root, company_id, output_dir, archive_total_bytes, archive_max_members)


def _prepare_locked(case_root, company_id, output_dir, archive_total_bytes, archive_max_members):
    expansion_budget = ExpansionBudget(archive_total_bytes, archive_max_members)
    load_identity(case_root, company_id)
    automatic = load_automatic_scope(case_root, company_id)
    ui_gap = load_ui_gap_scope(case_root, company_id)
    automatic_gaps = load_automatic_source_gaps(case_root, company_id, automatic["session_id"])
    manual = load_manual_trade_scope(case_root, company_id, verify_artifacts=True)
    mail = load_mail_capture_scope(case_root, company_id, automatic, verify_artifacts=True)
    output = full_extract_root(case_root, output_dir)
    output.mkdir(parents=True, exist_ok=True)
    output_base = safe_relative(output, case_root)
    db = open_db(output / "state.sqlite3")
    legacy_path = case_root / "derived" / "full_extract" / "state.sqlite3"
    legacy = sqlite3.connect(f"{legacy_path.resolve().as_uri()}?mode=ro", uri=True) if legacy_path.is_file() and legacy_path.resolve() != (output / "state.sqlite3").resolve() else None
    if legacy is not None:
        legacy.row_factory = sqlite3.Row
    category_counts: dict[str, int] = {}
    scope_counts: dict[str, int] = {}
    timestamp = now()
    try:
        db.execute("BEGIN IMMEDIATE")
        for table in ("files", "tasks", "content_objects", "occurrences", "source_gaps"):
            db.execute(f"UPDATE {table} SET active=0")

        sources: dict[str, dict[str, Any]] = {}
        occurrences: dict[str, dict[str, Any]] = {}

        def add_source(source_scope: str, path: Path, known_digest: str | None, role: str, add_occurrence: bool = True, depth: int = 0) -> None:
            rel = safe_relative(path, case_root)
            prior = sources.get(rel)
            if prior is not None:
                if known_digest and prior.get("known_digest") and prior["known_digest"] != known_digest:
                    raise RuntimeError("EVIDENCE_SCOPE_DECLARATION_CONFLICT")
                return
            sources[rel] = {"source_scope": source_scope, "path": path, "known_digest": known_digest, "depth": depth}
            if add_occurrence:
                digest = known_digest or sha256_file(path)
                sources[rel]["known_digest"] = digest
                occurrence_id = sha256_bytes(f"{source_scope}\0{role}\0{rel}\0{digest}".encode("utf-8"))
                occurrences[occurrence_id] = {
                    "occurrence_id": occurrence_id, "content_sha256": digest,
                    "source_scope": source_scope, "role": role, "source_rel": rel,
                    "source_identity_sha256": None, "owner_sha256": None, "resolution": "CAPTURED",
                }

        for path in sorted(
            (p for p in automatic["session_root"].rglob("*") if p.is_file() and not p.name.endswith(".part")),
            key=lambda p: p.as_posix().casefold(),
        ):
            add_source("automatic", path, None, "automatic_session_object")
        if ui_gap["status"] == "PASS":
            for path in sorted(
                (p for p in ui_gap["session_root"].rglob("*") if p.is_file() and not p.name.endswith(".part")),
                key=lambda p: p.as_posix().casefold(),
            ):
                add_source("ui_gap_revisit", path, None, "ui_gap_revisit_object")
        for row in manual["artifacts"]:
            add_source("manual_trade", row["path"], row["sha256"], "manual_trade_artifact")

        related_paths = {row["source_rel"] for row in mail["occurrences"] if row["role"] == "mail_related_resource"}
        for row in mail["artifacts"]:
            rel = row["relative_path"]
            if rel not in sources:
                scope = "historical_mail_related" if rel in related_paths else "historical_mail_capture"
                add_source(scope, row["path"], row["sha256"], "mail_manifest_artifact", add_occurrence=False)
        for row in mail["occurrences"]:
            if row["occurrence_id"] in occurrences and occurrences[row["occurrence_id"]] != row:
                raise RuntimeError("PROCESSING_SCOPE_OCCURRENCE_COLLISION")
            occurrences[row["occurrence_id"]] = row

        preparation_gaps: list[dict[str, Any]] = []
        expanded_containers: set[str] = set()
        expanded_object_cache: dict[str, Path] = {}
        queue = list(sorted(sources))
        queue_index = 0
        while queue_index < len(queue):
            rel = queue[queue_index]
            queue_index += 1
            source = sources[rel]
            path = source["path"]
            digest = source["known_digest"] or sha256_file(path)
            source["known_digest"] = digest
            magic, category, _ = identify(path)
            if category not in {"archive", "office"} or digest in expanded_containers:
                continue
            expanded_containers.add(digest)
            members, archive_gaps = expand_container_members(
                path, digest, category, output, int(source.get("depth") or 0), expanded_object_cache, expansion_budget,
            )
            for gap in archive_gaps:
                identity = gap.get("source_identity_sha256")
                code = gap["error_code"]
                preparation_gaps.append({
                    "gap_id": sha256_bytes(f"container\0{digest}\0{identity or ''}\0{code}".encode("utf-8")),
                    "content_sha256": digest, "source_scope": "container_processing",
                    "role": "office_embedded_member" if category == "office" else "archive_member",
                    "source_identity_sha256": identity, "owner_sha256": digest,
                    "error_code": code, "http_status": None,
                })
            for member in members:
                member_rel = safe_relative(member["path"], case_root)
                is_new_source = member_rel not in sources
                add_source(
                    member["source_scope"], member["path"], member["sha256"], member["role"],
                    add_occurrence=False, depth=member["depth"],
                )
                occurrence_id = sha256_bytes(
                    f"{member['source_scope']}\0{digest}\0{member['source_identity_sha256']}\0{member['sha256']}".encode("utf-8")
                )
                occurrences[occurrence_id] = {
                    "occurrence_id": occurrence_id, "content_sha256": member["sha256"],
                    "source_scope": member["source_scope"], "role": member["role"],
                    "source_rel": member_rel, "source_identity_sha256": member["source_identity_sha256"],
                    "owner_sha256": digest, "resolution": "SAFE_EXTRACTED",
                }
                if is_new_source:
                    queue.append(member_rel)

        content_objects: dict[str, dict[str, Any]] = {}
        for rel, source in sorted(sources.items(), key=lambda item: item[0].casefold()):
            path = source["path"]
            digest = source["known_digest"] or sha256_file(path)
            stat = path.stat()
            magic, category, mime = identify(path)
            scope = source["source_scope"]
            category_counts[category] = category_counts.get(category, 0) + 1
            scope_counts[scope] = scope_counts.get(scope, 0) + 1
            db.execute(
                """INSERT INTO files(source_rel,sha256,bytes,mtime_ns,magic,category,mime,source_scope,active,prepared_at)
                   VALUES(?,?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(source_rel) DO UPDATE SET sha256=excluded.sha256,bytes=excluded.bytes,
                   mtime_ns=excluded.mtime_ns,magic=excluded.magic,category=excluded.category,
                   mime=excluded.mime,source_scope=excluded.source_scope,active=1,prepared_at=excluded.prepared_at""",
                (rel, digest, stat.st_size, stat.st_mtime_ns, magic, category, mime, scope, timestamp),
            )
            existing = content_objects.get(digest)
            if existing is not None and existing["bytes"] != stat.st_size:
                raise RuntimeError("CONTENT_OBJECT_SIZE_CONFLICT")
            if existing is None:
                content_objects[digest] = {
                    "sha256": digest, "bytes": stat.st_size, "magic": magic,
                    "category": category, "mime": mime, "representative_rel": rel,
                }
                db.execute(
                    """INSERT INTO content_objects(sha256,bytes,magic,category,mime,representative_rel,active,prepared_at)
                       VALUES(?,?,?,?,?,?,1,?)
                       ON CONFLICT(sha256) DO UPDATE SET bytes=excluded.bytes,magic=excluded.magic,
                       category=excluded.category,mime=excluded.mime,representative_rel=excluded.representative_rel,
                       active=1,prepared_at=excluded.prepared_at""",
                    (digest, stat.st_size, magic, category, mime, rel, timestamp),
                )

        for row in occurrences.values():
            if row["content_sha256"] not in content_objects:
                raise RuntimeError("PROCESSING_SCOPE_OCCURRENCE_OBJECT_MISSING")
            db.execute(
                """INSERT INTO occurrences(occurrence_id,content_sha256,source_scope,role,source_rel,
                   source_identity_sha256,owner_sha256,resolution,active,prepared_at)
                   VALUES(?,?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(occurrence_id) DO UPDATE SET content_sha256=excluded.content_sha256,
                   source_scope=excluded.source_scope,role=excluded.role,source_rel=excluded.source_rel,
                   source_identity_sha256=excluded.source_identity_sha256,owner_sha256=excluded.owner_sha256,
                   resolution=excluded.resolution,active=1,prepared_at=excluded.prepared_at""",
                (row["occurrence_id"], row["content_sha256"], row["source_scope"], row["role"], row["source_rel"],
                 row.get("source_identity_sha256"), row.get("owner_sha256"), row["resolution"], timestamp),
            )

        gap_rows = [*automatic_gaps["source_gaps"], *mail["source_gaps"], *preparation_gaps]
        for index, gap in enumerate(manual.get("source_gaps", []), 1):
            code = re.sub(r"[^A-Z0-9_\-]+", "_", str(gap.get("code") or "MANUAL_TRADE_SOURCE_GAP").upper())[:120]
            gap_rows.append({
                "gap_id": sha256_bytes(f"manual_trade\0{index}\0{code}".encode("utf-8")),
                "content_sha256": None, "source_scope": "manual_trade", "role": "manual_trade_source",
                "source_identity_sha256": None, "owner_sha256": None,
                "error_code": code or "MANUAL_TRADE_SOURCE_GAP", "http_status": None,
            })
        gap_rows = list({row["gap_id"]: row for row in gap_rows}.values())
        for row in gap_rows:
            db.execute(
                """INSERT INTO source_gaps(gap_id,content_sha256,source_scope,role,source_identity_sha256,
                   owner_sha256,error_code,http_status,active,recorded_at) VALUES(?,?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(gap_id) DO UPDATE SET content_sha256=excluded.content_sha256,
                   source_scope=excluded.source_scope,role=excluded.role,
                   source_identity_sha256=excluded.source_identity_sha256,owner_sha256=excluded.owner_sha256,
                   error_code=excluded.error_code,http_status=excluded.http_status,active=1,recorded_at=excluded.recorded_at""",
                (row["gap_id"], row.get("content_sha256"), row["source_scope"], row["role"],
                 row.get("source_identity_sha256"), row.get("owner_sha256"), row["error_code"],
                 row.get("http_status"), timestamp),
            )

        reused_legacy = 0
        for digest, obj in sorted(content_objects.items()):
            for kind in tasks_for(obj["category"], obj["magic"]):
                prior = db.execute(
                    "SELECT * FROM tasks WHERE content_sha256=? AND task_kind=? ORDER BY (state='completed') DESC,updated_at DESC LIMIT 1",
                    (digest, kind),
                ).fetchone()
                task_id = str(prior["task_id"]) if prior else sha256_bytes(f"v2\0{digest}\0{kind}".encode("utf-8"))
                source_rel = str(prior["source_rel"]) if prior and str(prior["source_rel"]) in sources else obj["representative_rel"]
                state, result_sha, error_code = "pending", None, None
                output_rel = task_output_rel(task_id, kind, output_base)
                candidates = [prior] if prior is not None else []
                if legacy is not None:
                    candidates.extend(legacy.execute(
                        "SELECT * FROM tasks WHERE source_sha256=? AND task_kind=? AND state='completed' ORDER BY updated_at DESC",
                        (digest, kind),
                    ).fetchall())
                for candidate_index, candidate in enumerate(candidates):
                    if candidate is None or candidate["state"] not in TERMINAL_TASK_STATES or not candidate["output_rel"]:
                        continue
                    prior_output = case_root / str(candidate["output_rel"])
                    if prior_output.exists() and candidate["result_sha256"] and stable_tree_hash(prior_output) == candidate["result_sha256"]:
                        state, output_rel, result_sha = str(candidate["state"]), str(candidate["output_rel"]), str(candidate["result_sha256"])
                        error_code = candidate["error_code"]
                        if legacy is not None and (prior is None or candidate_index > 0):
                            reused_legacy += 1
                        break
                db.execute(
                    """INSERT INTO tasks(task_id,source_rel,source_sha256,content_sha256,task_kind,state,active,
                       output_rel,result_sha256,error_code,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?,?)
                       ON CONFLICT(task_id) DO UPDATE SET source_rel=excluded.source_rel,
                       source_sha256=excluded.source_sha256,content_sha256=excluded.content_sha256,
                       task_kind=excluded.task_kind,state=excluded.state,active=1,output_rel=excluded.output_rel,
                       result_sha256=excluded.result_sha256,error_code=excluded.error_code,updated_at=excluded.updated_at""",
                    (task_id, source_rel, digest, digest, kind, state, output_rel, result_sha, error_code, timestamp),
                )
                if state == "source_gap" and error_code:
                    task_gap = {
                        "gap_id": sha256_bytes(f"task_processing\0{task_id}\0{error_code}".encode("utf-8")),
                        "content_sha256": digest, "source_scope": "task_processing", "role": kind,
                        "source_identity_sha256": None, "owner_sha256": None,
                        "error_code": str(error_code), "http_status": None,
                    }
                    gap_rows.append(task_gap)
                    db.execute(
                        """INSERT INTO source_gaps(gap_id,content_sha256,source_scope,role,error_code,active,recorded_at)
                           VALUES(?,?,?,?,?,1,?) ON CONFLICT(gap_id) DO UPDATE SET active=1,recorded_at=excluded.recorded_at""",
                        (task_gap["gap_id"], digest, "task_processing", kind, error_code, timestamp),
                    )

        meta = {
            "schema": SCHEMA, "scope_schema": PROCESSING_SCOPE_V2_SCHEMA,
            "company_id": company_id, "prepared_at": timestamp,
            "output_root_rel": output_base,
            "automatic_session_id": automatic["session_id"],
            "processing_scope_v1_rel": safe_relative(automatic["processing_scope_path"], case_root),
            "processing_scope_v1_sha256": automatic["processing_scope_sha256"],
            "ui_gap_status": ui_gap["status"],
            "ui_gap_manifest_rel": safe_relative(ui_gap["manifest_path"], case_root) if ui_gap["manifest_path"] else "",
            "ui_gap_manifest_sha256": ui_gap["manifest_sha256"] or "",
            "automatic_final_status_rel": safe_relative(automatic["final_status_path"], case_root),
            "automatic_final_status_sha256": automatic["final_status_sha256"],
            "capture_reconciliation_rel": safe_relative(automatic_gaps["path"], case_root) if automatic_gaps["path"] else "",
            "capture_reconciliation_sha256": automatic_gaps["sha256"] or "",
            "manual_trade_manifest_rel": safe_relative(manual["manifest_path"], case_root),
            "manual_trade_manifest_sha256": manual["manifest_sha256"],
            "manual_trade_status": manual["status"],
            "mail_capture_manifest_rel": safe_relative(mail["manifest_path"], case_root) if mail["manifest_path"] else "",
            "mail_capture_manifest_sha256": mail["manifest_sha256"] or "",
        }
        db.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", meta.items())
        db.commit()

        counts = {
            "evidence_files": len(sources),
            "content_objects": len(content_objects),
            "occurrences": len(occurrences),
            "source_gaps": len(gap_rows),
            "evidence_bytes": int(db.execute("SELECT COALESCE(SUM(bytes),0) FROM files WHERE active=1").fetchone()[0]),
            "automatic_files": scope_counts.get("automatic", 0),
            "ui_gap_files": scope_counts.get("ui_gap_revisit", 0),
            "manual_trade_files": scope_counts.get("manual_trade", 0),
            "historical_mail_related_files": scope_counts.get("historical_mail_related", 0),
            "tasks": int(db.execute("SELECT COUNT(*) FROM tasks WHERE active=1").fetchone()[0]),
            "media_files": sum(category_counts.get(item, 0) for item in ("audio", "video", "media")),
            "manual_source_gaps": manual["source_gap_count"],
            "mail_source_gaps": len(mail["source_gaps"]),
            "automatic_source_gaps": len(automatic_gaps["source_gaps"]),
            "container_source_gaps": len(preparation_gaps),
            "reused_legacy_tasks": reused_legacy,
        }
        scope_payload = {
            "schema": PROCESSING_SCOPE_V2_SCHEMA, "schema_version": 2,
            "company_id": company_id, "generated_at": timestamp,
            "status": "PASS_WITH_SOURCE_GAPS" if gap_rows else "PASS",
            "parents": {
                "processing_scope_v1": {"relative_path": meta["processing_scope_v1_rel"], "sha256": automatic["processing_scope_sha256"]},
                "automatic_final_status": {"relative_path": meta["automatic_final_status_rel"], "sha256": automatic["final_status_sha256"]},
                "capture_reconciliation": ({"relative_path": meta["capture_reconciliation_rel"], "sha256": automatic_gaps["sha256"]} if automatic_gaps["path"] else None),
                "manual_trade_manifest": {"relative_path": meta["manual_trade_manifest_rel"], "sha256": manual["manifest_sha256"]},
                "mail_capture_manifest": ({"relative_path": meta["mail_capture_manifest_rel"], "sha256": mail["manifest_sha256"]} if mail["manifest_path"] else None),
                "ui_gap_revisit_manifest": ({"relative_path": meta["ui_gap_manifest_rel"], "sha256": ui_gap["manifest_sha256"]} if ui_gap["manifest_path"] else None),
            },
            "source_summaries": {
                "automatic": {"session_id": automatic["session_id"], "files": scope_counts.get("automatic", 0)},
                "ui_gap_revisit": {"status": ui_gap["status"], "session_id": ui_gap.get("session_id"), "files": scope_counts.get("ui_gap_revisit", 0)},
                "manual_trade": {"status": manual["status"], "files": scope_counts.get("manual_trade", 0), "source_gaps": manual["source_gap_count"]},
                "mail_capture": {
                    "status": mail["status"], "declared_counts": mail["counts"],
                    "unique_artifact_files": len(mail["artifacts"]),
                    "historical_related_files": scope_counts.get("historical_mail_related", 0),
                    "source_gaps": len(mail["source_gaps"]),
                },
            },
            "counts": counts,
            "content_objects": [dict(row) for row in db.execute("SELECT sha256,bytes,magic,category,mime,representative_rel FROM content_objects WHERE active=1 ORDER BY sha256")],
            "occurrences": [dict(row) for row in db.execute("SELECT occurrence_id,content_sha256,source_scope,role,source_rel,source_identity_sha256,owner_sha256,resolution FROM occurrences WHERE active=1 ORDER BY occurrence_id")],
            "source_gaps": [dict(row) for row in db.execute("SELECT gap_id,content_sha256,source_scope,role,source_identity_sha256,owner_sha256,error_code,http_status FROM source_gaps WHERE active=1 ORDER BY gap_id")],
        }
        scope_path = output / PROCESSING_SCOPE_V2_NAME
        atomic_json(scope_path, scope_payload)
        scope_sha = sha256_file(scope_path)
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('processing_scope_v2_rel',?)", (safe_relative(scope_path, case_root),))
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('processing_scope_v2_sha256',?)", (scope_sha,))
        db.commit()
        write_indexes(case_root, db, output)
        manifest = {
            "schema": SCHEMA, "stage": "prepare", "company_id": company_id,
            "generated_at": now(), "processing_scope_v2": {"relative_path": safe_relative(scope_path, case_root), "sha256": scope_sha},
            "counts": counts, "categories": category_counts,
        }
        atomic_json(output / "prepare_manifest.json", manifest)
        counts["manifest_sha256"] = sha256_file(output / "prepare_manifest.json")  # type: ignore[assignment]
        counts["processing_scope_v2_sha256"] = scope_sha  # type: ignore[assignment]
        return counts
    finally:
        if legacy is not None:
            legacy.close()
        db.close()


def run_quiet(command: list[str], log_path: Path, cwd: Path | None = None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        completed = subprocess.run(command, cwd=cwd, stdout=log, stderr=log, check=False)
    if completed.returncode:
        raise RuntimeError(f"SUBPROCESS_EXIT_{completed.returncode}")


def tool(value: str | None, default_name: str) -> str:
    if value:
        path = Path(value)
        if not path.is_file():
            raise RuntimeError(f"{default_name.upper()}_NOT_FOUND")
        return str(path)
    found = shutil.which(default_name)
    if not found:
        raise RuntimeError(f"{default_name.upper()}_NOT_FOUND")
    return found


def extract_text(source: Path) -> dict[str, Any]:
    raw = source.read_bytes()
    decoded = decode_text(raw)
    if source.suffix.casefold() == ".json":
        try:
            text = "\n".join(json_scalar_text(json.loads(decoded)))
        except json.JSONDecodeError:
            text = decoded
    elif source.suffix.casefold() in {".html", ".htm", ".xhtml", ".rawhtml"}:
        parser = VisibleText()
        parser.feed(decoded)
        text = html.unescape("\n".join(parser.parts))
    else:
        text = decoded
    return {"schema": SCHEMA, "kind": "text", "source_sha256": sha256_file(source), "text": text, "text_chars": len(text)}


def extract_pdf(source: Path) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PYPDF_NOT_AVAILABLE") from exc
    try:
        reader = PdfReader(source)
    except Exception as exc:
        raise SourceGap("PDF_PARSE_FAILED") from exc
    encryption_state = "NOT_ENCRYPTED"
    if reader.is_encrypted:
        try:
            unlocked = bool(reader.decrypt(""))
        except Exception:
            unlocked = False
        if not unlocked:
            raise SourceGap("PDF_PASSWORD_REQUIRED")
        encryption_state = "PDF_ENCRYPTED_EMPTY_PASSWORD_ACCESSIBLE"
    pages = [{"page": index, "text": page.extract_text() or ""} for index, page in enumerate(reader.pages, 1)]
    return {"schema": SCHEMA, "kind": "pdf_native_text", "source_sha256": sha256_file(source), "encryption_state": encryption_state, "pages": pages, "page_count": len(pages), "text_chars": sum(len(p["text"]) for p in pages)}


def extract_office(source: Path) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    with zipfile.ZipFile(source) as archive:
        for name in sorted(archive.namelist(), key=str.casefold):
            if not name.casefold().endswith((".xml", ".rels")):
                continue
            data = archive.read(name)
            try:
                root = ElementTree.fromstring(data)
            except ElementTree.ParseError:
                continue
            values: list[str] = []
            for element in root.iter():
                if element.text and element.text.strip():
                    values.append(element.text)
                for key, value in sorted(element.attrib.items()):
                    if value.strip():
                        values.append(f"@{key}={value}")
                if element.tail and element.tail.strip():
                    values.append(element.tail)
            if values:
                parts.append({"part": name, "text": "\n".join(values), "text_chars": sum(len(v) for v in values)})
    return {"schema": SCHEMA, "kind": "office_xml_text", "source_sha256": sha256_file(source), "parts": parts, "part_count": len(parts), "text_chars": sum(p["text_chars"] for p in parts)}


def inspect_archive(source: Path) -> dict[str, Any]:
    magic, _, _ = identify(source)
    if magic == "rar":
        executable = sevenzip_executable()
        if executable is None:
            raise SourceGap("RAR_EXTRACTOR_UNAVAILABLE", {"format": "rar"})
        listing = subprocess.run(
            [executable, "l", "-slt", "-sccUTF-8", "-bb0", str(source)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        if listing.returncode:
            raise SourceGap("RAR_LISTING_FAILED", {"format": "rar", "exit_code": listing.returncode})
        members = []
        encrypted = 0
        blocked = 0
        for info in sorted(parse_sevenzip_slt(listing.stdout, source), key=lambda item: item.get("Path", "").casefold()):
            name = info.get("Path", "")
            is_encrypted = info.get("Encrypted") == "+"
            safe_name = safe_archive_member_name(name)
            attributes = info.get("Attributes", "").upper()
            is_symlink = bool(info.get("Symbolic Link") or info.get("Hard Link")) or attributes.startswith("L")
            encrypted += int(is_encrypted)
            blocked += int(safe_name is None or is_symlink)
            members.append({
                "name": name,
                "name_sha256": sha256_bytes(name.encode("utf-8", errors="replace")),
                "bytes": int(info.get("Size") or 0),
                "compressed_bytes": int(info.get("Packed Size") or 0),
                "crc32": str(info.get("CRC") or ""),
                "is_directory": info.get("Folder") == "+",
                "encrypted": is_encrypted,
                "path_safe": safe_name is not None,
                "symlink": is_symlink,
                "content_sha256": None,
            })
        payload = {
            "schema": SCHEMA, "kind": "archive_inventory", "source_sha256": sha256_file(source),
            "format": "rar", "member_count": len(members), "encrypted_members": encrypted,
            "blocked_members": blocked, "members": members,
        }
        if encrypted:
            raise SourceGap("ARCHIVE_PASSWORD_REQUIRED", payload)
        if blocked:
            raise SourceGap("ARCHIVE_UNSAFE_MEMBERS_BLOCKED", payload)
        return payload
    if magic not in {"zip", "bad-zip"}:
        raise SourceGap("ARCHIVE_FORMAT_UNSUPPORTED", {"format": magic})
    try:
        with zipfile.ZipFile(source) as archive:
            members = []
            encrypted = 0
            blocked = 0
            for info in sorted(archive.infolist(), key=lambda item: item.filename.casefold()):
                is_encrypted = bool(info.flag_bits & 0x1)
                mode = (info.external_attr >> 16) & 0xFFFF
                safe_name = safe_archive_member_name(info.filename)
                is_symlink = stat.S_ISLNK(mode)
                encrypted += int(is_encrypted)
                blocked += int(safe_name is None or is_symlink)
                member_sha = None
                if safe_name is not None and not info.is_dir() and not is_symlink and not is_encrypted:
                    digest = hashlib.sha256()
                    with archive.open(info, "r") as stream:
                        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                            digest.update(chunk)
                    member_sha = digest.hexdigest()
                members.append({
                    "name": info.filename,
                    "name_sha256": sha256_bytes(info.filename.encode("utf-8", errors="replace")),
                    "bytes": info.file_size,
                    "compressed_bytes": info.compress_size,
                    "crc32": f"{info.CRC:08x}",
                    "is_directory": info.is_dir(),
                    "encrypted": is_encrypted,
                    "path_safe": safe_name is not None,
                    "symlink": is_symlink,
                    "content_sha256": member_sha,
                })
    except zipfile.BadZipFile as exc:
        raise SourceGap("ARCHIVE_CORRUPT") from exc
    payload = {
        "schema": SCHEMA, "kind": "archive_inventory", "source_sha256": sha256_file(source),
        "format": "zip", "member_count": len(members), "encrypted_members": encrypted,
        "blocked_members": blocked,
        "members": members,
    }
    if encrypted:
        raise SourceGap("ARCHIVE_PASSWORD_REQUIRED", payload)
    if blocked:
        raise SourceGap("ARCHIVE_UNSAFE_MEMBERS_BLOCKED", payload)
    return payload


def extract_eps(source: Path) -> dict[str, Any]:
    text = decode_text(source.read_bytes())
    return {
        "schema": SCHEMA, "kind": "eps_text", "source_sha256": sha256_file(source),
        "text": text, "text_chars": len(text),
    }


def classify_binary(source: Path) -> dict[str, Any]:
    magic, category, mime = identify(source)
    return {
        "schema": SCHEMA, "kind": "binary_classification", "source_sha256": sha256_file(source),
        "bytes": source.stat().st_size, "magic": magic, "category": category, "mime": mime,
    }


def _extract_legacy_office_libreoffice(source: Path, output_dir: Path) -> Path:
    """Fail-closed local fallback after the required read-only COM attempt.

    LibreOffice gets a private disposable profile so a damaged legacy file
    cannot block on a shared first-run, recovery, or lock dialog.  It is never
    used for an unknown OLE container merely because the magic bytes match.
    """
    target = {
        ".doc": ("docx", "converted.docx"),
        ".xls": ("xlsx", "converted.xlsx"),
        ".ppt": ("pptx", "converted.pptx"),
    }.get(source.suffix.casefold())
    if target is None:
        raise SourceGap("OPAQUE_BINARY")
    script = Path(__file__).with_name("convert_legacy_office_libreoffice.ps1")
    if not script.is_file():
        raise SourceGap("LEGACY_OFFICE_ALL_LOCAL_CONVERTERS_UNAVAILABLE")
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "libreoffice_fallback.log"
    with log_path.open("ab") as log:
        try:
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script),
                    "-Source", str(source.resolve()), "-OutputDir", str(output_dir.resolve()),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceGap("LEGACY_OFFICE_ALL_LOCAL_CONVERTERS_TIMEOUT") from exc
    converted = output_dir / target[1]
    if completed.returncode or not converted.is_file() or converted.stat().st_size <= 0:
        # antiword is a final read-only text salvage path for genuine Word
        # binaries that both Office and LibreOffice reject.  Its stdout is
        # captured directly to the local artifact and never printed.
        antiword_path = os.environ.get("CRM_ANTIWORD") or shutil.which("antiword")
        antiword = Path(antiword_path) if antiword_path else None
        if source.suffix.casefold() == ".doc" and antiword is not None and antiword.is_file():
            try:
                salvaged = subprocess.run(
                    [str(antiword), str(source.resolve())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                raise SourceGap("LEGACY_OFFICE_ALL_LOCAL_CONVERTERS_TIMEOUT") from exc
            with (output_dir / "antiword_fallback.log").open("ab") as log:
                log.write(salvaged.stderr)
            text_value = decode_text(salvaged.stdout) if salvaged.returncode == 0 else ""
            if text_value.strip():
                text_path = output_dir / "converted.txt"
                atomic_text(text_path, text_value)
                payload = {
                    "schema": SCHEMA,
                    "kind": "legacy_office_salvaged_text",
                    "source_sha256": sha256_file(source),
                    "text": text_value,
                    "text_chars": len(text_value),
                    "conversion_engine": "antiword_after_com_and_libreoffice_failure",
                    "structure_limit": "PLAIN_TEXT_ONLY",
                }
                atomic_json(output_dir / "index.json", payload)
                return output_dir
        raise SourceGap("LEGACY_OFFICE_ALL_LOCAL_CONVERTERS_FAILED")
    payload = extract_office(converted)
    payload.update({
        "kind": "legacy_office_converted_text",
        "converted_sha256": sha256_file(converted),
        "conversion_engine": "libreoffice_headless_after_com_failure",
    })
    atomic_json(output_dir / "index.json", payload)
    return output_dir


def extract_legacy_office(source: Path, output_dir: Path) -> Path:
    if source.suffix.casefold() not in {".doc", ".xls", ".ppt"}:
        raise SourceGap("OPAQUE_BINARY")
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        script = Path(__file__).with_name("convert_legacy_office_readonly.ps1")
        if not script.is_file():
            raise SourceGap("LEGACY_OFFICE_COM_UNAVAILABLE")
        output_dir.mkdir(parents=True, exist_ok=True)
        converted_name = {".doc": "converted.docx", ".xls": "converted.xlsx", ".ppt": "converted.pptx"}.get(source.suffix.casefold())
        converted = output_dir / str(converted_name or "")
        # A prior read-only COM attempt may have timed out while Office was
        # still releasing its source lock.  On resume, do not repeat the same
        # modal-prone attempt; use the isolated local fallback after the lock
        # has had time to clear.
        if (output_dir / "powershell_com.log").is_file() and not converted.is_file():
            return _extract_legacy_office_libreoffice(source, output_dir)
        com_error = ""
        try:
            with (output_dir / "powershell_com.log").open("ab") as log:
                completed = subprocess.run(
                    [
                        "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", str(script),
                        "-Source", str(source.resolve()), "-OutputDir", str(output_dir.resolve()),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    check=False,
                    timeout=180,
                )
        except subprocess.TimeoutExpired:
            com_error = "LEGACY_OFFICE_COM_TIMEOUT"
            completed = None
        if completed is not None and completed.returncode:
            com_error = "LEGACY_OFFICE_COM_CONVERSION_FAILED"
        if not com_error and (not converted_name or not converted.is_file()):
            com_error = "LEGACY_OFFICE_COM_CONVERSION_FAILED"
        if com_error:
            return _extract_legacy_office_libreoffice(source, output_dir)
        payload = extract_office(converted)
        payload.update({"kind": "legacy_office_converted_text", "converted_sha256": sha256_file(converted), "conversion_engine": "powershell_com"})
        atomic_json(output_dir / "index.json", payload)
        return output_dir
    suffix = source.suffix.casefold()
    output_dir.mkdir(parents=True, exist_ok=True)
    pythoncom.CoInitialize()
    app = document = None
    try:
        if suffix == ".doc":
            app = win32com.client.DispatchEx("Word.Application")
            app.Visible = False
            app.DisplayAlerts = 0
            document = app.Documents.Open(str(source.resolve()), ReadOnly=True, AddToRecentFiles=False)
            converted = output_dir / "converted.docx"
            document.SaveAs2(str(converted), FileFormat=16)
        elif suffix == ".xls":
            app = win32com.client.DispatchEx("Excel.Application")
            app.Visible = False
            app.DisplayAlerts = False
            document = app.Workbooks.Open(str(source.resolve()), ReadOnly=True, AddToMru=False)
            converted = output_dir / "converted.xlsx"
            document.SaveAs(str(converted), FileFormat=51)
        elif suffix == ".ppt":
            app = win32com.client.DispatchEx("PowerPoint.Application")
            document = app.Presentations.Open(str(source.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
            converted = output_dir / "converted.pptx"
            document.SaveAs(str(converted), 24)
        else:
            raise SourceGap("LEGACY_OFFICE_EXTENSION_UNSUPPORTED")
        payload = extract_office(converted)
        payload.update({"kind": "legacy_office_converted_text", "converted_sha256": sha256_file(converted)})
        atomic_json(output_dir / "index.json", payload)
        return output_dir
    except SourceGap:
        raise
    except Exception:
        return _extract_legacy_office_libreoffice(source, output_dir)
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def tesseract_image(source: Path, output_dir: Path, args: argparse.Namespace, label: str) -> dict[str, Any]:
    exe = tool(args.tesseract, "tesseract")
    # EN: Explicit language data wins; otherwise honor the standard Tesseract environment.
    # 中文：优先使用显式语言包路径，否则遵循 Tesseract 标准环境变量。
    tessdata = Path(args.tessdata_dir).resolve() if args.tessdata_dir else (DEFAULT_TESSDATA if DEFAULT_TESSDATA and DEFAULT_TESSDATA.is_dir() else None)
    tessdata_args = ["--tessdata-dir", str(tessdata)] if tessdata else []
    language_check = subprocess.run([exe, *tessdata_args, "--list-langs"], capture_output=True, text=True, check=False)
    available = {line.strip() for line in language_check.stdout.splitlines() if line.strip()}
    requested = {item.strip() for item in args.ocr_languages.split("+") if item.strip()}
    if language_check.returncode or not requested.issubset(available):
        raise RuntimeError("OCR_LANGUAGE_PACK_MISSING")
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / label
    log = output_dir / "process.log"
    run_quiet([exe, str(source), str(base), *tessdata_args, "-l", args.ocr_languages, "--psm", "6", "-c", "tessedit_create_tsv=1"], log)
    tsv_path = base.with_suffix(".tsv")
    if not tsv_path.is_file():
        raise RuntimeError("OCR_OUTPUT_MISSING")
    boxes: list[dict[str, Any]] = []
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    with tsv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            text_value = str(row.get("text") or "").strip()
            try:
                level = int(row.get("level") or 0)
                confidence = float(row.get("conf") or -1)
            except (TypeError, ValueError):
                continue
            if level != 5 or not text_value:
                continue
            try:
                box = {
                    "page": int(row.get("page_num") or 0), "block": int(row.get("block_num") or 0),
                    "paragraph": int(row.get("par_num") or 0), "line": int(row.get("line_num") or 0),
                    "left": int(row.get("left") or 0), "top": int(row.get("top") or 0),
                    "width": int(row.get("width") or 0), "height": int(row.get("height") or 0),
                    "confidence": confidence, "text": text_value,
                }
            except (TypeError, ValueError):
                continue
            boxes.append(box)
            lines.setdefault((box["block"], box["paragraph"], box["line"]), []).append(text_value)
            if confidence >= 0:
                confidences.append(confidence)
    text = "\n".join(" ".join(lines[key]) for key in sorted(lines))
    text_path = base.with_suffix(".txt")
    atomic_text(text_path, text + ("\n" if text else ""))
    boxes_path = base.with_suffix(".ocr.json")
    summary = {
        "schema": SCHEMA, "source_image_sha256": sha256_file(source),
        "text_chars": len(text), "box_count": len(boxes),
        "mean_confidence": (sum(confidences) / len(confidences)) if confidences else None,
        "blank": not bool(text.strip()), "boxes": boxes,
    }
    atomic_json(boxes_path, summary)
    return {
        "text_path": text_path, "tsv_path": tsv_path, "boxes_path": boxes_path,
        "text_chars": summary["text_chars"], "box_count": summary["box_count"],
        "mean_confidence": summary["mean_confidence"], "blank": summary["blank"],
    }


def ocr_pdf(source: Path, output_dir: Path, args: argparse.Namespace) -> Path:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PYPDF_NOT_AVAILABLE") from exc
    try:
        reader = PdfReader(source)
    except Exception as exc:
        raise SourceGap("PDF_PARSE_FAILED") from exc
    encryption_state = "NOT_ENCRYPTED"
    if reader.is_encrypted:
        try:
            unlocked = bool(reader.decrypt(""))
        except Exception:
            unlocked = False
        if not unlocked:
            raise SourceGap("PDF_PASSWORD_REQUIRED")
        encryption_state = "PDF_ENCRYPTED_EMPTY_PASSWORD_ACCESSIBLE"
    pdftoppm = tool(args.pdftoppm, "pdftoppm")
    frames = output_dir / "pages"
    frames.mkdir(parents=True, exist_ok=True)
    try:
        run_quiet([pdftoppm, "-png", "-r", "300", str(source), str(frames / "page")], output_dir / "render.log")
    except RuntimeError as exc:
        raise SourceGap("PDF_RENDER_FAILED") from exc
    pages = sorted(frames.glob("page-*.png"), key=lambda p: p.name)
    if not pages:
        raise RuntimeError("PDF_RENDER_EMPTY")
    results = []
    for index, image in enumerate(pages, 1):
        row = tesseract_image(image, output_dir / "ocr", args, f"page_{index:06d}")
        results.append({
            "page": index, "image_rel": image.relative_to(output_dir).as_posix(),
            "text_rel": row["text_path"].relative_to(output_dir).as_posix(),
            "tsv_rel": row["tsv_path"].relative_to(output_dir).as_posix(),
            "boxes_rel": row["boxes_path"].relative_to(output_dir).as_posix(),
            "text_chars": row["text_chars"], "box_count": row["box_count"],
            "mean_confidence": row["mean_confidence"], "blank": row["blank"],
        })
    atomic_json(output_dir / "index.json", {"schema": SCHEMA, "source_sha256": sha256_file(source), "encryption_state": encryption_state, "pages": results, "page_count": len(results)})
    return output_dir


def ocr_eps(source: Path, output_dir: Path, args: argparse.Namespace) -> Path:
    if getattr(args, "ghostscript", None):
        ghostscript = tool(args.ghostscript, "ghostscript")
    else:
        ghostscript = next((value for name in ("gswin64c", "gswin32c", "gs") if (value := shutil.which(name))), None)
        if ghostscript is None:
            raise SourceGap("EPS_RENDERER_UNAVAILABLE")
    pages = output_dir / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    try:
        run_quiet([
            ghostscript, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=png16m", "-r300",
            f"-sOutputFile={pages / 'page_%06d.png'}", str(source),
        ], output_dir / "render.log")
    except RuntimeError as exc:
        raise SourceGap("EPS_RENDER_FAILED") from exc
    rendered = sorted(pages.glob("page_*.png"), key=lambda path: path.name)
    if not rendered:
        raise SourceGap("EPS_RENDER_EMPTY")
    results = []
    for index, image in enumerate(rendered, 1):
        row = tesseract_image(image, output_dir / "ocr", args, f"page_{index:06d}")
        results.append({
            "page": index, "image_rel": image.relative_to(output_dir).as_posix(),
            "text_rel": row["text_path"].relative_to(output_dir).as_posix(),
            "tsv_rel": row["tsv_path"].relative_to(output_dir).as_posix(),
            "boxes_rel": row["boxes_path"].relative_to(output_dir).as_posix(),
            "text_chars": row["text_chars"], "box_count": row["box_count"],
            "mean_confidence": row["mean_confidence"], "blank": row["blank"],
        })
    atomic_json(output_dir / "index.json", {
        "schema": SCHEMA, "source_sha256": sha256_file(source),
        "page_count": len(results), "pages": results,
    })
    return output_dir


def ffprobe(source: Path, output: Path, args: argparse.Namespace) -> Path:
    exe = tool(args.ffprobe, "ffprobe")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".tmp.json")
    with temp.open("wb") as handle, (output.parent / "ffprobe.log").open("ab") as log:
        completed = subprocess.run([exe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)], stdout=handle, stderr=log, check=False)
    if completed.returncode:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"FFPROBE_EXIT_{completed.returncode}")
    payload = json.loads(temp.read_text(encoding="utf-8"))
    payload["source_sha256"] = sha256_file(source)
    atomic_json(output, payload)
    temp.unlink(missing_ok=True)
    return output


def run_asr(source: Path, output_dir: Path, category: str, args: argparse.Namespace) -> Path:
    # EN: Media ASR is an external local adapter, not an implicit private installation.
    # 中文：媒体 ASR 是外部本地适配器，不隐式依赖私有安装目录。
    configured_script = args.asr_script or DEFAULT_ASR
    if not configured_script:
        raise RuntimeError("LOCAL_ASR_PIPELINE_NOT_CONFIGURED")
    script = Path(configured_script).resolve()
    if not script.is_file():
        raise RuntimeError("LOCAL_ASR_PIPELINE_NOT_FOUND")
    asr_input = source
    if category in {"video", "media"}:
        ffmpeg = tool(args.ffmpeg, "ffmpeg")
        asr_input = output_dir / "source_audio.wav"
        output_dir.mkdir(parents=True, exist_ok=True)
        run_quiet([ffmpeg, "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(asr_input)], output_dir / "audio_extract.log")
    asr_root = output_dir / "asr"
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "asr_subprocess.log").open("ab") as log:
        completed = subprocess.run(
            [args.asr_python or sys.executable, str(script), "--input", str(asr_input), "--output", str(asr_root)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, check=False,
        )
    result_candidates = [asr_root / "state" / "pipeline_result.json", asr_root / "_state" / "pipeline_result.json"]
    result = next((path for path in result_candidates if path.is_file()), None)
    payload = read_json_object(result, "ASR_PIPELINE_RESULT") if result else None
    if completed.returncode == 0 and payload and payload.get("status") == "complete":
        atomic_json(output_dir / "source_binding.json", {
            "schema": SCHEMA, "source_sha256": sha256_file(source), "asr_input_sha256": sha256_file(asr_input),
            "asr_status": "DUAL_ASR_COMPLETE", "asr_result_rel": result.relative_to(output_dir).as_posix(),
        })
        return output_dir

    # Some clips can contain an audio stream but no
    # speech.  The standard runner stops when Whisper emits zero segments, so
    # finish the independent Qwen and diarization passes instead of pretending
    # the missing transcript is a generic process failure.
    error_text = str(payload.get("error") or "") if isinstance(payload, dict) else ""
    whisper_zero_segment_candidate = (
        isinstance(payload, dict)
        and payload.get("status") == "failed"
        and (
            "Whisper 未生成任何片段" in error_text
            # Some Windows subprocesses mojibake the Chinese tail while the
            # stable engine prefix survives.  Qwen plus diarization still have
            # to independently corroborate the no-speech terminal below.
            or error_text.strip().casefold().startswith("whisper ")
        )
    )
    if whisper_zero_segment_candidate:
        spec = importlib.util.spec_from_file_location("okki_local_asr_completion", script)
        if spec is None or spec.loader is None:
            raise RuntimeError("ASR_LOCAL_MODULE_LOAD_FAILED")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pipeline = module.Pipeline(asr_input, asr_root)
        qwen = pipeline.run_qwen()
        chunks = qwen.get("results") if isinstance(qwen, dict) else None
        if not isinstance(chunks, list):
            raise RuntimeError("ASR_QWEN_RESULT_INVALID")
        nonempty = 0
        for row in chunks:
            if isinstance(row, dict) and any(str(row.get(key) or "").strip() for key in ("text", "transcript", "prediction")):
                nonempty += 1
        diarization_status = "COMPLETE"
        diarization_log = asr_root / "logs" / "3dspeaker_diarization.log"
        diagnostic = ""
        if diarization_log.is_file():
            diagnostic = diarization_log.read_text(encoding="utf-8", errors="replace")[-32_768:]
        if "max() iterable argument is empty" in diagnostic:
            diarization = []
            diarization_status = "NOT_APPLICABLE_NO_VOICE_SEGMENTS"
        else:
            try:
                diarization = pipeline.run_diarization()
            except Exception:
                diagnostic = ""
                if diarization_log.is_file():
                    diagnostic = diarization_log.read_text(encoding="utf-8", errors="replace")[-32_768:]
            # 3D-Speaker has no embeddings to cluster when both independent
            # ASR engines found no speech.  That is a valid N/A terminal state,
            # not a failed media task.  Any other diarization failure remains
            # fail-closed.
                if "max() iterable argument is empty" in diagnostic:
                    diarization = []
                    diarization_status = "NOT_APPLICABLE_NO_VOICE_SEGMENTS"
                else:
                    raise
        if nonempty == 0 and diarization_status == "NOT_APPLICABLE_NO_VOICE_SEGMENTS":
            final_asr_status = "NO_SPEECH_CONFIRMED_BY_WHISPER_AND_QWEN"
        elif diarization_status == "NOT_APPLICABLE_NO_VOICE_SEGMENTS":
            final_asr_status = "DUAL_ASR_DIVERGENCE_QWEN_TEXT_WITHOUT_VOICE"
        elif nonempty == 0:
            final_asr_status = "DUAL_ASR_DIVERGENCE_VOICE_WITHOUT_TRANSCRIPT"
        else:
            final_asr_status = "DUAL_ASR_DIVERGENCE_WHISPER_EMPTY"
        completion = {
            "schema": SCHEMA,
            "source_sha256": sha256_file(source),
            "asr_input_sha256": sha256_file(asr_input),
            "asr_status": final_asr_status,
            "whisper_segments": 0,
            "qwen_chunks": len(chunks),
            "qwen_nonempty_chunks": nonempty,
            "diarization_segments": len(diarization) if isinstance(diarization, list) else 0,
            "diarization_status": diarization_status,
            "initial_pipeline_result_rel": result.relative_to(output_dir).as_posix(),
        }
        atomic_json(output_dir / "no_speech_dual_asr_completion.json", completion)
        atomic_json(output_dir / "source_binding.json", completion)
        return output_dir
    raise RuntimeError(f"ASR_SUBPROCESS_EXIT_{completed.returncode}" if completed.returncode else "ASR_RESULT_INCOMPLETE")


def image_dhash(path: Path) -> int:
    try:
        from PIL import Image
    except ImportError:
        return int(sha256_file(path)[:16], 16)
    with Image.open(path) as image:
        gray = image.convert("L").resize((9, 8))
        pixels = list(gray.getdata())
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return value


def frame_is_new(value: int, existing: list[int]) -> bool:
    return all((value ^ item).bit_count() > 3 for item in existing)


def video_frame_ocr(source: Path, output_dir: Path, args: argparse.Namespace) -> Path:
    ffmpeg = tool(args.ffmpeg, "ffmpeg")
    frames = output_dir / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    run_quiet([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vf", "fps=1", "-vsync", "vfr", str(frames / "fps_%08d.png")], output_dir / "fps_extract.log")
    scene_log = output_dir / "scene_extract.log"
    run_quiet([ffmpeg, "-y", "-hide_banner", "-i", str(source), "-vf", "select='gt(scene,0.25)',showinfo", "-vsync", "vfr", str(frames / "scene_%08d.png")], scene_log)
    scene_times = [float(value) for value in re.findall(r"pts_time:([0-9.]+)", scene_log.read_text(encoding="utf-8", errors="replace"))]
    candidates: list[tuple[Path, float, str]] = []
    for index, path in enumerate(sorted(frames.glob("fps_*.png")), 0):
        candidates.append((path, float(index), "one_fps"))
    for index, path in enumerate(sorted(frames.glob("scene_*.png")), 0):
        candidates.append((path, scene_times[index] if index < len(scene_times) else -1.0, "scene_change"))
    seen_hashes: list[int] = []
    rows: list[dict[str, Any]] = []
    for ordinal, (path, timestamp, reason) in enumerate(sorted(candidates, key=lambda item: (item[1], item[2])), 1):
        value = image_dhash(path)
        if not frame_is_new(value, seen_hashes):
            continue
        seen_hashes.append(value)
        ocr = tesseract_image(path, output_dir / "ocr", args, f"frame_{ordinal:08d}")
        rows.append({
            "timestamp_seconds": timestamp, "selection_reason": reason,
            "frame_sha256": sha256_file(path), "frame_rel": path.relative_to(output_dir).as_posix(),
            "text_rel": ocr["text_path"].relative_to(output_dir).as_posix(),
            "tsv_rel": ocr["tsv_path"].relative_to(output_dir).as_posix(),
            "boxes_rel": ocr["boxes_path"].relative_to(output_dir).as_posix(),
            "text_chars": ocr["text_chars"], "box_count": ocr["box_count"],
            "mean_confidence": ocr["mean_confidence"], "blank": ocr["blank"],
        })
    atomic_json(output_dir / "index.json", {"schema": SCHEMA, "source_sha256": sha256_file(source), "candidate_frames": len(candidates), "unique_frames": len(rows), "frames": rows})
    return output_dir


def execute_task(case_root: Path, row: Any, args: argparse.Namespace) -> Path:
    source = case_root / row["source_rel"]
    output = case_root / row["output_rel"]
    if sha256_file(source) != row["source_sha256"]:
        raise RuntimeError("SOURCE_HASH_CHANGED")
    kind = row["task_kind"]
    if kind == "extract_text":
        atomic_json(output, extract_text(source))
    elif kind == "extract_pdf_native":
        atomic_json(output, extract_pdf(source))
    elif kind == "extract_office_xml":
        atomic_json(output, extract_office(source))
    elif kind == "extract_legacy_office":
        output = extract_legacy_office(source, output)
    elif kind == "inspect_archive":
        atomic_json(output, inspect_archive(source))
    elif kind == "extract_eps_text":
        atomic_json(output, extract_eps(source))
    elif kind == "classify_binary":
        atomic_json(output, classify_binary(source))
    elif kind == "ocr_image":
        result = tesseract_image(source, output, args, "image")
        atomic_json(output / "index.json", {
            "schema": SCHEMA, "source_sha256": row["source_sha256"],
            "text_rel": result["text_path"].relative_to(output).as_posix(),
            "tsv_rel": result["tsv_path"].relative_to(output).as_posix(),
            "boxes_rel": result["boxes_path"].relative_to(output).as_posix(),
            "text_chars": result["text_chars"], "box_count": result["box_count"],
            "mean_confidence": result["mean_confidence"], "blank": result["blank"],
        })
    elif kind == "ocr_pdf_all_pages":
        output = ocr_pdf(source, output, args)
    elif kind == "ocr_eps_rendered":
        output = ocr_eps(source, output, args)
    elif kind == "ffprobe_media":
        output = ffprobe(source, output, args)
    elif kind == "asr_dual_diarization":
        category = row.get("source_category") if isinstance(row, dict) else row["source_category"]
        if not category:
            raise RuntimeError("SOURCE_INVENTORY_MISSING")
        output = run_asr(source, output, str(category), args)
    elif kind == "video_frame_ocr":
        output = video_frame_ocr(source, output, args)
    else:
        raise RuntimeError("UNKNOWN_TASK_KIND")
    return output


def sanitized_error(exc: Exception) -> str:
    text = str(exc)
    if re.fullmatch(r"[A-Z0-9_\-]+", text):
        return text[:120]
    return type(exc).__name__.upper()


def source_gap_output(case_root: Path, row: dict[str, Any], gap: SourceGap) -> Path:
    output = case_root / str(row["output_rel"])
    payload = {
        "schema": SCHEMA,
        "kind": "source_gap",
        "source_sha256": row["source_sha256"],
        "task_kind": row["task_kind"],
        "error_code": gap.code,
        "metadata": gap.metadata,
    }
    if output.suffix:
        atomic_json(output, payload)
        return output
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "source_gap.json", payload)
    return output


def execute_task_worker(payload: tuple[str, dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    case_root_text, row, argument_values = payload
    case_root = Path(case_root_text)
    args = argparse.Namespace(**argument_values)
    task_output = case_root / str(row["output_rel"])
    log_path = task_output.parent / "logs" / f"{row['task_id']}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as task_log:
        with contextlib.redirect_stdout(task_log), contextlib.redirect_stderr(task_log):
            try:
                output = execute_task(case_root, row, args)
                return {
                    "task_id": row["task_id"], "state": "completed",
                    "result_sha256": stable_tree_hash(output), "error_code": None,
                }
            except SourceGap as gap:
                output = source_gap_output(case_root, row, gap)
                return {
                    "task_id": row["task_id"], "state": "source_gap",
                    "result_sha256": stable_tree_hash(output), "error_code": gap.code,
                }
            except Exception as exc:
                return {
                    "task_id": row["task_id"], "state": "failed",
                    "result_sha256": None, "error_code": sanitized_error(exc),
                }


def stage_tessdata(case_root: Path, output_root: Path, args: argparse.Namespace) -> None:
    """Copy the offline language pack to an ASCII-only case-local runtime.

    The installed Windows Tesseract build cannot open non-ASCII tessdata paths.
    Staging also makes the exact OCR language inputs part of the case evidence.
    """
    source = Path(args.tessdata_dir).resolve() if args.tessdata_dir else DEFAULT_TESSDATA
    if source is None or not source.is_dir():
        return
    target = output_root / "runtime" / "tessdata"
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.glob("*.traineddata"), key=lambda p: p.name.casefold()):
        destination = target / item.name
        if not destination.is_file() or sha256_file(destination) != sha256_file(item):
            shutil.copy2(item, destination)
    args.tessdata_dir = str(target)


# EN: Process only pending work under exclusive case ownership.
# 中文：在独占案例所有权下只处理未完成任务。
def run(case_root: Path, company_id: str, args: argparse.Namespace) -> dict[str, int]:
    with case_extraction_lock(case_root):
        return _run_locked(case_root, company_id, args)


def _run_locked(case_root: Path, company_id: str, args: argparse.Namespace) -> dict[str, int]:
    load_identity(case_root, company_id)
    automatic = load_automatic_scope(case_root, company_id)
    ui_gap = load_ui_gap_scope(case_root, company_id)
    automatic_gaps = load_automatic_source_gaps(case_root, company_id, automatic["session_id"])
    manual = load_manual_trade_scope(case_root, company_id, verify_artifacts=True)
    mail = load_mail_capture_scope(case_root, company_id, automatic, verify_artifacts=True)
    output = full_extract_root(case_root, getattr(args, "output_dir", None))
    db = open_db(output / "state.sqlite3")
    completed = failed = source_gap_completed = 0
    try:
        meta = {str(row["key"]): str(row["value"]) for row in db.execute("SELECT key,value FROM meta")}
        controls = {
            "company_id": company_id,
            "automatic_session_id": automatic["session_id"],
            "processing_scope_v1_sha256": automatic["processing_scope_sha256"],
            "automatic_final_status_sha256": automatic["final_status_sha256"],
            "capture_reconciliation_sha256": automatic_gaps["sha256"] or "",
            "manual_trade_manifest_sha256": manual["manifest_sha256"],
            "mail_capture_manifest_sha256": mail["manifest_sha256"] or "",
            "ui_gap_status": ui_gap["status"],
            "ui_gap_manifest_sha256": ui_gap["manifest_sha256"] or "",
            "output_root_rel": safe_relative(output, case_root),
        }
        if any(meta.get(key) != str(value) for key, value in controls.items()):
            raise RuntimeError("PREPARED_SCOPE_BINDING_MISMATCH")
        scope_path = output / PROCESSING_SCOPE_V2_NAME
        if not scope_path.is_file() or sha256_file(scope_path) != meta.get("processing_scope_v2_sha256"):
            raise RuntimeError("PROCESSING_SCOPE_V2_BINDING_MISMATCH")
        if not args.skip_models:
            stage_tessdata(case_root, output, args)
        # A process can die after leasing a task but before recording a result.
        # A task is private to this single-process runner, so every stale running
        # row is safe to return to pending when a new invocation starts.
        db.execute("UPDATE tasks SET state='pending',error_code='INTERRUPTED_RECOVERED',updated_at=? WHERE active=1 AND state='running'", (now(),))
        db.commit()
        rows = [dict(row) for row in db.execute(
            """SELECT t.*,f.category AS source_category FROM tasks t
               JOIN files f ON f.source_rel=t.source_rel AND f.active=1
               WHERE t.active=1 AND t.state IN ('pending','failed')
               ORDER BY t.source_rel,t.task_kind"""
        ).fetchall()]
        if args.max_tasks is not None:
            rows = rows[: args.max_tasks]

        model_kinds = {"asr_dual_diarization", "video_frame_ocr", "ocr_image", "ocr_pdf_all_pages", "ocr_eps_rendered"}
        if args.skip_models:
            rows = [row for row in rows if row["task_kind"] not in model_kinds]
        parse_kinds = {
            "extract_text", "extract_pdf_native", "extract_office_xml", "inspect_archive",
            "extract_eps_text", "classify_binary", "ffprobe_media",
        }
        ocr_kinds = {"ocr_image", "ocr_pdf_all_pages", "ocr_eps_rendered", "video_frame_ocr"}
        office_kinds = {"extract_legacy_office"}
        asr_kinds = {"asr_dual_diarization"}
        phases = [
            ([row for row in rows if row["task_kind"] in parse_kinds], max(1, min(32, int(getattr(args, "parse_workers", None) or (os.cpu_count() or 1))))),
            ([row for row in rows if row["task_kind"] in office_kinds], 1),
            ([row for row in rows if row["task_kind"] in ocr_kinds], max(1, min(24, int(getattr(args, "ocr_workers", None) or max(1, (os.cpu_count() or 2) // 2))))),
            ([row for row in rows if row["task_kind"] in asr_kinds], 1),
        ]
        argument_names = (
            "skip_models", "ffmpeg", "ffprobe", "pdftoppm", "ghostscript", "tesseract", "tessdata_dir",
            "ocr_languages", "asr_script", "asr_python",
        )
        argument_values = {name: getattr(args, name, None) for name in argument_names}
        parallel_safe = sys.modules.get(__name__) is not None

        for phase_rows, workers in phases:
            if not phase_rows:
                continue
            started = now()
            db.executemany(
                "UPDATE tasks SET state='running',attempts=attempts+1,error_code=NULL,updated_at=? WHERE task_id=?",
                ((started, row["task_id"]) for row in phase_rows),
            )
            db.commit()
            payloads = [(str(case_root), row, argument_values) for row in phase_rows]
            if not parallel_safe or workers == 1 or len(payloads) == 1:
                results = [execute_task_worker(payload) for payload in payloads]
            else:
                context = multiprocessing.get_context("spawn")
                with concurrent.futures.ProcessPoolExecutor(max_workers=min(workers, len(payloads)), mp_context=context) as executor:
                    results = list(executor.map(execute_task_worker, payloads, chunksize=1))
            for result in results:
                state = result["state"]
                db.execute(
                    "UPDATE tasks SET state=?,result_sha256=?,error_code=?,updated_at=? WHERE task_id=?",
                    (state, result["result_sha256"], result["error_code"], now(), result["task_id"]),
                )
                if state == "completed":
                    completed += 1
                elif state == "source_gap":
                    source_gap_completed += 1
                    task = db.execute("SELECT content_sha256,task_kind FROM tasks WHERE task_id=?", (result["task_id"],)).fetchone()
                    gap_id = sha256_bytes(f"task_processing\0{result['task_id']}\0{result['error_code']}".encode("utf-8"))
                    db.execute(
                        """INSERT INTO source_gaps(gap_id,content_sha256,source_scope,role,error_code,active,recorded_at)
                           VALUES(?,?,?,?,?,1,?) ON CONFLICT(gap_id) DO UPDATE SET active=1,recorded_at=excluded.recorded_at""",
                        (gap_id, task["content_sha256"], "task_processing", task["task_kind"], result["error_code"], now()),
                    )
                else:
                    failed += 1
                db.commit()
        build_fts(case_root, db, output)
        build_relations(case_root, company_id, db, output)
        write_indexes(case_root, db, output)
        remaining = int(db.execute("SELECT COUNT(*) FROM tasks WHERE active=1 AND state NOT IN ('completed','source_gap')").fetchone()[0])
        counts = {
            "completed_this_run": completed, "source_gap_this_run": source_gap_completed,
            "failed_this_run": failed, "remaining": remaining,
            "total": int(db.execute("SELECT COUNT(*) FROM tasks WHERE active=1").fetchone()[0]),
            "source_gaps": int(db.execute("SELECT COUNT(*) FROM source_gaps WHERE active=1").fetchone()[0]),
            "manual_source_gaps": manual["source_gap_count"],
        }
        manifest = {"schema": SCHEMA, "stage": "run", "company_id": company_id, "generated_at": now(), "counts": counts}
        atomic_json(output / "run_manifest.json", manifest)
        counts["manifest_sha256"] = sha256_file(output / "run_manifest.json")  # type: ignore[assignment]
        return counts
    finally:
        db.close()


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def text_from_artifact(path: Path) -> str:
    if path.suffix.casefold() == ".json":
        try:
            return "\n".join(iter_strings(json.loads(path.read_text(encoding="utf-8-sig"))))
        except Exception:
            return ""
    if path.suffix.casefold() in TEXT_SUFFIXES:
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return ""
    return ""


def build_fts(case_root: Path, db: sqlite3.Connection, output_root: Path) -> None:
    target = output_root / "full_text.sqlite3"
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    out = sqlite3.connect(temp)
    try:
        out.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, source_rel TEXT NOT NULL, source_sha256 TEXT NOT NULL, artifact_rel TEXT NOT NULL, task_kind TEXT NOT NULL, text_chars INTEGER NOT NULL)")
        out.execute("CREATE VIRTUAL TABLE full_text USING fts5(source_rel UNINDEXED, source_sha256 UNINDEXED, artifact_rel UNINDEXED, task_kind UNINDEXED, text, tokenize='unicode61')")
        for row in db.execute("SELECT * FROM tasks WHERE active=1 AND state='completed' ORDER BY source_rel,task_kind"):
            output = case_root / row["output_rel"]
            files = [output] if output.is_file() else sorted((p for p in output.rglob("*") if p.is_file()), key=lambda p: p.as_posix().casefold())
            for artifact in files:
                text = text_from_artifact(artifact)
                if not text:
                    continue
                cursor = out.execute("INSERT INTO documents(source_rel,source_sha256,artifact_rel,task_kind,text_chars) VALUES(?,?,?,?,?)", (row["source_rel"], row["source_sha256"], safe_relative(artifact, case_root), row["task_kind"], len(text)))
                out.execute("INSERT INTO full_text(rowid,source_rel,source_sha256,artifact_rel,task_kind,text) VALUES(?,?,?,?,?,?)", (cursor.lastrowid, row["source_rel"], row["source_sha256"], safe_relative(artifact, case_root), row["task_kind"], text))
        out.commit()
    finally:
        out.close()
    os.replace(temp, target)


def infer_type(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold())
    for token, kind in ID_TYPES.items():
        if token in normalized and (normalized.endswith("_id") or normalized == "id" or "_id_" in normalized):
            return kind
    return None


def entity_node(kind: str, value: str) -> dict[str, Any]:
    return {"record_type": "node", "node_id": f"{kind}:{sha256_bytes(value.encode('utf-8'))}", "entity_type": kind, "value": value}


def build_relations(case_root: Path, company_id: str, db: sqlite3.Connection, output_root: Path) -> None:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    company = entity_node("company", company_id)
    nodes[company["node_id"]] = company

    def add_edge(left: str, right: str, relation: str, source_rel: str, pointer: str) -> None:
        if left == right:
            return
        edge_id = sha256_bytes(f"{left}\0{right}\0{relation}\0{source_rel}\0{pointer}".encode("utf-8"))
        edges[edge_id] = {"record_type": "edge", "edge_id": edge_id, "from": left, "to": right, "relation": relation, "source_rel": source_rel, "json_pointer": pointer}

    def walk(value: Any, source_rel: str, pointer: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            local: list[str] = []
            for key, item in value.items():
                kind = infer_type(str(key))
                if kind and isinstance(item, (str, int)) and str(item).strip():
                    node = entity_node(kind, str(item).strip())
                    nodes[node["node_id"]] = node
                    local.append(node["node_id"])
                    add_edge(company["node_id"], node["node_id"], "observed_in_customer_case", source_rel, f"{pointer}/{key}")
                if isinstance(item, str) and ("mail" in str(key).casefold() or str(key).casefold() in {"sender", "receiver", "cc", "bcc", "from", "to"}):
                    for address in re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", item, flags=re.I):
                        node = entity_node("email", address)
                        nodes[node["node_id"]] = node
                        local.append(node["node_id"])
                        add_edge(company["node_id"], node["node_id"], "email_participant", source_rel, f"{pointer}/{key}")
            for index, left in enumerate(local):
                for right in local[index + 1:]:
                    add_edge(left, right, "cooccurs_in_record", source_rel, pointer or "/")
            found.extend(local)
            for key, item in value.items():
                found.extend(walk(item, source_rel, f"{pointer}/{key}"))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                found.extend(walk(item, source_rel, f"{pointer}/{index}"))
        return found

    for file_row in db.execute("SELECT source_rel,magic FROM files WHERE active=1 ORDER BY source_rel"):
        path = case_root / file_row["source_rel"]
        suffix = path.suffix.casefold()
        if suffix not in {".json", ".jsonl"} and not str(file_row["magic"]).startswith("json"):
            continue
        try:
            decoded = path.read_text(encoding="utf-8-sig")
            values = [json.loads(line) for line in decoded.splitlines() if line.strip()] if suffix == ".jsonl" else [json.loads(decoded)]
            for index, value in enumerate(values):
                walk(value, file_row["source_rel"], f"/line/{index + 1}" if suffix == ".jsonl" else "")
        except Exception:
            continue
    target = output_root / "relationship_graph.jsonl"
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in [*nodes.values(), *edges.values()]]
    atomic_text(target, "\n".join(lines) + ("\n" if lines else ""))


def csv_atomic(path: Path, headers: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def write_indexes(case_root: Path, db: sqlite3.Connection, root: Path) -> None:
    files = [dict(row) for row in db.execute("SELECT source_rel,sha256,bytes,magic,category,mime,source_scope,active,prepared_at FROM files WHERE active=1 ORDER BY source_rel")]
    objects = [dict(row) for row in db.execute("SELECT sha256,bytes,magic,category,mime,representative_rel,active,prepared_at FROM content_objects WHERE active=1 ORDER BY sha256")]
    occurrences = [dict(row) for row in db.execute("SELECT occurrence_id,content_sha256,source_scope,role,source_rel,source_identity_sha256,owner_sha256,resolution,active,prepared_at FROM occurrences WHERE active=1 ORDER BY occurrence_id")]
    gaps = [dict(row) for row in db.execute("SELECT gap_id,content_sha256,source_scope,role,source_identity_sha256,owner_sha256,error_code,http_status,active,recorded_at FROM source_gaps WHERE active=1 ORDER BY gap_id")]
    tasks = [dict(row) for row in db.execute("SELECT task_id,source_rel,source_sha256,content_sha256,task_kind,state,output_rel,result_sha256,attempts,error_code,updated_at FROM tasks WHERE active=1 ORDER BY source_rel,task_kind")]
    csv_atomic(root / "file_inventory.csv", ["source_rel", "sha256", "bytes", "magic", "category", "mime", "source_scope", "active", "prepared_at"], files)
    csv_atomic(root / "content_object_index.csv", ["sha256", "bytes", "magic", "category", "mime", "representative_rel", "active", "prepared_at"], objects)
    csv_atomic(root / "occurrence_index.csv", ["occurrence_id", "content_sha256", "source_scope", "role", "source_rel", "source_identity_sha256", "owner_sha256", "resolution", "active", "prepared_at"], occurrences)
    csv_atomic(root / "source_gaps.csv", ["gap_id", "content_sha256", "source_scope", "role", "source_identity_sha256", "owner_sha256", "error_code", "http_status", "active", "recorded_at"], gaps)
    csv_atomic(root / "evidence_index.csv", ["task_id", "source_rel", "source_sha256", "content_sha256", "task_kind", "state", "output_rel", "result_sha256", "attempts", "error_code", "updated_at"], tasks)
    media_rows = []
    task_map: dict[str, dict[str, str]] = {}
    for task in tasks:
        task_map.setdefault(task["content_sha256"], {})[task["task_kind"]] = task["state"]
    for row in files:
        if row["category"] not in {"audio", "video", "media"}:
            continue
        media_rows.append({"source_rel": row["source_rel"], "sha256": row["sha256"], "bytes": row["bytes"], "category": row["category"], "ffprobe": task_map.get(row["sha256"], {}).get("ffprobe_media", "not_scheduled"), "dual_asr_diarization": task_map.get(row["sha256"], {}).get("asr_dual_diarization", "not_scheduled"), "video_frame_ocr": task_map.get(row["sha256"], {}).get("video_frame_ocr", "not_scheduled")})
    csv_atomic(root / "media_inventory.csv", ["source_rel", "sha256", "bytes", "category", "ffprobe", "dual_asr_diarization", "video_frame_ocr"], media_rows)
    coverage: list[dict[str, Any]] = []
    for row in db.execute("SELECT source_scope,COUNT(*) count,COALESCE(SUM(bytes),0) bytes FROM files WHERE active=1 GROUP BY source_scope ORDER BY source_scope"):
        coverage.append({"scope": "evidence_scope", "name": row["source_scope"], "total": row["count"], "completed": row["count"], "pending": 0, "failed": 0, "bytes": row["bytes"]})
    for row in db.execute("SELECT source_scope,role,COUNT(*) count FROM occurrences WHERE active=1 GROUP BY source_scope,role ORDER BY source_scope,role"):
        coverage.append({"scope": "occurrence", "name": f"{row['source_scope']}:{row['role']}", "total": row["count"], "completed": row["count"], "pending": 0, "failed": 0, "bytes": ""})
    for row in db.execute("SELECT category,COUNT(*) count,COALESCE(SUM(bytes),0) bytes FROM files WHERE active=1 GROUP BY category ORDER BY category"):
        coverage.append({"scope": "file_category", "name": row["category"], "total": row["count"], "completed": row["count"], "pending": 0, "failed": 0, "bytes": row["bytes"]})
    for row in db.execute("SELECT task_kind,COUNT(*) total,SUM(state IN ('completed','source_gap')) completed,SUM(state IN ('pending','running')) pending,SUM(state='failed') failed FROM tasks WHERE active=1 GROUP BY task_kind ORDER BY task_kind"):
        coverage.append({"scope": "task_kind", "name": row["task_kind"], "total": row["total"], "completed": row["completed"], "pending": row["pending"], "failed": row["failed"], "bytes": ""})
    csv_atomic(root / "coverage.csv", ["scope", "name", "total", "completed", "pending", "failed", "bytes"], coverage)


# EN: Reconcile counts, source bindings and outputs; fail closed on mismatch.
# 中文：核对数量、来源绑定与输出；不一致时拒绝通过。
def verify(case_root: Path, company_id: str, output_dir: Path | str | None = None) -> tuple[dict[str, Any], int]:
    load_identity(case_root, company_id)
    automatic = load_automatic_scope(case_root, company_id)
    ui_gap = load_ui_gap_scope(case_root, company_id)
    automatic_gaps = load_automatic_source_gaps(case_root, company_id, automatic["session_id"])
    manual = load_manual_trade_scope(case_root, company_id, verify_artifacts=True)
    mail = load_mail_capture_scope(case_root, company_id, automatic, verify_artifacts=True)
    root = full_extract_root(case_root, output_dir)
    state_path = root / "state.sqlite3"
    if not state_path.is_file():
        raise RuntimeError("FULL_EXTRACT_STATE_MISSING")
    db = open_db(state_path)
    mismatched = missing_outputs = bad_outputs = 0
    try:
        meta = {str(row["key"]): str(row["value"]) for row in db.execute("SELECT key,value FROM meta")}
        binding_mismatch = 0
        expected_meta = {
            "company_id": company_id,
            "automatic_session_id": automatic["session_id"],
            "processing_scope_v1_rel": safe_relative(automatic["processing_scope_path"], case_root),
            "processing_scope_v1_sha256": automatic["processing_scope_sha256"],
            "automatic_final_status_rel": safe_relative(automatic["final_status_path"], case_root),
            "automatic_final_status_sha256": automatic["final_status_sha256"],
            "capture_reconciliation_rel": safe_relative(automatic_gaps["path"], case_root) if automatic_gaps["path"] else "",
            "capture_reconciliation_sha256": automatic_gaps["sha256"] or "",
            "manual_trade_manifest_rel": safe_relative(manual["manifest_path"], case_root),
            "manual_trade_manifest_sha256": manual["manifest_sha256"],
            "manual_trade_status": manual["status"],
            "mail_capture_manifest_rel": safe_relative(mail["manifest_path"], case_root) if mail["manifest_path"] else "",
            "mail_capture_manifest_sha256": mail["manifest_sha256"] or "",
            "ui_gap_status": ui_gap["status"],
            "ui_gap_manifest_rel": safe_relative(ui_gap["manifest_path"], case_root) if ui_gap["manifest_path"] else "",
            "ui_gap_manifest_sha256": ui_gap["manifest_sha256"] or "",
            "output_root_rel": safe_relative(root, case_root),
        }
        for key, value in expected_meta.items():
            if meta.get(key) != str(value):
                binding_mismatch += 1
        files = db.execute("SELECT * FROM files WHERE active=1 ORDER BY source_rel").fetchall()
        for row in files:
            path = case_root / row["source_rel"]
            if not path.is_file() or sha256_file(path) != row["sha256"]:
                mismatched += 1
        expected_automatic = {
            safe_relative(path, case_root)
            for path in automatic["session_root"].rglob("*")
            if path.is_file() and not path.name.endswith(".part")
        }
        expected_manual = {safe_relative(row["path"], case_root) for row in manual["artifacts"]}
        expected_ui_gap = {
            safe_relative(path, case_root)
            for path in (ui_gap["session_root"].rglob("*") if ui_gap["status"] == "PASS" else [])
            if path.is_file() and not path.name.endswith(".part")
        }
        base_paths = expected_automatic | expected_ui_gap | expected_manual
        expected_historical_mail = {row["relative_path"] for row in mail["artifacts"] if row["relative_path"] not in base_paths}
        actual_automatic = {str(row["source_rel"]) for row in files if row["source_scope"] == "automatic"}
        actual_manual = {str(row["source_rel"]) for row in files if row["source_scope"] == "manual_trade"}
        actual_ui_gap = {str(row["source_rel"]) for row in files if row["source_scope"] == "ui_gap_revisit"}
        actual_historical_mail = {str(row["source_rel"]) for row in files if str(row["source_scope"]).startswith("historical_mail")}
        evidence_scope_mismatch = (
            len(expected_automatic ^ actual_automatic)
            + len(expected_ui_gap ^ actual_ui_gap)
            + len(expected_manual ^ actual_manual)
            + len(expected_historical_mail ^ actual_historical_mail)
        )
        objects = db.execute("SELECT * FROM content_objects WHERE active=1 ORDER BY sha256").fetchall()
        object_map = {str(row["sha256"]): row for row in objects}
        object_mismatch = sum(
            str(row["sha256"]) not in object_map or int(object_map[str(row["sha256"])]["bytes"]) != int(row["bytes"])
            for row in files
        )
        occurrences = db.execute("SELECT * FROM occurrences WHERE active=1 ORDER BY occurrence_id").fetchall()
        occurrence_mismatch = sum(str(row["content_sha256"]) not in object_map for row in occurrences)
        scope_path = root / PROCESSING_SCOPE_V2_NAME
        scope_mismatch = 0
        if not scope_path.is_file() or sha256_file(scope_path) != meta.get("processing_scope_v2_sha256"):
            scope_mismatch = 1
        else:
            scope_v2 = read_json_object(scope_path, "PROCESSING_SCOPE_V2")
            if scope_v2.get("schema") != PROCESSING_SCOPE_V2_SCHEMA or scope_v2.get("schema_version") != 2 or str(scope_v2.get("company_id") or "") != company_id:
                scope_mismatch = 1
            else:
                scope_object_ids = {str(row.get("sha256") or "") for row in scope_v2.get("content_objects", []) if isinstance(row, dict)}
                scope_occurrence_ids = {str(row.get("occurrence_id") or "") for row in scope_v2.get("occurrences", []) if isinstance(row, dict)}
                scope_gap_ids = {str(row.get("gap_id") or "") for row in scope_v2.get("source_gaps", []) if isinstance(row, dict)}
                database_gap_ids = {str(row["gap_id"]) for row in db.execute("SELECT gap_id FROM source_gaps WHERE active=1")}
                if (
                    scope_object_ids != set(object_map)
                    or scope_occurrence_ids != {str(row["occurrence_id"]) for row in occurrences}
                    or not scope_gap_ids.issubset(database_gap_ids)
                ):
                    scope_mismatch = 1
        tasks = db.execute("SELECT * FROM tasks WHERE active=1 ORDER BY task_id").fetchall()
        for row in tasks:
            if row["state"] not in TERMINAL_TASK_STATES:
                continue
            path = case_root / row["output_rel"]
            if not path.exists():
                missing_outputs += 1
            elif stable_tree_hash(path) != row["result_sha256"]:
                bad_outputs += 1
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        pending = sum(row["state"] in {"pending", "running"} for row in tasks)
        failed = sum(row["state"] == "failed" for row in tasks)
        task_source_gaps = sum(row["state"] == "source_gap" for row in tasks)
        source_gap_count = int(db.execute("SELECT COUNT(*) FROM source_gaps WHERE active=1").fetchone()[0])
        required = [root / name for name in (
            "prepare_manifest.json", "run_manifest.json", PROCESSING_SCOPE_V2_NAME,
            "file_inventory.csv", "content_object_index.csv", "occurrence_index.csv",
            "source_gaps.csv", "evidence_index.csv", "media_inventory.csv", "coverage.csv",
            "full_text.sqlite3", "relationship_graph.jsonl",
        )]
        missing_indexes = sum(not path.is_file() for path in required)
        fts_integrity = "missing"
        fts_rows = 0
        if (root / "full_text.sqlite3").is_file():
            fts = sqlite3.connect(root / "full_text.sqlite3")
            try:
                fts_integrity = str(fts.execute("PRAGMA integrity_check").fetchone()[0])
                fts_rows = int(fts.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            finally:
                fts.close()
        relations = 0
        relation_path = root / "relationship_graph.jsonl"
        if relation_path.is_file():
            with relation_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        json.loads(line)
                        relations += 1
        pass_state = bool(files) and not any((binding_mismatch, scope_mismatch, evidence_scope_mismatch, object_mismatch, occurrence_mismatch, mismatched, missing_outputs, bad_outputs, pending, failed, missing_indexes)) and integrity == "ok" and fts_integrity == "ok"
        if pass_state and source_gap_count:
            status, code = "INCOMPLETE_SOURCE_GAPS", 3
        elif pass_state:
            status, code = "PASS", 0
        else:
            status, code = "INCOMPLETE", 2
        result = {
            "evidence_files": len(files),
            "automatic_files": len(actual_automatic),
            "manual_trade_files": len(actual_manual),
            "historical_mail_files": len(actual_historical_mail),
            "content_objects": len(objects),
            "occurrences": len(occurrences),
            "tasks": len(tasks),
            "pending": pending,
            "failed": failed,
            "task_source_gaps": task_source_gaps,
            "source_gaps": source_gap_count,
            "binding_mismatch": binding_mismatch,
            "processing_scope_v2_mismatch": scope_mismatch,
            "evidence_scope_mismatch": evidence_scope_mismatch,
            "content_object_mismatch": object_mismatch,
            "occurrence_mismatch": occurrence_mismatch,
            "evidence_hash_mismatch": mismatched,
            "missing_outputs": missing_outputs,
            "bad_output_hash": bad_outputs,
            "missing_indexes": missing_indexes,
            "fts_rows": fts_rows,
            "relation_rows": relations,
            "sqlite_integrity": integrity,
            "fts_integrity": fts_integrity,
            "auto7_status": "PASS",
            "automatic_session_id": automatic["session_id"],
            "manual_trade_status": manual["status"],
            "manual_source_gaps": manual["source_gap_count"],
            "mail_capture_status": mail["status"],
            "mail_source_gaps": len(mail["source_gaps"]),
            "automatic_source_gaps": len(automatic_gaps["source_gaps"]),
            "status": status,
        }
        atomic_json(root / "verification.json", {"schema": SCHEMA, "company_id": company_id, "verified_at": now(), **result})
        result["verification_sha256"] = sha256_file(root / "verification.json")
        return result, code
    finally:
        db.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Local-only OKKI single-customer full extraction")
    subs = root.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run", "verify"):
        item = subs.add_parser(name)
        item.add_argument("--case-root", type=Path, required=True)
        item.add_argument("--company-id", required=True)
        item.add_argument("--output-dir", type=Path, help="case-relative isolated output; default: derived/full_extract_v2")
        if name == "prepare":
            item.add_argument("--archive-total-bytes", type=int, default=DEFAULT_ARCHIVE_TOTAL_BYTES, help="total actual expanded bytes across all containers (default: 4 GiB)")
            item.add_argument("--archive-max-members", type=int, default=DEFAULT_ARCHIVE_MAX_MEMBERS, help="total inspected archive entries across all containers (default: 10000)")
        if name == "run":
            item.add_argument("--max-tasks", type=int)
            item.add_argument("--parse-workers", type=int, default=min(32, os.cpu_count() or 1))
            item.add_argument("--ocr-workers", type=int, default=min(24, max(1, (os.cpu_count() or 2) // 2)))
            item.add_argument("--skip-models", action="store_true")
            item.add_argument("--ffmpeg")
            item.add_argument("--ffprobe")
            item.add_argument("--pdftoppm")
            item.add_argument("--ghostscript")
            item.add_argument("--tesseract")
            item.add_argument("--tessdata-dir", help="local Tesseract language data; defaults to TESSDATA_PREFIX or the installed Tesseract pack")
            item.add_argument("--ocr-languages", default="chi_sim+eng")
            item.add_argument("--asr-script")
            item.add_argument("--asr-python")
    return root


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    args = parser().parse_args()
    case_root = args.case_root.resolve()
    try:
        if args.command == "prepare":
            result = prepare(case_root, args.company_id, args.output_dir, archive_total_bytes=args.archive_total_bytes, archive_max_members=args.archive_max_members)
            output_status("prepare", "READY", **result)
            return 0
        if args.command == "run":
            result = run(case_root, args.company_id, args)
            output_status("run", "COMPLETE" if result["remaining"] == 0 else "INCOMPLETE", **result)
            return 0 if result["remaining"] == 0 else 2
        result, code = verify(case_root, args.company_id, args.output_dir)
        output_status("verify", result.pop("status"), **result)
        return code
    except Exception as exc:
        output_status(args.command, "FAILED", error_code=sanitized_error(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
