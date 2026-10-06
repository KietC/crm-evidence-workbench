# EN: Synthetic fixtures only; never read real captures or private customer directories.
# 中文：仅使用合成测试夹具，不读取真实采集或私有客户目录。
from __future__ import annotations

import unittest

import collect_customer_pi_archive as pi


class PiArchiveUnitTest(unittest.TestCase):
    def test_structure_groups_require_real_invoice_fields(self) -> None:
        value = (
            "PROFORMA INVOICE PI No. A-100 Date 2026-01-02 Buyer Example "
            "Description Quantity Unit Price Amount Total USD Payment Terms FOB"
        )
        groups = {label for label, pattern in pi.STRUCTURE_PATTERNS.items() if pattern.search(value)}
        self.assertGreaterEqual(len(groups), 5)

    def test_bare_pi_is_not_a_complete_phrase(self) -> None:
        self.assertIsNone(__import__("re").search(r"pro\s*[- ]?forma\s+invoice", "Please review PI", flags=__import__("re").I))

    def test_stable_object_identity_ignores_signature_only(self) -> None:
        first = "https://cdn.xiaoman.cn/a/file.pdf?signature=one&x-oss-process=image/resize"
        second = "https://cdn.xiaoman.cn/a/file.pdf?signature=two&x-oss-process=image/resize"
        self.assertEqual(pi.source_url_identity_key(first), pi.source_url_identity_key(second))


if __name__ == "__main__":
    unittest.main()
