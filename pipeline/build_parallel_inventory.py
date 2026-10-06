#!/usr/bin/env python3
# EN: Local-only build parallel inventory utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
"""Build a local-only hash inventory and searchable evidence index in parallel."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sqlite3


TEXT_EXTENSIONS = {
    ".css", ".csv", ".htm", ".html", ".js", ".json", ".jsonl",
    ".md", ".mjs", ".text", ".txt", ".xml",
}


def inspect_file(item: tuple[Path, Path]) -> dict:
    root, path = item
    rel = path.relative_to(root).as_posix()
    digest = hashlib.sha256()
    with path.open("rb", buffering=1024 * 1024) as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    stat = path.stat()
    return {
        "root": str(root),
        "relative_path": rel,
        "absolute_path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest().upper(),
        "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "extension": path.suffix.lower(),
    }


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", required=True)
    parser.add_argument("--jsonl", required=True)
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--workers", type=int, default=max(4, min(128, (os.cpu_count() or 8) - 8)))
    parser.add_argument("--text-max-bytes", type=int, default=2 * 1024 * 1024)
    args = parser.parse_args()

    roots = [Path(value).resolve() for value in args.root]
    jsonl_path = Path(args.jsonl).resolve()
    sqlite_path = Path(args.sqlite).resolve()
    output_paths = {
        jsonl_path,
        sqlite_path,
        Path(str(sqlite_path) + "-wal"),
        Path(str(sqlite_path) + "-shm"),
    }
    files = sorted(
        (root, path)
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and path.resolve() not in output_paths
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(inspect_file, files, chunksize=16))
    rows.sort(key=lambda row: (row["root"].lower(), row["relative_path"].lower()))

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    if sqlite_path.exists():
        sqlite_path.unlink()
    db = sqlite3.connect(sqlite_path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, root TEXT, relative_path TEXT, "
            "absolute_path TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT, mime TEXT, extension TEXT)"
        )
        db.execute("CREATE UNIQUE INDEX files_path ON files(root, relative_path)")
        db.execute("CREATE INDEX files_sha256 ON files(sha256)")
        db.execute("CREATE VIRTUAL TABLE text_fts USING fts5(relative_path, content, tokenize='unicode61')")
        for row in rows:
            cursor = db.execute(
                "INSERT INTO files(root, relative_path, absolute_path, size, mtime_ns, sha256, mime, extension) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(row[key] for key in (
                    "root", "relative_path", "absolute_path", "size", "mtime_ns", "sha256", "mime", "extension"
                )),
            )
            rel_lower = row["relative_path"].lower()
            if (
                row["extension"] in TEXT_EXTENSIONS
                and row["size"] <= args.text_max_bytes
                and "/static/" not in f"/{rel_lower}"
            ):
                content = Path(row["absolute_path"]).read_text(encoding="utf-8", errors="replace")
                db.execute("INSERT INTO text_fts(rowid, relative_path, content) VALUES (?, ?, ?)",
                           (cursor.lastrowid, row["relative_path"], content))
        db.commit()
        text_rows = db.execute("SELECT count(*) FROM text_fts").fetchone()[0]
    finally:
        db.close()

    print(json.dumps({
        "files": len(rows),
        "bytes": sum(row["size"] for row in rows),
        "workers": args.workers,
        "text_rows": text_rows,
        "jsonl": str(jsonl_path),
        "sqlite": str(sqlite_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
