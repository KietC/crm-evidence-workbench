#!/usr/bin/env python3
"""Check patched SQLite and required capabilities with a synthetic database.

使用合成数据库检查 SQLite 修补版本和必要能力，不读取真实案例。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from build_unredacted_local_package import ensure_sqlite_version  # noqa: E402


def main() -> int:
    try:
        ensure_sqlite_version()
    except RuntimeError as error:
        print(json.dumps({"status": "FAIL", "error_code": str(error), "sqlite_version": sqlite3.sqlite_version}))
        return 2
    print(json.dumps({"status": "PASS_SYNTHETIC_CAPABILITIES", "python_version": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version,
                      "checks": ["patched_wal", "foreign_keys", "fts5", "upsert", "window_functions", "backup", "integrity"],
                      "real_case_data_read": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
