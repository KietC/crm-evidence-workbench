# EN: Synthetic fixtures only; never read real captures or private customer directories.
# 中文：仅使用合成测试夹具，不读取真实采集或私有客户目录。
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import time
import zipfile
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("single_customer_full_extract.py")
SPEC = importlib.util.spec_from_file_location("single_customer_full_extract", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class ExpansionAndLockRegression(unittest.TestCase):
    def test_stream_budget_cleans_partial_and_duplicate_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = {}
            budget = mod.ExpansionBudget(7, 10)
            first = mod.persist_content_stream(io.BytesIO(b"abc"), root, ".txt", cache, budget)
            second = mod.persist_content_stream(io.BytesIO(b"abc"), root, ".txt", cache, budget)
            self.assertEqual(first[0], second[0])
            self.assertEqual(budget.bytes, 6)
            with self.assertRaisesRegex(mod.SourceGap, "ARCHIVE_TOTAL_BYTE_LIMIT"):
                mod.persist_content_stream(io.BytesIO(b"def"), root, ".txt", cache, budget)
            self.assertFalse(list(root.rglob("*.part")))
            self.assertEqual(len(cache), 1)

    def test_multiple_roots_nested_and_member_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as archive:
                archive.writestr("payload.txt", b"12345")
            outer = root / "outer.zip"
            other = root / "other.zip"
            with zipfile.ZipFile(outer, "w") as archive:
                archive.writestr("nested.zip", inner.getvalue())
            with zipfile.ZipFile(other, "w") as archive:
                archive.writestr("payload.txt", b"12345")
                archive.writestr("second.txt", b"12345")
            budget = mod.ExpansionBudget(len(inner.getvalue()) + 6, 10)
            cache = {}
            members, gaps = mod.expand_container_members(outer, "outer", "archive", root, 0, cache, budget)
            self.assertFalse(gaps)
            nested, gaps = mod.expand_container_members(members[0]["path"], "inner", "archive", root, 1, cache, budget)
            self.assertEqual(len(nested), 1)
            later, gaps = mod.expand_container_members(other, "other", "archive", root, 0, cache, budget)
            self.assertFalse(later)
            self.assertEqual(gaps[0]["error_code"], "ARCHIVE_TOTAL_BYTE_LIMIT")
            self.assertFalse(list(root.rglob("*.part")))
            limited = mod.ExpansionBudget(1000, 1)
            rows, gaps = mod.expand_container_members(other, "other", "archive", root, 0, {}, limited)
            self.assertEqual(len(rows), 1)
            self.assertEqual(gaps[0]["error_code"], "ARCHIVE_TOTAL_MEMBER_LIMIT")

    def test_rejected_archive_entries_cannot_bypass_member_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "unsafe.zip"
            with zipfile.ZipFile(source, "w") as archive:
                for index in range(10):
                    archive.writestr(f"../bad-{index}.txt", b"unsafe")
            budget = mod.ExpansionBudget(1000, 2)
            rows, gaps = mod.expand_container_members(source, "unsafe", "archive", root, 0, {}, budget)
            self.assertFalse(rows)
            self.assertEqual(budget.members, 2)
            self.assertEqual(len(gaps), 3)
            self.assertEqual(gaps[-1]["error_code"], "ARCHIVE_TOTAL_MEMBER_LIMIT")
            stopped, gaps = mod.expand_container_members(source, "unsafe", "archive", root, 0, {}, budget)
            self.assertFalse(stopped)
            self.assertEqual(gaps[0]["error_code"], "ARCHIVE_TOTAL_MEMBER_LIMIT")

    def test_run_prepare_cross_process_lock_and_owner_death(self):
        with tempfile.TemporaryDirectory() as folder:
            case = make_case(Path(folder))
            mod.prepare(case, "123456789")
            ready = Path(folder) / "ready"
            code = (
                "import importlib.util, pathlib, time; "
                f"s=importlib.util.spec_from_file_location('extract', {str(MODULE_PATH)!r}); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                f"root=pathlib.Path({str(case)!r}); ready=pathlib.Path({str(ready)!r}); "
                "m._run_locked=lambda *a: (ready.write_text('locked'), time.sleep(60)); "
                "m.run(root, '123456789', None)"
            )
            owner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                for command in ("run", "prepare"):
                    contender = subprocess.run([sys.executable, str(MODULE_PATH), command,
                        "--case-root", str(case), "--company-id", "123456789"],
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(contender.returncode, 2)
                    self.assertIn("CASE_EXTRACTION_LOCK_BUSY", contender.stdout)
            finally:
                owner.kill()
                owner.communicate(timeout=10)
            args = Args()
            args.skip_models = True
            result = mod.run(case, "123456789", args)
            self.assertEqual(result["failed_this_run"], 0)
            mod.prepare(case, "123456789")
            self.assertEqual(mod.run(case, "123456789", args)["completed_this_run"], 0)

    def test_prepare_budget_persists_gaps_and_duplicate_occurrences(self):
        with tempfile.TemporaryDirectory() as folder:
            case = make_case(Path(folder))
            current = session_dir(case)
            for name in ("one.zip", "two.zip"):
                with zipfile.ZipFile(current / name, "w") as archive:
                    archive.writestr(name + "-first.txt", b"same")
                    archive.writestr(name + "-second.txt", b"same")
            mod.prepare(case, "123456789", archive_total_bytes=13)
            with contextlib.closing(sqlite3.connect(extract_dir(case) / "state.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM occurrences WHERE role='archive_member' AND active=1").fetchone()[0], 3)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM source_gaps WHERE error_code='ARCHIVE_TOTAL_BYTE_LIMIT' AND active=1").fetchone()[0], 0)
            self.assertFalse(list(extract_dir(case).rglob("*.part")))


class Args:
    max_tasks = None
    parse_workers = 1
    ocr_workers = 1
    output_dir = None
    skip_models = False
    ffmpeg = None
    ffprobe = None
    pdftoppm = None
    ghostscript = None
    tesseract = None
    tessdata_dir = None
    ocr_languages = "eng"
    asr_script = None
    asr_python = None


def make_case(root: Path, company_id: str = "123456789") -> Path:
    case = root / f"company_{company_id}"
    session = case / "raw" / "sessions" / "capture_test"
    (session / "api").mkdir(parents=True)
    (case / "work").mkdir(parents=True)
    (case / "manifests").mkdir(parents=True)
    (case / "case_identity.json").write_text(json.dumps({"company_id": company_id}), encoding="utf-8")
    (case / "work" / "capture_state.json").write_text(json.dumps({"running": False, "companyId": company_id, "phase": "complete"}), encoding="utf-8")
    (case / "manifests" / "processing_scope_latest.json").write_text(json.dumps({
        "schema": 1,
        "company_id": company_id,
        "capture_status": "pass",
        "default_session_id": "capture_test",
        "object_manifest": "manifests/objects.jsonl",
        "default_object_filter": {"field": "session_id", "equals": "capture_test"},
    }), encoding="utf-8")
    (session / "final_status.json").write_text(json.dumps({"phase": "complete", "errors": [], "metrics": {"reconciliation_failures": 0}}), encoding="utf-8")
    write_manual_manifest(case, [])
    return case


def session_dir(case: Path) -> Path:
    return case / "raw" / "sessions" / "capture_test"


def extract_dir(case: Path) -> Path:
    return case / "derived" / "full_extract_v2"


def write_manual_manifest(case: Path, relative_paths: list[str], status: str = "PASS", gaps: list[dict] | None = None) -> Path:
    directory = case / "evidence" / "manual_trade" / "manual_trade_test"
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for relative in relative_paths:
        path = directory / relative
        artifacts.append({"relative_path": relative, "bytes": path.stat().st_size, "sha256": mod.sha256_file(path)})
    payload = {
        "schema": "okki.trade.manual_capture_manifest.v1",
        "status": status,
        "company_id": case.name.removeprefix("company_"),
        "source_gaps": gaps or [],
        "counts": {"artifact_files": len(artifacts), "artifact_bytes": sum(row["bytes"] for row in artifacts)},
        "artifacts": artifacts,
    }
    manifest = directory / "trade_manual_capture_manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def artifact_ref(case: Path, path: Path, sequence: int, source_identity: str) -> dict:
    return {
        "sequence": sequence,
        "relative_path": path.relative_to(case).as_posix(),
        "sha256": mod.sha256_file(path),
        "bytes": path.stat().st_size,
        "http_status": 200,
        "mime_type": "application/octet-stream",
        "source_identity_sha256": source_identity,
    }


def write_mail_manifest(case: Path) -> Path:
    current = session_dir(case)
    old = case / "raw" / "sessions" / "capture_old" / "mail_related"
    old.mkdir(parents=True)
    detail_one = current / "mail_detail_1.json"
    detail_two = current / "mail_detail_2.json"
    track_one = current / "mail_track_1.json"
    track_two = current / "mail_track_2.json"
    for index, path in enumerate((detail_one, detail_two, track_one, track_two), 1):
        path.write_text(json.dumps({"synthetic": index}), encoding="utf-8")
    related = old / "attachment.png"
    related.write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic")
    identities = [f"{index:064x}" for index in range(1, 8)]
    mails = [
        {
            "mail_id": "MAIL-SYNTHETIC-1", "user_id": "USER-SYNTHETIC",
            "detail_status": "AVAILABLE",
            "detail": artifact_ref(case, detail_one, 1, identities[0]),
            "track": artifact_ref(case, track_one, 2, identities[1]),
            "related_resources": [{
                "source_identity_sha256": identities[2], "resolution": "EXACT_SOURCE_IDENTITY",
                "artifact_source_identity_sha256": identities[2], "source_unavailable_http_status": None,
                "artifact": artifact_ref(case, related, 3, identities[2]),
            }],
        },
        {
            "mail_id": "MAIL-SYNTHETIC-2", "user_id": "USER-SYNTHETIC",
            "detail_status": "SOURCE_DELETED",
            "detail": artifact_ref(case, detail_two, 4, identities[3]),
            "track": artifact_ref(case, track_two, 5, identities[4]),
            "related_resources": [
                {
                    "source_identity_sha256": identities[5], "resolution": "EXACT_SOURCE_IDENTITY",
                    "artifact_source_identity_sha256": identities[2], "source_unavailable_http_status": None,
                    "artifact": artifact_ref(case, related, 6, identities[2]),
                },
                {
                    "source_identity_sha256": identities[6], "resolution": "SOURCE_UNAVAILABLE",
                    "artifact_source_identity_sha256": None, "source_unavailable_http_status": 404,
                    "artifact": None,
                },
            ],
        },
    ]
    payload = {
        "schema": "okki.crm.mail_capture_manifest.v2", "schema_version": 2,
        "artifact_type": "mail_capture_manifest.v2", "status": "PASS",
        "company_id": case.name.removeprefix("company_"), "session_id": "capture_test",
        "counts": {
            "mails": 2, "detail_available": 1, "detail_source_deleted": 1,
            "detail_artifacts": 2, "track_artifacts": 2,
            "related_resource_candidates": 3, "related_resource_artifacts": 2,
            "related_resource_source_unavailable": 1, "related_resource_unresolved": 0,
        },
        "mails": mails,
    }
    manifest = case / "manifests" / "mail_capture_manifest_latest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    scope_path = case / "manifests" / "processing_scope_latest.json"
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    scope["mail_capture_manifest"] = {
        "path": manifest.relative_to(case).as_posix(),
        "bytes": manifest.stat().st_size,
        "sha256": mod.sha256_file(manifest),
    }
    scope_path.write_text(json.dumps(scope), encoding="utf-8")
    return manifest


class FullExtractTests(unittest.TestCase):
    def test_unknown_ole_suffix_is_opaque_not_forced_through_office(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "unknown.db"
            source.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 64)
            with self.assertRaises(mod.SourceGap) as raised:
                mod.extract_legacy_office(source, root / "out")
            self.assertEqual(raised.exception.code, "OPAQUE_BINARY")

    def test_run_asr_accepts_standard_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.wav"
            source.write_bytes(b"RIFF" + b"\0" * 64)
            fake = root / "fake_asr.py"
            fake.write_text(
                "import argparse,json,pathlib\n"
                "p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()\n"
                "d=pathlib.Path(a.output)/'state';d.mkdir(parents=True);(d/'pipeline_result.json').write_text(json.dumps({'status':'complete'}),encoding='utf-8')\n",
                encoding="utf-8",
            )
            args = Args()
            args.asr_script = str(fake)
            args.asr_python = sys.executable
            output = root / "out"
            mod.run_asr(source, output, "audio", args)
            binding = json.loads((output / "source_binding.json").read_text(encoding="utf-8"))
            self.assertEqual(binding["asr_status"], "DUAL_ASR_COMPLETE")
            self.assertEqual(binding["asr_result_rel"], "asr/state/pipeline_result.json")

    def test_run_asr_closes_no_speech_when_diarization_has_no_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.wav"
            source.write_bytes(b"RIFF" + b"\0" * 64)
            fake = root / "fake_asr.py"
            fake.write_text(
                "import argparse,json,pathlib\n"
                "class Pipeline:\n"
                " def __init__(self,source,root): self.root=pathlib.Path(root)\n"
                " def run_qwen(self): return {'results': []}\n"
                " def run_diarization(self):\n"
                "  p=self.root/'logs';p.mkdir(parents=True,exist_ok=True);(p/'3dspeaker_diarization.log').write_text('ValueError: max() iterable argument is empty',encoding='utf-8');raise RuntimeError('empty')\n"
                "if __name__=='__main__':\n"
                " p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args();d=pathlib.Path(a.output)/'state';d.mkdir(parents=True);(d/'pipeline_result.json').write_text(json.dumps({'status':'failed','error':'Whisper mojibake-zero-segments'}),encoding='utf-8');raise SystemExit(2)\n",
                encoding="utf-8",
            )
            args = Args()
            args.asr_script = str(fake)
            args.asr_python = sys.executable
            output = root / "out"
            mod.run_asr(source, output, "audio", args)
            binding = json.loads((output / "source_binding.json").read_text(encoding="utf-8"))
            self.assertEqual(binding["asr_status"], "NO_SPEECH_CONFIRMED_BY_WHISPER_AND_QWEN")
            self.assertEqual(binding["diarization_status"], "NOT_APPLICABLE_NO_VOICE_SEGMENTS")
            self.assertEqual(binding["diarization_segments"], 0)

    def test_run_asr_preserves_qwen_text_as_divergence_without_voice(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sample.wav"
            source.write_bytes(b"RIFF" + b"\0" * 64)
            fake = root / "fake_asr.py"
            fake.write_text(
                "import argparse,json,pathlib\n"
                "class Pipeline:\n"
                " def __init__(self,source,root): self.root=pathlib.Path(root)\n"
                " def run_qwen(self): return {'results': [{'text':'MODEL_ONLY'}]}\n"
                " def run_diarization(self):\n"
                "  p=self.root/'logs';p.mkdir(parents=True,exist_ok=True);(p/'3dspeaker_diarization.log').write_text('ValueError: max() iterable argument is empty',encoding='utf-8');raise RuntimeError('empty')\n"
                "if __name__=='__main__':\n"
                " p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args();d=pathlib.Path(a.output)/'state';d.mkdir(parents=True);(d/'pipeline_result.json').write_text(json.dumps({'status':'failed','error':'Whisper mojibake-zero-segments'}),encoding='utf-8');raise SystemExit(2)\n",
                encoding="utf-8",
            )
            args = Args()
            args.asr_script = str(fake)
            args.asr_python = sys.executable
            output = root / "out"
            mod.run_asr(source, output, "audio", args)
            binding = json.loads((output / "source_binding.json").read_text(encoding="utf-8"))
            self.assertEqual(binding["asr_status"], "DUAL_ASR_DIVERGENCE_QWEN_TEXT_WITHOUT_VOICE")
            self.assertEqual(binding["qwen_nonempty_chunks"], 1)
            self.assertEqual(binding["diarization_status"], "NOT_APPLICABLE_NO_VOICE_SEGMENTS")

    def test_parse_sevenzip_slt_excludes_archive_header_and_keeps_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "sample.rar"
            archive.write_bytes(b"synthetic")
            listing = f"""
Path = {archive.resolve()}
Type = Rar

Path = folder/file.txt
Size = 9
Packed Size = 7
Folder = -
Encrypted = -
Attributes = A

Path = folder
Size = 0
Packed Size = 0
Folder = +
Encrypted = -
Attributes = D
"""
            rows = mod.parse_sevenzip_slt(listing, archive)
            self.assertEqual([row["Path"] for row in rows], ["folder/file.txt", "folder"])

    def test_prepare_run_verify_text_and_office_without_stdout_content(self) -> None:
        secret = "CUSTOMER_SECRET_TEXT_NEVER_PRINT"
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            api = session_dir(case) / "api"
            (api / "record.json").write_text(json.dumps({"company_id": "123456789", "contact_id": "C-9", "sender": "hidden@example.test", "body": secret}), encoding="utf-8")
            office = session_dir(case) / "document.docx"
            with zipfile.ZipFile(office, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", f'<w:document xmlns:w="urn:test"><w:p><w:t>{secret}</w:t></w:p></w:document>')
            manual_dir = case / "evidence" / "manual_trade" / "manual_trade_test"
            (manual_dir / "states").mkdir()
            (manual_dir / "states" / "trade.json").write_text(json.dumps({"trade_id": "T-1", "body": "MANUAL_ONLY_TOKEN"}), encoding="utf-8")
            write_manual_manifest(case, ["states/trade.json"])
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                prepared = mod.prepare(case, "123456789")
                result = mod.run(case, "123456789", Args())
                verified, code = mod.verify(case, "123456789")
            self.assertNotIn(secret, stream.getvalue())
            self.assertEqual(prepared["automatic_files"], 3)
            self.assertEqual(prepared["manual_trade_files"], 1)
            self.assertEqual(result["remaining"], 0)
            self.assertEqual(code, 0)
            self.assertEqual(verified["status"], "PASS")
            db = sqlite3.connect(extract_dir(case) / "full_text.sqlite3")
            try:
                self.assertGreater(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM full_text WHERE full_text MATCH 'CUSTOMER_SECRET_TEXT_NEVER_PRINT'").fetchone()[0], 0)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM full_text WHERE full_text MATCH 'MANUAL_ONLY_TOKEN'").fetchone()[0], 0)
            finally:
                db.close()
            relations = (extract_dir(case) / "relationship_graph.jsonl").read_text(encoding="utf-8")
            self.assertIn("contact", relations)
            self.assertIn("trade_record", relations)
            for name in ("file_inventory.csv", "evidence_index.csv", "media_inventory.csv", "coverage.csv", "verification.json"):
                self.assertTrue((extract_dir(case) / name).is_file())

    def test_media_prepare_only_schedules_models_and_does_not_start_them(self) -> None:
        secret = "MEDIA_SECRET_NOT_ON_CONSOLE"
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            media = session_dir(case) / f"{secret}.mp3"
            media.write_bytes(b"ID3" + b"\0" * 64)
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = mod.prepare(case, "123456789")
            self.assertNotIn(secret, stream.getvalue())
            self.assertEqual(result["media_files"], 1)
            db = sqlite3.connect(extract_dir(case) / "state.sqlite3")
            try:
                kinds = {row[0] for row in db.execute("SELECT task_kind FROM tasks")}
                states = {row[0] for row in db.execute("SELECT state FROM tasks")}
            finally:
                db.close()
            self.assertTrue({"ffprobe_media", "asr_dual_diarization"}.issubset(kinds))
            self.assertEqual(states, {"pending"})

    def test_identity_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            with self.assertRaisesRegex(RuntimeError, "IDENTITY_MISMATCH"):
                mod.prepare(case, "999")

    def test_prepare_refuses_running_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            state_path = case / "work" / "capture_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["running"] = True
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "AUTO_CAPTURE_RUNNING"):
                mod.prepare(case, "123456789")

    def test_only_authoritative_session_and_extended_text_types_are_processed(self) -> None:
        marker = "TEXTUAL_BIN_MARKER_42"
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            old_session = case / "raw" / "sessions" / "capture_old"
            old_session.mkdir(parents=True)
            (old_session / "must_not_be_seen.json").write_text('{"secret":"OLD_SESSION_TOKEN"}', encoding="utf-8")
            current = session_dir(case)
            samples = {
                "events.jsonl": '{"activity_id":"E-1","body":"JSONL_TOKEN"}\n',
                "page.mhtml": "MHTML_TOKEN",
                "page.rawhtml": "<html><body>RAWHTML_TOKEN</body></html>",
                "app.js": "const token = 'JS_TOKEN';",
                "style.css": ".token::after { content: 'CSS_TOKEN'; }",
                "icon.svg": "<svg xmlns='http://www.w3.org/2000/svg'><text>SVG_TOKEN</text></svg>",
                "payload.bin": marker,
            }
            for name, body in samples.items():
                (current / name).write_text(body, encoding="utf-8")
            prepared = mod.prepare(case, "123456789")
            result = mod.run(case, "123456789", Args())
            self.assertEqual(result["remaining"], 0)
            self.assertEqual(prepared["automatic_files"], 1 + len(samples))
            db = sqlite3.connect(extract_dir(case) / "state.sqlite3")
            try:
                paths = {row[0] for row in db.execute("SELECT source_rel FROM files WHERE active=1")}
            finally:
                db.close()
            self.assertFalse(any("capture_old" in path for path in paths))
            fts = sqlite3.connect(extract_dir(case) / "full_text.sqlite3")
            try:
                self.assertGreater(fts.execute("SELECT COUNT(*) FROM full_text WHERE full_text MATCH ?", (marker,)).fetchone()[0], 0)
            finally:
                fts.close()
            relations = (extract_dir(case) / "relationship_graph.jsonl").read_text(encoding="utf-8")
            self.assertIn("events.jsonl", relations)
            self.assertIn('"entity_type":"activity"', relations)

    def test_manual_source_gap_is_explicitly_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            (session_dir(case) / "note.txt").write_text("synthetic", encoding="utf-8")
            write_manual_manifest(
                case,
                [],
                status="PASS_WITH_SOURCE_GAPS",
                gaps=[{"code": "source_did_not_return", "detail": "synthetic gap"}],
            )
            mod.prepare(case, "123456789")
            mod.run(case, "123456789", Args())
            verified, code = mod.verify(case, "123456789")
            self.assertEqual(code, 3)
            self.assertEqual(verified["status"], "INCOMPLETE_SOURCE_GAPS")
            self.assertEqual(verified["manual_source_gaps"], 1)

    def test_automatic_external_source_gaps_are_counted_without_storing_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            (session_dir(case) / "note.txt").write_text("synthetic", encoding="utf-8")
            verification = case / "verification"
            verification.mkdir()
            (verification / "capture_reconciliation_latest.json").write_text(json.dumps({
                "schema": "synthetic", "status": "PASS_WITH_WARNINGS",
                "company_id": "123456789", "session_id": "capture_test",
                "checks": [{
                    "id": "external_passive_source_unavailable_after_retry", "status": "warning",
                    "actual": {"failure_rows": 6, "unique_urls": 2}, "evidence": [],
                }],
            }), encoding="utf-8")
            prepared = mod.prepare(case, "123456789")
            self.assertEqual(prepared["automatic_source_gaps"], 2)
            mod.run(case, "123456789", Args())
            verified, code = mod.verify(case, "123456789")
            self.assertEqual(code, 3)
            self.assertEqual(verified["automatic_source_gaps"], 2)

    def test_manual_artifact_hash_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            manual_dir = case / "evidence" / "manual_trade" / "manual_trade_test"
            artifact = manual_dir / "state.json"
            artifact.write_text('{"value":"one"}', encoding="utf-8")
            write_manual_manifest(case, ["state.json"])
            mod.prepare(case, "123456789")
            artifact.write_text('{"value":"two"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "MANUAL_TRADE_ARTIFACT_HASH_MISMATCH"):
                mod.verify(case, "123456789")

    def test_interrupted_task_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            (session_dir(case) / "note.txt").write_text("synthetic", encoding="utf-8")
            mod.prepare(case, "123456789")
            state = extract_dir(case) / "state.sqlite3"
            db = sqlite3.connect(state)
            try:
                db.execute("UPDATE tasks SET state='running'")
                db.commit()
            finally:
                db.close()
            result = mod.run(case, "123456789", Args())
            self.assertEqual(result["remaining"], 0)

    def test_processing_scope_v2_adds_historical_mail_objects_and_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            write_mail_manifest(case)
            result = mod.prepare(case, "123456789")
            self.assertEqual(result["historical_mail_related_files"], 1)
            self.assertEqual(result["mail_source_gaps"], 2)
            scope_path = extract_dir(case) / "processing_scope.v2.json"
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
            self.assertEqual(scope["schema"], "okki.processing_scope.v2")
            self.assertEqual(scope["status"], "PASS_WITH_SOURCE_GAPS")
            self.assertGreater(scope["counts"]["occurrences"], scope["counts"]["content_objects"])
            self.assertFalse((case / "derived" / "full_extract").exists())
            db = sqlite3.connect(extract_dir(case) / "state.sqlite3")
            try:
                db.row_factory = sqlite3.Row
                mail_related = db.execute("SELECT COUNT(*) FROM occurrences WHERE role='mail_related_resource' AND active=1").fetchone()[0]
                objects = db.execute("SELECT COUNT(*) FROM content_objects WHERE active=1").fetchone()[0]
                distinct_tasks = db.execute("SELECT COUNT(DISTINCT content_sha256 || ':' || task_kind) FROM tasks WHERE active=1").fetchone()[0]
                tasks = db.execute("SELECT COUNT(*) FROM tasks WHERE active=1").fetchone()[0]
            finally:
                db.close()
            self.assertEqual(mail_related, 2)
            self.assertEqual(tasks, distinct_tasks)
            self.assertEqual(objects, result["content_objects"])

    def test_prepare_reuses_completed_legacy_results_without_rewriting_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            (session_dir(case) / "note.txt").write_text("legacy synthetic", encoding="utf-8")
            old_args = Args()
            old_args.output_dir = Path("derived/full_extract")
            mod.prepare(case, "123456789", old_args.output_dir)
            old_run = mod.run(case, "123456789", old_args)
            self.assertEqual(old_run["remaining"], 0)
            old_state = case / "derived" / "full_extract" / "state.sqlite3"
            old_hash = mod.sha256_file(old_state)
            result = mod.prepare(case, "123456789")
            self.assertGreater(result["reused_legacy_tasks"], 0)
            self.assertEqual(mod.sha256_file(old_state), old_hash)
            db = sqlite3.connect(extract_dir(case) / "state.sqlite3")
            try:
                reused = db.execute("SELECT COUNT(*) FROM tasks WHERE active=1 AND state='completed' AND output_rel LIKE 'derived/full_extract/%'").fetchone()[0]
            finally:
                db.close()
            self.assertGreater(reused, 0)

    def test_all_supported_format_families_receive_terminal_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            current = session_dir(case)
            (current / "sample.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
            with zipfile.ZipFile(current / "sample.docx", "w") as archive:
                archive.writestr("word/document.xml", "<w:document xmlns:w='urn:test'><w:t>synthetic</w:t></w:document>")
            (current / "sample.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64)
            with zipfile.ZipFile(current / "sample.zip", "w") as archive:
                archive.writestr("one.txt", "synthetic")
            (current / "sample.eps").write_text("%!PS-Adobe-3.0 EPSF-3.0\n", encoding="ascii")
            (current / "sample.txt").write_text("synthetic", encoding="utf-8")
            (current / "sample.bin").write_bytes(b"\0\x01\x02\x03")
            (current / "sample.mp4").write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 32)
            mod.prepare(case, "123456789")
            db = sqlite3.connect(extract_dir(case) / "state.sqlite3")
            try:
                kinds = {row[0] for row in db.execute("SELECT task_kind FROM tasks WHERE active=1")}
            finally:
                db.close()
            self.assertTrue({
                "extract_pdf_native", "ocr_pdf_all_pages", "extract_office_xml",
                "extract_legacy_office", "inspect_archive", "extract_eps_text",
                "ocr_eps_rendered", "extract_text", "classify_binary", "ffprobe_media",
                "asr_dual_diarization", "video_frame_ocr",
            }.issubset(kinds))

    def test_zip_members_are_safely_extracted_recursively_and_related_to_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            current = session_dir(case)
            nested_bytes = io.BytesIO()
            with zipfile.ZipFile(nested_bytes, "w") as nested:
                nested.writestr("deep.txt", "deep synthetic")
            outer = current / "outer.zip"
            with zipfile.ZipFile(outer, "w") as archive:
                archive.writestr("good.txt", "good synthetic")
                archive.writestr("nested.zip", nested_bytes.getvalue())
                archive.writestr("../escape.txt", "must not escape")
                link = zipfile.ZipInfo("unsafe-link")
                link.create_system = 3
                link.external_attr = (0o120777 << 16)
                archive.writestr(link, "good.txt")
            result = mod.prepare(case, "123456789")
            self.assertGreaterEqual(result["container_source_gaps"], 2)
            self.assertFalse((case.parent / "escape.txt").exists())
            db = sqlite3.connect(extract_dir(case) / "state.sqlite3")
            try:
                roles = db.execute("SELECT role,COUNT(*) FROM occurrences WHERE source_scope='archive_member' AND active=1 GROUP BY role").fetchall()
                gap_codes = {row[0] for row in db.execute("SELECT error_code FROM source_gaps WHERE source_scope='container_processing' AND active=1")}
                member_tasks = db.execute("""SELECT COUNT(*) FROM tasks t JOIN files f ON f.source_rel=t.source_rel
                    WHERE f.source_scope='archive_member' AND t.task_kind='extract_text' AND t.active=1""").fetchone()[0]
            finally:
                db.close()
            self.assertTrue(roles)
            self.assertGreaterEqual(member_tasks, 2)
            self.assertIn("ARCHIVE_MEMBER_PATH_TRAVERSAL_BLOCKED", gap_codes)
            self.assertIn("ARCHIVE_MEMBER_SYMLINK_BLOCKED", gap_codes)

    def test_pdf_empty_password_is_accessible_but_nonempty_password_is_source_gap(self) -> None:
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            empty = root / "empty.pdf"
            locked = root / "locked.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.encrypt("")
            with empty.open("wb") as handle:
                writer.write(handle)
            result = mod.extract_pdf(empty)
            self.assertEqual(result["encryption_state"], "PDF_ENCRYPTED_EMPTY_PASSWORD_ACCESSIBLE")
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.encrypt("synthetic-password")
            with locked.open("wb") as handle:
                writer.write(handle)
            with self.assertRaisesRegex(mod.SourceGap, "PDF_PASSWORD_REQUIRED"):
                mod.extract_pdf(locked)

    def test_locked_pdf_tasks_finish_as_source_gaps_without_password_attempts(self) -> None:
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            locked = session_dir(case) / "locked.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.encrypt("synthetic-password")
            with locked.open("wb") as handle:
                writer.write(handle)
            mod.prepare(case, "123456789")
            result = mod.run(case, "123456789", Args())
            self.assertEqual(result["remaining"], 0)
            db = sqlite3.connect(extract_dir(case) / "state.sqlite3")
            try:
                states = db.execute("SELECT task_kind,state,error_code FROM tasks WHERE source_rel LIKE '%locked.pdf' ORDER BY task_kind").fetchall()
            finally:
                db.close()
            self.assertEqual({row[1] for row in states}, {"source_gap"})
            self.assertEqual({row[2] for row in states}, {"PDF_PASSWORD_REQUIRED"})

    def test_cli_process_pool_smoke_keeps_source_text_off_console(self) -> None:
        secret = "PROCESS_POOL_SECRET_NEVER_PRINT"
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            (session_dir(case) / "one.txt").write_text(secret, encoding="utf-8")
            (session_dir(case) / "two.txt").write_text("synthetic two", encoding="utf-8")
            common = [str(MODULE_PATH), "--case-root", str(case), "--company-id", "123456789"]
            prepared = subprocess.run(
                [sys.executable, common[0], "prepare", *common[1:]],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
            completed = subprocess.run(
                [sys.executable, common[0], "run", *common[1:], "--parse-workers", "2", "--ocr-workers", "1"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertNotIn(secret, prepared.stdout + prepared.stderr + completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout.strip())
            self.assertEqual(payload["remaining"], 0)

    @unittest.skipUnless(shutil.which("tesseract"), "tesseract not installed")
    def test_image_ocr_is_local_indexed_and_content_quiet(self) -> None:
        from PIL import Image, ImageDraw, ImageFont

        secret = "LOCALONLYOCR123"
        with tempfile.TemporaryDirectory() as temp:
            case = make_case(Path(temp))
            image = Image.new("RGB", (800, 180), "white")
            font_path = Path(r"C:\Windows\Fonts\arial.ttf")
            font = ImageFont.truetype(str(font_path), 64) if font_path.is_file() else ImageFont.load_default()
            ImageDraw.Draw(image).text((20, 40), secret, fill="black", font=font)
            image.save(session_dir(case) / "synthetic.png")
            args = Args()
            args.ocr_languages = "eng"
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                mod.prepare(case, "123456789")
                result = mod.run(case, "123456789", args)
                verified, code = mod.verify(case, "123456789")
            self.assertNotIn(secret, stream.getvalue())
            self.assertEqual(result["remaining"], 0)
            self.assertEqual(code, 0)
            self.assertEqual(verified["status"], "PASS")
            db = sqlite3.connect(extract_dir(case) / "full_text.sqlite3")
            try:
                self.assertGreater(db.execute("SELECT COUNT(*) FROM full_text WHERE full_text MATCH ?", (secret,)).fetchone()[0], 0)
            finally:
                db.close()
            state = sqlite3.connect(extract_dir(case) / "state.sqlite3")
            try:
                output_rel = state.execute("SELECT output_rel FROM tasks WHERE task_kind='ocr_image' AND active=1").fetchone()[0]
            finally:
                state.close()
            index = json.loads((case / output_rel / "index.json").read_text(encoding="utf-8"))
            boxes = json.loads((case / output_rel / index["boxes_rel"]).read_text(encoding="utf-8"))
            self.assertFalse(index["blank"])
            self.assertGreater(index["box_count"], 0)
            self.assertEqual(index["box_count"], len(boxes["boxes"]))
            self.assertIn("mean_confidence", index)


if __name__ == "__main__":
    unittest.main()

