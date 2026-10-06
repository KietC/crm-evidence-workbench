#!/usr/bin/env python3
# EN: Local-only build evidence corpus utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
import hashlib
import html
import json
import re
import sys
import copy
from html.parser import HTMLParser
from pathlib import Path


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())


def strip_html(value: str) -> str:
    parser = TextExtractor()
    try:
        parser.feed(value or "")
        return "\n".join(parser.parts)
    except Exception:
        return re.sub(r"<[^>]+>", " ", value or "")


def normalize(value) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def latest_session_dir(case_root: Path) -> Path:
    sessions = sorted((case_root / "raw" / "sessions").glob("capture_*"))
    if not sessions:
        raise RuntimeError("no capture_* session found")
    return sessions[-1]


def _list_paths(value, path=()):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_list_paths(item, path + (key,)))
    elif isinstance(value, list) and len(value) > 1:
        found.append((path, value))
        for index, item in enumerate(value):
            found.extend(_list_paths(item, path + (index,)))
    return found


def _get_path(value, path):
    for key in path:
        value = value[key]
    return value


def _set_path(value, path, replacement):
    parent = value
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = replacement


def split_large_record(record, max_chars):
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return [record]
    candidates = _list_paths(record)
    if candidates:
        path, values = max(candidates, key=lambda item: len(json.dumps(item[1], ensure_ascii=False, separators=(",", ":"))))
        base = copy.deepcopy(record)
        _set_path(base, path, [])
        groups, current = [], []
        for item in values:
            trial = copy.deepcopy(base)
            _set_path(trial, path, current + [item])
            if current and len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) > max_chars:
                groups.append(current)
                current = [item]
            else:
                current.append(item)
        if current:
            groups.append(current)
        parts = []
        for index, group in enumerate(groups, 1):
            part = copy.deepcopy(base)
            _set_path(part, path, group)
            part["fragment"] = {
                "method": "list_partition",
                "json_path": "/" + "/".join(map(str, path)),
                "index": index,
                "count": len(groups),
            }
            parts.extend(split_large_record(part, max_chars))
        return parts
    # Last-resort lossless text fragmentation for a scalar-heavy response.
    overhead = 1200
    width = max(1000, max_chars - overhead)
    fragments = [encoded[index:index + width] for index in range(0, len(encoded), width)]
    return [{
        "evidence_relative_path": record.get("evidence_relative_path"),
        "observed": record.get("observed", True),
        "fragment": {"method": "utf8_json_text", "index": index, "count": len(fragments)},
        "payload_text_fragment": fragment,
    } for index, fragment in enumerate(fragments, 1)]


def write_chunks(output_dir: Path, category: str, records, max_chars=60000, split_large=False, compact_output=False):
    if split_large:
        records = [part for record in records for part in split_large_record(record, max_chars)]
    chunks, current, current_chars = [], [], 0
    for record in records:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if current and current_chars + len(encoded) > max_chars:
            chunks.append(current)
            current, current_chars = [], 0
        current.append(record)
        current_chars += len(encoded)
    if current:
        chunks.append(current)
    manifest = []
    for index, chunk in enumerate(chunks, 1):
        path = output_dir / f"{category}_{index:03d}.json"
        if compact_output:
            payload = json.dumps({"category": category, "records": chunk}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            payload = json.dumps({"category": category, "records": chunk}, ensure_ascii=False, indent=2).encode("utf-8")
        path.write_bytes(payload)
        manifest.append({
            "category": category,
            "chunk": index,
            "relative_path": str(path),
            "records": len(chunk),
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
        })
    return manifest


# EN: Parse CLI inputs and emit status-only results.
# 中文：解析命令行并仅输出状态结果。
def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_evidence_corpus.py CASE_ROOT")
    case_root = Path(sys.argv[1])
    output_dir = case_root / "derived" / "normalized" / "corpus"
    output_dir.mkdir(parents=True, exist_ok=True)

    mail_records = []
    for path in sorted((case_root / "raw" / "mail" / "details").glob("mail_detail_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("code") != 0:
            continue
        data = payload.get("data") or {}
        plain = normalize(data.get("plain_text"))
        content = plain or normalize(strip_html(data.get("content") or ""))
        mail_records.append({
            "evidence_relative_path": str(path.relative_to(case_root)),
            "mail_id_sha256": sha256_text(data.get("mail_id")),
            "observed": True,
            "date": data.get("receive_time") or data.get("create_time"),
            "subject": data.get("subject"),
            "sender": data.get("sender"),
            "receiver": data.get("receiver"),
            "cc": data.get("cc"),
            "bcc": data.get("bcc"),
            "summary": data.get("summary"),
            "content": content,
            "attachments": [
                {key: item.get(key) for key in ("name", "file_name", "size", "file_size", "ext", "type") if item.get(key) is not None}
                for field in ("attachment_list", "large_attach_list", "trade_document_list")
                for item in (data.get(field) or [])
                if isinstance(item, dict)
            ],
            "has_track": bool(data.get("has_track")),
            "view_count": int(data.get("view_count") or 0),
        })

    document_records = []
    document_index = json.loads((case_root / "derived" / "documents" / "index.json").read_text(encoding="utf-8"))
    ocr_index_path = case_root / "derived" / "ocr" / "index.json"
    ocr_index = json.loads(ocr_index_path.read_text(encoding="utf-8")) if ocr_index_path.exists() else {"pages": []}
    ocr_by_doc = {}
    for page in ocr_index.get("pages", []):
        ocr_payload = json.loads((case_root / Path(page["json_relative_path"])).read_text(encoding="utf-8"))
        ocr_by_doc.setdefault(page["document_index"], []).append({
            "page": page["page"],
            "text": ocr_payload.get("text", ""),
            "average_score": ocr_payload.get("average_score"),
            "evidence_relative_path": page["json_relative_path"],
        })
    for document in document_index["documents"]:
        text_path = case_root / "derived" / "documents" / f"document_{document['index']:04d}" / "text.txt"
        document_records.append({
            "document_index": document["index"],
            "observed": True,
            "source_relative_path": document["source_relative_path"],
            "source_sha256": document["source_sha256"],
            "native_text": normalize(text_path.read_text(encoding="utf-8")) if text_path.exists() else "",
            "ocr_pages": ocr_by_doc.get(document["index"], []),
        })

    vlm_records = []
    for path in sorted((case_root / "derived" / "vlm" / "tags").glob("image_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        vlm_records.append({
            "evidence_relative_path": str(path.relative_to(case_root)),
            "source_relative_path": payload.get("source_relative_path"),
            "source_sha256": payload.get("sha256"),
            "status": payload.get("status"),
            "model": payload.get("model"),
            "response": payload.get("response"),
            "inferred": True,
        })

    image_ocr_records = []
    image_ocr_index_path = case_root / "derived" / "ocr_images" / "index.json"
    if image_ocr_index_path.exists():
        image_ocr_index = json.loads(image_ocr_index_path.read_text(encoding="utf-8"))
        for item in image_ocr_index.get("images", []):
            if item.get("status") != "completed" or not item.get("text_chars"):
                continue
            path = case_root / Path(item["json_relative_path"])
            payload = json.loads(path.read_text(encoding="utf-8"))
            image_ocr_records.append({
                "evidence_relative_path": item["json_relative_path"],
                "source_relative_path": item["source_relative_path"],
                "source_sha256": payload.get("source_sha256"),
                "engine": payload.get("engine"),
                "text": payload.get("text"),
                "average_score": payload.get("average_score"),
                "observed_from_pixels": True,
            })

    api_records = []
    api_dir = latest_session_dir(case_root) / "api"
    allowed_prefixes = ("root_reload_statistics_retry__", "tab_dynamic__", "tab_info__", "tab_trade__", "tab_tips__", "tab_seaData__", "tab_statistics__", "tab_relational__", "tab_history__")
    allowed_root_labels = {
        "GeneralObjectRead_Detail",
        "opportunityRead_list",
        "reportRead_detail",
        "tipsRead_pull",
        "tipsRead_companyInfo",
        "customerV2Read_FormFieldList",
        "customerRead_relatedProductList",
        "customerRead_customerOrderList",
        "customerRead_companyDiffList",
        "fileFolderRead_folderList",
        "fileFolderRead_fileList",
        "customerV2Read_detail",
        "customerV2Read_GetNextMoveToPublicDateTips",
        "customerRead_getReference",
        "customerRead_adviceCount",
    }
    api_by_hash = {}
    for path in sorted(api_dir.glob("*.json")):
        if not path.name.startswith(allowed_prefixes):
            continue
        endpoint_label = path.name.split("__")[1] if "__" in path.name else path.stem
        if endpoint_label not in allowed_root_labels:
            continue
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        digest = sha256_bytes(raw)
        relative_path = str(path.relative_to(case_root))
        if digest in api_by_hash:
            api_by_hash[digest]["duplicate_evidence_relative_paths"].append(relative_path)
            continue
        record = {
            "evidence_relative_path": str(path.relative_to(case_root)),
            "duplicate_evidence_relative_paths": [],
            "observed": True,
            "endpoint_label": endpoint_label,
            "payload": payload,
        }
        api_by_hash[digest] = record
        api_records.append(record)

    customs_records = [{
        "status": ["customs_candidate_unverified", "not_bound_to_customer"],
        "warning": "Do not promote any candidate company, product, amount, trend, or trading partner to customer fact without deterministic identity confirmation.",
        "candidate_index": json.loads((case_root / "raw" / "customs" / "candidate_index.json").read_text(encoding="utf-8")),
        "detail_index": json.loads((case_root / "raw" / "customs" / "detail_index.json").read_text(encoding="utf-8")),
    }]
    for index in range(1, 21):
        company_files = sorted(api_dir.glob(f"ciq_{index:03d}_customs__ciqRead_company__*.json"))
        if not company_files:
            continue
        path = company_files[-1]
        customs_records.append({
            "candidate_index": index,
            "status": ["customs_candidate_unverified", "not_bound_to_customer"],
            "evidence_relative_path": str(path.relative_to(case_root)),
            "company_payload": json.loads(path.read_text(encoding="utf-8")),
        })

    manifest = []
    manifest += write_chunks(output_dir, "mail", mail_records)
    manifest += write_chunks(output_dir, "documents", document_records)
    manifest += write_chunks(output_dir, "vlm", vlm_records)
    manifest += write_chunks(output_dir, "image_ocr", image_ocr_records)
    manifest += write_chunks(output_dir, "root_api", api_records, max_chars=25000, split_large=True, compact_output=True)
    manifest += write_chunks(output_dir, "customs_unverified", customs_records, max_chars=25000, split_large=True, compact_output=True)
    result = {
        "manifest": manifest,
        "summary": {
            "mail_records": len(mail_records),
            "document_records": len(document_records),
            "vlm_records": len(vlm_records),
            "image_ocr_records": len(image_ocr_records),
            "root_api_records": len(api_records),
            "customs_records": len(customs_records),
            "chunks": len(manifest),
            "bytes": sum(item["bytes"] for item in manifest),
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
