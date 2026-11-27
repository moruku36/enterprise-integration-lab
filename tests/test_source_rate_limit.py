"""Offline checks of the local source's bounded, durable manual retry gate."""
from contextlib import closing, redirect_stderr
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

import import_job
from import_job import SCHEMA, apply_page, claim_staff_request, inspect_staff_queue, open_state, read_run, record_failure
from import_job import resume_run, start_run, transaction
from worker import LabError, SourcePage, fetch_page, request_token


class RetryHint(unittest.TestCase):
    def test_only_one_bounded_source_delay_is_accepted(self):
        for values, expected in (
            ([], None), (["0"], None), (["1"], 1), (["300"], 300), (["301"], None),
            (["1", "2"], None), (["2, 3"], None), (["-1"], None), (["1.5"], None),
            (["9999999"], None), (["Fri, 31 Dec 1999 23:59:59 GMT"], None),
        ):
            with self.subTest(values=values):
                transport = httpx.MockTransport(
                    lambda request: httpx.Response(429, headers=[("Retry-After", value) for value in values]),
                )
                with httpx.Client(transport=transport) as client, self.assertRaises(LabError) as caught:
                    fetch_page(client, "synthetic", "after-002", 2)
                self.assertEqual(caught.exception.code, "source_http_429")
                self.assertEqual(caught.exception.retry_after_seconds, expected)

    def test_identity_throttle_is_not_a_source_resume_hint(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "8"}))
        with httpx.Client(transport=transport) as client, self.assertRaises(LabError) as caught:
            request_token(client, "synthetic")
        self.assertEqual(caught.exception.code, "token_http_429")
        self.assertIsNone(caught.exception.retry_after_seconds)


class DurableGate(unittest.TestCase):
    def test_staff_queue_and_resume_use_the_saved_deadline_after_reopening(self):
        directory = Path(self.enterContext(TemporaryDirectory())).resolve() / "state"
        self.enterContext(patch.object(import_job, "STATE_ROOT", directory))
        run_id = "a" * 32
        path, _ = import_job.store_paths("staff", create=True)
        rows = (("SOURCE-001", "Fictional source case 1"), ("SOURCE-002", "Fictional source case 2"))
        with closing(open_state(path)) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO staff_requests VALUES (?, ?, ?)", (run_id, "fictional", "fixture-request-key"))
            with transaction(db):
                run = claim_staff_request(db, run_id)
            apply_page(db, run, SourcePage(rows, "after-002"))
            with patch.object(import_job, "time", return_value=1000.25):
                record_failure(db, run_id, LabError("synthetic throttle", "source_http_429", retry_after_seconds=8))

        before = path.read_bytes()
        with patch.object(import_job, "time", return_value=1008.99):
            self.assertEqual(inspect_staff_queue()["blocking_run"], {
                "run_id": run_id, "status": "failed", "committed_pages": 1,
                "next_action": "wait_rate_limit", "retry_not_before_unix": 1009,
            })
            with closing(open_state(path)) as db:
                persisted = read_run(db, run_id)
                self.assertEqual(persisted.result()["retry_not_before_unix"], 1009)
                with self.assertRaises(LabError) as caught:
                    resume_run(db, run_id)
                self.assertEqual(caught.exception.code, "source_retry_wait")
                self.assertEqual(read_run(db, run_id), persisted)
        self.assertEqual(path.read_bytes(), before)

        with patch.object(import_job, "time", return_value=1009):
            self.assertEqual(inspect_staff_queue()["blocking_run"]["next_action"], "resume_after_recovery")
            with closing(open_state(path)) as db:
                resumed = resume_run(db, run_id)
                self.assertEqual((resumed.run_id, resumed.status, resumed.next_cursor), (run_id, "running", "after-002"))
                self.assertNotIn("retry_not_before_unix", resumed.result())
                self.assertEqual(db.execute("SELECT COUNT(*) FROM import_retry_gates").fetchone()[0], 0)

    def test_unsupported_throttle_keeps_old_store_non_retryable(self):
        directory = Path(self.enterContext(TemporaryDirectory())).resolve()
        path = directory / "old-shape.sqlite3"
        with closing(open_state(path)) as db:
            db.executescript(SCHEMA)
            run = start_run(db)
            record_failure(db, run.run_id, LabError("no trustworthy hint", "source_http_429"))
            failed = read_run(db, run.run_id)
            self.assertNotIn("retry_not_before_unix", failed.result())
            with self.assertRaisesRegex(LabError, "not safe to resume"):
                resume_run(db, run.run_id)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_schema WHERE name = 'import_retry_gates'").fetchone())
            self.assertEqual(read_run(db, run.run_id), failed)

    def test_inconsistent_gate_is_not_treated_as_permission_to_resume(self):
        directory = Path(self.enterContext(TemporaryDirectory())).resolve()
        path = directory / "store.sqlite3"
        with closing(open_state(path)) as db:
            db.executescript(SCHEMA)
            run = start_run(db)
            db.execute(import_job.RETRY_GATE_SCHEMA)
            db.execute("INSERT INTO import_retry_gates VALUES (?, ?)", (run.run_id, 1))
            with self.assertRaisesRegex(LabError, "stored checkpoint is inconsistent") as caught:
                resume_run(db, run.run_id)
            self.assertEqual(caught.exception.code, "invalid_checkpoint")

    def test_cli_early_resume_never_invokes_worker(self):
        directory = Path(self.enterContext(TemporaryDirectory())).resolve() / "state"
        self.enterContext(patch.object(import_job, "STATE_ROOT", directory))
        path, _ = import_job.store_paths("demo", create=True)
        with closing(open_state(path)) as db:
            db.executescript(SCHEMA)
            run = start_run(db)
            with patch.object(import_job, "time", return_value=1000):
                record_failure(db, run.run_id, LabError("synthetic throttle", "source_http_429", retry_after_seconds=5))
        proof = path.read_bytes()
        output = StringIO()
        with (patch.object(import_job, "time", return_value=1004),
              patch.object(sys, "argv", ["import_job", "resume", run.run_id]),
              patch.object(import_job, "run_import") as worker, redirect_stderr(output)):
            self.assertEqual(import_job.main(), 1)
        self.assertIn("wait until Unix time 1005", output.getvalue())
        worker.assert_not_called()
        self.assertEqual(path.read_bytes(), proof)
