"""Check the local post-import reconciliation, without a source or identity service."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import import_job as job
import staff_requests
from worker import SourcePage

LAB_ROWS = tuple((f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7))


class OperatorReconciliation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch.object(job, "STATE_ROOT", self.tmp / ".import-lab"))
        for name in ("fixture_secret", "request_token", "fetch_page", "import_lease"):
            self.enterContext(patch.object(job, name, side_effect=AssertionError("read-only, offline")))

    def cli(self, run_id, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["import_job", *args, "reconcile", run_id]), redirect_stdout(out), redirect_stderr(err):
            code = job.main()
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def complete(self, db):
        run = job.start_run(db)
        for p in range(3):
            end = 2 * (p + 1)
            run = job.apply_page(db, run, SourcePage(LAB_ROWS[end - 2:end], f"after-{end:03d}" if end < 6 else None))
        return run

    def create_store(self):
        path, lease = job.store_paths("demo", create=True)
        db = self.enterContext(closing(job.open_state(path)))
        db.executescript(job.SCHEMA)
        return path, lease, db

    def test_cli_reconciles_exact_values_and_does_not_create_a_lease(self):
        path, lease, db = self.create_store()
        first, repeat = self.complete(db), self.complete(db)
        self.assertEqual((first.changed_records, repeat.changed_records), (6, 0))
        db_bytes = path.read_bytes()
        expected = {
            "run_id": repeat.run_id, "snapshot": "case-fixture-v1",
            "expected_records": 6, "stored_records": 6, "missing_records": 0, "unexpected_records": 0,
            "value_mismatches": 0, "checkpoint_ids_match": True, "matches": True,
        }

        # Exercise the same module entry point documented for learners in a fresh process.
        (self.tmp / "import_job.py").write_bytes(Path(job.__file__).read_bytes())
        process = subprocess.run([sys.executable, "-B", "-m", "import_job", "reconcile", repeat.run_id],
                                 cwd=self.tmp, env={"PYTHONPATH": str(Path(job.__file__).parent)},
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual((process.returncode, json.loads(process.stdout), process.stderr), (0, expected, ""))
        self.assertEqual(self.cli(repeat.run_id), (0, expected, ""))
        self.assertEqual(path.read_bytes(), db_bytes)
        self.assertFalse(lease.exists())

    def test_matching_count_cannot_hide_wrong_or_missing_records(self):
        path, lease, db = self.create_store()
        run = self.complete(db)
        db.execute("UPDATE records SET title = 'DIAGNOSTIC_VALUE' WHERE id = 'SOURCE-001'")
        db.execute("DELETE FROM records WHERE id = 'SOURCE-006'")
        db.execute("INSERT INTO records VALUES ('EXTRA_FIXTURE', 'DIAGNOSTIC_VALUE')")
        original = path.read_bytes()
        code, result, err = self.cli(run.run_id)
        self.assertEqual((code, err, result["matches"]), (1, "", False))
        self.assertEqual((result["expected_records"], result["stored_records"]), (6, 6))
        self.assertEqual((result["missing_records"], result["unexpected_records"], result["value_mismatches"]), (1, 1, 1))
        self.assertTrue(result["checkpoint_ids_match"])
        self.assertNotIn("DIAGNOSTIC_VALUE", json.dumps(result))
        self.assertNotIn("EXTRA_FIXTURE", json.dumps(result))
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(lease.exists())

    def test_wrong_checkpoint_ids_are_detected_despite_matching_destination(self):
        _, _, db = self.create_store()
        run = self.complete(db)
        ids = (*run.seen_ids[:-1], "wrong-synthetic-id")
        db.execute("UPDATE import_runs SET seen_ids = ? WHERE run_id = ?", (json.dumps(ids), run.run_id))
        code, result, err = self.cli(run.run_id)
        self.assertEqual((code, err, result["matches"], result["checkpoint_ids_match"]), (1, "", False, False))
        self.assertEqual(result["stored_records"], 6)
        self.assertEqual(result["missing_records"] + result["unexpected_records"] + result["value_mismatches"], 0)

    def test_unfinished_cancelled_and_unknown_never_claim_success(self):
        unknown = "f" * 32
        code, result, err = self.cli(unknown)
        self.assertEqual((code, result), (1, None))
        self.assertIn("unknown import store", err)
        self.assertFalse(job.STATE_ROOT.exists())
        path, lease, db = self.create_store()
        run = job.apply_page(db, job.start_run(db), SourcePage(LAB_ROWS[:2], "after-002"))
        code, result, err = self.cli(run.run_id)
        self.assertEqual((code, result), (1, None))
        self.assertIn("requires a complete run", err)
        db.execute("UPDATE import_runs SET status = 'failed', failure = 'source_http_503' WHERE run_id = ?", (run.run_id,))
        self.assertEqual(self.cli(run.run_id), (code, result, err))
        self.assertIn("not found", self.cli(unknown)[2])
        queued, _ = staff_requests.accept("synthetic-importer", "reconcile-queued-0001")
        staff_path, staff_lease = job.store_paths("staff", create=False)
        self.assertIn("not found", self.cli(queued["run_id"], "--store", "staff")[2])
        with closing(job.open_state(staff_path)) as staff_db:
            job.cancel_staff_request(staff_db, queued["run_id"])
        self.assertEqual(self.cli(queued["run_id"], "--store", "staff"), (code, result, err))
        self.assertFalse(lease.exists() or staff_lease.exists())
        self.assertTrue(path.exists())

    def test_reader_uses_one_snapshot_even_when_destination_changes(self):
        path, _, db = self.create_store()
        mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        self.assertEqual(mode, "wal")
        run = self.complete(db)
        original_read = job.read_run

        def read_and_then_change_state(reader, run_id):
            before = original_read(reader, run_id)
            # A separate writer can commit in WAL mode while the read transaction is open.
            db.execute("UPDATE records SET title = 'later synthetic value' WHERE id = 'SOURCE-001'")
            return before

        with patch.object(job, "read_run", side_effect=read_and_then_change_state):
            self.assertTrue(job.reconcile_import(path, run.run_id)["matches"])
        self.assertEqual(job.reconcile_import(path, run.run_id)["value_mismatches"], 1)
