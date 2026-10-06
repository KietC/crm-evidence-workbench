#!/usr/bin/env python3
# EN: Local-only build lossless field final utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
"""Deterministically union every field summary without semantic top-N loss."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


LIST_FIELDS = ("observed", "inferred", "conflicts", "missing", "evidence_paths")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-root", required=True)
    args = parser.parse_args()
    case_root = Path(args.case_root).resolve()
    shard_manifest = json.loads(
        (case_root / "derived" / "normalized" / "field_shards" / "manifest.json").read_text(encoding="utf-8")
    )
    source_dir = case_root / "derived" / "llm" / "field_shard_summaries"
    reduce_root = case_root / "derived" / "llm" / "field_reduction"
    final_dir = reduce_root / "final"
    final_dir.mkdir(parents=True, exist_ok=True)

    categories = sorted(shard_manifest["field_categories"])
    category_audit = {}
    total_records = 0
    total_sources = 0
    for category in categories:
        sources = sorted(source_dir.glob(f"{category}_*.summary.json"))
        expected_sources = sum(1 for item in shard_manifest["shards"] if item["field_category"] == category)
        if len(sources) != expected_sources:
            raise RuntimeError(f"{category}: expected {expected_sources} summaries, found {len(sources)}")
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in sources]
        record_count = sum(int(item.get("input_record_count") or 0) for item in payloads)
        total_records += record_count
        total_sources += len(sources)
        merged = {
            "field_category": category,
            "input_record_count": record_count,
            "source_summary_paths": [str(path.relative_to(case_root)) for path in sources],
        }
        provenance = {}
        input_items = {}
        output_items = {}
        duplicates = {}
        for field in LIST_FIELDS:
            seen = {}
            ordered = []
            input_count = 0
            for path, payload in zip(sources, payloads):
                values = payload.get(field) or []
                if not isinstance(values, list):
                    raise RuntimeError(f"{path.name}: {field} is not a list")
                input_count += len(values)
                relative = str(path.relative_to(case_root))
                for value in values:
                    encoded = canonical(value)
                    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                    if digest not in seen:
                        seen[digest] = value
                        ordered.append(value)
                        provenance[digest] = {"field": field, "source_summary_paths": [relative]}
                    elif relative not in provenance[digest]["source_summary_paths"]:
                        provenance[digest]["source_summary_paths"].append(relative)
            merged[field] = ordered
            input_items[field] = input_count
            output_items[field] = len(ordered)
            duplicates[field] = input_count - len(ordered)
        merged["item_provenance"] = provenance
        merged["lossless_audit"] = {
            "input_items": input_items,
            "output_unique_items": output_items,
            "exact_duplicates_removed": duplicates,
            "reconciled": all(input_items[k] == output_items[k] + duplicates[k] for k in LIST_FIELDS),
        }
        target = final_dir / f"{category}.json"
        target.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        category_audit[category] = {
            "relative_path": str(target.relative_to(case_root)),
            "input_record_count": record_count,
            "source_summary_count": len(sources),
            **merged["lossless_audit"],
        }

    result = {
        "mode": "deterministic_lossless_exact_union",
        "rounds": 0,
        "model_calls": 0,
        "categories": category_audit,
        "input_summary_files": total_sources,
        "input_records": int(shard_manifest["input_records"]),
        "final_record_count_sum": total_records,
        "reconciled": (
            total_sources == len(shard_manifest["shards"])
            and total_records == int(shard_manifest["input_records"])
            and all(item["reconciled"] for item in category_audit.values())
        ),
    }
    (reduce_root / "reduction_manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "categories": len(category_audit),
        "input_summary_files": total_sources,
        "input_records": result["input_records"],
        "final_record_count_sum": total_records,
        "reconciled": result["reconciled"],
    }, separators=(",", ":")))
    return 0 if result["reconciled"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
