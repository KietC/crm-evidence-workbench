# EN: Verify capture metadata, count closure and evidence references without printing source content.
# 中文：验证采集元数据、数量闭合与证据引用，不打印源正文。
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_AI_HOSTS = {
    "api.openai.com",
    "chatgpt.com",
    "openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.mistral.ai",
    "api.cohere.com",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text_hidden_safe(path: Path, text: str) -> None:
    """Atomically replace an existing Windows Hidden file and preserve its attributes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    attributes: int | None = None
    if os.name == "nt" and path.exists():
        value = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if value != 0xFFFFFFFF:
            attributes = int(value)
    temporary.write_text(text, encoding="utf-8")
    try:
        if path.exists():
            try:
                path.unlink()
            except PermissionError:
                if os.name != "nt" or attributes is None:
                    raise
                writable = attributes & ~0x1 & ~0x2 & ~0x4
                if not ctypes.windll.kernel32.SetFileAttributesW(str(path), writable):
                    raise
                path.unlink()
        os.replace(temporary, path)
        if os.name == "nt" and attributes is not None:
            if not ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes):
                raise OSError("unable to restore Windows file attributes")
    finally:
        temporary.unlink(missing_ok=True)


def filesystem_path(path: Path) -> Path:
    """Use Win32 extended paths so valid evidence names over MAX_PATH remain readable."""
    if os.name != "nt":
        return path
    raw = str(path.resolve(strict=False))
    if raw.startswith("\\\\?\\"):
        return Path(raw)
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSONL {path}:{line_no}: {exc}") from exc
    return rows


def verify_reference(
    case_root: Path,
    rel: object,
    expected: object,
    expected_size: object,
    verified: dict[str, tuple[int, str]],
    label: str,
) -> str | None:
    if not isinstance(rel, str) or not rel or not isinstance(expected, str) or not expected:
        return f"{label} missing path/hash"
    target = filesystem_path(case_root / rel)
    if not target.exists():
        return f"missing {label}: {rel}"
    cache_key = str(target).casefold()
    cached = verified.get(cache_key)
    if cached is None:
        cached = (target.stat().st_size, sha256_file(target))
        verified[cache_key] = cached
    actual_size, actual_hash = cached
    if isinstance(expected_size, int) and actual_size != expected_size:
        return f"size mismatch {label}: {rel}"
    if actual_hash.lower() != expected.lower():
        return f"hash mismatch {label}: {rel}"
    return None


def verify_objects(case_root: Path, verified: dict[str, tuple[int, str]]) -> tuple[int, list[str]]:
    rows = read_jsonl(case_root / "manifests" / "objects.jsonl")
    errors: list[str] = []
    for row in rows:
        rel = row.get("relative_path")
        expected = row.get("sha256")
        error = verify_reference(case_root, rel, expected, row.get("size"), verified, "object")
        if error:
            errors.append(error)
    return len(rows), errors


def verify_indexes(case_root: Path, verified: dict[str, tuple[int, str]]) -> tuple[dict[str, int], list[str]]:
    specs = {
        "sha256_index": case_root / "manifests" / "sha256_index.jsonl",
        "source_url_index": case_root / "manifests" / "source_url_index.jsonl",
    }
    counts: dict[str, int] = {}
    errors: list[str] = []
    for label, index_path in specs.items():
        rows = read_jsonl(index_path)
        counts[label] = len(rows)
        for row_no, row in enumerate(rows, 1):
            digest = row.get("sha256")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
                errors.append(f"invalid digest {label}:{row_no}")
                continue
            if label == "source_url_index":
                source_key = row.get("source_url_sha256")
                if not isinstance(source_key, str) or re.fullmatch(r"[0-9a-fA-F]{64}", source_key) is None:
                    errors.append(f"invalid source key {label}:{row_no}")
                    continue
            error = verify_reference(
                case_root,
                row.get("relative_path"),
                digest,
                row.get("size"),
                verified,
                f"{label} row {row_no}",
            )
            if error:
                errors.append(error)
    return counts, errors


def verify_network(case_root: Path) -> tuple[int, list[str]]:
    rows = read_jsonl(case_root / "request_ledger.jsonl")
    errors: list[str] = []
    for row in rows:
        raw_url = row.get("url") or ""
        try:
            host = (urlsplit(raw_url).hostname or "").lower()
        except ValueError:
            host = ""
        if host in FORBIDDEN_AI_HOSTS or any(host.endswith(f".{x}") for x in FORBIDDEN_AI_HOSTS):
            errors.append(f"external AI request observed at sequence {row.get('sequence')}: {host}")
    return len(rows), errors


def verify_source_scan(project_root: Path) -> list[str]:
    errors: list[str] = []
    pattern = re.compile(
        r"(?:api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis\.com|api\.mistral\.ai|api\.cohere\.com)",
        re.I,
    )
    allow_literal_paths = {
        "config/scope.json",
        "verify/verify_case.py",
        "capture/cdp_resource.mjs",
        "app/adapters/okki/v1/adapter.json",
        "app/src/tests/adapter.test.ts",
    }
    ignored_parts = {"node_modules", "dist", "runtime", "__pycache__"}
    for file_path in project_root.rglob("*"):
        relative = file_path.relative_to(project_root)
        if any(part in ignored_parts for part in relative.parts):
            continue
        if file_path.suffix.lower() not in {".py", ".mjs", ".js", ".ts", ".json", ".md"}:
            continue
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if pattern.search(text) and relative.as_posix() not in allow_literal_paths and file_path.name != "README.md":
            errors.append(f"external AI endpoint literal in source: {file_path}")
    return errors


def resolve_company_id(case_root: Path) -> str:
    for relative, key in [
        ("case_identity.json", "company_id"),
        ("verification/capture_reconciliation_latest.json", "company_id"),
        ("case.json", "root_company_id"),
    ]:
        path = case_root / relative
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get(key)
            if value is not None and str(value).isdigit():
                return str(value)
        except (OSError, ValueError, AttributeError):
            pass
    legacy = case_root.name.removeprefix("company_")
    return legacy if legacy.isdigit() else "unknown"


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-root")
    parser.add_argument("--company-id")
    source_mode = parser.add_mutually_exclusive_group()
    source_mode.add_argument("--skip-source-scan", action="store_true")
    source_mode.add_argument("--source-scan-only", action="store_true")
    args = parser.parse_args()
    if args.source_scan_only:
        errors = verify_source_scan(PROJECT_ROOT)
        result = {
            "schema": 1,
            "status": "passed" if not errors else "failed",
            "source_scan_only": True,
            "errors": errors,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not errors else 1
    if not args.case_root:
        parser.error("--case-root is required unless --source-scan-only is used")
    case_root = Path(args.case_root).resolve()
    company_id = args.company_id or resolve_company_id(case_root)
    if not case_root.exists():
        print(json.dumps({"status": "failed", "error": "case root missing"}, ensure_ascii=False))
        return 2
    verified_files: dict[str, tuple[int, str]] = {}
    object_count, object_errors = verify_objects(case_root, verified_files)
    index_counts, index_errors = verify_indexes(case_root, verified_files)
    request_count, network_errors = verify_network(case_root)
    source_errors = [] if args.skip_source_scan else verify_source_scan(PROJECT_ROOT)
    errors = object_errors + index_errors + network_errors + source_errors
    result = {
        "schema": 1,
        "company_id": company_id,
        "status": "passed" if not errors else "failed",
        "objects_checked": object_count,
        "unique_files_hashed": len(verified_files),
        "hash_index_entries_checked": index_counts["sha256_index"],
        "source_url_index_entries_checked": index_counts["source_url_index"],
        "requests_checked": request_count,
        "external_ai_uploads_observed": len(network_errors),
        "errors": errors,
    }
    output = case_root / "verification" / "verify_case_latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    write_text_hidden_safe(output, json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
