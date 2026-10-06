#!/usr/bin/env python3
# EN: Serve a loopback-only read-only evidence explorer; it has no public-network login layer.
# 中文：提供仅回环监听、只读的本地证据浏览器；不暴露到外网。
"""Local-only, read-only browser for a single customer's unredacted SQLite corpus.

The server deliberately has no third-party or network dependencies.  It binds
only to 127.0.0.1, opens SQLite in read-only/query-only mode, parameterizes all
record-value predicates, enforces bounded pagination, and never writes record
values to the process log.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


HOST = "127.0.0.1"
DEFAULT_PORT = 18765
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_PAGE = 100_000
QUERY_TIMEOUT_SECONDS = 20.0
MAX_QUERY_CHARS = 512
LIST_TEXT_PREVIEW_CHARS = 4000
DEFAULT_DOCUMENT_CHUNK_CHARS = 1_000_000
MAX_DOCUMENT_CHUNK_CHARS = 4_000_000
MAX_DOCUMENT_OFFSET = 2_000_000_000
SAFE_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
VIEW_ALIASES = {
    "documents": ("documents_fts", "full_text", "documents"),
    "nodes": ("nodes",),
    "relation_fact": ("relation_fact", "relation_facts"),
    "relation_occurrence": ("relation_occurrence", "relation_occurrences"),
}
PREFERRED_COLUMNS = {
    "documents": (
        "document_id", "id", "source_rel", "source_sha256", "artifact_rel",
        "task_kind", "title", "text", "body", "content", "text_chars",
    ),
    "nodes": (
        "node_id", "node_type", "entity_type", "value", "normalized_value",
        "display_value", "occurrence_count", "first_seen_utc", "last_seen_utc",
    ),
    "relation_fact": (
        "fact_id", "relation_id", "source_node_id", "from_node_id",
        "relation_type", "relation", "target_node_id", "to_node_id",
        "occurrence_count", "source_count", "first_seen_utc", "last_seen_utc",
    ),
    "relation_occurrence": (
        "occurrence_id", "fact_id", "source_rel", "json_pointer", "location",
        "observed_at_utc", "context", "sequence_no",
    ),
}
TEXT_COLUMN_HINTS = {
    "text", "body", "content", "title", "value", "normalized_value",
    "display_value", "source_rel", "artifact_rel", "json_pointer", "context",
    "relation", "relation_type", "node_type", "entity_type", "task_kind",
}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _quote_identifier(value: str) -> str:
    # Names come only from sqlite_schema / table_info, never from HTTP input.
    return '"' + value.replace('"', '""') + '"'


def _parse_positive(value: str | None, default: int, maximum: int, name: str) -> int:
    if value is None or value == "":
        return default
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{name.upper()}_INVALID")
    number = int(value)
    if number < 1 or number > maximum:
        raise ValueError(f"{name.upper()}_OUT_OF_RANGE")
    return number


def _parse_nonnegative(value: str | None, default: int, maximum: int, name: str) -> int:
    if value is None or value == "":
        return default
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{name.upper()}_INVALID")
    number = int(value)
    if number < 0 or number > maximum:
        raise ValueError(f"{name.upper()}_OUT_OF_RANGE")
    return number


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class TableSpec:
    view: str
    table: str
    columns: tuple[str, ...]
    search_columns: tuple[str, ...]
    is_fts: bool
    has_rowid: bool
    order_columns: tuple[str, ...]


class ExplorerDatabase:
    """Read-only facade over the four supported logical views."""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        path = Path(db_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("DATABASE_NOT_FILE")
        self.path = path
        self._uri = f"{path.as_uri()}?mode=ro"
        self.specs = self._discover_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._uri,
            uri=True,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _discover_schema(self) -> dict[str, TableSpec]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute(
                "SELECT name, type, COALESCE(sql, '') AS sql FROM sqlite_schema "
                "WHERE type IN ('table','view') ORDER BY name"
            ).fetchall()
            catalog = {str(row["name"]): row for row in rows}
            specs: dict[str, TableSpec] = {}
            missing: list[str] = []
            for view, aliases in VIEW_ALIASES.items():
                table = next((candidate for candidate in aliases if candidate in catalog), None)
                if table is None:
                    missing.append(view)
                    continue
                if not IDENTIFIER_RE.fullmatch(table):
                    raise ValueError("DATABASE_TABLE_NAME_UNSAFE")
                info = db.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
                columns = tuple(str(row[1]) for row in info)
                if not columns or any(not IDENTIFIER_RE.fullmatch(column) for column in columns):
                    raise ValueError(f"DATABASE_COLUMNS_INVALID_{view.upper()}")
                sql = str(catalog[table]["sql"] or "")
                is_fts = bool(re.search(r"\bVIRTUAL\s+TABLE\b.*\bUSING\s+fts[345]\b", sql, re.I | re.S))
                has_rowid = is_fts or not bool(re.search(r"\bWITHOUT\s+ROWID\b", sql, re.I))
                primary_keys = tuple(
                    str(row[1]) for row in sorted((row for row in info if int(row[5] or 0) > 0), key=lambda row: int(row[5]))
                )
                stable_preferences = {
                    "documents": ("document_id", "id"),
                    "nodes": ("node_id",),
                    "relation_fact": ("fact_id", "from_node_id", "to_node_id", "relation", "source_node_id", "target_node_id", "relation_type"),
                    "relation_occurrence": ("occurrence_id", "fact_id"),
                }[view]
                order_columns = primary_keys or tuple(column for column in stable_preferences if column in columns)
                if not has_rowid and not order_columns:
                    raise ValueError(f"DATABASE_STABLE_KEY_MISSING_{view.upper()}")
                preferred = [column for column in PREFERRED_COLUMNS[view] if column in columns]
                output_columns = tuple(preferred + [column for column in columns if column not in preferred])
                searchable = tuple(
                    column for column in output_columns
                    if column.casefold() in TEXT_COLUMN_HINTS
                    or any(hint in column.casefold() for hint in ("name", "mail", "phone", "address", "path", "value", "text", "content", "type"))
                )
                if not searchable:
                    searchable = output_columns[: min(4, len(output_columns))]
                specs[view] = TableSpec(view, table, output_columns, searchable, is_fts, has_rowid, order_columns)
            if missing:
                raise ValueError("DATABASE_REQUIRED_VIEWS_MISSING_" + "_".join(item.upper() for item in missing))
            return specs

    def integrity(self) -> str:
        with contextlib.closing(self.connect()) as db:
            return str(db.execute("PRAGMA integrity_check").fetchone()[0])

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        with contextlib.closing(self.connect()) as db:
            deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            for view, spec in self.specs.items():
                row = db.execute(f"SELECT COUNT(*) FROM {_quote_identifier(spec.table)}").fetchone()
                result[view] = int(row[0])
        return result

    @staticmethod
    def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
        if not IDENTIFIER_RE.fullmatch(table):
            raise ValueError("DATABASE_TABLE_NAME_UNSAFE")
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({_quote_identifier(table)})")}

    def export_catalog_payload(self, output: Path, company_id: str | None, explorer_port: int) -> dict[str, int]:
        """Build the Excel catalog payload without reading full_text or occurrence rows.

        Node values and the two endpoint values of each de-duplicated relation
        fact are intentionally retained. Full document bodies and detailed
        relation_occurrence records never enter the payload.
        中文：保留节点原值与关系端点，完整正文及细粒度出现记录不进入此目录载荷。
        """
        with contextlib.closing(self.connect()) as db:
            required = {
                "documents": {"source_rel"},
                "nodes": {"node_id", "value"},
                "relation_fact": {"from_node_id", "to_node_id", "relation"},
                "relation_occurrence": {"occurrence_id", "relation", "source_rel"},
                "files": {"source_rel", "sha256", "bytes", "category", "mime", "active"},
                "tasks": {"task_id", "source_rel", "task_kind", "state", "attempts"},
                "coverage": {"scope", "name", "total", "completed", "pending", "failed"},
                "manual_trade_artifacts": {"artifact_id", "case_relative_path", "sha256", "bytes"},
                "meta": {"key", "value"},
            }
            for table, columns in required.items():
                if not columns.issubset(self._table_columns(db, table)):
                    raise ValueError(f"CATALOG_SCHEMA_INVALID_{table.upper()}")

            if not company_id:
                row = db.execute("SELECT value FROM meta WHERE key='company_id'").fetchone()
                company_id = str(row[0]).strip() if row else ""
            if not company_id:
                raise ValueError("CATALOG_COMPANY_ID_MISSING")

            node_columns = self._table_columns(db, "nodes")
            entity_column = "entity_type" if "entity_type" in node_columns else "node_type" if "node_type" in node_columns else None
            if entity_column is None:
                raise ValueError("CATALOG_NODE_TYPE_COLUMN_MISSING")

            fact_columns = self._table_columns(db, "relation_fact")
            evidence_column = "evidence_count" if "evidence_count" in fact_columns else "occurrence_count" if "occurrence_count" in fact_columns else None
            if evidence_column is None:
                raise ValueError("CATALOG_FACT_COUNT_COLUMN_MISSING")
            first_source_expr = "rf.first_source_rel" if "first_source_rel" in fact_columns else "''"
            last_source_expr = "rf.last_source_rel" if "last_source_rel" in fact_columns else first_source_expr

            logical_sql = (
                f"SELECT rf.from_node_id,nf.{_quote_identifier(entity_column)} AS from_type,nf.value AS from_value,"
                f"rf.relation,rf.to_node_id,nt.{_quote_identifier(entity_column)} AS to_type,nt.value AS to_value,"
                f"rf.{_quote_identifier(evidence_column)} AS evidence_count,{first_source_expr} AS first_source_rel,"
                f"{last_source_expr} AS last_source_rel FROM relation_fact rf "
                "LEFT JOIN nodes nf ON nf.node_id=rf.from_node_id LEFT JOIN nodes nt ON nt.node_id=rf.to_node_id "
                "ORDER BY rf.from_node_id,rf.to_node_id,rf.relation"
            )
            logical_relation_rows: list[list[Any]] = []
            node_evidence: dict[str, list[Any]] = {}
            for row in db.execute(logical_sql):
                from_id, from_type, from_value, relation, to_id, to_type, to_value, evidence, first_source, last_source = row
                fact_key = "\x00".join((str(from_id), str(relation), str(to_id))).encode("utf-8")
                fact_id = hashlib.sha256(fact_key).hexdigest().upper()[:32]
                logical_relation_rows.append([
                    fact_id, from_id, from_type or "", from_value or "", relation, to_id, to_type or "", to_value or "",
                    int(evidence or 0), first_source or "", last_source or "",
                ])
                for node_id in (str(from_id), str(to_id)):
                    aggregate = node_evidence.setdefault(node_id, [0, ""])
                    aggregate[0] += int(evidence or 0)
                    if not aggregate[1] and first_source:
                        aggregate[1] = str(first_source)

            entity_count_rows = [
                [row[0] or "", int(row[1]), int(row[2]), "按 nodes 原值聚合"]
                for row in db.execute(
                    f"SELECT {_quote_identifier(entity_column)},COUNT(*),COUNT(DISTINCT value) FROM nodes "
                    f"GROUP BY {_quote_identifier(entity_column)} ORDER BY COUNT(*) DESC,{_quote_identifier(entity_column)}"
                )
            ]
            node_detail_rows = []
            for row in db.execute(
                f"SELECT node_id,{_quote_identifier(entity_column)},value FROM nodes ORDER BY node_id"
            ):
                evidence, first_source = node_evidence.get(str(row[0]), [0, ""])
                node_detail_rows.append([row[0], row[1] or "", row[2] or "", "", int(evidence), first_source])

            fact_counts = {str(row[0]): int(row[1]) for row in db.execute("SELECT relation,COUNT(*) FROM relation_fact GROUP BY relation")}
            occurrence_counts = {
                str(row[0]): (int(row[1]), int(row[2]))
                for row in db.execute("SELECT relation,COUNT(*),COUNT(DISTINCT source_rel) FROM relation_occurrence GROUP BY relation")
            }
            relation_stat_rows = []
            for relation in sorted(set(fact_counts) | set(occurrence_counts), key=str.casefold):
                occurrences, sources = occurrence_counts.get(relation, (0, 0))
                relation_stat_rows.append([relation, fact_counts.get(relation, 0), occurrences, sources, "逻辑事实与 occurrence 分层统计"])

            coverage_rows = []
            for row in db.execute("SELECT scope,name,total,completed,pending,failed FROM coverage ORDER BY scope,name"):
                unfinished = int(row[4] or 0) + int(row[5] or 0)
                status = "PASS" if unfinished == 0 and int(row[3] or 0) == int(row[2] or 0) else "INCOMPLETE"
                coverage_rows.append([f"{row[0]} / {row[1]}", int(row[2] or 0), int(row[3] or 0), unfinished, status, "coverage"])

            file_catalog_rows = [
                [row[0], row[4] or "", row[0], int(row[2] or 0), row[1] or "", row[3] or "", "ACTIVE" if int(row[5] or 0) else "INACTIVE"]
                for row in db.execute("SELECT source_rel,sha256,bytes,mime,category,active FROM files ORDER BY source_rel")
            ]
            for row in db.execute(
                "SELECT artifact_id,case_relative_path,sha256,bytes FROM manual_trade_artifacts ORDER BY artifact_id"
            ):
                file_catalog_rows.append([
                    f"manual:{row[0]}", "manual_trade", row[1] or "", int(row[3] or 0), row[2] or "",
                    "application/octet-stream", "ACTIVE",
                ])

            task_columns = self._table_columns(db, "tasks")
            output_expr = "output_rel" if "output_rel" in task_columns else "''"
            result_expr = "result_sha256" if "result_sha256" in task_columns else "''"
            error_expr = "error_code" if "error_code" in task_columns else "''"
            task_catalog_rows = [
                [row[0], row[1] or "", row[2] or "", row[3] or "", row[4] or "", row[5] or "", int(row[6] or 0), row[7] or ""]
                for row in db.execute(
                    f"SELECT task_id,task_kind,source_rel,state,{output_expr},{result_expr},attempts,{error_expr} FROM tasks ORDER BY task_id"
                )
            ]

            counts = {
                "documents": int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]),
                "nodes": len(node_detail_rows),
                "relation_facts": len(logical_relation_rows),
                "relation_occurrences": int(db.execute("SELECT COUNT(*) FROM relation_occurrence").fetchone()[0]),
                "files": len(file_catalog_rows),
                "tasks": len(task_catalog_rows),
            }

        payload = {
            "schema": "okki.single_customer.unredacted_catalog_workbook_payload.v1",
            "company_id": company_id,
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "database": {
                "filename": self.path.name,
                "bytes": self.path.stat().st_size,
                "sha256": _sha256_file(self.path),
                "integrity": self.integrity(),
            },
            "explorer": {"url": f"http://127.0.0.1:{explorer_port}/"},
            "counts": counts,
            "coverage_rows": coverage_rows,
            "entity_count_rows": entity_count_rows,
            "node_detail_rows": node_detail_rows,
            "relation_stat_rows": relation_stat_rows,
            "file_catalog_rows": file_catalog_rows,
            "task_catalog_rows": task_catalog_rows,
            "logical_relation_rows": logical_relation_rows,
        }
        # Explicit fail-closed assertion: neither bodies nor occurrence detail
        # are materialized in the JSON handed to the workbook builder.
        forbidden = {"full_text_rows", "document_text_rows", "relation_occurrence_rows", "occurrence_rows"}
        if forbidden.intersection(payload):
            raise ValueError("CATALOG_FORBIDDEN_ROWS_PRESENT")
        _atomic_json(output, payload)
        return counts

    def search(self, view: str, query: str, page: int, page_size: int) -> dict[str, Any]:
        if view not in self.specs:
            raise ValueError("VIEW_INVALID")
        query = query.strip()
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError("QUERY_TOO_LONG")
        spec = self.specs[view]
        offset = (page - 1) * page_size
        if offset > MAX_PAGE * MAX_PAGE_SIZE:
            raise ValueError("OFFSET_OUT_OF_RANGE")
        table = _quote_identifier(spec.table)
        select_expressions: list[str] = []
        truncated_columns: list[str] = []
        for column in spec.columns:
            quoted = _quote_identifier(column)
            if view == "documents" and column.casefold() in {"text", "body", "content"}:
                select_expressions.append(
                    f"CASE WHEN length({quoted})>? THEN substr({quoted},1,?) ELSE {quoted} END AS {quoted}"
                )
                truncated_columns.append(column)
            else:
                select_expressions.append(quoted)
        select_columns = ",".join(select_expressions)
        parameters: list[Any] = []
        predicate = ""
        if query:
            if spec.is_fts:
                predicate = f" WHERE {table} MATCH ?"
                parameters.append(query)
            else:
                like = f"%{_escape_like(query)}%"
                clauses = [f"CAST({_quote_identifier(column)} AS TEXT) LIKE ? ESCAPE '\\'" for column in spec.search_columns]
                predicate = " WHERE " + " OR ".join(clauses)
                parameters.extend([like] * len(clauses))
        count_sql = f"SELECT COUNT(*) FROM {table}{predicate}"
        identity_sql = "rowid AS _rowid," if spec.has_rowid else ""
        order_sql = "rowid" if spec.has_rowid else ",".join(_quote_identifier(column) for column in spec.order_columns)
        preview_parameters: list[Any] = []
        for _column in truncated_columns:
            preview_parameters.extend([LIST_TEXT_PREVIEW_CHARS, LIST_TEXT_PREVIEW_CHARS])
        for column in truncated_columns:
            quoted = _quote_identifier(column)
            select_columns += f",CASE WHEN length({quoted})>? THEN 1 ELSE 0 END AS {_quote_identifier(f'_{column}_truncated')}"
            preview_parameters.append(LIST_TEXT_PREVIEW_CHARS)
        data_sql = f"SELECT {identity_sql}{select_columns} FROM {table}{predicate} ORDER BY {order_sql} LIMIT ? OFFSET ?"
        with contextlib.closing(self.connect()) as db:
            deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            try:
                total = int(db.execute(count_sql, parameters).fetchone()[0])
                rows = db.execute(data_sql, [*preview_parameters, *parameters, page_size, offset]).fetchall()
            except sqlite3.OperationalError as exc:
                # Do not include SQLite text: an FTS parser error can echo the query.
                if "interrupted" in str(exc).casefold():
                    raise TimeoutError("QUERY_TIMEOUT") from None
                raise ValueError("QUERY_INVALID") from None
        records: list[dict[str, Any]] = []
        for row in rows:
            record = {key: row[key] for key in row.keys()}
            if not spec.has_rowid:
                record["_key"] = "\x1f".join(str(record.get(column, "")) for column in spec.order_columns)
            records.append(record)
        identity_columns = ["_rowid"] if spec.has_rowid else ["_key"]
        truncation_flags = [f"_{column}_truncated" for column in truncated_columns]
        return {
            "view": view,
            "table": spec.table,
            "columns": [*identity_columns, *spec.columns, *truncation_flags],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
            "records": records,
        }

    def document_chunk(self, rowid: int, offset: int, length: int) -> tuple[str, int]:
        return self.document_slice(rowid, offset, length), self.document_total(rowid)

    def _document_target(self) -> tuple[TableSpec, str]:
        spec = self.specs["documents"]
        if not spec.has_rowid:
            raise ValueError("DOCUMENT_ROWID_UNAVAILABLE")
        text_column = next((column for column in spec.columns if column.casefold() in {"text", "body", "content"}), None)
        if text_column is None:
            raise ValueError("DOCUMENT_TEXT_COLUMN_MISSING")
        return spec, text_column

    def document_total(self, rowid: int) -> int:
        spec, text_column = self._document_target()
        table = _quote_identifier(spec.table)
        column = _quote_identifier(text_column)
        with contextlib.closing(self.connect()) as db:
            deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            try:
                row = db.execute(f"SELECT length({column}) AS total_chars FROM {table} WHERE rowid=?", (rowid,)).fetchone()
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).casefold():
                    raise TimeoutError("QUERY_TIMEOUT") from None
                raise ValueError("QUERY_INVALID") from None
        if row is None:
            raise ValueError("DOCUMENT_NOT_FOUND")
        return int(row["total_chars"] or 0)

    def document_slice(self, rowid: int, offset: int, length: int) -> str:
        spec, text_column = self._document_target()
        table = _quote_identifier(spec.table)
        column = _quote_identifier(text_column)
        with contextlib.closing(self.connect()) as db:
            deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            try:
                row = db.execute(
                    f"SELECT substr({column}, ?, ?) AS chunk FROM {table} WHERE rowid=?", (offset + 1, length, rowid)
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).casefold():
                    raise TimeoutError("QUERY_TIMEOUT") from None
                raise ValueError("QUERY_INVALID") from None
        if row is None:
            raise ValueError("DOCUMENT_NOT_FOUND")
        return str(row["chunk"] or "")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>单客户不匿名本地资料库</title>
<style>
:root{color-scheme:light;--navy:#0b2545;--blue:#2e74b5;--pale:#e8eef5;--line:#d9e2f3;--ink:#1f2937}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 "Segoe UI","Microsoft YaHei",sans-serif;color:var(--ink);background:#f6f8fb}
header{background:var(--navy);color:#fff;padding:18px 24px}header h1{margin:0;font-size:22px}header p{margin:5px 0 0;color:#dbeafe}
main{padding:18px 22px}.controls{display:grid;grid-template-columns:190px minmax(260px,1fr) 100px 92px;gap:10px;background:#fff;padding:14px;border:1px solid var(--line);border-radius:8px;position:sticky;top:0;z-index:2}
select,input,button{font:inherit;padding:9px 10px;border:1px solid #b8c6d9;border-radius:5px;background:#fff}button{background:var(--blue);color:#fff;border-color:var(--blue);cursor:pointer}button:disabled{opacity:.45;cursor:default}
.status{display:flex;gap:18px;align-items:center;margin:12px 2px;color:#44546a}.status strong{color:var(--navy)}
.tablebox{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:8px;max-height:calc(100vh - 230px)}table{border-collapse:separate;border-spacing:0;min-width:100%;width:max-content}th,td{padding:8px 10px;border-right:1px solid #edf1f7;border-bottom:1px solid #e5e7eb;vertical-align:top;max-width:560px;white-space:pre-wrap;overflow-wrap:anywhere}th{position:sticky;top:0;background:var(--pale);color:#1f3a5f;text-align:left;z-index:1}tr:nth-child(even) td{background:#fafcff}
.pager{display:flex;justify-content:flex-end;align-items:center;gap:8px;margin-top:10px}.empty{padding:40px;color:#64748b;text-align:center}.error{color:#b91c1c;font-weight:600}.doclink{display:inline-block;margin-left:8px;color:#0b5cab;font-weight:600}
@media(max-width:760px){.controls{grid-template-columns:1fr}.tablebox{max-height:none}}
</style></head><body>
<header><h1>单客户不匿名本地资料库</h1><p>只读 · 仅 127.0.0.1 · 全文与 occurrence 不进入 Excel</p></header>
<main><form class="controls" id="searchForm">
<select id="view" aria-label="数据视图"><option value="documents">全文文档</option><option value="nodes">实体节点</option><option value="relation_fact">逻辑关系</option><option value="relation_occurrence">关系发生记录</option></select>
<input id="q" maxlength="512" placeholder="输入关键词；全文文档支持 SQLite FTS5 查询" autocomplete="off">
<select id="pageSize" aria-label="每页条数"><option>10</option><option selected>25</option><option>50</option><option>100</option></select>
<button type="submit">查询</button></form>
<div class="status"><span id="dbStatus">正在读取结构…</span><span id="resultStatus"></span></div>
<div class="tablebox" id="tablebox"><div class="empty">请输入关键词或直接浏览。</div></div>
<div class="pager"><button id="prev" type="button">上一页</button><span id="pageLabel">第 1 页</span><button id="next" type="button">下一页</button></div>
</main><script>
"use strict";
const state={page:1,pages:1}; const $=id=>document.getElementById(id);
function text(value){if(value===null)return "NULL"; if(typeof value==="object")return JSON.stringify(value,null,2); return String(value)}
function render(payload){const box=$("tablebox"); box.replaceChildren(); const table=document.createElement("table"); const head=document.createElement("thead"); const hr=document.createElement("tr");
payload.columns.forEach(name=>{const th=document.createElement("th");th.textContent=name;hr.appendChild(th)});head.appendChild(hr);table.appendChild(head);const body=document.createElement("tbody");
payload.records.forEach(record=>{const tr=document.createElement("tr");payload.columns.forEach(name=>{const td=document.createElement("td");td.textContent=text(record[name]);if(payload.view==="documents"&&name==="_rowid"){const a=document.createElement("a");a.className="doclink";a.textContent="分块查看完整正文";a.target="_blank";a.rel="noopener";a.href="/document?rowid="+encodeURIComponent(record[name]);td.appendChild(a)}tr.appendChild(td)});body.appendChild(tr)});table.appendChild(body);box.appendChild(table);
state.page=payload.page;state.pages=payload.pages;$("resultStatus").textContent=`共 ${payload.total.toLocaleString()} 条`;$("pageLabel").textContent=`第 ${payload.page.toLocaleString()} / ${payload.pages.toLocaleString()} 页`;$("prev").disabled=payload.page<=1;$("next").disabled=payload.page>=payload.pages}
async function load(){const p=new URLSearchParams({view:$("view").value,q:$("q").value,page:String(state.page),page_size:$("pageSize").value});$("resultStatus").textContent="查询中…";
try{const res=await fetch("/api/search?"+p,{headers:{"Accept":"application/json"}});const data=await res.json();if(!res.ok)throw new Error(data.error||"QUERY_FAILED");render(data)}catch(err){$("resultStatus").textContent="查询失败";const box=$("tablebox");box.replaceChildren();const div=document.createElement("div");div.className="empty error";div.textContent=err.message;box.appendChild(div)}}
$("searchForm").addEventListener("submit",event=>{event.preventDefault();state.page=1;load()});$("prev").addEventListener("click",()=>{if(state.page>1){state.page--;load()}});$("next").addEventListener("click",()=>{if(state.page<state.pages){state.page++;load()}});$("view").addEventListener("change",()=>{state.page=1;load()});
fetch("/api/meta").then(r=>r.json()).then(m=>{$("dbStatus").textContent=`数据库完整性 ${m.integrity} · 文档 ${m.counts.documents.toLocaleString()} · 节点 ${m.counts.nodes.toLocaleString()} · 逻辑关系 ${m.counts.relation_fact.toLocaleString()} · occurrence ${m.counts.relation_occurrence.toLocaleString()}`;load()}).catch(()=>{$("dbStatus").textContent="数据库结构读取失败"});
</script></body></html>"""


DOCUMENT_HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>完整正文分块查看</title>
<style>body{margin:0;font:14px/1.55 "Segoe UI","Microsoft YaHei",sans-serif;background:#f6f8fb;color:#1f2937}header{position:sticky;top:0;background:#0b2545;color:#fff;padding:12px 18px;display:flex;gap:12px;align-items:center;z-index:2}button,a{padding:7px 12px;border:0;border-radius:4px;background:#2e74b5;color:#fff;text-decoration:none}button:disabled{opacity:.45}span{color:#dbeafe}pre{margin:16px;padding:18px;background:#fff;border:1px solid #d9e2f3;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;min-height:200px}</style></head>
<body><header><strong>完整正文（分块只读）</strong><button id="prev">上一块</button><button id="next">下一块</button><a id="download">下载完整正文</a><span id="state">读取中…</span></header><pre id="text"></pre>
<script>"use strict";const rowid=__ROWID__;const size=1000000;let offset=0,total=0;const $=id=>document.getElementById(id);
async function load(){const p=new URLSearchParams({rowid:String(rowid),offset:String(offset),length:String(size)});const r=await fetch("/api/document?"+p);if(!r.ok){const e=await r.json();throw new Error(e.error||"DOCUMENT_FAILED")}total=Number(r.headers.get("X-Total-Chars")||0);$("text").textContent=await r.text();$("state").textContent=`字符 ${offset.toLocaleString()}–${Math.min(total,offset+size).toLocaleString()} / ${total.toLocaleString()}`;$("prev").disabled=offset===0;$("next").disabled=offset+size>=total}
$("download").href="/api/document/download?rowid="+encodeURIComponent(rowid);$("prev").onclick=()=>{offset=Math.max(0,offset-size);load()};$("next").onclick=()=>{if(offset+size<total){offset+=size;load()}};load().catch(e=>{$("state").textContent=e.message});</script></body></html>"""


class ExplorerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        database: ExplorerDatabase,
        *,
        integrity_status: str | None = None,
        startup_counts: dict[str, int] | None = None,
    ):
        super().__init__(address, ExplorerHandler)
        self.database = database
        # The package is immutable while this read-only explorer is running.
        # Re-running a 2.5 GiB integrity_check for every browser refresh makes
        # the UI appear hung and causes short health probes to disconnect.
        self.integrity_status = integrity_status if integrity_status is not None else database.integrity()
        self.startup_counts = dict(startup_counts) if startup_counts is not None else database.counts()


class ExplorerHandler(BaseHTTPRequestHandler):
    server: ExplorerServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Query strings and record values are intentionally never logged.
        return

    def _request_is_local(self) -> bool:
        remote = self.client_address[0]
        if remote not in {"127.0.0.1", "::1"}:
            return False
        host = self.headers.get("Host", "").strip().casefold()
        if host.startswith("["):
            host_name = host.split("]", 1)[0] + "]"
        else:
            host_name = host.split(":", 1)[0]
        return host_name in SAFE_HOSTS

    def _send(self, status: HTTPStatus, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A local tab or readiness probe may close before a large response
            # finishes; this is not a server failure and should not emit noise.
            return

    def _send_error_code(self, status: HTTPStatus, code: str) -> None:
        self._send(status, "application/json; charset=utf-8", _json_bytes({"error": code}))

    def _send_document_chunk(self, chunk: str, rowid: int, offset: int, total: int) -> None:
        payload = chunk.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("X-Document-Rowid", str(rowid))
        self.send_header("X-Chunk-Offset", str(offset))
        self.send_header("X-Chunk-Chars", str(len(chunk)))
        self.send_header("X-Total-Chars", str(total))
        self.send_header("X-Next-Offset", str(min(total, offset + len(chunk))))
        self.end_headers()
        self.wfile.write(payload)

    def _send_document_download(self, rowid: int) -> None:
        total = self.server.database.document_total(rowid)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="document_{rowid}.txt"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Total-Chars", str(total))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        offset = 0
        try:
            while offset < total:
                chunk = self.server.database.document_slice(rowid, offset, DEFAULT_DOCUMENT_CHUNK_CHARS)
                if not chunk:
                    break
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
                offset += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._request_is_local():
            self._send_error_code(HTTPStatus.MISDIRECTED_REQUEST, "LOCALHOST_ONLY")
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
            return
        if parsed.path == "/document":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=3)
            try:
                rowid_raw = params.get("rowid", [None])[0]
                if rowid_raw is None:
                    raise ValueError("ROWID_REQUIRED")
                rowid = _parse_positive(rowid_raw, 1, 9_223_372_036_854_775_807, "rowid")
                html = DOCUMENT_HTML.replace("__ROWID__", str(rowid)).encode("utf-8")
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", html)
            except ValueError as exc:
                code = str(exc) if re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "REQUEST_INVALID"
                self._send_error_code(HTTPStatus.BAD_REQUEST, code)
            return
        if parsed.path == "/api/meta":
            try:
                payload = {
                    "schema": "okki.unredacted_explorer.meta.v1",
                    "integrity": self.server.integrity_status,
                    "counts": self.server.startup_counts,
                    "views": {
                        name: {"table": spec.table, "columns": list(spec.columns), "fts": spec.is_fts}
                        for name, spec in self.server.database.specs.items()
                    },
                }
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", _json_bytes(payload))
            except (sqlite3.Error, TimeoutError):
                self._send_error_code(HTTPStatus.SERVICE_UNAVAILABLE, "DATABASE_UNAVAILABLE")
            return
        if parsed.path == "/api/search":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
            try:
                view = params.get("view", ["documents"])[0]
                query = params.get("q", [""])[0]
                page = _parse_positive(params.get("page", [None])[0], 1, MAX_PAGE, "page")
                page_size = _parse_positive(params.get("page_size", [None])[0], DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, "page_size")
                result = self.server.database.search(view, query, page, page_size)
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", _json_bytes(result))
            except ValueError as exc:
                code = str(exc) if re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "REQUEST_INVALID"
                self._send_error_code(HTTPStatus.BAD_REQUEST, code)
            except TimeoutError:
                self._send_error_code(HTTPStatus.SERVICE_UNAVAILABLE, "QUERY_TIMEOUT")
            except sqlite3.Error:
                self._send_error_code(HTTPStatus.SERVICE_UNAVAILABLE, "DATABASE_UNAVAILABLE")
            return
        if parsed.path == "/api/document/download":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=3)
            try:
                rowid_raw = params.get("rowid", [None])[0]
                if rowid_raw is None:
                    raise ValueError("ROWID_REQUIRED")
                rowid = _parse_positive(rowid_raw, 1, 9_223_372_036_854_775_807, "rowid")
                self._send_document_download(rowid)
            except ValueError as exc:
                code = str(exc) if re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "REQUEST_INVALID"
                status = HTTPStatus.NOT_FOUND if code == "DOCUMENT_NOT_FOUND" else HTTPStatus.BAD_REQUEST
                self._send_error_code(status, code)
            except TimeoutError:
                self._send_error_code(HTTPStatus.SERVICE_UNAVAILABLE, "QUERY_TIMEOUT")
            except sqlite3.Error:
                self._send_error_code(HTTPStatus.SERVICE_UNAVAILABLE, "DATABASE_UNAVAILABLE")
            return
        if parsed.path == "/api/document":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=5)
            try:
                rowid_raw = params.get("rowid", [None])[0]
                if rowid_raw is None:
                    raise ValueError("ROWID_REQUIRED")
                rowid = _parse_positive(rowid_raw, 1, 9_223_372_036_854_775_807, "rowid")
                offset = _parse_nonnegative(params.get("offset", [None])[0], 0, MAX_DOCUMENT_OFFSET, "offset")
                length = _parse_positive(
                    params.get("length", [None])[0], DEFAULT_DOCUMENT_CHUNK_CHARS, MAX_DOCUMENT_CHUNK_CHARS, "length"
                )
                chunk, total = self.server.database.document_chunk(rowid, offset, length)
                self._send_document_chunk(chunk, rowid, offset, total)
            except ValueError as exc:
                code = str(exc) if re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "REQUEST_INVALID"
                status = HTTPStatus.NOT_FOUND if code == "DOCUMENT_NOT_FOUND" else HTTPStatus.BAD_REQUEST
                self._send_error_code(status, code)
            except TimeoutError:
                self._send_error_code(HTTPStatus.SERVICE_UNAVAILABLE, "QUERY_TIMEOUT")
            except sqlite3.Error:
                self._send_error_code(HTTPStatus.SERVICE_UNAVAILABLE, "DATABASE_UNAVAILABLE")
            return
        self._send_error_code(HTTPStatus.NOT_FOUND, "NOT_FOUND")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error_code(HTTPStatus.METHOD_NOT_ALLOWED, "READ_ONLY_GET_ONLY")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def create_server(
    database: ExplorerDatabase,
    port: int = DEFAULT_PORT,
    *,
    integrity_status: str | None = None,
    startup_counts: dict[str, int] | None = None,
) -> ExplorerServer:
    if port < 0 or port > 65535:
        raise ValueError("PORT_OUT_OF_RANGE")
    return ExplorerServer(
        (HOST, port),
        database,
        integrity_status=integrity_status,
        startup_counts=startup_counts,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Browse customer_full_unredacted.sqlite3 locally and read-only.")
    parser.add_argument("--db", required=True, help="Path to customer_full_unredacted.sqlite3")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", dest="open_browser", help="Open the local UI in the default browser")
    parser.add_argument("--check", action="store_true", help="Validate schema/integrity and exit without serving")
    parser.add_argument("--company-id", help="Company id used only in the local catalog workbook payload")
    parser.add_argument("--export-catalog-payload", type=Path, help="Write the metadata/node/relation-fact JSON payload and exit")
    return parser


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        database = ExplorerDatabase(args.db)
        integrity = database.integrity()
        if integrity != "ok":
            raise ValueError("DATABASE_INTEGRITY_FAILED")
        counts = database.counts()
        if args.export_catalog_payload:
            catalog_counts = database.export_catalog_payload(args.export_catalog_payload.resolve(), args.company_id, args.port)
            print(json.dumps({"status": "PASS", "catalog_counts": catalog_counts}, sort_keys=True))
            return 0
        if args.check:
            print(json.dumps({"status": "PASS", "integrity": integrity, "counts": counts}, sort_keys=True))
            return 0
        server = create_server(database, args.port, integrity_status=integrity, startup_counts=counts)
    except (OSError, sqlite3.Error, ValueError) as exc:
        code = str(exc) if re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "EXPLORER_START_FAILED"
        print(code, file=sys.stderr)
        return 2
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(json.dumps({"status": "READY", "url": url, "database": database.path.name, "counts": counts}, sort_keys=True), flush=True)
    if args.open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url, new=1)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
