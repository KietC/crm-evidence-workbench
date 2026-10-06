#!/usr/bin/env python3
"""Check selected public source without printing matched confidential values.

检查拟公开源码，不输出命中的机密值。此启发式工具不证明绝对隐私安全，
发布前仍需人工检查所选文件；真实案例目录不进入源码选择范围。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

IGNORED = {".git", "node_modules", "dist", ".venv", "venv", "__pycache__", ".pytest_cache"}
PRIVATE_DIRECTORIES = {"cases", "training_runs", "raw", "derived", "evidence", "runtime", "profile", "profiles", "logs", "deliveries", "state"}
SOURCE_SUFFIXES = {".py", ".ts", ".mjs", ".cjs", ".js", ".ps1", ".json", ".jsonc", ".md", ".txt", ".yml", ".yaml", ".html", ".css"}
SOURCE_NAMES = {"license", ".gitignore", ".gitattributes", ".env.example"}
RULES = {
    "PRIVATE_KEY": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GITHUB_TOKEN": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "AWS_KEY": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "SERVICE_KEY": re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{35,}\b"),
    "USER_HOME_PATH": re.compile(r"(?i)[A-Z]:\\+Users\\+[^\\\s'\"]+\\|/home/(?!example\b)[A-Za-z0-9_.-]+/|/Users/[A-Za-z0-9_.-]+/"),
    "SESSION_LITERAL": re.compile(r'(?i)["\'](?:session_cookie|refresh_token|access_token)["\']\s*:\s*["\'][A-Za-z0-9._-]{35,}["\']'),
}


def selected_files(root: Path, tracked: bool) -> tuple[list[Path], list[dict[str, object]]]:
    """Reject links and private selected files before any body is opened.

    在打开正文前拒绝链接及私密文件；只选择明确的源码类型。
    """
    findings: list[dict[str, object]] = []
    staged_links: set[str] = set()
    if tracked:
        completed = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=root, check=True, capture_output=True)
        candidates = []
        for entry in completed.stdout.split(b"\0"):
            if not entry:
                continue
            metadata, filename = entry.split(b"\t", 1)
            relative_name = os.fsdecode(filename)
            mode, _, stage = metadata.split(b" ", 2)
            if mode == b"120000" or stage != b"0":
                # Reject index links/conflicts even if the working tree was replaced.
                # 即使工作区已替换，也拒绝索引中的链接或未解决冲突。
                staged_links.add(relative_name)
            candidates.append(root / relative_name)
    else:
        candidates = []
        for directory, children, names in os.walk(root, followlinks=False):
            children[:] = [name for name in children if name.casefold() not in IGNORED and name.casefold() not in PRIVATE_DIRECTORIES]
            for child in children:
                node = Path(directory) / child
                if node.is_symlink() or node.is_junction():
                    findings.append({"path": node.relative_to(root).as_posix(), "rule": "SOURCE_LINK"})
            children[:] = [name for name in children if not (Path(directory) / name).is_symlink() and not (Path(directory) / name).is_junction()]
            candidates.extend(Path(directory) / name for name in names)
    selected = []
    for candidate in candidates:
        relative = candidate.relative_to(root)
        if relative.as_posix() in staged_links:
            findings.append({"path": relative.as_posix(), "rule": "SOURCE_INDEX_LINK_OR_CONFLICT"})
            continue
        if any(part.casefold() in IGNORED for part in relative.parts):
            if tracked:
                findings.append({"path": relative.as_posix(), "rule": "EXCLUDED_FILE_SELECTED"})
            continue
        if any(part.casefold() in PRIVATE_DIRECTORIES for part in relative.parts):
            findings.append({"path": relative.as_posix(), "rule": "PRIVATE_FILE_SELECTED"})
            continue
        ancestor = candidate
        linked = False
        while ancestor != root:
            if ancestor.is_symlink() or ancestor.is_junction():
                linked = True
                break
            ancestor = ancestor.parent
        if linked:
            findings.append({"path": relative.as_posix(), "rule": "SOURCE_LINK"})
        elif candidate.name.casefold().startswith(".env") and candidate.name.casefold() != ".env.example":
            findings.append({"path": relative.as_posix(), "rule": "ENV_FILE_SELECTED"})
        elif candidate.suffix.casefold() == ".json" and re.search(r"(?i)cookie|storage.?state|browser.?profile", candidate.name):
            findings.append({"path": relative.as_posix(), "rule": "SESSION_FILE_SELECTED"})
        elif candidate.suffix.casefold() not in SOURCE_SUFFIXES and candidate.name.casefold() not in SOURCE_NAMES:
            findings.append({"path": relative.as_posix(), "rule": "NON_SOURCE_FILE"})
        elif candidate.is_file() or tracked:
            selected.append(candidate)
    return sorted(selected), findings


def scan(root: Path, tracked: bool = False, deny_terms: list[str] | None = None) -> dict[str, object]:
    """Return paths, rule codes and hashes only, never the matching text.

    仅返回路径、规则码及哈希，不返回匹配正文。
    """
    files, findings = selected_files(root, tracked)
    manifest = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        # Audit the exact Git index bytes, not a cleaned working-tree replacement.
        # 检查 Git 索引中的真实待提交字节，而非被清理后的工作区替代文件。
        if tracked:
            completed = subprocess.run(["git", "show", f":{relative}"], cwd=root, capture_output=True, check=True)
            payload = completed.stdout
        else:
            payload = path.read_bytes()
        try:
            body = payload.decode("utf-8-sig")
        except UnicodeError:
            findings.append({"path": relative, "rule": "NON_UTF8_SOURCE"})
            continue
        for label, expression in RULES.items():
            for matched in expression.finditer(body):
                findings.append({"path": relative, "line": body.count("\n", 0, matched.start()) + 1, "rule": label})
        for index, term in enumerate(deny_terms or []):
            if term and term.casefold() in body.casefold():
                findings.append({"path": relative, "rule": "PRIVATE_DENY_TERM", "term_index": index})
        manifest.append({"path": relative, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    return {"schema": "evidence_trail.source_scan.v1", "status": "PASS_HEURISTIC_SOURCE_ONLY" if not findings else "FAIL",
            "selected_files": len(files), "findings": findings, "files": manifest,
            "real_case_directories_scanned": False, "manual_review_required": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect public source, not production data / 检查公开源码而非生产资料")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tracked", action="store_true", help="only inspect Git-selected files / 仅检查 Git 所选文件")
    parser.add_argument("--deny-file", type=Path, help="external JSON array of confidential literals; never commit it / 外部私密禁用词数组，不要提交")
    parser.add_argument("--output", type=Path, help="write a content-free receipt outside the repository / 回执放仓库外")
    args = parser.parse_args()
    terms = json.loads(args.deny_file.read_text(encoding="utf-8-sig")) if args.deny_file else []
    if not isinstance(terms, list) or not all(isinstance(value, str) for value in terms):
        parser.error("deny-file must be a JSON array of strings")
    result = scan(args.root.resolve(), args.tracked, terms)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "selected_files", "findings")}, ensure_ascii=False))
    return 0 if not result["findings"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
