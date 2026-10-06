# EN: Public-release regression tests use metadata and synthetic paths only.
# 中文：开源发布回归测试只使用元数据与合成路径。
from __future__ import annotations

import argparse
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_completion_v2_delivery as completion
import build_relation_timeline_v2 as relationship
import build_unredacted_local_package as package
import collect_customer_pi_archive as invoice
import extract_pi_tables_local as tables
import single_customer_full_extract as extract


class PublicPortabilityTests(unittest.TestCase):
    def test_asr_without_configuration_fails_before_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(extract, "DEFAULT_ASR", None):
            root = Path(temporary)
            args = argparse.Namespace(asr_script=None)
            with self.assertRaisesRegex(RuntimeError, "LOCAL_ASR_PIPELINE_NOT_CONFIGURED"):
                extract.run_asr(root / "synthetic.wav", root / "output", "audio", args)
            self.assertFalse((root / "output").exists())

    def test_language_pack_without_configuration_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(extract, "DEFAULT_TESSDATA", None):
            root = Path(temporary)
            args = argparse.Namespace(tessdata_dir=None)
            extract.stage_tessdata(root, root / "output", args)
            self.assertIsNone(args.tessdata_dir)
            self.assertFalse((root / "output").exists())

    def test_duckdb_defaults_are_conservative(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            threads, memory = package.duckdb_settings()
        self.assertGreaterEqual(threads, 1)
        self.assertLessEqual(threads, 8)
        self.assertEqual(memory, "4GB")

    def test_duckdb_explicit_settings_validate_before_sql(self) -> None:
        with patch.dict(os.environ, {"CRM_DUCKDB_THREADS": "1", "CRM_DUCKDB_MEMORY_LIMIT": "256MB"}, clear=True):
            self.assertEqual(package.duckdb_settings(), (1, "256MB"))
        with patch.dict(os.environ, {"CRM_DUCKDB_THREADS": "0"}, clear=True):
            with self.assertRaisesRegex(package.PackageError, "DUCKDB_THREADS_INVALID"):
                package.duckdb_settings()
        with patch.dict(os.environ, {"CRM_DUCKDB_THREADS": "1", "CRM_DUCKDB_MEMORY_LIMIT": "4GB'; select"}, clear=True):
            with self.assertRaisesRegex(package.PackageError, "DUCKDB_MEMORY_LIMIT_INVALID"):
                package.duckdb_settings()

    def test_relationship_api_requires_explicit_expectations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(relationship.BuildError, "EXPLICIT_ATTACHMENT_EXPECTATIONS_REQUIRED"):
                relationship.build_package(root, "123456789", root / "synthetic-package")
            self.assertFalse((root / "derived").exists())

    def test_expected_counts_are_required_not_production_defaults(self) -> None:
        for cli, prefix in ((completion.parser(), ["prepare"]), (relationship.parser(), ["build"]),
                            (invoice.parser(), ["build"]), (tables.parser(), [])):
            # EN: argparse must reject an empty invocation before reading any evidence.
            # 中文：argparse 必须先拒绝缺失参数，再读取任何证据。
            with self.subTest(cli=cli.prog, prefix=prefix), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.parse_args(prefix)

    def test_source_interpreter_discovery_has_no_workstation_fallback(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            candidates = relationship.python_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertNotIn("codex-runtimes", candidates[0])

    def test_official_sqlite_backports_are_accepted(self) -> None:
        for version in ((3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 1)):
            with self.subTest(version=version):
                package.ensure_sqlite_version(version)
        for version in ((3, 44, 5), (3, 45, 3), (3, 50, 6), (3, 51, 2)):
            with self.subTest(version=version):
                with self.assertRaisesRegex(package.PackageError, "SQLITE_VERSION_TOO_OLD"):
                    package.ensure_sqlite_version(version)

    def test_current_sqlite_required_capabilities(self) -> None:
        package.ensure_sqlite_capabilities()


if __name__ == "__main__":
    unittest.main()
