#!/usr/bin/env python3
# EN: Local-only build field shards utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
"""Losslessly route every derived evidence item into bounded field shards."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def category_for(section: str, source_kind: str) -> str:
    key = section.lower()
    if source_kind in {"vlm", "image_ocr"}:
        return "visual_and_ocr"
    if source_kind == "customs":
        return "customs_identity"
    if any(token in key for token in ("conflict", "missing", "zero", "evidence_path")):
        return "conflicts_missing_paths"
    if any(token in key for token in ("product", "specification", "certification", "commercial_term", "drawing", "material")):
        return "products_specifications"
    if any(token in key for token in ("procurement_habit", "preference", "objection")):
        return "habits_preferences_objections"
    if any(token in key for token in ("need", "pain", "focus", "tip")):
        return "needs_pain_focus"
    if any(token in key for token in ("timeline", "stage")):
        return "buying_stage_timeline"
    if any(token in key for token in ("customer_type", "stakeholder", "role", "signal")):
        return "customer_type_roles"
    if any(token in key for token in ("transaction", "opportun", "analytic", "document_metadata", "operation_history", "order")):
        return "transactions_operations"
    return "identity_general_facts"


def source_kind(path: Path) -> str:
    name = path.name.lower()
    if name.startswith("mail_"):
        return "mail"
    if name.startswith("root_api_") or name == "root_profile.json":
        return "root_api"
    if name.startswith("documents_"):
        return "documents"
    if name.startswith("vlm_"):
        return "vlm"
    if name.startswith("image_ocr_"):
        return "image_ocr"
    if "customs_identity" in name:
        return "customs"
    return "derived"


def explode_top_level(path: Path, case_root: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    kind = source_kind(path)
    relative_path = str(path.relative_to(case_root))
    records = []
    if not isinstance(payload, dict):
        payload = {"value": payload}
    for section, value in payload.items():
        values = value if isinstance(value, list) else [value]
        if not values:
            records.append({
                "source_kind": kind, "source_summary_relative_path": relative_path,
                "section": section, "item_index": None, "value": [],
            })
        for index, item in enumerate(values):
            records.append({
                "source_kind": kind,
                "source_summary_relative_path": relative_path,
                "section": section,
                "item_index": index if isinstance(value, list) else None,
                "value": item,
            })
    return records


def pack_records(records, max_chars):
    packs, current, chars = [], [], 0
    for record in records:
        size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        if current and chars + size > max_chars:
            packs.append(current)
            current, chars = [], 0
        current.append(record)
        chars += size
    if current:
        packs.append(current)
    return packs


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-chars", type=int, default=24000)
    args = parser.parse_args()
    case_root = Path(args.case_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = []
    summary_dir = case_root / "derived" / "llm" / "chunk_summaries"
    sources.extend(sorted(summary_dir.glob("mail_???.summary.json")))
    sources.extend(sorted(summary_dir.glob("root_api_???.summary.json")))
    sources.append(summary_dir / "documents_001.summary.json")
    sources.extend(sorted(summary_dir.glob("vlm_???.summary.json")))
    sources.append(summary_dir / "image_ocr_001.summary.json")
    sources.append(case_root / "derived" / "llm" / "customs_identity_report.json")
    sources = [path for path in sources if path.exists()]

    by_category = {}
    source_counts = {}
    for path in sources:
        records = explode_top_level(path, case_root)
        source_counts[str(path.relative_to(case_root))] = len(records)
        for record in records:
            category = category_for(record["section"], record["source_kind"])
            by_category.setdefault(category, []).append(record)

    manifest = []
    for category in sorted(by_category):
        records = by_category[category]
        for index, pack in enumerate(pack_records(records, args.max_chars), 1):
            payload = {
                "field_category": category,
                "shard_index": index,
                "input_record_count": len(pack),
                "records": pack,
            }
            path = output_dir / f"{category}_{index:03d}.json"
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            path.write_bytes(data)
            manifest.append({
                "field_category": category, "shard_index": index,
                "input_record_count": len(pack), "relative_path": str(path.relative_to(case_root)),
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            })
    result = {
        "method": "lossless_top_level_field_routing",
        "source_files": len(sources),
        "source_record_counts": source_counts,
        "input_records": sum(source_counts.values()),
        "field_categories": {key: len(value) for key, value in by_category.items()},
        "shards": manifest,
        "shard_records": sum(item["input_record_count"] for item in manifest),
    }
    (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "source_files": result["source_files"], "input_records": result["input_records"],
        "shard_records": result["shard_records"], "categories": len(result["field_categories"]),
        "shards": len(result["shards"]), "max_bytes": max(item["bytes"] for item in manifest),
    }, separators=(",", ":")))
    return 0 if result["input_records"] == result["shard_records"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
