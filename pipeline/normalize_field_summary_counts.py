#!/usr/bin/env python3
# EN: Local-only normalize field summary counts utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
"""Bind first-level field summary counts/categories to deterministic shard metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-root", required=True)
    args = parser.parse_args()
    case_root = Path(args.case_root).resolve()
    source_dir = case_root / "derived" / "normalized" / "field_shards"
    summary_dir = case_root / "derived" / "llm" / "field_shard_summaries"
    changed = 0
    total_records = 0
    for source in sorted(path for path in source_dir.glob("*.json") if path.name != "manifest.json"):
        summary = summary_dir / f"{source.stem}.summary.json"
        if not summary.exists():
            continue
        source_payload = json.loads(source.read_text(encoding="utf-8"))
        payload = json.loads(summary.read_text(encoding="utf-8"))
        payload["field_category"] = source_payload["field_category"]
        payload["input_record_count"] = source_payload["input_record_count"]
        summary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        total_records += int(source_payload["input_record_count"])
        changed += 1
    print(json.dumps({"normalized_files": changed, "input_record_count_sum": total_records}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
