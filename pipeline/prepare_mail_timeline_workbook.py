# EN: Prepare local mail text and attachments; chronological order takes precedence over hierarchy.
# 中文：准备本地邮件正文及附件；时间顺序优先于父子层级。
from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image, ImageOps


SCHEMA = "okki.single_customer.mail_timeline_workbook_payload.v1"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
URL_RE = re.compile(r"^https?://", re.I)
SENTENCE_END_RE = re.compile(r"[。！？.!?]+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
STABLE_OBJECT_HOSTS = {
    "cdn.xiaoman.cn",
    "v4client-oss.xiaoman.cn",
    "v4client-oss-new.xiaoman.cn",
    "v4client.oss-cn-hangzhou.aliyuncs.com",
}
SECRET_QUERY_KEYS = {"signature", "token", "access_token", "authorization", "auth", "api_key"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return CONTROL_RE.sub("", text)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        return clean_text(html.unescape("".join(parser.parts)))
    except Exception:
        return clean_text(re.sub(r"<[^>]+>", " ", value))


def split_excel_text(value: str, max_chars: int = 15000, max_newlines: int = 180) -> list[str]:
    text = clean_text(value)
    if not text:
        return [""]
    chunks: list[str] = []
    start = 0
    total = len(text)
    while start < total:
        hard_end = min(total, start + max_chars)
        newline_end = hard_end
        if text.count("\n", start, hard_end) > max_newlines:
            cursor = start
            for _ in range(max_newlines):
                found = text.find("\n", cursor + 1, hard_end + 1)
                if found < 0:
                    break
                cursor = found
            if cursor > start:
                newline_end = cursor + 1
        end = min(hard_end, newline_end)
        if end < total:
            boundary = max(text.rfind("\n\n", start + 200, end), text.rfind("\n", start + 200, end))
            if boundary > start + 200:
                end = boundary + 1
        if end <= start:
            end = min(total, start + max_chars)
        chunks.append(text[start:end])
        start = end
    return chunks


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def resolve_rel(case_root: Path, relative: str | None, bases: Iterable[Path] = ()) -> Path | None:
    if not relative:
        return None
    raw = str(relative)
    direct = Path(raw)
    if direct.is_absolute() and direct.exists():
        return direct
    posix = Path(*PurePosixPath(raw.replace("\\", "/")).parts)
    candidates = [case_root / posix]
    candidates.extend(base / posix for base in bases)
    candidates.extend(base / Path(raw).name for base in bases)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _redacted_url(raw_url: str) -> str:
    try:
        split = urlsplit(raw_url)
        query: list[tuple[str, str]] = []
        for key, value in parse_qsl(split.query, keep_blank_values=True):
            if key.lower() in SECRET_QUERY_KEYS:
                value = f"<redacted:sha256:{sha256_text(value)[:16]}>"
            query.append((key, value))
        return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))
    except Exception:
        return raw_url


def source_url_identity(raw_url: str) -> str:
    raw = raw_url.replace("&amp;", "&").replace("&AMP;", "&")
    try:
        split = urlsplit(raw)
        host = (split.hostname or "").lower()
        if host in STABLE_OBJECT_HOSTS:
            port = f":{split.port}" if split.port else ""
            canonical = f"{split.scheme.lower()}://{host}{port}{split.path}"
            keep = [(k, v) for k, v in parse_qsl(split.query, keep_blank_values=True) if k in {"x-oss-process", "versionId", "versionid"}]
            if keep:
                canonical += "?" + urlencode(keep)
            return sha256_text(canonical)
    except Exception:
        pass
    return sha256_text(_redacted_url(raw))


def collect_url_metadata(value: Any, output: dict[str, dict[str, str]]) -> None:
    if isinstance(value, dict):
        filename = ""
        for key in ("file_name", "filename", "origin_name", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                filename = clean_text(candidate).strip()
                break
        metadata = {
            "filename": filename,
            "mime": clean_text(value.get("mime_type") or value.get("content_type") or ""),
            "size": clean_text(value.get("file_size") or value.get("size") or ""),
        }
        for child in value.values():
            if isinstance(child, str) and URL_RE.match(child):
                output[source_url_identity(child)] = metadata
        for child in value.values():
            collect_url_metadata(child, output)
    elif isinstance(value, list):
        for child in value:
            collect_url_metadata(child, output)


def human_filename(source_rel: str, sha: str, category: str) -> str:
    suffix = Path(source_rel).suffix.lower()
    return f"attachment_{sha[:12]}{suffix or ('.bin' if category == 'other' else '')}"


def most_common_text(values: Iterable[str]) -> str:
    counter = collections.Counter(clean_text(value).strip() for value in values if clean_text(value).strip())
    return counter.most_common(1)[0][0] if counter else ""


def make_thumbnail(source: Path, target: Path, max_size: tuple[int, int] = (360, 220)) -> tuple[str, int, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        try:
            opened.seek(0)
        except Exception:
            pass
        image = ImageOps.exif_transpose(opened).copy()
    if image.mode in {"RGBA", "LA"}:
        base = Image.new("RGB", image.size, "white")
        alpha = image.getchannel("A") if "A" in image.getbands() else None
        base.paste(image.convert("RGB"), mask=alpha)
        image = base
    else:
        image = image.convert("RGB")
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    width, height = image.size
    image.save(target, format="JPEG", quality=74, optimize=True, progressive=True)
    return str(target), width, height


def completed_task(tasks: list[dict[str, Any]], kind: str, case_root: Path) -> dict[str, Any] | None:
    candidates = [row for row in tasks if row.get("task_kind") == kind and row.get("state") == "completed" and row.get("output_rel")]
    for row in sorted(candidates, key=lambda item: ("completion_v2" not in str(item.get("output_rel")), str(item.get("output_rel")))):
        path = resolve_rel(case_root, str(row["output_rel"]))
        if path and path.exists():
            result = dict(row)
            result["output_path"] = path
            return result
    return None


def read_task_json(task: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    path = Path(task["output_path"])
    index = path if path.is_file() else path / "index.json"
    return load_json(index), index


def read_rel_text(case_root: Path, index_path: Path, relative: str | None) -> str:
    path = resolve_rel(case_root, relative, bases=(index_path.parent,))
    if not path or not path.exists() or not path.is_file():
        return ""
    try:
        return clean_text(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return ""


def extract_pdf_text(case_root: Path, tasks: list[dict[str, Any]]) -> tuple[str, str, str]:
    native_pages: dict[int, str] = {}
    native = completed_task(tasks, "extract_pdf_native", case_root)
    if native:
        try:
            payload, _ = read_task_json(native)
            for page in payload.get("pages") or []:
                if isinstance(page, dict):
                    native_pages[int(page.get("page") or 0)] = clean_text(page.get("text"))
        except Exception:
            native_pages = {}
    ocr_pages: dict[int, str] = {}
    ocr = completed_task(tasks, "ocr_pdf_all_pages", case_root)
    if ocr:
        try:
            payload, index_path = read_task_json(ocr)
            for page in payload.get("pages") or []:
                if isinstance(page, dict):
                    number = int(page.get("page") or 0)
                    ocr_pages[number] = read_rel_text(case_root, index_path, page.get("text_rel"))
        except Exception:
            ocr_pages = {}
    numbers = sorted(set(native_pages) | set(ocr_pages))
    pieces: list[str] = []
    for number in numbers:
        text = ocr_pages.get(number, "").strip() or native_pages.get(number, "").strip()
        pieces.append(f"[第 {number} 页]\n{text if text else '（本页未识别到可读文字）'}")
    result = "\n\n".join(pieces).strip()
    if result:
        return result, "PDF逐页OCR，空页回退原生文字", "PASS"
    return "原件已保留，但未得到可用文字。", "PDF OCR/原生提取", "TEXT_EXTRACTION_UNRESOLVED"


def extract_parts_text(case_root: Path, tasks: list[dict[str, Any]], kind: str, label: str) -> tuple[str, str, str]:
    task = completed_task(tasks, kind, case_root)
    if not task:
        return "原件已保留，但文字提取失败。", label, "TEXT_EXTRACTION_UNRESOLVED"
    try:
        payload, _ = read_task_json(task)
        pieces = []
        for part in payload.get("parts") or []:
            if not isinstance(part, dict):
                continue
            text = clean_text(part.get("text")).strip()
            if text:
                pieces.append(f"[{clean_text(part.get('part') or '文档内容')}]\n{text}")
        if not pieces:
            top_text = clean_text(payload.get("text")).strip()
            if top_text:
                pieces.append(top_text)
        result = "\n\n".join(pieces).strip()
        if result:
            return result, label, "PASS"
    except Exception:
        pass
    return "原件已保留，但文字提取失败。", label, "TEXT_EXTRACTION_UNRESOLVED"


def extract_plain_text(case_root: Path, tasks: list[dict[str, Any]], kind: str, label: str) -> tuple[str, str, str]:
    task = completed_task(tasks, kind, case_root)
    if not task:
        return "原件已保留，但文字提取失败。", label, "TEXT_EXTRACTION_UNRESOLVED"
    try:
        payload, _ = read_task_json(task)
        text = clean_text(payload.get("text")).strip()
        if text:
            return text, label, "PASS"
    except Exception:
        pass
    return "原件已保留，但文字提取失败。", label, "TEXT_EXTRACTION_UNRESOLVED"


def extract_eps(case_root: Path, tasks: list[dict[str, Any]]) -> tuple[str, str, str, Path | None]:
    task = completed_task(tasks, "ocr_eps_rendered", case_root)
    if not task:
        return "原件已保留，但EPS渲染OCR失败。", "EPS渲染OCR", "TEXT_EXTRACTION_UNRESOLVED", None
    try:
        payload, index_path = read_task_json(task)
        pieces: list[str] = []
        preview: Path | None = None
        for page in payload.get("pages") or []:
            if not isinstance(page, dict):
                continue
            number = int(page.get("page") or 0)
            text = read_rel_text(case_root, index_path, page.get("text_rel")).strip()
            pieces.append(f"[第 {number} 页]\n{text if text else '（本页未识别到可读文字）'}")
            if preview is None:
                candidate = resolve_rel(case_root, page.get("image_rel"), bases=(index_path.parent,))
                if candidate and candidate.exists():
                    preview = candidate
        return "\n\n".join(pieces), "EPS渲染OCR", "PASS", preview
    except Exception:
        return "原件已保留，但EPS渲染OCR失败。", "EPS渲染OCR", "TEXT_EXTRACTION_UNRESOLVED", None


def extract_archive_index(case_root: Path, tasks: list[dict[str, Any]]) -> tuple[str, str, str]:
    task = completed_task(tasks, "inspect_archive", case_root)
    if not task:
        return "原件已保留，但压缩包目录读取失败。", "压缩包只读检查", "ARCHIVE_INDEX_UNRESOLVED"
    try:
        payload, _ = read_task_json(task)
        members = payload.get("members") or []
        lines = [f"压缩包格式：{clean_text(payload.get('format'))}", f"成员数：{len(members)}"]
        for index, member in enumerate(members, 1):
            if not isinstance(member, dict):
                continue
            name = clean_text(member.get("member_path") or member.get("path") or member.get("name") or f"member_{index}")
            size = member.get("uncompressed_bytes") or member.get("bytes") or ""
            lines.append(f"{index}. {name}" + (f"（{size} bytes）" if size != "" else ""))
        return "\n".join(lines), "压缩包只读目录", "PASS"
    except Exception:
        return "原件已保留，但压缩包目录读取失败。", "压缩包只读检查", "ARCHIVE_INDEX_UNRESOLVED"


def sentence_candidates(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", clean_text(text)).strip()
    return [piece.strip(" ，,;；:\t") for piece in SENTENCE_END_RE.split(cleaned) if len(piece.strip()) >= 6]


def representative_sentence(text: str) -> str:
    candidates = sentence_candidates(text)
    if not candidates:
        return re.sub(r"\s+", " ", clean_text(text)).strip()[:160]
    corpus = " ".join(candidates).lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9_-]{1,}|[\u4e00-\u9fff]", corpus)
    frequencies = collections.Counter(tokens)
    stop = {"the", "and", "for", "with", "this", "that", "from", "have", "you", "your", "的", "了", "和", "是", "在", "有", "我", "你"}
    for word in stop:
        frequencies.pop(word, None)
    def score(candidate: str) -> tuple[float, int]:
        toks = re.findall(r"[a-z0-9][a-z0-9_-]{1,}|[\u4e00-\u9fff]", candidate.lower())
        value = sum(frequencies.get(token, 0) for token in toks) / max(1, len(toks))
        return value, min(len(candidate), 180)
    return max(candidates, key=score)[:180]


def one_sentence(prefix: str, core: str, suffix: str = "") -> str:
    body = re.sub(r"\s+", " ", clean_text(core)).strip()
    body = SENTENCE_END_RE.sub("，", body).strip(" ，,;；:")
    body = body[:180].rstrip(" ，,;；:")
    text = f"{prefix}{body}{suffix}" if body else f"{prefix}无法可靠识别具体内容{suffix}"
    text = SENTENCE_END_RE.sub("，", text).strip(" ，,;；:")
    return text + "。"


def video_summary(case_root: Path, tasks: list[dict[str, Any]]) -> tuple[str, str, str]:
    transcript = ""
    asr = completed_task(tasks, "asr_dual_diarization", case_root)
    if asr:
        root = Path(asr["output_path"])
        if root.is_dir():
            candidates: list[str] = []
            for path in root.rglob("source_audio.txt"):
                try:
                    text = clean_text(path.read_text(encoding="utf-8-sig", errors="replace")).strip()
                except Exception:
                    continue
                meaningful = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", text))
                if meaningful >= 8:
                    candidates.append(text)
            if candidates:
                transcript = max(candidates, key=len)
    if transcript:
        return one_sentence("视频语音主要内容：", representative_sentence(transcript)), "本地双ASR抽取式一句话总结", "PASS"
    frame_texts: list[str] = []
    frame = completed_task(tasks, "video_frame_ocr", case_root)
    if frame:
        try:
            payload, index_path = read_task_json(frame)
            seen: set[str] = set()
            for item in payload.get("frames") or []:
                if not isinstance(item, dict):
                    continue
                text = read_rel_text(case_root, index_path, item.get("text_rel")).strip()
                norm = re.sub(r"\s+", " ", text)
                if len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", norm)) >= 6 and norm not in seen:
                    seen.add(norm)
                    frame_texts.append(norm)
        except Exception:
            frame_texts = []
    if frame_texts:
        joined = " ".join(frame_texts)
        return one_sentence("视频主要展示画面文字：", representative_sentence(joined), "，未检测到可靠语音"), "本地逐帧OCR抽取式一句话总结", "PASS_NO_SPEECH"
    return "该视频未检测到可可靠转写的语音或画面文字，无法形成更具体的一句话摘要。", "本地双ASR与逐帧OCR", "SOURCE_GAP_NO_RELIABLE_CONTENT"


@dataclass
class MailRecord:
    mail_id: str
    manifest_ordinal: int
    detail_status: str
    detail_rel: str
    detail_sha: str
    source_data: dict[str, Any]
    event_time: str
    local_sort_key: str
    stable_order_key: str
    time_status: str
    direction: str
    raw_parent_id: str
    parent_id: str
    reply_mail_id: str
    parent_relation_status: str
    body: str
    body_source: str
    url_metadata: dict[str, dict[str, str]]

    @property
    def sort_tuple(self) -> tuple[Any, ...]:
        return (0 if self.time_status == "KNOWN_LOCAL_UNZONED" else 1, self.local_sort_key, self.stable_order_key, self.mail_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-root", required=True)
    parser.add_argument("--delivery-root", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--assets-dir", required=True)
    parser.add_argument("--verification", required=True)
    # EN: External expected counts make coverage validation reusable and fail closed.
    # 中文：显式期望数量使覆盖验收可复用；不满足时不得伪装成功。
    for label in ("mails", "known-time-mails", "pending-time-mails", "mail-attachment-relations",
                  "attachment-occurrences", "attachments", "unresolved-documents"):
        parser.add_argument("--expected-" + label, type=int, required=True)
    return parser.parse_args()


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    args = parse_args()
    case_root = Path(args.case_root).resolve()
    delivery_root = Path(args.delivery_root).resolve()
    work_root = Path(args.work_root).resolve()
    output_path = Path(args.output).resolve()
    assets_dir = Path(args.assets_dir).resolve()
    verification_path = Path(args.verification).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = case_root / "manifests" / "mail_capture_manifest_latest.json"
    relation_db_path = delivery_root / "relationship_v2" / "relationship_timeline_v2.sqlite3"
    state_db_path = work_root / "state.sqlite3"
    manifest = load_json(manifest_path)
    manifest_mails = manifest.get("mails") or []

    relation = sqlite3.connect(f"file:{relation_db_path}?mode=ro", uri=True)
    relation.row_factory = sqlite3.Row
    timeline_map: dict[str, dict[str, str]] = {}
    for row in relation.execute(
        """
        SELECT a.source_rel,te.local_sort_key,te.stable_order_key,te.normalization_status,
               te.time_basis,te.timestamp_precision
        FROM timeline_event te
        JOIN artifacts a ON a.artifact_id=te.artifact_id
        WHERE te.timeline_axis='business' AND te.field_name='receive_time'
          AND te.json_pointer='/data/receive_time'
        """
    ):
        timeline_map[str(row["source_rel"]).replace("\\", "/")] = dict(row)

    artifact_by_sha: dict[str, dict[str, Any]] = {}
    for row in relation.execute(
        """
        SELECT DISTINCT a.artifact_id,a.source_rel,a.source_sha256,a.source_bytes,a.category,a.mime,a.current_hash_verified
        FROM attachment_occurrence o JOIN artifacts a ON a.artifact_id=o.artifact_id
        WHERE o.artifact_id IS NOT NULL
        """
    ):
        sha = str(row["source_sha256"] or "").lower()
        if sha and sha not in artifact_by_sha:
            artifact_by_sha[sha] = dict(row)
    relation.close()

    state = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
    state.row_factory = sqlite3.Row
    tasks_by_sha: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in state.execute(
        "SELECT source_sha256,task_kind,state,output_rel,result_sha256,error_code,active FROM tasks WHERE active=1"
    ):
        tasks_by_sha[str(row["source_sha256"] or "").lower()].append(dict(row))
    state.close()

    mails: list[MailRecord] = []
    manifest_by_mail: dict[str, dict[str, Any]] = {}
    for ordinal, manifest_mail in enumerate(manifest_mails, 1):
        mail_id = clean_text(manifest_mail.get("mail_id")).strip()
        manifest_by_mail[mail_id] = manifest_mail
        detail = manifest_mail.get("detail") or {}
        detail_rel = clean_text(detail.get("relative_path")).replace("\\", "/")
        detail_sha = clean_text(detail.get("sha256")).lower()
        data: dict[str, Any] = {}
        url_metadata: dict[str, dict[str, str]] = {}
        if detail_rel:
            detail_path = resolve_rel(case_root, detail_rel)
            if detail_path and detail_path.exists():
                payload = load_json(detail_path)
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    data = payload["data"]
                    collect_url_metadata(payload, url_metadata)
        raw_mail_id = clean_text(data.get("mail_id")).strip()
        if raw_mail_id:
            mail_id = raw_mail_id
        event_time = clean_text(data.get("receive_time")).strip()
        timeline = timeline_map.get(detail_rel, {})
        local_sort_key = clean_text(timeline.get("local_sort_key") or event_time)
        stable_order_key = clean_text(timeline.get("stable_order_key") or f"{ordinal:09d}:{mail_id}")
        known = bool(event_time and local_sort_key)
        mail_type = clean_text(data.get("mail_type")).strip()
        direction = "发出" if mail_type == "2" else "收到" if mail_type == "1" else "未知"
        parent_id = clean_text(data.get("reply_to_mail_id")).strip()
        if parent_id.lower() in {"", "0", "none", "null"}:
            parent_id = ""
        reply_mail_id = clean_text(data.get("reply_mail_id")).strip()
        if reply_mail_id.lower() in {"", "0", "none", "null"}:
            reply_mail_id = ""
        plain_text = clean_text(data.get("plain_text")).strip()
        if plain_text:
            body = plain_text
            body_source = "plain_text"
        else:
            body = html_to_text(clean_text(data.get("content"))).strip()
            body_source = "content_html_to_text"
        mails.append(
            MailRecord(
                mail_id=mail_id,
                manifest_ordinal=ordinal,
                detail_status=clean_text(manifest_mail.get("detail_status")),
                detail_rel=detail_rel,
                detail_sha=detail_sha,
                source_data=data,
                event_time=event_time,
                local_sort_key=local_sort_key,
                stable_order_key=stable_order_key,
                time_status="KNOWN_LOCAL_UNZONED" if known else "TIME_UNRESOLVED",
                direction=direction,
                raw_parent_id=parent_id,
                parent_id=parent_id,
                reply_mail_id=reply_mail_id,
                parent_relation_status="",
                body=body,
                body_source=body_source,
                url_metadata=url_metadata,
            )
        )
    mail_by_id = {mail.mail_id: mail for mail in mails}

    for mail in mails:
        raw_parent = mail.raw_parent_id
        if not raw_parent:
            mail.parent_id = ""
            mail.parent_relation_status = ""
            continue
        if raw_parent == mail.mail_id:
            mail.parent_id = ""
            mail.parent_relation_status = "RAW_PARENT_SELF_DROPPED"
            continue
        parent = mail_by_id.get(raw_parent)
        if parent is None:
            mail.parent_id = ""
            mail.parent_relation_status = "RAW_PARENT_NOT_CAPTURED"
            continue
        if (
            parent.time_status == "KNOWN_LOCAL_UNZONED"
            and mail.time_status == "KNOWN_LOCAL_UNZONED"
            and parent.local_sort_key > mail.local_sort_key
        ):
            mail.parent_id = ""
            mail.parent_relation_status = "RAW_PARENT_AFTER_CHILD_DROPPED"
            continue
        mail.parent_id = raw_parent
        mail.parent_relation_status = "VALID_SOURCE_PARENT"

    def cycle_members() -> list[list[str]]:
        done: set[str] = set()
        cycles: list[list[str]] = []
        for start in mail_by_id:
            if start in done:
                continue
            path: list[str] = []
            position: dict[str, int] = {}
            current = start
            while current and current in mail_by_id and current not in done:
                if current in position:
                    cycles.append(path[position[current]:])
                    break
                position[current] = len(path)
                path.append(current)
                current = mail_by_id[current].parent_id
            done.update(path)
        return cycles

    for cycle in cycle_members():
        if not cycle:
            continue
        break_id = max(cycle, key=lambda mail_id: mail_by_id[mail_id].sort_tuple)
        mail_by_id[break_id].parent_id = ""
        mail_by_id[break_id].parent_relation_status = "RAW_PARENT_CYCLE_DROPPED"

    pair_map: dict[tuple[str, str], dict[str, Any]] = {}
    attachment_names: dict[str, list[str]] = collections.defaultdict(list)
    attachment_occurrences: collections.Counter[str] = collections.Counter()
    for mail in mails:
        manifest_mail = manifest_by_mail.get(mail.mail_id) or manifest_mails[mail.manifest_ordinal - 1]
        for resource_ordinal, resource in enumerate(manifest_mail.get("related_resources") or [], 1):
            artifact = resource.get("artifact") or {}
            sha = clean_text(artifact.get("sha256")).lower()
            if not SHA_RE.fullmatch(sha):
                continue
            attachment_occurrences[sha] += 1
            identity = clean_text(resource.get("source_identity_sha256")).lower()
            artifact_identity = clean_text(resource.get("artifact_source_identity_sha256")).lower()
            metadata = mail.url_metadata.get(identity) or mail.url_metadata.get(artifact_identity) or {}
            filename = clean_text(metadata.get("filename")).strip()
            if filename:
                attachment_names[sha].append(filename)
            key = (mail.mail_id, sha)
            current = pair_map.get(key)
            if current is None:
                pair_map[key] = {
                    "mail_id": mail.mail_id,
                    "sha256": sha,
                    "resource_ordinal": resource_ordinal,
                    "resolution": clean_text(resource.get("resolution")),
                    "source_identity_sha256": identity,
                    "artifact_rel": clean_text(artifact.get("relative_path")).replace("\\", "/"),
                    "artifact_mime": clean_text(artifact.get("mime_type")),
                    "artifact_bytes": int(artifact.get("bytes") or 0),
                    "occurrence_count": 1,
                    "filename": filename,
                }
            else:
                current["occurrence_count"] += 1
                current["resource_ordinal"] = min(int(current["resource_ordinal"]), resource_ordinal)
                if not current.get("filename") and filename:
                    current["filename"] = filename

    parent_ids_by_sha: dict[str, set[str]] = collections.defaultdict(set)
    pairs_by_mail: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for pair in pair_map.values():
        parent_ids_by_sha[pair["sha256"]].add(pair["mail_id"])
        pairs_by_mail[pair["mail_id"]].append(pair)
    for pair_list in pairs_by_mail.values():
        pair_list.sort(key=lambda row: (int(row["resource_ordinal"]), str(row["sha256"])))

    attachment_order = sorted(
        parent_ids_by_sha,
        key=lambda sha: (
            min((mail_by_id[mid].sort_tuple for mid in parent_ids_by_sha[sha] if mid in mail_by_id), default=(9, "", "", "")),
            sha,
        ),
    )
    attachment_id_by_sha = {sha: f"ATT-{index:04d}" for index, sha in enumerate(attachment_order, 1)}

    content_rows: list[dict[str, Any]] = []
    attachment_summary: dict[str, dict[str, Any]] = {}
    thumbnail_count = 0
    image_thumbnail_count = 0
    eps_thumbnail_count = 0
    video_summary_count = 0
    text_unresolved_count = 0
    for sha in attachment_order:
        artifact = artifact_by_sha.get(sha, {})
        source_rel = clean_text(artifact.get("source_rel") or next((p["artifact_rel"] for p in pair_map.values() if p["sha256"] == sha), "")).replace("\\", "/")
        source_path = resolve_rel(case_root, source_rel)
        category = clean_text(artifact.get("category") or "other")
        mime = clean_text(artifact.get("mime") or next((p["artifact_mime"] for p in pair_map.values() if p["sha256"] == sha), ""))
        names = attachment_names.get(sha, [])
        filename = most_common_text(names) or human_filename(source_rel, sha, category)
        alternate_names = sorted({clean_text(name).strip() for name in names if clean_text(name).strip() and clean_text(name).strip() != filename})
        parents = sorted((mail_by_id[mid] for mid in parent_ids_by_sha[sha] if mid in mail_by_id), key=lambda item: item.sort_tuple)
        first_parent = parents[0] if parents else None
        first_time = first_parent.event_time if first_parent else ""
        first_mail_id = first_parent.mail_id if first_parent else ""
        tasks = tasks_by_sha.get(sha, [])
        content_type = "OTHER_ATTACHMENT"
        method = "原件保留"
        status = "PASS_ORIGINAL_ONLY"
        text = "原件已保留；此类型不转写为正文。"
        preview_path = ""
        preview_width = 0
        preview_height = 0
        if category == "image":
            content_type = "IMAGE"
            text = "图片原件已直接嵌入本行，SHA对应未修改的原图。"
            method = "原图缩略预览（原件SHA不变）"
            status = "PASS"
            if source_path and source_path.exists():
                try:
                    preview_path, preview_width, preview_height = make_thumbnail(source_path, assets_dir / f"{sha}.jpg")
                    thumbnail_count += 1
                    image_thumbnail_count += 1
                except Exception:
                    status = "IMAGE_PREVIEW_UNRESOLVED"
                    text = "图片原件已保留，但Excel缩略预览生成失败。"
        elif category == "document":
            content_type = "DOC_TEXT_CHUNK"
            text, method, status = extract_pdf_text(case_root, tasks)
        elif category == "office":
            content_type = "DOC_TEXT_CHUNK"
            text, method, status = extract_parts_text(case_root, tasks, "extract_office_xml", "OOXML全文提取")
        elif category == "legacy-office":
            content_type = "DOC_TEXT_CHUNK"
            text, method, status = extract_parts_text(case_root, tasks, "extract_legacy_office", "旧版Office只读转换与全文提取")
        elif category == "text":
            content_type = "DOC_TEXT_CHUNK"
            text, method, status = extract_plain_text(case_root, tasks, "extract_text", "文本原样提取")
        elif category == "eps":
            content_type = "EPS_OCR_TEXT"
            text, method, status, eps_preview = extract_eps(case_root, tasks)
            if eps_preview and eps_preview.exists():
                try:
                    preview_path, preview_width, preview_height = make_thumbnail(eps_preview, assets_dir / f"{sha}.jpg")
                    thumbnail_count += 1
                    eps_thumbnail_count += 1
                except Exception:
                    preview_path = ""
        elif category == "archive":
            content_type = "ARCHIVE_INDEX"
            text, method, status = extract_archive_index(case_root, tasks)
        elif category == "video":
            content_type = "VIDEO_SUMMARY"
            text, method, status = video_summary(case_root, tasks)
            video_summary_count += 1
        if status == "TEXT_EXTRACTION_UNRESOLVED":
            text_unresolved_count += 1
        derived_sha = sha256_text(text)
        chunks = [text] if content_type in {"IMAGE", "VIDEO_SUMMARY", "OTHER_ATTACHMENT"} else split_excel_text(text)
        attachment_id = attachment_id_by_sha[sha]
        start_sequence = len(content_rows) + 1
        for chunk_no, chunk in enumerate(chunks, 1):
            content_rows.append(
                {
                    "content_sequence": len(content_rows) + 1,
                    "first_mail_time": first_time,
                    "first_mail_id": first_mail_id,
                    "attachment_id": attachment_id,
                    "content_type": content_type,
                    "filename": filename,
                    "alternate_names": " | ".join(alternate_names),
                    "category": category,
                    "mime": mime,
                    "content": chunk,
                    "chunk_no": chunk_no,
                    "chunk_total": len(chunks),
                    "original_sha256": sha,
                    "derived_sha256": derived_sha,
                    "source_rel": source_rel,
                    "method": method,
                    "status": status,
                    "parent_mail_count": len(parents),
                    "occurrence_count": int(attachment_occurrences[sha]),
                    "preview_path": preview_path if chunk_no == 1 else "",
                    "preview_width": preview_width if chunk_no == 1 else 0,
                    "preview_height": preview_height if chunk_no == 1 else 0,
                }
            )
        attachment_summary[sha] = {
            "attachment_id": attachment_id,
            "filename": filename,
            "category": category,
            "mime": mime,
            "source_rel": source_rel,
            "content_start_sequence": start_sequence,
            "content_chunk_count": len(chunks),
            "status": status,
        }

    known_mails = sorted((mail for mail in mails if mail.time_status == "KNOWN_LOCAL_UNZONED"), key=lambda item: item.sort_tuple)
    unresolved_mails = sorted((mail for mail in mails if mail.time_status != "KNOWN_LOCAL_UNZONED"), key=lambda item: (item.manifest_ordinal, item.mail_id))
    time_inversions = sum(1 for left, right in zip(known_mails, known_mails[1:]) if left.sort_tuple > right.sort_tuple)

    parent_after_child = sum(1 for mail in mails if mail.parent_relation_status == "RAW_PARENT_AFTER_CHILD_DROPPED")
    parent_missing = sum(1 for mail in mails if mail.parent_relation_status == "RAW_PARENT_NOT_CAPTURED")

    def has_parent_cycle() -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(mail_id: str) -> bool:
            if mail_id in visiting:
                return True
            if mail_id in visited:
                return False
            visiting.add(mail_id)
            parent = mail_by_id.get(mail_id).parent_id if mail_id in mail_by_id else ""
            if parent and parent in mail_by_id and visit(parent):
                return True
            visiting.remove(mail_id)
            visited.add(mail_id)
            return False
        return any(visit(mail_id) for mail_id in mail_by_id)

    parent_cycle = has_parent_cycle()
    timeline_rows: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    timeline_sequence = 0
    for mail_order, mail in enumerate(known_mails, 1):
        data = mail.source_data
        anomaly = "" if mail.parent_relation_status in {"", "VALID_SOURCE_PARENT"} else mail.parent_relation_status
        body = mail.body or "（邮件正文为空或源站未返回可读正文）"
        body_chunks = split_excel_text(body)
        body_sha = sha256_text(body)
        common = {
            "mail_order": mail_order,
            "event_time": mail.event_time,
            "time_status": mail.time_status,
            "direction": mail.direction,
            "mail_id": mail.mail_id,
            "parent_mail_id": mail.parent_id,
            "raw_parent_mail_id": mail.raw_parent_id,
            "reply_mail_id": mail.reply_mail_id,
            "sender": clean_text(data.get("sender")),
            "receiver": clean_text(data.get("receiver")),
            "cc": clean_text(data.get("cc")),
            "bcc": clean_text(data.get("bcc")),
            "subject": clean_text(data.get("subject")),
            "mail_sha256": mail.detail_sha,
            "body_sha256": body_sha,
            "source_rel": mail.detail_rel,
            "time_anomaly": anomaly,
            "local_sort_key": mail.local_sort_key,
            "stable_order_key": mail.stable_order_key,
            "source_ordinal": mail.manifest_ordinal,
        }
        for chunk_no, chunk in enumerate(body_chunks, 1):
            timeline_sequence += 1
            timeline_rows.append(
                {
                    **common,
                    "timeline_sequence": timeline_sequence,
                    "record_type": "EMAIL_BODY_CHUNK",
                    "content": chunk,
                    "chunk_no": chunk_no,
                    "chunk_total": len(body_chunks),
                    "body_source": mail.body_source,
                    "attachment_ordinal": "",
                    "attachment_id": "",
                    "attachment_name": "",
                    "attachment_category": "",
                    "attachment_sha256": "",
                    "attachment_content_start": "",
                    "processing_status": "PASS" if mail.body else "EMPTY_BODY",
                }
            )
        for pair in pairs_by_mail.get(mail.mail_id, []):
            sha = pair["sha256"]
            summary = attachment_summary[sha]
            timeline_sequence += 1
            timeline_rows.append(
                {
                    **common,
                    "timeline_sequence": timeline_sequence,
                    "record_type": "ATTACHMENT_REF",
                    "content": f"附件内容见“02_附件内容”，编号 {summary['attachment_id']}。",
                    "chunk_no": "",
                    "chunk_total": "",
                    "body_source": "",
                    "attachment_ordinal": pair["resource_ordinal"],
                    "attachment_id": summary["attachment_id"],
                    "attachment_name": summary["filename"],
                    "attachment_category": summary["category"],
                    "attachment_sha256": sha,
                    "attachment_content_start": summary["content_start_sequence"],
                    "processing_status": summary["status"],
                }
            )
            relation_rows.append(
                {
                    "event_time": mail.event_time,
                    "direction": mail.direction,
                    "mail_id": mail.mail_id,
                    "parent_mail_id": mail.parent_id,
                    "raw_parent_mail_id": mail.raw_parent_id,
                    "subject": clean_text(data.get("subject")),
                    "attachment_ordinal": pair["resource_ordinal"],
                    "attachment_id": summary["attachment_id"],
                    "attachment_name": summary["filename"],
                    "category": summary["category"],
                    "attachment_sha256": sha,
                    "occurrence_count": pair["occurrence_count"],
                    "resolution": pair["resolution"],
                    "source_rel": summary["source_rel"],
                    "content_start_sequence": summary["content_start_sequence"],
                    "local_sort_key": mail.local_sort_key,
                    "stable_order_key": mail.stable_order_key,
                }
            )

    pending_rows = [
        {
            "manifest_ordinal": mail.manifest_ordinal,
            "mail_id": mail.mail_id,
            "detail_status": mail.detail_status,
            "reason": "源端邮件详情已删除或缺少可靠业务时间；未按父子关系猜测时间。",
            "mail_sha256": mail.detail_sha,
            "source_rel": mail.detail_rel,
        }
        for mail in unresolved_mails
    ]

    category_counts = collections.Counter(artifact_by_sha[sha].get("category") for sha in attachment_order)
    mail_sha_valid = sum(1 for mail in mails if SHA_RE.fullmatch(mail.detail_sha))
    attachment_sha_valid = sum(1 for sha in attachment_order if SHA_RE.fullmatch(sha))
    named_attachment_count = sum(1 for sha in attachment_order if attachment_summary[sha]["filename"] and not attachment_summary[sha]["filename"].startswith("attachment_"))
    qa_rows = [
        ["邮件总数", len(mails), args.expected_mails, "PASS" if len(mails) == args.expected_mails else "FAIL", "范围以当前mail_capture_manifest_latest.json为准"],
        ["进入时间线的邮件", len(known_mails), args.expected_known_time_mails, "PASS" if len(known_mails) == args.expected_known_time_mails else "FAIL", "全部按receive_time本地无时区排序"],
        ["时间待核邮件", len(unresolved_mails), args.expected_pending_time_mails, "PASS" if len(unresolved_mails) == args.expected_pending_time_mails else "FAIL", "缺失时间不使用父子关系推测"],
        ["时间顺序逆序数", time_inversions, 0, "PASS" if time_inversions == 0 else "FAIL", "时间关系优先"],
        ["唯一邮件附件关系", len(pair_map), args.expected_mail_attachment_relations, "PASS" if len(pair_map) == args.expected_mail_attachment_relations else "FAIL", "重复出现合并为occurrence_count"],
        ["附件发生记录", sum(attachment_occurrences.values()), args.expected_attachment_occurrences, "PASS" if sum(attachment_occurrences.values()) == args.expected_attachment_occurrences else "FAIL", "全部出现记录已计数"],
        ["唯一附件", len(attachment_order), args.expected_attachments, "PASS" if len(attachment_order) == args.expected_attachments else "FAIL", "按原件SHA去重，关系不丢"],
        ["图片缩略图直接嵌入", image_thumbnail_count, int(category_counts.get("image", 0)), "PASS" if image_thumbnail_count == int(category_counts.get("image", 0)) else "FAIL", "缩略图仅用于Excel预览，SHA对应原图"],
        ["EPS预览嵌入", eps_thumbnail_count, int(category_counts.get("eps", 0)), "PASS" if eps_thumbnail_count == int(category_counts.get("eps", 0)) else "FAIL", "EPS先渲染后预览"],
        ["视频一句话摘要", video_summary_count, int(category_counts.get("video", 0)), "PASS" if video_summary_count == int(category_counts.get("video", 0)) else "FAIL", "本地双ASR与逐帧OCR"],
        ["文档文字提取未闭合", text_unresolved_count, args.expected_unresolved_documents, "SOURCE_GAP" if text_unresolved_count == args.expected_unresolved_documents else "FAIL", "登记无法处理的原件；原件与SHA始终保留"],
        ["邮件SHA格式有效", mail_sha_valid, len(mails), "PASS" if mail_sha_valid == len(mails) else "FAIL", "SHA-256 64位十六进制"],
        ["附件SHA格式有效", attachment_sha_valid, len(attachment_order), "PASS" if attachment_sha_valid == len(attachment_order) else "FAIL", "SHA-256 64位十六进制"],
        ["父邮件晚于子邮件", parent_after_child, "仅标记", "PASS", "不改变真实时间顺序"],
        ["父邮件未在本案捕获", parent_missing, "仅标记", "PASS", "不跨客户页面补抓"],
        ["父子关系环", 1 if parent_cycle else 0, 0, "PASS" if not parent_cycle else "FAIL", "父子关系不参与跨时间排序"],
        ["恢复出的原文件名", named_attachment_count, len(attachment_order), "INFO", "无法恢复时使用SHA派生稳定名称"],
        ["时区状态", len(known_mails), "local_unzoned", "PASS", "不擅自添加UTC或时区"],
    ]

    payload = {
        "schema": SCHEMA,
        "company_id": clean_text(manifest.get("company_id")),
        "source_manifest": str(manifest_path),
        "source_manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
        "timeline_note": "receive_time为源系统本地无时区时间；严格按local_sort_key、stable_order_key、mail_id升序。父子关系只标记，不跨时间重排。",
        "timeline_rows": timeline_rows,
        "content_rows": content_rows,
        "relation_rows": relation_rows,
        "pending_rows": pending_rows,
        "qa_rows": qa_rows,
        "counts": {
            "mails": len(mails),
            "known_time_mails": len(known_mails),
            "pending_time_mails": len(unresolved_mails),
            "timeline_rows": len(timeline_rows),
            "unique_mail_attachment_relations": len(pair_map),
            "attachment_occurrences": sum(attachment_occurrences.values()),
            "unique_attachments": len(attachment_order),
            "content_rows": len(content_rows),
            "image_thumbnails": image_thumbnail_count,
            "eps_thumbnails": eps_thumbnail_count,
            "video_summaries": video_summary_count,
            "text_unresolved": text_unresolved_count,
            "time_inversions": time_inversions,
            "parent_after_child": parent_after_child,
            "parent_missing": parent_missing,
            "parent_cycle": 1 if parent_cycle else 0,
        },
        "category_counts": dict(sorted((str(key), int(value)) for key, value in category_counts.items())),
    }

    temp = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, output_path)

    verification = {
        "schema": "okki.single_customer.mail_timeline_preparation_verification.v1",
        "status": ("FAIL" if any(row[3] == "FAIL" for row in qa_rows) else "PASS_WITH_SOURCE_GAPS" if (text_unresolved_count or unresolved_mails) else "PASS"),
        "payload": str(output_path),
        "payload_sha256": sha256_bytes(output_path.read_bytes()),
        "counts": payload["counts"],
        "category_counts": payload["category_counts"],
        "source_gaps": {
            "source_deleted_mails": len(unresolved_mails),
            "text_extraction_unresolved": text_unresolved_count,
        },
    }
    vtemp = verification_path.with_suffix(verification_path.suffix + ".tmp")
    with vtemp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(verification, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(vtemp, verification_path)

    print(json.dumps({"status": verification["status"], "counts": payload["counts"], "category_counts": payload["category_counts"]}, ensure_ascii=False))
    return 2 if verification["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
