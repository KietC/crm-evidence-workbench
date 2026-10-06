#!/usr/bin/env python3
"""Run portable source checks and synthetic Python tests without CRM access.

运行可移植源码检查及合成 Python 测试，不访问 CRM 或真实案例。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from privacy_scan import scan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--output", type=Path, help="optional content-free receipt outside source tree")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    receipt = {"schema": "evidence_trail.synthetic_checks.v1", "status": "PASS", "real_capture_started": False, "checks": []}
    source_audit = scan(root)
    if source_audit["findings"]:
        print(json.dumps({"status": "FAIL", "findings": source_audit["findings"]}))
        return 2
    for row in source_audit["files"]:
        path = root / row["path"]
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=row["path"])
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8-sig"))
        if path.suffix == ".md":
            # Check relative links outside fenced examples; do not fetch external URLs.
            # 检查代码块外相对链接，不访问外部网址。
            body = re.sub(r"```[\s\S]*?```", "", path.read_text(encoding="utf-8"))
            for target in re.findall(r"\]\(([^)]+)\)", body):
                target = target.split("#")[0].split()[0].strip("<>") if target.split("#")[0].strip() else ""
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not (path.parent / target).is_file():
                    raise RuntimeError(f"DOCUMENT_LINK_MISSING: {row['path']}")
    receipt["checks"].append({"name": "source_privacy_syntax_links", "status": "PASS", "files": source_audit["selected_files"]})
    if not args.skip_tests:
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "OKKI_DUCKDB_PYTHON": sys.executable}
        for directory in ("pipeline", "verify", "scripts/tests"):
            result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", directory, "-p", "test_*.py"], cwd=root, env=environment, check=False)
            receipt["checks"].append({"name": f"synthetic_{directory}", "exit_code": result.returncode})
            if result.returncode:
                receipt["status"] = "FAIL"
                break
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
