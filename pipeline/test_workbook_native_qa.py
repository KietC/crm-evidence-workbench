"""Synthetic native-QA contract tests; never launch Office or read real cases.

仅用合成文件验证原生验收契约，不启动 Office、不读取真实案例。
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from build_completion_v2_delivery import DeliveryError, validate_workbook_native_qa


class NativeWorkbookQATests(unittest.TestCase):
    def fixture(self, stage: Path) -> tuple[Path, dict]:
        qa_dir = stage / "qa"
        qa_dir.mkdir()
        workbook = stage / "synthetic.xlsx"
        workbook.write_bytes(b"synthetic workbook bytes")
        structural = qa_dir / "workbook_v2_verification.json"
        structural.write_text('{"controls":{"native_office_open_verified":false}}', encoding="utf-8")
        record = {
            "schema": "evidence_trail.workbook_native_qa.v1", "company_id": "123456789", "status": "PASS",
            "opened_read_only": True, "repair_prompt_seen": False, "all_sheets_visual_inspection_pass": True,
            "formula_errors": 0, "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "structural_receipt_sha256": hashlib.sha256(structural.read_bytes()).hexdigest(),
        }
        return workbook, record

    def test_svg_structural_receipt_without_independent_native_qa_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            workbook, _ = self.fixture(stage)
            with self.assertRaises(DeliveryError):
                validate_workbook_native_qa(stage, "123456789", workbook)

    def test_matching_synthetic_contract_is_accepted_without_claiming_actual_office_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            workbook, record = self.fixture(stage)
            (stage / "qa" / "workbook_native_qa.json").write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(validate_workbook_native_qa(stage, "123456789", workbook), record)

    def test_failed_flags_and_stale_hashes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            workbook, record = self.fixture(stage)
            for key, value in (("opened_read_only", False), ("repair_prompt_seen", True),
                               ("all_sheets_visual_inspection_pass", False), ("formula_errors", 1),
                               ("workbook_sha256", "0" * 64), ("structural_receipt_sha256", "0" * 64),
                               ("company_id", "999999999")):
                candidate = copy.deepcopy(record)
                candidate[key] = value
                (stage / "qa" / "workbook_native_qa.json").write_text(json.dumps(candidate), encoding="utf-8")
                with self.subTest(field=key), self.assertRaises(DeliveryError):
                    validate_workbook_native_qa(stage, "123456789", workbook)


if __name__ == "__main__":
    unittest.main()
