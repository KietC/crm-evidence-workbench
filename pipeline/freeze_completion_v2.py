#!/usr/bin/env python3
# EN: Freeze control metadata and hashes before a successor processing run.
# 中文：在继承处理开始前冻结控制元数据和哈希。
"""Freeze metadata and hashes for the single-customer completion-v2 run.

The command never opens customer-body databases.  It hashes selected authority
files, copies only small control manifests, and publishes a resumable run
descriptor below the case ``work`` directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "okki.single_customer.completion_v2.freeze.v1"
COPY_LIMIT = 64 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_delivery(case_root: Path) -> Path:
    base = case_root / "deliveries" / "full_unredacted_local"
    candidates = [item for item in base.glob("final_*") if item.is_dir()]
    if not candidates:
        raise RuntimeError("FINAL_DELIVERY_NOT_FOUND")
    return max(candidates, key=lambda item: (item.stat().st_mtime_ns, item.name))


def authority_files(case_root: Path, delivery: Path) -> list[tuple[str, Path]]:
    values = [
        ("case_identity", case_root / "case_identity.json"),
        ("processing_scope_v1", case_root / "manifests" / "processing_scope_latest.json"),
        ("mail_capture_manifest", case_root / "manifests" / "mail_capture_manifest_latest.json"),
        ("capture_reconciliation", case_root / "verification" / "capture_reconciliation_latest.json"),
        ("full_extract_verification", case_root / "derived" / "full_extract" / "verification.json"),
        ("full_extract_state", case_root / "derived" / "full_extract" / "state.sqlite3"),
        ("full_text_v1", case_root / "derived" / "full_extract" / "full_text.sqlite3"),
        ("relationship_graph_v1", case_root / "derived" / "full_extract" / "relationship_graph.jsonl"),
        ("delivery_manifest_v1", delivery / "DELIVERY_MANIFEST.json"),
        ("delivery_checksums_v1", delivery / "DELIVERY_CHECKSUMS.sha256"),
        ("delivery_verification_v1", delivery / "verification.json"),
        ("delivery_database_v1", delivery / "customer_full_unredacted.sqlite3"),
        ("delivery_graph_v1", delivery / "relations.graphml.gz"),
    ]
    missing = [label for label, path in values if not path.is_file()]
    if missing:
        raise RuntimeError("AUTHORITY_FILE_MISSING:" + ",".join(missing))
    return values


# EN: Bind selected control files by bytes and SHA before creating a run descriptor.
# 中文：按字节与 SHA 绑定控制文件后创建运行描述。
def freeze(case_root: Path, company_id: str, run_id: str | None, workers: int) -> dict[str, Any]:
    identity = json.loads((case_root / "case_identity.json").read_text(encoding="utf-8"))
    if str(identity.get("company_id") or "") != company_id:
        raise RuntimeError("CASE_IDENTITY_MISMATCH")
    if not run_id:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not run_id.replace("_", "").replace("-", "").replace("T", "").replace("Z", "").isalnum():
        raise RuntimeError("RUN_ID_INVALID")
    run_root = case_root / "work" / f"completion_v2_{run_id}"
    if run_root.exists():
        raise RuntimeError("RUN_ROOT_EXISTS")
    for name in ("baseline", "state", "derived", "exports", "logs", "qa"):
        (run_root / name).mkdir(parents=True, exist_ok=False)

    delivery = latest_delivery(case_root)
    entries = authority_files(case_root, delivery)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        hashes = list(pool.map(lambda item: sha256_file(item[1]), entries))

    rows: list[dict[str, Any]] = []
    for (label, path), digest in zip(entries, hashes, strict=True):
        stat = path.stat()
        copied_rel = None
        if stat.st_size <= COPY_LIMIT:
            target = run_root / "baseline" / f"{label}{path.suffix}"
            shutil.copy2(path, target)
            copied_rel = target.relative_to(run_root).as_posix()
        rows.append(
            {
                "label": label,
                "path": str(path.resolve()),
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": digest,
                "copied_rel": copied_rel,
            }
        )

    csv_path = run_root / "baseline" / "source_hashes.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["label", "path", "bytes", "mtime_ns", "sha256", "copied_rel"])
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "schema": SCHEMA,
        "status": "FROZEN",
        "generated_at": utc_now(),
        "company_id": company_id,
        "run_id": run_id,
        "run_root": str(run_root.resolve()),
        "previous_delivery": str(delivery.resolve()),
        "authority_files": rows,
        "counts": {"files": len(rows), "bytes": sum(int(row["bytes"]) for row in rows), "copied": sum(bool(row["copied_rel"]) for row in rows)},
    }
    atomic_json(run_root / "baseline" / "freeze_manifest.json", payload)
    pointer = {
        "schema": "okki.single_customer.completion_v2.current.v1",
        "company_id": company_id,
        "run_id": run_id,
        "run_root": str(run_root.resolve()),
        "status": "FROZEN",
        "updated_at": utc_now(),
    }
    atomic_json(case_root / "work" / "completion_v2_current.json", pointer)
    return {"status": "FROZEN", "run_id": run_id, "files": len(rows), "bytes": payload["counts"]["bytes"]}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Freeze completion-v2 authority metadata")
    result.add_argument("--case-root", required=True, type=Path)
    result.add_argument("--company-id", required=True)
    result.add_argument("--run-id")
    result.add_argument("--workers", type=int, default=8)
    return result


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    args = parser().parse_args()
    try:
        result = freeze(args.case_root.resolve(), args.company_id, args.run_id, args.workers)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error_code": str(exc).splitlines()[0][:160]}, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
