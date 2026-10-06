#!/usr/bin/env python3
# EN: Local-only assemble field profile utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
"""Assemble the ten fully reduced field summaries into the final profile schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    case_root = Path(args.case_root).resolve()
    final_dir = case_root / "derived" / "llm" / "field_reduction" / "final"
    fields = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in final_dir.glob("*.json")}
    required = {
        "identity_general_facts", "customer_type_roles", "buying_stage_timeline", "needs_pain_focus",
        "habits_preferences_objections", "products_specifications", "transactions_operations",
        "visual_and_ocr", "conflicts_missing_paths", "customs_identity",
    }
    missing = sorted(required - fields.keys())
    if missing:
        raise RuntimeError(f"missing reduced fields: {missing}")
    customs_report = json.loads((case_root / "derived" / "llm" / "customs_identity_report.json").read_text(encoding="utf-8"))
    reduction = json.loads((case_root / "derived" / "llm" / "field_reduction" / "reduction_manifest.json").read_text(encoding="utf-8"))
    profile = {
        "identity": fields["identity_general_facts"],
        "customer_type": fields["customer_type_roles"],
        "stakeholder_roles": fields["customer_type_roles"],
        "buying_stage": fields["buying_stage_timeline"],
        "focus_points": fields["needs_pain_focus"],
        "procurement_habits": fields["habits_preferences_objections"],
        "needs": fields["needs_pain_focus"],
        "pain_points": fields["needs_pain_focus"],
        "products": fields["products_specifications"],
        "specifications": fields["products_specifications"],
        "preferences": fields["habits_preferences_objections"],
        "objections": fields["habits_preferences_objections"],
        "timeline": fields["buying_stage_timeline"],
        "crm_counts": fields["transactions_operations"],
        "document_facts": fields["products_specifications"],
        "visual_inferences": fields["visual_and_ocr"],
        "customs_identity_status": customs_report.get("overall_status", "unconfirmed"),
        "confirmed_customs_facts": customs_report.get("confirmed_customer_customs_facts", []),
        "conflicts": fields["conflicts_missing_paths"],
        "missing_fields": {
            "field_summary": fields["conflicts_missing_paths"].get("missing", []),
            "customs": customs_report.get("missing_identity_fields", []),
        },
        "evidence_paths": {
            category: payload.get("evidence_paths", []) for category, payload in fields.items()
        },
        "field_reduction_audit": reduction,
        "fields": fields,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"fields": len(fields), "input_records": reduction["input_records"], "reconciled": reduction["reconciled"], "customs_identity_status": profile["customs_identity_status"]}, separators=(",", ":")))
    return 0 if reduction["reconciled"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
