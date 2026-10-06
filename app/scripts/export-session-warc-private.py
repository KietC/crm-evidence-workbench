from __future__ import annotations

import gzip
import argparse
import hashlib
import io
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "runtime" / "python-packages"))

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


def read_jsonl(path: Path) -> list[dict]:
    output = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            output.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return output


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


parser = argparse.ArgumentParser(description="Export one already-captured local session to WARC/1.1 gzip without network access.")
parser.add_argument("session_root", help="absolute raw/sessions/<session_id> directory")
args = parser.parse_args()
session_root = Path(args.session_root).resolve()
if session_root.parent.name != "sessions" or not session_root.is_dir():
    raise SystemExit("the argument must be an existing raw/sessions/<session_id> directory")
case_root = session_root.parents[2]
session_id = session_root.name
responses = read_jsonl(session_root / "network" / "responses.jsonl")
requests = read_jsonl(case_root / "request_ledger.jsonl")
request_by_sequence = {(str(row.get("session_id")), int(row.get("sequence", -1))): row for row in requests if isinstance(row.get("sequence"), int)}

output_root = case_root / "work" / "archive"
output_root.mkdir(parents=True, exist_ok=True)
output = output_root / f"{hashlib.sha256(session_id.encode()).hexdigest()[:16]}.warc.gz"
temp = output.with_suffix(output.suffix + ".tmp")
records = 0
response_bodies = 0
missing_bodies = 0

with temp.open("wb") as stream:
    writer = WARCWriter(stream, gzip=True, warc_version="1.1")
    for response in responses:
        sequence = int(response.get("sequence", -1))
        url = str(response.get("url") or "about:blank")
        request = request_by_sequence.get((session_id, sequence))
        if request:
            parsed = urlsplit(url)
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            request_headers = [(str(key), str(value)) for key, value in (request.get("headers") or {}).items()]
            request_line = f"{request.get('method', 'GET')} {target} HTTP/1.1"
            body = b""
            relative = request.get("post_data_relative_path")
            if isinstance(relative, str):
                try:
                    body = (case_root / relative).read_bytes()
                except OSError:
                    body = b""
            record = writer.create_warc_record(url, "request", payload=io.BytesIO(body), http_headers=StatusAndHeaders(request_line, request_headers, protocol="HTTP/1.1"))
            writer.write_record(record)
            records += 1

        relative = response.get("bodyRelativePath")
        if not isinstance(relative, str):
            missing_bodies += 1
            continue
        body_path = case_root / relative
        try:
            payload = body_path.open("rb")
        except OSError:
            missing_bodies += 1
            continue
        response_headers = [(str(key), str(value)) for key, value in (response.get("responseHeaders") or {}).items()]
        if not any(key.lower() == "content-type" for key, _ in response_headers):
            response_headers.append(("Content-Type", str(response.get("mimeType") or "application/octet-stream")))
        status_line = f"{int(response.get('status') or 0)} {response.get('statusText') or ''}".strip()
        with payload:
            record = writer.create_warc_record(url, "response", payload=payload, http_headers=StatusAndHeaders(status_line, response_headers, protocol="HTTP/1.1"))
            writer.write_record(record)
        records += 1
        response_bodies += 1

temp.replace(output)
manifest = {
    "schema": 1,
    "format": "WARC/1.1 gzip",
    "session_id_sha256": hashlib.sha256(session_id.encode()).hexdigest(),
    "relative_path": str(output.relative_to(case_root)),
    "size": output.stat().st_size,
    "sha256": sha256_file(output),
    "records": records,
    "response_bodies": response_bodies,
    "responses_without_body": missing_bodies,
    "source": "local evidence ledgers; no network access",
}
(output.with_suffix(output.suffix + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
print(json.dumps({"privacy": {"case_name": False, "company_id": False, "customer_content": False, "external_upload": False}, "warc_relative": str(output.relative_to(case_root)), "size": manifest["size"], "sha256": manifest["sha256"], "records": records, "response_bodies": response_bodies, "responses_without_body": missing_bodies}, ensure_ascii=False))
