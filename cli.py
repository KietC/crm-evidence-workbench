#!/usr/bin/env python3
"""Reusable post-processing entry point for a single-customer capture case.

Browser collection is owned by the independent local app under ``app/``.
This CLI owns deterministic local post-processing and verification; it never
sends case data to an external AI endpoint.

可复用的单客户本地后处理入口。浏览器采集由独立 app 负责；此入口
只负责确定性的本地处理与验证，不向外部 AI 接口发送案例资料。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT / "pipeline"
VERIFY = ROOT / "verify"


def run(script: Path, *args: object) -> None:
    """Forward explicit arguments and preserve the subprocess exit code.

    转发明确参数，并保留子进程退出码。
    """
    command = [sys.executable, str(script), *(str(arg) for arg in args)]
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def case_path(value: str) -> Path:
    """Resolve one local directory without traversing unrelated cases.

    解析一个本地目录，不遍历无关案例。
    """
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"case directory not found: {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Evidence Trail local CRM processing pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="verify scope, counts, hashes and privacy boundary")
    verify.add_argument("--case-root", type=case_path)
    verify.add_argument("--company-id")
    source_mode = verify.add_mutually_exclusive_group()
    source_mode.add_argument("--skip-source-scan", action="store_true")
    source_mode.add_argument("--source-scan-only", action="store_true")

    corpus = sub.add_parser("rebuild-corpus", help="rebuild normalized evidence corpus")
    corpus.add_argument("--case-root", required=True, type=case_path)

    inventory = sub.add_parser("index-raw", help="hash and FTS-index all raw evidence")
    inventory.add_argument("--case-root", required=True, type=case_path)
    inventory.add_argument("--workers", type=int, default=max(4, (os.cpu_count() or 8) - 4))

    prepare = sub.add_parser("prepare-fields", help="build lossless field-routed shards")
    prepare.add_argument("--case-root", required=True, type=case_path)
    prepare.add_argument("--max-chars", type=int, default=24000)

    normalize = sub.add_parser("normalize-fields", help="normalize model summary audit counts")
    normalize.add_argument("--case-root", required=True, type=case_path)

    reduce = sub.add_parser("reduce-fields", help="build deterministic lossless exact-union field finals")
    reduce.add_argument("--case-root", required=True, type=case_path)

    assemble = sub.add_parser("assemble-profile", help="assemble ten reduced fields into one profile")
    assemble.add_argument("--case-root", required=True, type=case_path)
    assemble.add_argument("--output")

    segment = sub.add_parser("prepare-segment", help="build bounded evidence input for segment selection")
    segment.add_argument("--case-root", required=True, type=case_path)
    segment.add_argument("--output")

    args = parser.parse_args()
    case = args.case_root
    if args.command == "verify":
        if args.source_scan_only:
            command = ["--source-scan-only"]
            run(VERIFY / "verify_case.py", *command)
            return 0
        if case is None:
            parser.error("verify requires --case-root unless --source-scan-only is used")
        command = ["--case-root", case]
        if args.company_id:
            command += ["--company-id", args.company_id]
        if args.skip_source_scan:
            command += ["--skip-source-scan"]
        run(VERIFY / "verify_case.py", *command)
    elif args.command == "rebuild-corpus":
        run(PIPELINE / "build_evidence_corpus.py", case)
    elif args.command == "index-raw":
        out = case / "verification"
        out.mkdir(parents=True, exist_ok=True)
        run(
            PIPELINE / "build_parallel_inventory.py",
            "--root", case / "raw",
            "--jsonl", out / "raw_inventory.jsonl",
            "--sqlite", out / "raw_inventory.sqlite",
            "--workers", args.workers,
        )
    elif args.command == "prepare-fields":
        run(
            PIPELINE / "build_field_shards.py",
            "--case-root", case,
            "--output-dir", case / "derived" / "normalized" / "field_shards",
            "--max-chars", args.max_chars,
        )
    elif args.command == "normalize-fields":
        run(PIPELINE / "normalize_field_summary_counts.py", "--case-root", case)
    elif args.command == "reduce-fields":
        run(PIPELINE / "build_lossless_field_final.py", "--case-root", case)
    elif args.command == "assemble-profile":
        output = Path(args.output).resolve() if args.output else case / "derived" / "llm" / "customer_profile.json"
        run(PIPELINE / "assemble_field_profile.py", "--case-root", case, "--output", output)
    elif args.command == "prepare-segment":
        output = Path(args.output).resolve() if args.output else case / "derived" / "normalized" / "segment_selection_input.json"
        run(PIPELINE / "build_segment_input.py", "--case-root", case, "--output", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
