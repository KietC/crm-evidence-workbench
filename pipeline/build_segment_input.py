#!/usr/bin/env python3
# EN: Local-only build segment input utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
"""Build a bounded, evidence-backed input for ten-way customer segmentation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


FIELDS = ("identity_general_facts", "customer_type_roles", "transactions_operations")


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    case_root = Path(args.case_root).resolve()
    final_dir = case_root / "derived" / "llm" / "field_reduction" / "final"
    fields = {}
    for name in FIELDS:
        path = final_dir / f"{name}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields[name] = {
            "field_category": raw["field_category"],
            "input_record_count": raw["input_record_count"],
            "observed": raw.get("observed", []),
            "inferred": raw.get("inferred", []),
            "conflicts": raw.get("conflicts", []),
            "missing": raw.get("missing", []),
        }
    payload = {
        "selection_scope": "single closest content-matrix segment",
        "customer_evidence_fields": fields,
        "customs_identity_rule": "candidate customs companies are not customer facts unless explicitly confirmed",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({"fields": len(fields), "bytes": output.stat().st_size}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
