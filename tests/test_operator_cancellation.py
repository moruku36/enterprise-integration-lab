"""Operator cancellation works without a source, including concurrent start."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

import import_job as job
import staff_requests
from worker import LabError, SourcePage


class OperatorCancellation(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(job, "STATE_ROOT", Path(tmp) / "state"))
        for name in ("fixture_secret", "request_token", "fetch_page"):
            self.enterContext(patch.object(job, name, side_effect=AssertionError("must stay offline")))

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["import_job", *args]), redirect_stdout(out), redirect_stderr(err):
            code = job.main()
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_cli_cancel_is_durable_and_idempotent_without_start_or_resume(self):
        pending, _ = staff_requests.accept("fixture-staff", "cancel-request-0001")
        run_id = pending["run_id"]
        path, lease = job.store_paths(job.STAFF_STORE, create=False)
        first = self.call("cancel-request", run_id)
        self.assertEqual((first[0], first[1]["status"], first[2]), (0, "cancelled", ""))
        self.assertFalse(lease.exists())
        db_bytes = path.read_bytes()
        self.assertEqual(self.call("cancel-request", run_id), first)
        self.assertEqual(self.call("--store", "staff", "status", run_id), first)
        self.assertEqual(self.call("queue")[1], {"queued": [], "blocking_run": None})
        self.assertEqual(path.read_bytes(), db_bytes)
        self.assertFalse(lease.exists())
        self.assertIn("was cancelled", self.call("run-request", run_id)[2])
        self.assertIn("was cancelled", self.call("--store", "staff", "resume", run_id)[2])
        with closing(job.open_state(path, read_only=True)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM staff_requests").fetchone()[0], 1)
            self.assertEqual(tuple(db.execute("SELECT status, failure FROM import_runs").fetchone()), (
                "failed", job.CANCELLED_BEFORE_START,
            ))
        replacement, _ = staff_requests.accept("fixture-staff", "replacement-request-01")
        with closing(job.open_state(path)) as db, job.transaction(db):
            self.assertEqual(job.claim_staff_request(db, replacement["run_id"]).status, "running")

    def test_cancel_does_not_create_an_unknown_store_or_request(self):
        missing = "0" * 32
        self.assertEqual(self.call("cancel-request", missing)[0], 1)
        self.assertFalse(job.STATE_ROOT.exists())
        staff_requests.accept("fixture-staff", "cancel-request-0002")
        path, lease = job.store_paths(job.STAFF_STORE, create=False)
        db_bytes = path.read_bytes()
        code, result, err = self.call("cancel-request", missing)
        self.assertEqual((code, result), (1, None))
        self.assertIn("request not found", err)
        self.assertEqual(path.read_bytes(), db_bytes)
        self.assertFalse(lease.exists())

    def test_started_work_is_unchanged_and_other_queue_items_can_cancel(self):
        started, _ = staff_requests.accept("fixture-staff", "cancel-request-0003")
        pending, _ = staff_requests.accept("fixture-staff", "cancel-request-0004")
        path, lease = job.store_paths(job.STAFF_STORE, create=False)
        with job.import_lease(lease), closing(job.open_state(path)) as db:
            with job.transaction(db):
                state = job.claim_staff_request(db, started["run_id"])
            self.assertIn("only a queued request", self.call("cancel-request", started["run_id"])[2])
            self.assertEqual(job.read_run(db, started["run_id"]), state)
            self.assertEqual(self.call("cancel-request", pending["run_id"])[1]["status"], "cancelled")
            self.assertEqual(job.read_run(db, started["run_id"]), state)
            queue = self.call("queue")[1]
            self.assertEqual(queue["queued"], [])
            self.assertEqual(queue["blocking_run"]["run_id"], started["run_id"])
            db.execute("UPDATE import_runs SET status = 'failed', failure = 'source_http_503' "
                       "WHERE run_id = ?", (state.run_id,))
            self.assertIn("only a queued request", self.call("cancel-request", state.run_id)[2])
            self.assertEqual(job.read_run(db, state.run_id).failure, "source_http_503")
            state = job.resume_run(db, state.run_id)
            for n in range(3):
                state = job.apply_page(db, state, SourcePage(
                    ((f"SOURCE-{2*n+1:03}", "Fixture A"), (f"SOURCE-{2*n+2:03}", "Fixture B")),
                    f"after-{2*n+2:03}" if n < 2 else None,
                ))
            self.assertEqual(state.status, "complete")
            self.assertIn("only a queued request", self.call("cancel-request", state.run_id)[2])
            self.assertEqual(job.read_run(db, state.run_id), state)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 6)

    def test_claim_and_cancel_race_has_only_one_winner(self):
        pending, _ = staff_requests.accept("fixture-staff", "cancel-request-0005")
        run_id = pending["run_id"]
        path, _ = job.store_paths(job.STAFF_STORE, create=False)
        barrier = Barrier(2, timeout=5)

        def attempt(action):
            with closing(job.open_state(path)) as db:
                barrier.wait()
                try:
                    if action == "start":
                        with job.transaction(db):
                            return job.claim_staff_request(db, run_id).status
                    return job.cancel_staff_request(db, run_id).status
                except LabError as error:
                    return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcome = tuple(pool.map(attempt, ["start", "cancel"]))
        self.assertIn(outcome, [("running", "request_already_started"), ("request_cancelled", "cancelled")])
        with closing(job.open_state(path, read_only=True)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0], 1)
            self.assertEqual(job.read_run(db, run_id).status, "running" if outcome[0] == "running" else "cancelled")

    def test_corrupt_cancellation_with_checkpoint_is_not_silently_ignored(self):
        pending, _ = staff_requests.accept("fixture-staff", "cancel-request-0006")
        run_id = pending["run_id"]
        path, _ = job.store_paths(job.STAFF_STORE, create=False)
        self.assertEqual(self.call("cancel-request", run_id)[0], 0)
        with closing(job.open_state(path)) as db:
            db.execute("UPDATE import_runs SET changed_records = 1 WHERE run_id = ?", (run_id,))
        for args in [("queue",), ("--store", "staff", "status", run_id), ("run-request", run_id)]:
            with self.subTest(args=args):
                code, result, err = self.call(*args)
                self.assertEqual((code, result), (1, None))
                self.assertIn("checkpoint is inconsistent", err)
