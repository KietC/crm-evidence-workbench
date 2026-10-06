#!/usr/bin/env python3
# EN: Local-only normalize object ledger utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: normalize_object_ledger.py CASE_ROOT")
    case_root = Path(sys.argv[1])
    ledger = case_root / "manifests" / "objects.jsonl"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    history = ledger.with_name(f"objects_history_{timestamp}.jsonl")
    superseded_path = ledger.with_name(f"superseded_objects_{timestamp}.jsonl")
    audit_path = case_root / "logs" / "ledger_normalization.jsonl"

    raw = ledger.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    last_position = {}
    for position, row in enumerate(rows):
        last_position[row["relative_path"]] = position
    kept = [row for position, row in enumerate(rows) if last_position[row["relative_path"]] == position]
    superseded = [row for position, row in enumerate(rows) if last_position[row["relative_path"]] != position]

    for row in kept:
        target = case_root / Path(row["relative_path"])
        if not target.is_file():
            raise RuntimeError(f"missing current object: {row['relative_path']}")
        if sha256_file(target).lower() != str(row["sha256"]).lower():
            raise RuntimeError(f"last ledger row does not match current object: {row['relative_path']}")

    shutil.copy2(ledger, history)
    superseded_path.write_text(
        "".join(json.dumps({**row, "superseded_reason": "repeated_label_path_overwrite"}, ensure_ascii=False) + "\n" for row in superseded),
        encoding="utf-8",
    )
    normalized = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in kept).encode("utf-8")
    temp = ledger.with_suffix(".jsonl.tmp")
    temp.write_bytes(normalized)
    os.replace(temp, ledger)
    audit = {
        "at": datetime.now(timezone.utc).isoformat(),
        "action": "normalize_object_ledger_last_write_wins_with_history",
        "input_rows": len(rows),
        "kept_rows": len(kept),
        "superseded_rows": len(superseded),
        "history_relative_path": str(history.relative_to(case_root)),
        "history_sha256": sha256_bytes(raw),
        "superseded_relative_path": str(superseded_path.relative_to(case_root)),
        "superseded_sha256": sha256_file(superseded_path),
        "normalized_sha256": sha256_file(ledger),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(audit, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps({"input_rows": len(rows), "kept_rows": len(kept), "superseded_rows": len(superseded)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
