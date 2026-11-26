"""Verify the runnable offline walkthrough from outside the repository."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

DEMO = Path(__file__).resolve().parents[1] / "examples" / "reconciliation_walkthrough.py"


class ReconciliationWalkthrough(unittest.TestCase):
    def test_offline_script_reports_each_stage_without_creating_working_directory_files(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", str(DEMO)], cwd=directory,
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(json.loads(result.stdout), {
            "mode": "offline_synthetic",
            "interrupted_before_reconciliation": {"pages": 1, "records": 2, "reconciliation_refused": True},
            "completed": {"same_run_id": True, "expected_records": 6, "stored_records": 6, "matches": True},
            "same_count_difference": {
                "status": "complete", "stored_records": 6, "missing_records": 1,
                "unexpected_records": 1, "value_mismatches": 1, "matches": False,
            },
            "reconciliation_left_stores_unchanged": True,
        })
