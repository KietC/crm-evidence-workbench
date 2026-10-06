"""Synthetic release-boundary regressions using throwaway local Git indexes.

使用临时本地 Git 索引验证发布边界，所有测试内容均为合成资料。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from privacy_scan import scan  # noqa: E402


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


class PrivacyScanTests(unittest.TestCase):
    def test_force_selected_generated_dependency_is_rejected(self) -> None:
        for folder in ("dist", "node_modules", ".venv", "Dist", "Node_Modules", ".VENV"):
            with tempfile.TemporaryDirectory(prefix="evidence-trail-scan-") as temporary:
                root = Path(temporary)
                git(root, "init", "-q")
                path = root / folder / "sample.json"
                path.parent.mkdir()
                path.write_text("{}", encoding="utf-8")
                git(root, "add", "-f", f"{folder}/sample.json")
                result = scan(root, tracked=True)
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(result["findings"][0]["rule"], "EXCLUDED_FILE_SELECTED")

    def test_staged_bytes_are_checked_even_when_working_tree_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evidence-trail-scan-") as temporary:
            root = Path(temporary)
            git(root, "init", "-q")
            path = root / "sample.py"
            synthetic_token = "gh" + "p_" + "A" * 36
            path.write_text(f'value = "{synthetic_token}"\n', encoding="utf-8")
            git(root, "add", "sample.py")
            path.write_text('value = "synthetic"\n', encoding="utf-8")
            result = scan(root, tracked=True)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["findings"][0]["rule"], "GITHUB_TOKEN")
            self.assertNotIn(synthetic_token, str(result))

    def test_private_directory_is_rejected_without_opening_body(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evidence-trail-scan-") as temporary:
            root = Path(temporary)
            git(root, "init", "-q")
            path = root / "cases" / "synthetic.json"
            path.parent.mkdir()
            path.write_text('{}', encoding="utf-8")
            git(root, "add", "cases/synthetic.json")
            path.unlink()
            result = scan(root, tracked=True)
            self.assertEqual(result["findings"][0]["rule"], "PRIVATE_FILE_SELECTED")
            self.assertEqual(result["selected_files"], 0)

    def test_session_filename_is_not_public_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evidence-trail-scan-") as temporary:
            root = Path(temporary)
            (root / "browser-cookies.json").write_text('{}', encoding="utf-8")
            result = scan(root)
            self.assertEqual(result["findings"][0]["rule"], "SESSION_FILE_SELECTED")

    def test_plain_source_has_deterministic_bound_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evidence-trail-scan-") as temporary:
            root = Path(temporary)
            (root / "sample.py").write_text('value = "synthetic"\n', encoding="utf-8")
            first = scan(root)
            second = scan(root)
            self.assertEqual(first["status"], "PASS_HEURISTIC_SOURCE_ONLY")
            self.assertEqual(first["files"], second["files"])

    def test_case_insensitive_private_and_session_selection(self) -> None:
        for filename, rule in (("Cases/sample.json", "PRIVATE_FILE_SELECTED"),
                               ("Runtime/sample.json", "PRIVATE_FILE_SELECTED"),
                               (".ENV.LOCAL", "ENV_FILE_SELECTED"),
                               ("Browser-Cookies.JSON", "SESSION_FILE_SELECTED")):
            with tempfile.TemporaryDirectory(prefix="evidence-trail-scan-") as temporary:
                root = Path(temporary)
                git(root, "init", "-q")
                path = root / filename
                path.parent.mkdir(exist_ok=True)
                path.write_text('{}', encoding="utf-8")
                git(root, "add", "-f", filename)
                result = scan(root, tracked=True)
                self.assertEqual(result["findings"][0]["rule"], rule)
                self.assertEqual(result["selected_files"], 0)

    def test_index_symlink_cannot_be_hidden_by_regular_working_tree_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evidence-trail-scan-") as temporary:
            root = Path(temporary)
            git(root, "init", "-q")
            path = root / "sample.py"
            path.write_text('value = "synthetic"\n', encoding="utf-8")
            git(root, "add", "sample.py")
            blob = subprocess.run(["git", "hash-object", "sample.py"], cwd=root,
                                  check=True, capture_output=True, text=True).stdout.strip()
            git(root, "update-index", "--cacheinfo", f"120000,{blob},sample.py")
            result = scan(root, tracked=True)
            self.assertEqual(result["findings"][0]["rule"], "SOURCE_INDEX_LINK_OR_CONFLICT")
            self.assertEqual(result["selected_files"], 0)


if __name__ == "__main__":
    unittest.main()
