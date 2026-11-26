"""Read-only operator queue checks using real, disposable SQLite stores."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import import_job as job
import staff_requests
from worker import SourcePage


class OperatorQueue(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.root = Path(tmp) / "state"
        self.enterContext(patch.object(job, "STATE_ROOT", self.root))
        for name in ("fixture_secret", "request_token", "fetch_page"):
            self.enterContext(patch.object(job, name, side_effect=AssertionError("queue must stay offline")))

    def inspect(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["import_job", *args, "queue"]), redirect_stdout(out), redirect_stderr(err):
            code = job.main()
        self.assertEqual((code, err.getvalue()), (0, ""))
        return json.loads(out.getvalue())

    def test_before_first_request_queue_is_empty_without_creating_a_store(self):
        self.assertEqual(self.inspect(), {"queued": [], "blocking_run": None})
        self.assertFalse(self.root.exists())
        with patch("sys.argv", ["import_job", "--store", "demo", "queue"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                job.main()
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.root.exists())

    def test_two_queued_ids_are_safe_readonly_and_not_automatically_claimed(self):
        a, _ = staff_requests.accept("fixture-subject-one", "fixture-key-only-one")
        b, _ = staff_requests.accept("fixture-subject-two", "fixture-key-only-two")
        path, lease = job.store_paths(job.STAFF_STORE, create=False)
        db_bytes = path.read_bytes()
        self.assertFalse(lease.exists())
        self.assertEqual(self.inspect("--store", "staff"), {
            "queued": sorted((a["run_id"], b["run_id"])), "blocking_run": None,
        })
        self.assertEqual(path.read_bytes(), db_bytes)
        self.assertFalse(lease.exists())

    def test_observe_running_and_retryable_failure_even_while_worker_owns_lease(self):
        a, _ = staff_requests.accept("synthetic-owner", "fixture-first-request")
        b, _ = staff_requests.accept("synthetic-owner", "fixture-second-request")
        run_id = a["run_id"]
        path, lease = job.store_paths(job.STAFF_STORE, create=False)
        with job.import_lease(lease), closing(job.open_state(path)) as db:
            with job.transaction(db):
                run = job.claim_staff_request(db, run_id)
            job.apply_page(db, run, SourcePage(
                (("SOURCE-001", "Fixture 1"), ("SOURCE-002", "Fixture 2")), "after-002",
            ))
            initial = path.read_bytes()
            self.assertEqual(self.inspect(), {"queued": [b["run_id"]], "blocking_run": {
                "run_id": run_id, "status": "running", "committed_pages": 1, "next_action": "check_worker",
            }})
            self.assertEqual(path.read_bytes(), initial)
            db.execute("UPDATE import_runs SET status = 'failed', failure = ?", ("source_http_503",))
            initial = path.read_bytes()
            self.assertEqual(self.inspect(), {"queued": [b["run_id"]], "blocking_run": {
                "run_id": run_id, "status": "failed", "committed_pages": 1,
                "next_action": "resume_after_recovery",
            }})
            self.assertEqual(path.read_bytes(), initial)
            db.execute("UPDATE import_runs SET failure = ?", ("invalid_snapshot",))
            self.assertEqual(self.inspect()["blocking_run"]["next_action"], "inspect_failure")
            self.assertEqual(job.read_run(db, run_id).failure, "invalid_snapshot")

    def test_existing_but_invalid_store_is_not_reported_as_empty(self):
        self.root.mkdir()
        (self.root / "staff.sqlite3").mkdir()
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["import_job", "queue"]), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(job.main(), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("not a regular file", err.getvalue())
