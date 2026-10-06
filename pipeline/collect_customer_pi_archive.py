#!/usr/bin/env python3
# EN: Archive invoice originals while retaining all parent-mail occurrences and explicit count gates.
# 中文：归档发票原件，保留全部父邮件出现关系及显式数量门槛。
"""Collect one customer's PI originals from the frozen completion-v2 evidence.

The command is deliberately local-only and metadata-silent on stdout.  Human
content is written only inside the immutable delivery directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA = "okki.single_customer.pi_archive.v1"
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
STABLE_OBJECT_HOSTS = {
    "cdn.xiaoman.cn",
    "v4client-oss.xiaoman.cn",
    "v4client-oss-new.xiaoman.cn",
    "v4client.oss-cn-hangzhou.aliyuncs.com",
}
SECRET_QUERY_KEYS = {"signature", "token", "access_token", "authorization", "auth", "api_key"}
STRUCTURE_PATTERNS = {
    "买卖双方": re.compile(r"\b(?:seller|supplier|buyer|consignee|bill\s+to|ship\s+to|exporter|importer)\b", re.I),
    "编号或日期": re.compile(r"\b(?:pi|p\s*/\s*i|invoice)\s*(?:no\.?|number|#)|\bdate\b", re.I),
    "品名数量单价": re.compile(r"\b(?:description|item|model|product|quantity|qty|unit\s+price)\b", re.I),
    "金额币种": re.compile(r"\b(?:total|subtotal|amount|usd|eur|gbp|cny|rmb)\b|[$€£¥]", re.I),
    "付款交付条款": re.compile(r"\b(?:payment\s+terms?|delivery\s+terms?|incoterms?|fob|cif|exw|deposit)\b", re.I),
    "签章": re.compile(r"\b(?:signature|authorized|stamp|signed)\b", re.I),
}
PI_NUMBER = re.compile(
    r"(?i)(?:pro\s*[- ]?forma\s+invoice|p\s*/\s*i|\bpi\b)\s*"
    r"(?:no\.?|number|#|:)?\s*([A-Z0-9][A-Z0-9._/-]{2,48})"
)


class ArchiveError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ArchiveError(code)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, canonical(value))


def connect_ro(path: Path) -> sqlite3.Connection:
    require(path.is_file(), "DATABASE_MISSING")
    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def read_json(path: Path, code: str) -> dict[str, Any]:
    require(path.is_file(), f"{code}_MISSING")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"{code}_INVALID") from exc
    require(isinstance(value, dict), f"{code}_INVALID")
    return value


def safe_case_file(case_root: Path, relative: str) -> Path:
    root = case_root.resolve()
    path = (root / str(relative).replace("/", os.sep)).resolve()
    require(path != root and root in path.parents and path.is_file(), "SOURCE_FILE_INVALID")
    return path


def safe_name(value: str, fallback: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip() or fallback
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    stem, suffix = Path(name).stem, Path(name).suffix
    stem = stem[:100].rstrip(" .") or fallback
    suffix = suffix[:16]
    return stem + suffix


def source_url_identity_key(raw_url: str) -> str:
    text = str(raw_url or "").replace("&amp;", "&")
    try:
        parts = urlsplit(text)
        if parts.scheme and parts.netloc and (parts.hostname or "").casefold() in STABLE_OBJECT_HOSTS:
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            selected: list[tuple[str, str]] = []
            for key in ("x-oss-process", "versionId", "versionid"):
                matched = next((item for item in pairs if item[0] == key), None)
                if matched is not None:
                    selected.append(matched)
            canonical_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(selected), ""))
            return sha256_bytes(canonical_url.encode("utf-8"))
        if parts.scheme and parts.netloc:
            redacted = []
            for key, value in parse_qsl(parts.query, keep_blank_values=True):
                if key.casefold() in SECRET_QUERY_KEYS:
                    value = f"<redacted:sha256:{sha256_bytes(value.encode('utf-8'))[:16].lower()}>"
                redacted.append((key, value))
            safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), parts.fragment))
            return sha256_bytes(safe_url.encode("utf-8"))
    except Exception:
        pass
    return sha256_bytes(text.encode("utf-8"))


def clean_text(value: Any) -> str:
    raw = html.unescape(str(value or ""))
    raw = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(raw.split())


def evidence_excerpt(text: str) -> str:
    match = re.search(r"pro\s*[- ]?forma", text, flags=re.I)
    if not match:
        match = re.search(r"\bp\s*/\s*i\b|\bpi\b", text, flags=re.I)
    if not match:
        return ""
    start = max(0, match.start() - 120)
    end = min(len(text), match.end() + 260)
    return " ".join(text[start:end].split())[:500]


def write_csv(path: Path, headers: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(headers), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in headers})
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def explicit_names(detail_data: Mapping[str, Any]) -> dict[str, list[str]]:
    output: dict[str, list[str]] = defaultdict(list)
    for list_name in ("attachment_list", "large_attach_list", "trade_document_list"):
        values = detail_data.get(list_name)
        if not isinstance(values, list):
            continue
        for row in values:
            if not isinstance(row, dict):
                continue
            raw_url = next((str(row.get(key) or "") for key in (
                "file_url", "fileUrl", "download_url", "downloadUrl", "url"
            ) if str(row.get(key) or "").strip()), "")
            file_name = str(row.get("file_name") or row.get("fileName") or "").strip()
            if raw_url and file_name:
                output[source_url_identity_key(raw_url)].append(file_name)
    return output


def mail_context(case_root: Path, manifest: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    mails = manifest.get("mails")
    require(isinstance(mails, list) and 1 <= ordinal <= len(mails), "MAIL_ORDINAL_INVALID")
    mail = mails[ordinal - 1]
    require(isinstance(mail, dict), "MAIL_RECORD_INVALID")
    detail_ref = mail.get("detail")
    require(isinstance(detail_ref, dict), "MAIL_DETAIL_REF_INVALID")
    detail_path = safe_case_file(case_root, str(detail_ref.get("relative_path") or ""))
    require(sha256_file(detail_path) == str(detail_ref.get("sha256") or "").upper(), "MAIL_DETAIL_HASH_MISMATCH")
    value = read_json(detail_path, "MAIL_DETAIL")
    data = value.get("data")
    if not isinstance(data, dict):
        data = {}
    return {
        "mail_id": str(mail.get("mail_id") or data.get("mail_id") or ""),
        "subject": clean_text(data.get("subject")),
        "receive_time": str(data.get("receive_time") or data.get("create_time") or data.get("update_time") or ""),
        "name_by_identity": explicit_names(data),
        "mail": mail,
    }


def extension_for(row: Mapping[str, Any], display_names: Sequence[str]) -> str:
    for name in display_names:
        suffix = Path(name).suffix.casefold()
        if suffix and len(suffix) <= 16:
            return suffix
    suffix = Path(str(row.get("source_rel") or "")).suffix.casefold()
    if suffix in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".doc", ".docx", ".xls", ".xlsx"}:
        return suffix
    mime = str(row.get("mime") or "").casefold()
    magic = str(row.get("magic") or "").casefold()
    if "pdf" in mime or "pdf" in magic:
        return ".pdf"
    if "png" in mime or "png" in magic:
        return ".png"
    return ".bin"


# EN: Build case-local derived artifacts from validated inputs.
# 中文：从校验后的输入构建案例内派生成果。
def build(args: argparse.Namespace) -> dict[str, Any]:
    case_root = args.case_root.resolve()
    require(case_root.is_dir(), "CASE_ROOT_MISSING")
    identity = read_json(case_root / "case_identity.json", "CASE_IDENTITY")
    require(str(identity.get("company_id") or "") == args.company_id, "CASE_IDENTITY_MISMATCH")
    completion = args.completion_root.resolve()
    require(completion.is_dir() and case_root in completion.parents, "COMPLETION_ROOT_INVALID")
    full_extract = completion / "full_extract_v2"
    relationship = completion / "relationship_v2" / "relationship_timeline_v2.sqlite3"
    fts_path = full_extract / "full_text.sqlite3"
    state_path = full_extract / "state.sqlite3"
    manifest_path = case_root / "manifests" / "mail_capture_manifest_latest.json"
    manifest = read_json(manifest_path, "MAIL_MANIFEST")
    require(str(manifest.get("company_id") or "") == args.company_id, "MAIL_MANIFEST_COMPANY_MISMATCH")
    require(str(manifest.get("status") or "").upper() == "PASS", "MAIL_MANIFEST_NOT_PASS")

    fts = connect_ro(fts_path)
    state = connect_ro(state_path)
    rel = connect_ro(relationship)
    try:
        strong = {str(row[0]).upper() for row in fts.execute(
            "SELECT DISTINCT source_sha256 FROM full_text WHERE full_text MATCH ?", ('"proforma"',)
        )}
        phrase = {str(row[0]).upper() for row in fts.execute(
            "SELECT DISTINCT source_sha256 FROM full_text WHERE full_text MATCH ?", ('"proforma invoice"',)
        )}
        occurrence_rows = [dict(row) for row in rel.execute(
            "SELECT occurrence_id,mail_node_id,source_mail_ordinal,source_resource_ordinal,"
            "source_pointer,artifact_source_rel,artifact_sha256,resolution "
            "FROM attachment_occurrence ORDER BY source_mail_ordinal,source_resource_ordinal,occurrence_id"
        ) if str(row["artifact_sha256"]).upper() in strong]
        candidate_shas = sorted({str(row["artifact_sha256"]).upper() for row in occurrence_rows})
        require(all(HEX64.fullmatch(item) for item in candidate_shas), "CANDIDATE_SHA_INVALID")

        file_rows: dict[str, dict[str, Any]] = {}
        for row in state.execute(
            "SELECT source_rel,sha256,bytes,magic,category,mime,source_scope FROM files WHERE active=1"
        ):
            digest = str(row["sha256"]).upper()
            if digest in candidate_shas and digest not in file_rows:
                file_rows[digest] = dict(row)
        require(set(file_rows) == set(candidate_shas), "CANDIDATE_FILE_MAPPING_INCOMPLETE")

        doc_ids: dict[str, list[int]] = defaultdict(list)
        for row in fts.execute("SELECT id,source_sha256 FROM documents"):
            digest = str(row["source_sha256"]).upper()
            if digest in candidate_shas:
                doc_ids[digest].append(int(row["id"]))

        contexts: dict[int, dict[str, Any]] = {}
        occurrences_by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
        display_names_by_sha: dict[str, set[str]] = defaultdict(set)
        parent_ids_by_sha: dict[str, set[str]] = defaultdict(set)
        for row in occurrence_rows:
            ordinal = int(row["source_mail_ordinal"])
            context = contexts.setdefault(ordinal, mail_context(case_root, manifest, ordinal))
            mail = context["mail"]
            resources = mail.get("related_resources") if isinstance(mail, dict) else None
            resource_ordinal = int(row["source_resource_ordinal"])
            resource = resources[resource_ordinal - 1] if isinstance(resources, list) and 1 <= resource_ordinal <= len(resources) else {}
            identity_key = str(resource.get("source_identity_sha256") or "").upper() if isinstance(resource, dict) else ""
            names = context["name_by_identity"].get(identity_key, [])
            for name in names:
                display_names_by_sha[str(row["artifact_sha256"]).upper()].add(name)
            digest = str(row["artifact_sha256"]).upper()
            parent_ids_by_sha[digest].add(context["mail_id"])
            occurrences_by_sha[digest].append({
                "occurrence_id": row["occurrence_id"],
                "sha256": digest,
                "mail_id": context["mail_id"],
                "mail_subject": context["subject"],
                "mail_receive_time_raw": context["receive_time"],
                "source_mail_ordinal": ordinal,
                "source_resource_ordinal": resource_ordinal,
                "source_pointer": row["source_pointer"],
                "resolution": row["resolution"],
                "declared_file_name": " | ".join(sorted(set(names), key=str.casefold)),
            })

        output_parent = args.output_parent.resolve()
        output_parent.mkdir(parents=True, exist_ok=True)
        final = output_parent / f"PI_Archive_{args.company_id}_{args.run_id}"
        stage = output_parent / f".PI_Archive_{args.company_id}_{args.run_id}.staging"
        require(not final.exists() and not stage.exists(), "OUTPUT_EXISTS")
        stage.mkdir()
        (stage / "00_manifest").mkdir()
        (stage / "01_confirmed_pi").mkdir()
        (stage / "02_suspected_pi").mkdir()
        (stage / "03_reference_only").mkdir()
        (stage / "04_non_pi_audit").mkdir()

        candidate_rows: list[dict[str, Any]] = []
        archive_by_sha: dict[str, str] = {}
        try:
            for index, digest in enumerate(candidate_shas, 1):
                source = file_rows[digest]
                source_path = safe_case_file(case_root, str(source["source_rel"]))
                require(source_path.stat().st_size == int(source["bytes"]), "SOURCE_SIZE_MISMATCH")
                require(sha256_file(source_path) == digest, "SOURCE_HASH_MISMATCH")
                text_parts: list[str] = []
                task_kinds: set[str] = set()
                for rowid in doc_ids.get(digest, []):
                    row = fts.execute("SELECT task_kind,text FROM full_text WHERE rowid=?", (rowid,)).fetchone()
                    if row is not None:
                        task_kinds.add(str(row["task_kind"]))
                        text_parts.append(str(row["text"] or ""))
                merged = "\n".join(text_parts)
                groups = [label for label, pattern in STRUCTURE_PATTERNS.items() if pattern.search(merged)]
                classification = "CONFIRMED_PI" if digest in phrase and len(groups) >= 3 else "SUSPECTED_PI"
                target_dir = "01_confirmed_pi" if classification == "CONFIRMED_PI" else "02_suspected_pi"
                display_names = sorted(display_names_by_sha.get(digest, set()), key=str.casefold)
                ext = extension_for(source, display_names)
                preferred = safe_name(display_names[0], f"PI_{index:04d}{ext}") if display_names else f"PI_{index:04d}_{digest[:12]}{ext}"
                if not Path(preferred).suffix:
                    preferred += ext
                archive_name = f"{index:04d}_{digest[:12]}_{preferred}"
                target = stage / target_dir / archive_name
                shutil.copy2(source_path, target)
                require(target.stat().st_size == source_path.stat().st_size, "COPY_SIZE_MISMATCH")
                require(sha256_file(target) == digest, "COPY_HASH_MISMATCH")
                archive_rel = target.relative_to(stage).as_posix()
                archive_by_sha[digest] = archive_rel
                numbers = []
                for match in PI_NUMBER.finditer(merged):
                    value = match.group(1).strip(" .,:;-/")
                    if value and value.casefold() not in {item.casefold() for item in numbers}:
                        numbers.append(value)
                    if len(numbers) >= 8:
                        break
                times = sorted(
                    (str(row["mail_receive_time_raw"]) for row in occurrences_by_sha[digest] if row["mail_receive_time_raw"]),
                    key=str.casefold,
                )
                candidate_rows.append({
                    "序号": index,
                    "分类": classification,
                    "归档文件": archive_rel,
                    "原始显示文件名": " | ".join(display_names),
                    "原始相对路径": source["source_rel"],
                    "SHA256": digest,
                    "字节数": int(source["bytes"]),
                    "MIME": source["mime"],
                    "内容类别": source["category"],
                    "来源范围": source["source_scope"],
                    "完整Proforma Invoice短语": "是" if digest in phrase else "否",
                    "结构字段组数": len(groups),
                    "结构字段组": " | ".join(groups),
                    "PI编号候选": " | ".join(numbers),
                    "提取任务": " | ".join(sorted(task_kinds)),
                    "证据摘录": evidence_excerpt(merged),
                    "出现次数": len(occurrences_by_sha[digest]),
                    "父邮件数": len(parent_ids_by_sha[digest]),
                    "最早业务时间原值": times[0] if times else "",
                    "判定依据": (
                        "附件正文命中完整Proforma Invoice且至少3组发票结构字段"
                        if classification == "CONFIRMED_PI"
                        else "附件正文仅宽口径命中Proforma，证据不足，保留待复核"
                    ),
                })

            occurrence_output: list[dict[str, Any]] = []
            for digest in candidate_shas:
                for row in occurrences_by_sha[digest]:
                    occurrence_output.append({
                        **row,
                        "classification": next(item["分类"] for item in candidate_rows if item["SHA256"] == digest),
                        "archive_rel": archive_by_sha[digest],
                    })
            occurrence_output.sort(key=lambda row: (
                str(row["mail_receive_time_raw"]), int(row["source_mail_ordinal"]), int(row["source_resource_ordinal"]), str(row["occurrence_id"])
            ))

            candidate_headers = list(candidate_rows[0]) if candidate_rows else []
            occurrence_headers = [
                "occurrence_id", "sha256", "classification", "archive_rel", "mail_id", "mail_subject",
                "mail_receive_time_raw", "source_mail_ordinal", "source_resource_ordinal", "source_pointer",
                "resolution", "declared_file_name",
            ]
            write_csv(stage / "00_manifest" / "candidate_classification.csv", candidate_headers, candidate_rows)
            write_csv(stage / "00_manifest" / "pi_occurrences.csv", occurrence_headers, occurrence_output)

            source_gaps = [dict(row) for row in state.execute(
                "SELECT gap_id,source_scope,role,error_code,http_status FROM source_gaps WHERE active=1 ORDER BY source_scope,role,error_code,gap_id"
            )]
            gap_headers = ["gap_id", "source_scope", "role", "error_code", "http_status"]
            write_csv(stage / "00_manifest" / "source_gaps.csv", gap_headers, source_gaps)
            write_csv(stage / "04_non_pi_audit" / "excluded_documents.csv", ["说明"], [{"说明": "本交付仅归档附件正文命中Proforma的内容对象；裸PI及普通Invoice/Quote/PO未纳入。"}])
            write_csv(stage / "03_reference_only" / "说明.csv", ["说明"], [{"说明": "未把仅邮件提及且无可取得原件的记录伪装成PI文件；全案例源缺口见00_manifest/source_gaps.csv。"}])

            confirmed = [row for row in candidate_rows if row["分类"] == "CONFIRMED_PI"]
            suspected = [row for row in candidate_rows if row["分类"] == "SUSPECTED_PI"]
            multiparent = sum(1 for digest in candidate_shas if len(parent_ids_by_sha[digest]) > 1)
            parent_mails = {row["mail_id"] for row in occurrence_output if row["mail_id"]}
            total_bytes = sum(int(row["字节数"]) for row in candidate_rows)
            require(len(candidate_rows) == args.expected_unique, "UNIQUE_PI_COUNT_GATE_FAILED")
            require(len(occurrence_output) == args.expected_occurrences, "PI_OCCURRENCE_COUNT_GATE_FAILED")
            require(len(parent_mails) == args.expected_parent_mails, "PI_PARENT_MAIL_COUNT_GATE_FAILED")
            require(multiparent == args.expected_multiparent, "PI_MULTIPARENT_COUNT_GATE_FAILED")
            require(total_bytes == args.expected_bytes, "PI_BYTES_GATE_FAILED")
            require(len(confirmed) == args.expected_confirmed, "CONFIRMED_PI_COUNT_GATE_FAILED")
            require(len(suspected) == args.expected_suspected, "SUSPECTED_PI_COUNT_GATE_FAILED")
            require(all(str(row["resolution"]) == "EXACT_SOURCE_IDENTITY" for row in occurrence_output), "PI_RESOLUTION_GATE_FAILED")

            lines = [
                f"# 客户 {args.company_id} PI 全量归档",
                "",
                f"生成时间：`{now_utc()}`",
                "",
                "## 结论",
                "",
                f"- 确认PI：**{len(confirmed)}** 个唯一原件。",
                f"- 待复核PI：**{len(suspected)}** 个唯一原件。",
                f"- 邮件附件出现：**{len(occurrence_output)}** 次，来自 **{len(parent_mails)}** 封父邮件。",
                f"- 多父附件：**{multiparent}** 个；同一SHA只复制一次，但所有父邮件关系均保留。",
                f"- 唯一原件总字节：**{total_bytes:,}**。",
                "- 邮件时间为CRM原始本地时间，未擅自转换为UTC。",
                "- 本案例仍有源站删除/失效等全局缺口，详见 `00_manifest/source_gaps.csv`。",
                "",
                "## 确认PI",
                "",
                "|序号|归档文件|原文件名|PI编号候选|父邮件数|出现次数|最早业务时间|SHA-256|",
                "|---:|---|---|---|---:|---:|---|---|",
            ]
            for row in confirmed:
                link = str(row["归档文件"]).replace(" ", "%20")
                lines.append(
                    f"|{row['序号']}|[{Path(str(row['归档文件'])).name}]({link})|"
                    f"{str(row['原始显示文件名']).replace('|', '\\|')}|{str(row['PI编号候选']).replace('|', '\\|')}|"
                    f"{row['父邮件数']}|{row['出现次数']}|{row['最早业务时间原值']}|`{row['SHA256']}`|"
                )
            lines.extend(["", "## 待复核PI", ""])
            for row in suspected:
                link = str(row["归档文件"]).replace(" ", "%20")
                lines.extend([
                    f"### {row['序号']}. [{Path(str(row['归档文件'])).name}]({link})",
                    "",
                    f"- SHA-256：`{row['SHA256']}`",
                    f"- 原文件名：{row['原始显示文件名'] or '未从邮件详情闭合'}",
                    f"- 结构字段：{row['结构字段组'] or '不足'}",
                    f"- 判定：{row['判定依据']}",
                    "",
                ])
            lines.extend([
                "## 父邮件与时间顺序",
                "",
                "完整出现关系见 `00_manifest/pi_occurrences.csv`，含父邮件ID、主题、原始接收时间、资源指针与归档文件。",
                "",
                "## 验收口径",
                "",
                "- `CONFIRMED_PI`：正文完整命中 `Proforma Invoice`，并至少命中3组发票结构字段。",
                "- `SUSPECTED_PI`：只命中宽口径 `Proforma`，未达到确认门槛。",
                "- 普通 Commercial/Tax Invoice、Quotation、PO、Packing List及裸 `PI` 不进入确认目录。",
                "- 原件只复制，不移动、不修改；副本逐件重新计算SHA-256。",
                "",
            ])
            atomic_write(stage / "PI总览.md", ("\n".join(lines) + "\n").encode("utf-8"))
            baseline = {
                "schema": SCHEMA + ".baseline",
                "company_id": args.company_id,
                "generated_at_utc": now_utc(),
                "inputs": {
                    "completion_root": str(completion),
                    "full_text_sha256": sha256_file(fts_path),
                    "state_sha256": sha256_file(state_path),
                    "relationship_sha256": sha256_file(relationship),
                    "mail_manifest_sha256": sha256_file(manifest_path),
                },
            }
            atomic_json(stage / "00_manifest" / "source_baseline.json", baseline)
            verification = {
                "schema": SCHEMA + ".verification",
                "status": "ACCESSIBLE_PI_ARCHIVE_PASS",
                "absolute_completeness_status": "ABSOLUTE_COMPLETENESS_SOURCE_GAPS" if source_gaps else "ABSOLUTE_COMPLETENESS_PASS",
                "company_id": args.company_id,
                "run_id": args.run_id,
                "generated_at_utc": now_utc(),
                "counts": {
                    "unique_pi_originals": len(candidate_rows),
                    "confirmed_pi": len(confirmed),
                    "suspected_pi": len(suspected),
                    "attachment_occurrences": len(occurrence_output),
                    "parent_mails": len(parent_mails),
                    "multiparent_originals": multiparent,
                    "unique_original_bytes": total_bytes,
                    "active_global_source_gaps": len(source_gaps),
                },
                "gates": {
                    "FULL_TEXT_ATTACHMENT_INTERSECTION_PASS": True,
                    "PI_STRUCTURE_CLASSIFICATION_PASS": True,
                    "MAIL_PARENT_OCCURRENCE_PASS": True,
                    "COPY_SHA256_PASS": True,
                    "ONE_PHYSICAL_COPY_PER_SHA_PASS": True,
                    "LOCAL_TIME_PRESERVATION_PASS": True,
                    "IMMUTABLE_ATOMIC_DELIVERY_PASS": True,
                },
            }
            atomic_json(stage / "00_manifest" / "verification.json", verification)
            files = sorted((path for path in stage.rglob("*") if path.is_file()), key=lambda item: item.relative_to(stage).as_posix().casefold())
            checksum_lines = [f"{sha256_file(path)}  {path.relative_to(stage).as_posix()}\n" for path in files]
            atomic_write(stage / "checksums.sha256", "".join(checksum_lines).encode("utf-8"))
            os.replace(stage, final)
            return {
                "status": verification["status"],
                "absolute_completeness_status": verification["absolute_completeness_status"],
                "output": str(final),
                "counts": verification["counts"],
                "verification_sha256": sha256_file(final / "00_manifest" / "verification.json"),
                "checksums_sha256": sha256_file(final / "checksums.sha256"),
            }
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    finally:
        fts.close()
        state.close()
        rel.close()


def read_checksums(path: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, separator, rel = line.partition("  ")
        require(separator == "  " and HEX64.fullmatch(digest), "CHECKSUM_LINE_INVALID")
        output[rel] = digest.upper()
    return output


# EN: Reconcile counts, source bindings and outputs; fail closed on mismatch.
# 中文：核对数量、来源绑定与输出；不一致时拒绝通过。
def verify(delivery: Path) -> dict[str, Any]:
    root = delivery.resolve()
    require(root.is_dir() and root.name.startswith("PI_Archive_"), "DELIVERY_INVALID")
    verification = read_json(root / "00_manifest" / "verification.json", "VERIFICATION")
    require(verification.get("schema") == SCHEMA + ".verification", "VERIFICATION_SCHEMA_INVALID")
    require(verification.get("status") == "ACCESSIBLE_PI_ARCHIVE_PASS", "VERIFICATION_STATUS_INVALID")
    checksums = read_checksums(root / "checksums.sha256")
    for rel, digest in checksums.items():
        path = (root / rel).resolve()
        require(root in path.parents and path.is_file(), "CHECKSUM_PATH_INVALID")
        require(sha256_file(path) == digest, "CHECKSUM_MISMATCH")
    candidate_rows = list(csv.DictReader((root / "00_manifest" / "candidate_classification.csv").open("r", encoding="utf-8-sig", newline="")))
    occurrence_rows = list(csv.DictReader((root / "00_manifest" / "pi_occurrences.csv").open("r", encoding="utf-8-sig", newline="")))
    counts = verification.get("counts")
    require(isinstance(counts, dict), "VERIFICATION_COUNTS_INVALID")
    require(len(candidate_rows) == int(counts.get("unique_pi_originals", -1)), "VERIFY_UNIQUE_COUNT_MISMATCH")
    require(len(occurrence_rows) == int(counts.get("attachment_occurrences", -1)), "VERIFY_OCCURRENCE_COUNT_MISMATCH")
    shas = {row["SHA256"] for row in candidate_rows}
    require(len(shas) == len(candidate_rows), "VERIFY_DUPLICATE_SHA")
    for row in candidate_rows:
        path = (root / row["归档文件"]).resolve()
        require(root in path.parents and path.is_file(), "VERIFY_ARCHIVE_FILE_MISSING")
        require(path.stat().st_size == int(row["字节数"]) and sha256_file(path) == row["SHA256"], "VERIFY_ARCHIVE_FILE_MISMATCH")
    require(all(row["sha256"] in shas for row in occurrence_rows), "VERIFY_OCCURRENCE_ORPHAN")
    return {
        "status": verification["status"],
        "absolute_completeness_status": verification["absolute_completeness_status"],
        "output": str(root),
        "counts": counts,
        "verification_sha256": sha256_file(root / "00_manifest" / "verification.json"),
        "checksums_sha256": sha256_file(root / "checksums.sha256"),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Collect immutable PI originals and parent-mail relations")
    commands = root.add_subparsers(dest="command", required=True)
    build_cmd = commands.add_parser("build")
    build_cmd.add_argument("--case-root", type=Path, required=True)
    build_cmd.add_argument("--company-id", required=True)
    build_cmd.add_argument("--completion-root", type=Path, required=True)
    build_cmd.add_argument("--output-parent", type=Path, required=True)
    build_cmd.add_argument("--run-id", required=True)
    build_cmd.add_argument("--expected-unique", type=int, required=True)
    build_cmd.add_argument("--expected-occurrences", type=int, required=True)
    build_cmd.add_argument("--expected-parent-mails", type=int, required=True)
    build_cmd.add_argument("--expected-multiparent", type=int, required=True)
    build_cmd.add_argument("--expected-bytes", type=int, required=True)
    build_cmd.add_argument("--expected-confirmed", type=int, required=True)
    build_cmd.add_argument("--expected-suspected", type=int, required=True)
    verify_cmd = commands.add_parser("verify")
    verify_cmd.add_argument("--delivery", type=Path, required=True)
    return root


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = build(args) if args.command == "build" else verify(args.delivery)
    except ArchiveError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "FAILED", "error_code": str(exc)}, separators=(",", ":")), flush=True)
        return 2
    except Exception:
        print(json.dumps({"schema": SCHEMA, "status": "FAILED", "error_code": "UNEXPECTED_ERROR"}, separators=(",", ":")), flush=True)
        return 3
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
