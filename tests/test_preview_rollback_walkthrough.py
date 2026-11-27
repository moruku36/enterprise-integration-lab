"""Run the documented no-server rollback walkthrough from an unrelated folder."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

WALKTHROUGH = Path(__file__).resolve().parents[1] / "examples" / "preview_rollback_walkthrough.py"


class PreviewRollbackWalkthrough(unittest.TestCase):
    def test_first_stage_and_replacement_are_atomic_across_processes(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", str(WALKTHROUGH)], cwd=directory,
                capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(list(Path(directory).iterdir()), [])
        report = json.loads(result.stdout)
        expected = {"independent_reader_saw_previous_state": True, "previous_record_count": 0,
                    "restart_preserved_state_and_bytes": True, "clean_retry_record_count": 6}
        self.assertEqual(report, {
            "mode": "offline_synthetic_precommit_rollback",
            "initial_stage": expected,
            "replace_prior_preview": {**expected, "previous_record_count": 6},
        })
