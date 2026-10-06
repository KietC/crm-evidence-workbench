# EN: Synthetic fixtures only; never read real captures or private customer directories.
# 中文：仅使用合成测试夹具，不读取真实采集或私有客户目录。
from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from pathlib import Path

from verify.verify_case import write_text_hidden_safe


class HiddenSafeWriteTests(unittest.TestCase):
    def test_replaces_hidden_file_and_preserves_hidden_attribute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            target.write_text("old", encoding="utf-8")
            if os.name == "nt":
                self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(str(target), 0x2))
            write_text_hidden_safe(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            if os.name == "nt":
                attributes = ctypes.windll.kernel32.GetFileAttributesW(str(target))
                self.assertNotEqual(attributes, 0xFFFFFFFF)
                self.assertTrue(attributes & 0x2)


if __name__ == "__main__":
    unittest.main()
