#!/usr/bin/env python3
# EN: Read completed OCR artifacts and original invoice tables; do not invent missing fields.
# 中文：读取已完成 OCR 与原始发票表格，不推断缺失字段。
"""Export existing per-page PI OCR and extract PI tables locally.

Only count/status metadata is printed. Customer text is written to the target
directory and never emitted on stdout/stderr.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping
from collections import defaultdict

import pdfplumber


SCHEMA = "okki.pi.ocr_table_extract.v1"
HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")
MONEY = re.compile(r"(?<![A-Za-z0-9])[-+]?\(?\d[\d, ]*(?:\.\d{1,4})?\)?")
DATE_PATTERNS = [
    re.compile(r"(?i)\bdate\s*[:#-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"),
    re.compile(r"(?i)\bdate\s*[:#-]?\s*(\d{4}[./-]\d{1,2}[./-]\d{1,2})"),
    re.compile(r"(?i)\bdate\s*[:#-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})"),
]
PI_NO = re.compile(
    r"(?i)(?:pro\s*[- ]?forma\s+invoice|p\s*/\s*i|\bpi\b)\s*"
    r"(?:no\.?|number|#|:)?\s*([A-Z0-9][A-Z0-9._/-]{2,48})"
)
CURRENCY_CODES = ("USD", "EUR", "GBP", "CNY", "RMB", "AED", "AUD", "CAD", "JPY", "SAR", "ZAR", "NGN")
TOTAL_PATTERNS = {
    "subtotal": re.compile(r"(?i)\bsub\s*total\b[^\d]{0,20}([\d,]+(?:\.\d{1,4})?)"),
    "discount": re.compile(r"(?i)\bdiscount\b[^\d]{0,20}([\d,]+(?:\.\d{1,4})?)"),
    "freight": re.compile(r"(?i)\b(?:freight|shipping)\b[^\d]{0,20}([\d,]+(?:\.\d{1,4})?)"),
    "tax": re.compile(r"(?i)\b(?:tax|vat)\b[^\d]{0,20}([\d,]+(?:\.\d{1,4})?)"),
    "total": re.compile(r"(?i)\b(?:grand\s+total|total\s+amount|invoice\s+total|total)\b[^\d]{0,20}([\d,]+(?:\.\d{1,4})?)"),
}


class ExtractError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ExtractError(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def read_json(path: Path, code: str) -> dict[str, Any]:
    require(path.is_file(), f"{code}_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ExtractError(f"{code}_INVALID") from exc
    require(isinstance(value, dict), f"{code}_INVALID")
    return value


def connect_ro(path: Path) -> sqlite3.Connection:
    require(path.is_file(), "STATE_DB_MISSING")
    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def within(root: Path, path: Path, code: str) -> Path:
    root = root.resolve()
    path = path.resolve()
    require(path != root and root in path.parents, code)
    return path


def parse_number(value: Any) -> float | None:
    raw = str(value or "").strip().replace(" ", "")
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    raw = re.sub(r"[^0-9,.-]", "", raw).replace(",", "")
    if not raw or raw in {"-", ".", "-."}:
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    return -number if negative else number


def compact(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())


def currency_from(text: str) -> str:
    upper = text.upper()
    for code in CURRENCY_CODES:
        if re.search(rf"\b{re.escape(code)}\b", upper):
            return "CNY" if code == "RMB" else code
    if "€" in text:
        return "EUR"
    if "£" in text:
        return "GBP"
    if "¥" in text or "￥" in text:
        return "CNY"
    if "$" in text:
        return "USD"
    return ""


def first_match(patterns: Iterable[re.Pattern[str]], text: str) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return compact(match.group(1))
    return ""


def amount_match(pattern: re.Pattern[str], text: str, *, last: bool = False) -> float | None:
    values = [parse_number(match.group(1)) for match in pattern.finditer(text)]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return values[-1] if last else values[0]


def header_map(row: list[str]) -> dict[str, int]:
    output: dict[str, int] = {}
    for index, cell in enumerate(row):
        normalized = compact(cell).casefold()
        if not normalized:
            continue
        if re.search(r"\b(?:sku|item\s*(?:no|code)|product\s*(?:no|code)|part\s*(?:no|number))\b", normalized):
            output.setdefault("sku", index)
        if re.search(r"\b(?:description|product|item|goods)\b", normalized):
            output.setdefault("description", index)
        if re.search(r"\b(?:model|specification|spec)\b", normalized):
            output.setdefault("spec", index)
        if re.search(r"\b(?:quantity|qty)\b", normalized):
            output.setdefault("qty", index)
        if re.search(r"\b(?:unit|uom)\b", normalized) and "price" not in normalized:
            output.setdefault("unit", index)
        if re.search(r"\b(?:unit\s*price|price)\b", normalized):
            output.setdefault("unit_price", index)
        if re.search(r"\b(?:amount|line\s*total|total\s*price)\b", normalized):
            output.setdefault("amount", index)
    return output


def cell(row: list[str], mapping: Mapping[str, int], key: str) -> str:
    index = mapping.get(key)
    return compact(row[index]) if index is not None and index < len(row) else ""


def table_items(table: list[list[Any]], page: int, table_index: int) -> list[dict[str, Any]]:
    rows = [[compact(cell_value) for cell_value in (row or [])] for row in table if row]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    header_at = -1
    mapping: dict[str, int] = {}
    for index, row in enumerate(rows[:8]):
        candidate = header_map(row)
        if len(candidate) >= 2 and ({"qty", "unit_price", "amount"} & set(candidate)):
            header_at = index
            mapping = candidate
            break
    output: list[dict[str, Any]] = []
    if header_at < 0:
        return output
    for row_index, row in enumerate(rows[header_at + 1 :], header_at + 2):
        joined = " ".join(row)
        if re.search(r"(?i)\b(?:sub\s*total|grand\s+total|payment\s+terms?|bank\s+details?)\b", joined):
            continue
        qty = parse_number(cell(row, mapping, "qty"))
        unit_price = parse_number(cell(row, mapping, "unit_price"))
        amount = parse_number(cell(row, mapping, "amount"))
        description = cell(row, mapping, "description")
        sku = cell(row, mapping, "sku")
        spec = cell(row, mapping, "spec")
        if not any((description, sku, spec, qty is not None, unit_price is not None, amount is not None)):
            continue
        output.append({
            "page": page,
            "table_index": table_index,
            "source_row": row_index,
            "sku": sku,
            "description": description,
            "spec": spec,
            "quantity": qty,
            "unit": cell(row, mapping, "unit"),
            "unit_price": unit_price,
            "line_amount": amount,
            "raw_cells": row,
            "extraction_method": "PDF_NATIVE_TABLE",
            "review_status": "PASS" if description and (qty is not None or amount is not None) else "NEEDS_REVIEW",
        })
    return output


def process_pdf(job: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(str(job["path"]))
    items: list[dict[str, Any]] = []
    table_count = 0
    native_text: list[str] = []
    error = ""
    try:
        with pdfplumber.open(path) as pdf:
            page_count = len(pdf.pages)
            for page_no, page in enumerate(pdf.pages, 1):
                native_text.append(page.extract_text() or "")
                tables = page.extract_tables() or []
                table_count += len(tables)
                for table_index, table in enumerate(tables, 1):
                    items.extend(table_items(table, page_no, table_index))
    except Exception:
        page_count = int(job.get("page_count") or 0)
        error = "PDF_TABLE_EXTRACTION_FAILED"
    return {"items": items, "table_count": table_count, "native_text": "\n".join(native_text), "page_count": page_count, "error": error}


def load_ocr_output(case_root: Path, output_rel: str, task_kind: str) -> tuple[list[dict[str, Any]], str]:
    output = within(case_root, case_root / output_rel, "OCR_OUTPUT_PATH_ESCAPE")
    require(output.exists(), "OCR_OUTPUT_MISSING")
    pages: list[dict[str, Any]] = []
    if task_kind == "ocr_pdf_all_pages":
        index = read_json(output / "index.json", "OCR_INDEX")
        raw_pages = index.get("pages")
        require(isinstance(raw_pages, list) and raw_pages, "OCR_INDEX_PAGES_INVALID")
        for entry in raw_pages:
            require(isinstance(entry, dict), "OCR_INDEX_PAGE_INVALID")
            text_rel = str(entry.get("text_rel") or "")
            text_path = within(output, output / text_rel, "OCR_TEXT_PATH_ESCAPE")
            text = text_path.read_text(encoding="utf-8-sig", errors="replace")
            pages.append({
                "page": int(entry.get("page") or len(pages) + 1),
                "text": text,
                "text_chars": int(entry.get("text_chars") or len(text)),
                "mean_confidence": entry.get("mean_confidence"),
                "blank": bool(entry.get("blank")),
            })
        return pages, str(index.get("engine") or "Tesseract completion_v2")

    json_files = sorted(output.rglob("*.json"))
    txt_files = sorted(output.rglob("*.txt"))
    require(bool(txt_files), "IMAGE_OCR_TEXT_MISSING")
    text_path = max(txt_files, key=lambda path: path.stat().st_size)
    text = text_path.read_text(encoding="utf-8-sig", errors="replace")
    meta: dict[str, Any] = {}
    for candidate in json_files:
        try:
            value = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(value, dict) and ("mean_confidence" in value or "text_chars" in value):
            meta = value
            break
    pages.append({
        "page": 1,
        "text": text,
        "text_chars": int(meta.get("text_chars") or len(text)),
        "mean_confidence": meta.get("mean_confidence"),
        "blank": bool(meta.get("blank")),
    })
    return pages, str(meta.get("engine") or "Tesseract completion_v2")


# EN: Build case-local derived artifacts from validated inputs.
# 中文：从校验后的输入构建案例内派生成果。
def build(args: argparse.Namespace) -> dict[str, Any]:
    case_root = args.case_root.resolve()
    pi_root = args.pi_root.resolve()
    require(case_root.is_dir() and pi_root.is_dir() and case_root in pi_root.parents, "ROOT_INVALID")
    candidate_path = args.archive_root.resolve() / "00_manifest" / "candidate_classification.csv"
    require(candidate_path.is_file(), "CANDIDATE_CSV_MISSING")
    with candidate_path.open("r", encoding="utf-8-sig", newline="") as stream:
        candidates = list(csv.DictReader(stream))
    require(len(candidates) == args.expected_files, "PI_COUNT_INVALID")
    pi_files = {path.name: path for path in pi_root.iterdir() if path.is_file() and path.suffix.casefold() in {".pdf", ".png"}}
    require(len(pi_files) == args.expected_files, "PI_FILES_INVALID")

    state_path = args.completion_root.resolve() / "full_extract_v2" / "state.sqlite3"
    db = connect_ro(state_path)
    try:
        task_map: dict[str, dict[str, Any]] = defaultdict(dict)
        for row in db.execute(
            "SELECT content_sha256,task_kind,state,output_rel,error_code FROM tasks "
            "WHERE active=1 AND task_kind IN ('ocr_pdf_all_pages','ocr_image')"
        ):
            task_map[str(row["content_sha256"]).upper()][str(row["task_kind"])] = dict(row)
    finally:
        db.close()

    ocr_dir = pi_root / "OCR"
    ocr_dir.mkdir(exist_ok=True)
    headers: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    ocr_by_sha: dict[str, dict[str, Any]] = {}

    for ordinal, row in enumerate(candidates, 1):
        digest = str(row.get("SHA256") or "").upper()
        require(HEX64.fullmatch(digest) is not None, "PI_SHA_INVALID")
        source_basename = Path(str(row.get("归档文件") or "")).name
        source = pi_files.get(source_basename)
        require(source is not None and sha256_file(source) == digest, "PI_SOURCE_HASH_MISMATCH")
        pi_id = f"PI-{ordinal:04d}"
        task_kind = "ocr_pdf_all_pages" if source.suffix.casefold() == ".pdf" else "ocr_image"
        task = task_map.get(digest, {}).get(task_kind)
        require(isinstance(task, dict) and task.get("state") == "completed" and not task.get("error_code"), "OCR_TASK_NOT_COMPLETE")
        pages, engine = load_ocr_output(case_root, str(task["output_rel"]), task_kind)
        require(pages and all(not page["blank"] and compact(page["text"]) for page in pages), "OCR_PAGE_EMPTY")
        combined = "\n\n".join(f"===== PAGE {page['page']} =====\n{page['text']}" for page in pages)
        ocr_rel = f"OCR/{pi_id}_{digest[:12]}.ocr.txt"
        atomic_write(pi_root / ocr_rel, combined.encode("utf-8"))
        confidences = [float(page["mean_confidence"]) for page in pages if page.get("mean_confidence") is not None]
        average_confidence = sum(confidences) / len(confidences) if confidences else None
        ocr_by_sha[digest] = {"pi_id": pi_id, "pages": pages, "combined": combined, "ocr_rel": ocr_rel, "engine": engine}
        ledger.append({
            "pi_id": pi_id,
            "source_sha256": digest,
            "file_type": source.suffix.casefold().lstrip(".").upper(),
            "total_pages": len(pages),
            "success_pages": len(pages),
            "failed_pages": 0,
            "ocr_engine": engine,
            "ocr_status": "PASS",
            "table_extraction_status": "PENDING",
            "line_item_count": 0,
            "average_confidence": average_confidence,
            "error_code": "",
            "review_status": "PASS",
            "ocr_text_rel": ocr_rel,
        })
        jobs.append({"sha": digest, "path": str(source), "page_count": len(pages)})

    result_by_sha: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ProcessPoolExecutor(max_workers=max(1, min(args.workers, os.cpu_count() or 1))) as executor:
        futures = {executor.submit(process_pdf, job): job for job in jobs if str(job["path"]).casefold().endswith(".pdf")}
        for future, job in futures.items():
            result_by_sha[str(job["sha"])] = future.result()
    for job in jobs:
        if str(job["path"]).casefold().endswith(".png"):
            result_by_sha[str(job["sha"])] = {"items": [], "table_count": 0, "native_text": "", "page_count": 1, "error": ""}

    line_items: list[dict[str, Any]] = []
    for ordinal, row in enumerate(candidates, 1):
        digest = str(row["SHA256"]).upper()
        ocr = ocr_by_sha[digest]
        result = result_by_sha[digest]
        full_text = compact(str(result.get("native_text") or "") + "\n" + ocr["combined"])
        pi_number = first_match([PI_NO], full_text)
        pi_date = first_match(DATE_PATTERNS, full_text)
        currency = currency_from(full_text)
        values = {
            key: amount_match(pattern, full_text, last=(key == "total"))
            for key, pattern in TOTAL_PATTERNS.items()
        }
        items = list(result.get("items") or [])
        for item_index, item in enumerate(items, 1):
            line_items.append({
                "pi_id": ocr["pi_id"],
                "source_sha256": digest,
                "page": item.get("page"),
                "line_number": item_index,
                "table_index": item.get("table_index"),
                "source_row": item.get("source_row"),
                "sku": item.get("sku") or "",
                "description": item.get("description") or "",
                "spec": item.get("spec") or "",
                "quantity": item.get("quantity"),
                "unit": item.get("unit") or "",
                "unit_price": item.get("unit_price"),
                "currency": currency,
                "line_amount": item.get("line_amount"),
                "ocr_confidence": next((page.get("mean_confidence") for page in ocr["pages"] if page["page"] == item.get("page")), None),
                "extraction_method": item.get("extraction_method") or "",
                "review_status": item.get("review_status") or "NEEDS_REVIEW",
                "raw_cells": item.get("raw_cells") or [],
            })
        table_status = "PASS" if items else "NO_TABLE_NEEDS_REVIEW"
        headers.append({
            "pi_id": ocr["pi_id"],
            "source_sha256": digest,
            "classification": row.get("分类") or "",
            "pi_number": pi_number,
            "pi_date_raw": pi_date,
            "currency": currency,
            "line_item_count": len(items),
            "subtotal": values["subtotal"],
            "discount": values["discount"],
            "freight": values["freight"],
            "tax": values["tax"],
            "other_charges": None,
            "pi_total": values["total"],
            "table_count": int(result.get("table_count") or 0),
            "ocr_status": "PASS",
            "table_extraction_status": table_status,
            "error_code": result.get("error") or "",
            "review_status": "PASS" if items and pi_number else "NEEDS_REVIEW",
            "ocr_text_rel": ocr["ocr_rel"],
        })
        ledger_row = next(item for item in ledger if item["source_sha256"] == digest)
        ledger_row["table_extraction_status"] = table_status
        ledger_row["line_item_count"] = len(items)
        if result.get("error"):
            ledger_row["error_code"] = result["error"]
            ledger_row["review_status"] = "NEEDS_REVIEW"

    require(len(headers) == len(ledger) == args.expected_files, "OUTPUT_COVERAGE_INVALID")
    require({row["source_sha256"] for row in headers} == {row["source_sha256"] for row in ledger}, "OUTPUT_SHA_SET_MISMATCH")
    require(sum(row["total_pages"] for row in ledger) == args.expected_pages, "OCR_PAGE_COUNT_INVALID")
    require(all(row["success_pages"] == row["total_pages"] and row["failed_pages"] == 0 for row in ledger), "OCR_COVERAGE_FAILED")

    atomic_json(pi_root / "pi_headers.json", {"schema": SCHEMA + ".headers", "rows": headers})
    atomic_json(pi_root / "pi_line_items.json", {"schema": SCHEMA + ".line_items", "rows": line_items})
    atomic_json(pi_root / "ocr_ledger.json", {"schema": SCHEMA + ".ocr_ledger", "rows": ledger})
    verification = {
        "schema": SCHEMA + ".verification",
        "status": "PASS",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "counts": {
            "pi_files": len(ledger),
            "pdf_files": sum(1 for row in ledger if row["file_type"] == "PDF"),
            "png_files": sum(1 for row in ledger if row["file_type"] == "PNG"),
            "ocr_pages": sum(row["total_pages"] for row in ledger),
            "ocr_pass_files": sum(1 for row in ledger if row["ocr_status"] == "PASS"),
            "header_rows": len(headers),
            "line_item_rows": len(line_items),
            "table_pass_files": sum(1 for row in headers if row["table_extraction_status"] == "PASS"),
            "table_review_files": sum(1 for row in headers if row["table_extraction_status"] != "PASS"),
        },
        "gates": {
            "PI_SHA_COVERAGE_PASS": True,
            "OCR_EXPECTED_FILES_PASS": True,
            "OCR_EXPECTED_PAGES_PASS": True,
            "TABLE_EXTRACTION_TERMINAL_PASS": True,
            "LOCAL_ONLY_PASS": True,
        },
    }
    atomic_json(pi_root / "ocr_table_verification.json", verification)
    return verification


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--pi-root", type=Path, required=True)
    parser.add_argument("--completion-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=16)
    # EN: Obtain expected counts from the user's current archive inventory.
    # 中文：期望数量应来自用户当前归档台账，不能使用历史客户数量。
    parser.add_argument("--expected-files", type=int, required=True)
    parser.add_argument("--expected-pages", type=int, required=True)
    return parser


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    try:
        result = build(parser().parse_args())
    except ExtractError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "FAILED", "error_code": str(exc)}, separators=(",", ":")))
        return 2
    except Exception:
        print(json.dumps({"schema": SCHEMA, "status": "FAILED", "error_code": "UNEXPECTED_ERROR"}, separators=(",", ":")))
        return 3
    print(json.dumps({"schema": SCHEMA, "status": result["status"], "counts": result["counts"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
