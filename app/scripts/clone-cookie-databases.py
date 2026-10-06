from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


COOKIE_DATABASES = (
    Path("Network") / "Cookies",
    Path("Partitions") / "okki-crm-capture" / "Network" / "Cookies",
)


def backup_database(source: Path, target: Path) -> bool:
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=15) as source_db:
        with sqlite3.connect(target, timeout=15) as target_db:
            source_db.backup(target_db, pages=256, sleep=0.05)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Consistently clone Chromium cookie SQLite databases without reading rows.")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()

    copied = 0
    for relative in COOKIE_DATABASES:
        copied += int(backup_database(args.source / relative, args.target / relative))
    print(f"cookie_databases_backed_up={copied}")
    return 0 if copied else 2


if __name__ == "__main__":
    raise SystemExit(main())
