#!/usr/bin/env python3
# EN: Local-only build completion v2 report receipt utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
"""Seal Word-COM and per-page render QA into the completion-v2 report receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
import zipfile
from pathlib import Path
from typing import Any, Sequence

from pypdf import PdfReader


SCHEMA = "okki.single_customer.completion_v2_report_verification.v1"


class ReceiptError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ReceiptError(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def read_json(path: Path) -> dict[str, Any]:
    require(path.is_file(), "WORD_VERIFICATION_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("WORD_VERIFICATION_INVALID") from exc
    require(isinstance(value, dict), "WORD_VERIFICATION_INVALID")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def ordered_pngs(root: Path) -> list[Path]:
    require(root.is_dir(), "RENDER_DIRECTORY_MISSING")
    rows = sorted(root.glob("*.png"), key=lambda path: path.name.casefold())
    require(rows and all(path.stat().st_size > 0 for path in rows), "RENDER_PAGES_MISSING")
    return rows


def build_receipt(args: argparse.Namespace) -> dict[str, Any]:
    docx = args.docx.resolve()
    pdf = args.pdf.resolve()
    require(docx.is_file() and pdf.is_file(), "REPORT_ARTIFACT_MISSING")
    try:
        with zipfile.ZipFile(docx) as archive:
            require(archive.testzip() is None and "word/document.xml" in archive.namelist(), "DOCX_INVALID")
    except zipfile.BadZipFile as exc:
        raise ReceiptError("DOCX_INVALID") from exc
    try:
        pdf_pages = len(PdfReader(str(pdf), strict=True).pages)
    except Exception as exc:
        raise ReceiptError("PDF_INVALID") from exc
    require(pdf_pages > 0, "PDF_EMPTY")
    word = read_json(args.word_verification.resolve())
    require(word.get("status") == "PASS", "WORD_COM_NOT_PASS")
    require(word.get("opened_read_only") is True, "WORD_COM_NOT_READ_ONLY")
    require(str(word.get("docx_sha256") or "").upper() == sha256_file(docx), "WORD_COM_DOCX_HASH_MISMATCH")
    require(str(word.get("pdf_sha256") or "").upper() == sha256_file(pdf), "WORD_COM_PDF_HASH_MISMATCH")
    word_pages = int(word.get("pages") or 0)
    require(word_pages == pdf_pages, "WORD_COM_PAGE_COUNT_MISMATCH")
    docx_pngs = ordered_pngs(args.docx_render_dir.resolve())
    pdf_pngs = ordered_pngs(args.pdf_render_dir.resolve())
    require(len(docx_pngs) == word_pages, "DOCX_RENDER_PAGE_COUNT_MISMATCH")
    require(len(pdf_pngs) == pdf_pages, "PDF_RENDER_PAGE_COUNT_MISMATCH")
    require(args.visual_inspection_pass, "VISUAL_INSPECTION_NOT_CONFIRMED")
    result = {
        "schema": SCHEMA,
        "status": "PASS",
        "visual_inspection_pass": True,
        "word_com_export": True,
        "opened_read_only": True,
        "docx_sha256": sha256_file(docx),
        "pdf_sha256": sha256_file(pdf),
        "pages": pdf_pages,
        "docx_render_pages": len(docx_pngs),
        "pdf_render_pages": len(pdf_pngs),
        "word_verification_sha256": sha256_file(args.word_verification.resolve()),
        "docx_render_hashes": [{"file": path.name, "sha256": sha256_file(path)} for path in docx_pngs],
        "pdf_render_hashes": [{"file": path.name, "sha256": sha256_file(path)} for path in pdf_pngs],
    }
    atomic_json(args.output.resolve(), result)
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Build the completion-v2 report QA receipt")
    result.add_argument("--docx", type=Path, required=True)
    result.add_argument("--pdf", type=Path, required=True)
    result.add_argument("--word-verification", type=Path, required=True)
    result.add_argument("--docx-render-dir", type=Path, required=True)
    result.add_argument("--pdf-render-dir", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--visual-inspection-pass", action="store_true")
    return result


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = build_receipt(args)
    except ReceiptError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "FAILED", "error_code": str(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps({"schema": SCHEMA, "status": result["status"], "pages": result["pages"], "docx_sha256": result["docx_sha256"], "pdf_sha256": result["pdf_sha256"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
