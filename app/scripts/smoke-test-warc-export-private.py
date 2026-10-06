from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "runtime" / "python-packages"))
from warcio.archiveiterator import ArchiveIterator

with tempfile.TemporaryDirectory(prefix="okki-warc-smoke-") as temporary:
    case = Path(temporary) / "case"
    session = case / "raw" / "sessions" / "capture_test"
    objects = session / "objects"
    network = session / "network"
    objects.mkdir(parents=True)
    network.mkdir(parents=True)
    body = objects / "0000001.json"
    body.write_bytes(b'{"ok":true}')
    request = {
        "session_id": "capture_test", "sequence": 1, "method": "GET",
        "url": "https://crm.xiaoman.cn/api/test", "headers": {"accept": "application/json"},
        "post_data_relative_path": None,
    }
    response = {
        "sequence": 1, "method": "GET", "url": "https://crm.xiaoman.cn/api/test", "status": 200,
        "statusText": "OK", "mimeType": "application/json", "responseHeaders": {"content-type": "application/json"},
        "bodyRelativePath": str(body.relative_to(case)), "error": None,
    }
    (case / "request_ledger.jsonl").write_text(json.dumps(request) + "\n", "utf-8")
    (network / "responses.jsonl").write_text(json.dumps(response) + "\n", "utf-8")
    result = subprocess.run([sys.executable, str(APP_ROOT / "scripts" / "export-session-warc-private.py"), str(session)], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    warc = case / payload["warc_relative"]
    with warc.open("rb") as stream:
        records = list(ArchiveIterator(stream))
    if len(records) != 2 or {record.rec_type for record in records} != {"request", "response"}:
        raise SystemExit("WARC smoke verification failed")
    print(json.dumps({"pass": True, "records": len(records), "types": sorted(record.rec_type for record in records), "external_upload": False}))
