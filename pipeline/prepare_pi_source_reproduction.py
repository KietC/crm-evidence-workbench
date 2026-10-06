#!/usr/bin/env python3
# EN: Prepare original invoice grids and page images, retaining native values and source bindings.
# 中文：准备原发票网格和页图，保留原值及来源绑定。
"""Prepare a local-only PI table/page manifest for exact Excel reproduction.

The script never prints PI text or file names. Native PDF table cells are kept
verbatim. OCR is used only to fill otherwise empty, geometrically bounded cells.
Pages without a reliable table grid are represented only by their source image.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

import pdfplumber
from PIL import Image


SCHEMA = "okki.pi.source_table_reproduction.v1"
HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")
MODEL_HEADER = re.compile(r"(?i)(?:\bmodel\b|\bspec(?:ification)?s?\b|型号|规格型号)")


class ReproductionError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ReproductionError(code)


def read_json(path: Path, code: str) -> dict[str, Any]:
    require(path.is_file(), f"{code}_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ReproductionError(f"{code}_INVALID") from exc
    require(isinstance(value, dict), f"{code}_INVALID")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with temp.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    result = str(value).replace("\x00", "")
    require(len(result) <= 32767, "CELL_TEXT_EXCEEDS_EXCEL_LIMIT")
    return result


def convert_page_image(source: Path, target: Path, *, max_width: int, quality: int) -> tuple[int, int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.stem}.{os.getpid()}.tmp.jpg")
    with Image.open(source) as image:
        image.load()
        if image.mode not in {"RGB", "L"}:
            base = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                base.paste(image, mask=image.getchannel("A"))
            else:
                base.paste(image.convert("RGB"))
            image = base
        elif image.mode == "L":
            image = image.convert("RGB")
        else:
            image = image.copy()
        if image.width > max_width:
            height = max(1, round(image.height * max_width / image.width))
            image = image.resize((max_width, height), Image.Resampling.LANCZOS)
        image.save(temp, format="JPEG", quality=quality, optimize=True, progressive=True)
        width, height = image.size
    os.replace(temp, target)
    return width, height, sha256_file(target)


def crop_page_region(
    source: Path,
    target: Path,
    bbox: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
    *,
    max_width: int = 520,
    quality: int = 90,
) -> tuple[int, int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.stem}.{os.getpid()}.tmp.jpg")
    with Image.open(source) as image:
        image.load()
        x0 = max(0, int(round(bbox[0] * image.width / page_width)))
        y0 = max(0, int(round(bbox[1] * image.height / page_height)))
        x1 = min(image.width, int(round(bbox[2] * image.width / page_width)))
        y1 = min(image.height, int(round(bbox[3] * image.height / page_height)))
        require(x1 > x0 and y1 > y0, "CELL_IMAGE_BBOX_INVALID")
        crop = image.crop((x0, y0, x1, y1)).convert("RGB")
        if crop.width > max_width:
            height = max(1, round(crop.height * max_width / crop.width))
            crop = crop.resize((max_width, height), Image.Resampling.LANCZOS)
        crop.save(temp, format="JPEG", quality=quality, optimize=True, progressive=True)
        width, height = crop.size
    os.replace(temp, target)
    return width, height, sha256_file(target)


def intersection_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def rebuild_ocr_text(boxes: Iterable[dict[str, Any]]) -> str:
    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for box in boxes:
        key = (int(box.get("block") or 0), int(box.get("paragraph") or 0), int(box.get("line") or 0))
        grouped.setdefault(key, []).append(box)
    lines: list[tuple[float, float, str]] = []
    for line_boxes in grouped.values():
        line_boxes.sort(key=lambda box: (float(box.get("left") or 0), float(box.get("top") or 0)))
        words = [safe_text(box.get("text")).strip() for box in line_boxes]
        words = [word for word in words if word]
        if words:
            lines.append((min(float(box.get("top") or 0) for box in line_boxes), min(float(box.get("left") or 0) for box in line_boxes), " ".join(words)))
    lines.sort(key=lambda item: (item[0], item[1]))
    return "\n".join(item[2] for item in lines)


def table_matrix_with_strict_ocr(table: Any, boxes: list[dict[str, Any]], sx: float, sy: float) -> tuple[list[list[str]], int]:
    native = table.extract() or []
    rows = [[safe_text(cell) for cell in (row or [])] for row in native]
    width = max((len(row) for row in rows), default=0)
    for row in rows:
        row.extend([""] * (width - len(row)))
    if not rows or width == 0:
        return rows, 0

    # Exact-reproduction mode: only native PDF cell text is written to Excel.
    # OCR remains a verification artifact and is never promoted into a cell,
    # because even geometrically bounded OCR can introduce characters that were
    # not printed as table text (especially inside product-picture cells).
    return rows, 0

    grid_cells: list[tuple[int, int, tuple[float, float, float, float]]] = []
    for row_index, row_obj in enumerate(table.rows):
        for col_index, bbox in enumerate(row_obj.cells):
            if bbox is not None and row_index < len(rows) and col_index < width:
                grid_cells.append((row_index, col_index, tuple(float(value) for value in bbox)))

    assigned: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for box in boxes:
        text = safe_text(box.get("text")).strip()
        confidence = float(box.get("confidence") or -1)
        if not text or confidence < 35:
            continue
        ocr_bbox = (
            float(box.get("left") or 0) * sx,
            float(box.get("top") or 0) * sy,
            (float(box.get("left") or 0) + float(box.get("width") or 0)) * sx,
            (float(box.get("top") or 0) + float(box.get("height") or 0)) * sy,
        )
        cx = (ocr_bbox[0] + ocr_bbox[2]) / 2
        cy = (ocr_bbox[1] + ocr_bbox[3]) / 2
        center_hits = [
            (max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])), row_index, col_index, bbox)
            for row_index, col_index, bbox in grid_cells
            if bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]
        ]
        if center_hits:
            _, row_index, col_index, _ = min(center_hits, key=lambda item: (item[0], item[1], item[2]))
        else:
            overlap_hits = [
                (intersection_area(ocr_bbox, bbox), row_index, col_index)
                for row_index, col_index, bbox in grid_cells
            ]
            overlap, row_index, col_index = max(overlap_hits, default=(0.0, -1, -1), key=lambda item: item[0])
            if overlap <= 0:
                continue
        assigned.setdefault((row_index, col_index), []).append(box)

    supplemented = 0
    for (row_index, col_index), cell_boxes in assigned.items():
        if not rows[row_index][col_index].strip():
            recovered = rebuild_ocr_text(cell_boxes).strip()
            if recovered:
                rows[row_index][col_index] = recovered
                supplemented += 1
    return rows, supplemented


def table_model_stats(matrix: list[list[str]]) -> tuple[int, int]:
    headers = 0
    populated = 0
    for row_index, row in enumerate(matrix[:8]):
        for col_index, value in enumerate(row):
            if MODEL_HEADER.search(value or ""):
                headers += 1
                populated += sum(1 for later in matrix[row_index + 1 :] if col_index < len(later) and later[col_index].strip())
    return headers, populated


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--max-image-width", type=int, default=1400)
    parser.add_argument("--jpeg-quality", type=int, default=86)
    parser.add_argument("--expected-files", type=int, required=True)
    parser.add_argument("--expected-pages", type=int, required=True)
    args = parser.parse_args()

    archive_root = args.archive_root.resolve()
    case_root = args.case_root.resolve()
    pi_root = archive_root / "PI"
    candidate_path = archive_root / "00_manifest" / "candidate_classification.csv"
    require(pi_root.is_dir(), "PI_ROOT_MISSING")
    require(candidate_path.is_file(), "CANDIDATE_CSV_MISSING")
    require(args.state_db.is_file(), "STATE_DB_MISSING")

    with candidate_path.open("r", encoding="utf-8-sig", newline="") as stream:
        candidates = list(csv.DictReader(stream))
    require(len(candidates) == args.expected_files, "PI_COUNT_INVALID")
    pi_files = {path.name: path for path in pi_root.iterdir() if path.is_file() and path.suffix.casefold() in {".pdf", ".png"}}
    require(len(pi_files) == args.expected_files, "PI_FILES_INVALID")

    db = sqlite3.connect(f"file:{args.state_db.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tasks: dict[tuple[str, str], dict[str, Any]] = {}
        for row in db.execute("SELECT content_sha256,task_kind,state,output_rel,error_code FROM tasks WHERE active=1 AND task_kind IN ('ocr_pdf_all_pages','ocr_image')"):
            tasks[(str(row["content_sha256"]).upper(), str(row["task_kind"]))] = dict(row)
    finally:
        db.close()

    assets_root = pi_root / "PI_Reproduction_assets"
    pages_root = assets_root / "pages"
    cells_root = assets_root / "cells"
    sheets: list[dict[str, Any]] = []
    total_pages = 0
    total_tables = 0
    total_supplemented = 0
    model_headers = 0
    model_values = 0
    image_only_pages = 0
    full_page_embeds = 0
    cell_image_count = 0

    for ordinal, candidate in enumerate(candidates, 1):
        digest = str(candidate.get("SHA256") or "").upper()
        require(HEX64.fullmatch(digest) is not None, "PI_SHA_INVALID")
        source = pi_files.get(Path(str(candidate.get("归档文件") or "")).name)
        require(source is not None and sha256_file(source) == digest, "PI_SOURCE_HASH_MISMATCH")
        pi_id = f"PI-{ordinal:04d}"
        sheet_name = f"PI{ordinal:03d}"
        task_kind = "ocr_pdf_all_pages" if source.suffix.casefold() == ".pdf" else "ocr_image"
        task = tasks.get((digest, task_kind))
        require(task is not None and task.get("state") == "completed" and not task.get("error_code"), "OCR_TASK_NOT_COMPLETE")
        task_output = (case_root / str(task["output_rel"])).resolve()
        require(case_root == task_output or case_root in task_output.parents, "OCR_OUTPUT_PATH_ESCAPE")
        pi_pages: list[dict[str, Any]] = []

        if source.suffix.casefold() == ".pdf":
            index = read_json(task_output / "index.json", "OCR_INDEX")
            page_entries = sorted(index.get("pages") or [], key=lambda row: int(row.get("page") or 0))
            with pdfplumber.open(source) as pdf:
                require(len(pdf.pages) == len(page_entries), "PDF_PAGE_COUNT_MISMATCH")
                for page_index, (page, entry) in enumerate(zip(pdf.pages, page_entries, strict=True), 1):
                    source_image = task_output / str(entry.get("image_rel") or "")
                    boxes_doc = read_json(task_output / str(entry.get("boxes_rel") or ""), "OCR_BOXES")
                    require(source_image.is_file(), "OCR_PAGE_IMAGE_MISSING")
                    boxes = boxes_doc.get("boxes") or []
                    require(isinstance(boxes, list), "OCR_BOXES_INVALID")
                    target_image = pages_root / f"{pi_id}_p{page_index:03d}.jpg"
                    width, height, image_sha = convert_page_image(source_image, target_image, max_width=args.max_image_width, quality=args.jpeg_quality)
                    sx = float(page.width) / float(width if width else 1)
                    sy = float(page.height) / float(height if height else 1)
                    # OCR boxes use the original rendered image size, not the compressed JPEG size.
                    with Image.open(source_image) as original_render:
                        sx = float(page.width) / float(original_render.width)
                        sy = float(page.height) / float(original_render.height)
                    tables: list[dict[str, Any]] = []
                    page_image_objects = []
                    full_page_background = False
                    for image_object in page.images:
                        image_bbox = (
                            float(image_object.get("x0") or 0),
                            float(image_object.get("top") or 0),
                            float(image_object.get("x1") or 0),
                            float(image_object.get("bottom") or 0),
                        )
                        image_area = max(0.0, image_bbox[2] - image_bbox[0]) * max(0.0, image_bbox[3] - image_bbox[1])
                        if image_area >= float(page.width) * float(page.height) * 0.8:
                            full_page_background = True
                        else:
                            page_image_objects.append(image_bbox)
                    for table_index, table in enumerate(page.find_tables(), 1):
                        matrix, supplemented = table_matrix_with_strict_ocr(table, boxes, sx, sy)
                        if not matrix or not any(any(cell.strip() for cell in row) for row in matrix):
                            continue
                        rows = len(matrix)
                        columns = max((len(row) for row in matrix), default=0)
                        h_count, v_count = table_model_stats(matrix)
                        model_headers += h_count
                        model_values += v_count
                        total_supplemented += supplemented
                        cell_images: list[dict[str, Any]] = []
                        seen_cells: set[tuple[int, int]] = set()
                        for row_index, row_obj in enumerate(table.rows):
                            for col_index, cell_bbox_raw in enumerate(row_obj.cells):
                                if cell_bbox_raw is None or (row_index, col_index) in seen_cells:
                                    continue
                                cell_bbox = tuple(float(value) for value in cell_bbox_raw)
                                matched = False
                                for image_bbox in page_image_objects:
                                    image_area = max(1.0, (image_bbox[2] - image_bbox[0]) * (image_bbox[3] - image_bbox[1]))
                                    cx = (image_bbox[0] + image_bbox[2]) / 2
                                    cy = (image_bbox[1] + image_bbox[3]) / 2
                                    if (
                                        cell_bbox[0] <= cx <= cell_bbox[2]
                                        and cell_bbox[1] <= cy <= cell_bbox[3]
                                    ) or intersection_area(cell_bbox, image_bbox) / image_area >= 0.25:
                                        matched = True
                                        break
                                if not matched:
                                    continue
                                seen_cells.add((row_index, col_index))
                                cell_target = cells_root / f"{pi_id}_p{page_index:03d}_t{table_index:02d}_r{row_index + 1:03d}_c{col_index + 1:03d}.jpg"
                                cell_width, cell_height, cell_sha = crop_page_region(
                                    source_image,
                                    cell_target,
                                    cell_bbox,
                                    float(page.width),
                                    float(page.height),
                                )
                                cell_images.append({
                                    "row": row_index,
                                    "column": col_index,
                                    "image_rel": cell_target.relative_to(pi_root).as_posix(),
                                    "image_sha256": cell_sha,
                                    "image_width": cell_width,
                                    "image_height": cell_height,
                                })
                                cell_image_count += 1
                        tables.append({
                            "table_index": table_index,
                            "bbox": [round(float(value), 4) for value in table.bbox],
                            "rows": rows,
                            "columns": columns,
                            "matrix": matrix,
                            "ocr_supplemented_cells": supplemented,
                            "cell_images": cell_images,
                        })
                    if not tables:
                        image_only_pages += 1
                    embed_page_image = bool(not tables or full_page_background)
                    if embed_page_image:
                        full_page_embeds += 1
                    total_tables += len(tables)
                    total_pages += 1
                    pi_pages.append({
                        "page": page_index,
                        "image_rel": target_image.relative_to(pi_root).as_posix(),
                        "image_sha256": image_sha,
                        "image_width": width,
                        "image_height": height,
                        "embed_page_image": embed_page_image,
                        "tables": tables,
                    })
        else:
            target_image = pages_root / f"{pi_id}_p001.jpg"
            width, height, image_sha = convert_page_image(source, target_image, max_width=args.max_image_width, quality=args.jpeg_quality)
            total_pages += 1
            image_only_pages += 1
            full_page_embeds += 1
            pi_pages.append({
                "page": 1,
                "image_rel": target_image.relative_to(pi_root).as_posix(),
                "image_sha256": image_sha,
                "image_width": width,
                "image_height": height,
                "embed_page_image": True,
                "tables": [],
            })

        sheets.append({
            "pi_id": pi_id,
            "sheet_name": sheet_name,
            "source_sha256": digest,
            "source_type": source.suffix.casefold().lstrip(".").upper(),
            "pages": pi_pages,
        })

    require(len(sheets) == args.expected_files, "SHEET_COUNT_INVALID")
    require(total_pages == args.expected_pages, "PAGE_COUNT_INVALID")
    manifest = {
        "schema": SCHEMA,
        "counts": {
            "pi_files": len(sheets),
            "pages": total_pages,
            "tables": total_tables,
            "image_only_pages": image_only_pages,
            "ocr_supplemented_cells": total_supplemented,
            "model_header_cells": model_headers,
            "model_value_cells": model_values,
            "cell_images": cell_image_count,
            "full_page_embeds": full_page_embeds,
        },
        "rules": {
            "native_cells_preserved": True,
            "ocr_promoted_to_cells": False,
            "pages_without_reliable_grid_are_image_only": True,
            "no_normalized_or_inferred_columns": True,
        },
        "sheets": sheets,
    }
    atomic_json(assets_root / "pi_reproduction_manifest.json", manifest)
    print(json.dumps({"status": "PASS", **manifest["counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
