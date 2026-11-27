"""Verify that a local operator receipt has one coherent SQLite read point."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import case_release
import import_job
from worker import SourcePage

FIRST_RUN = "e" * 32
SECOND_RUN = "f" * 32
FIRST_SNAPSHOT = "case-fixture-v1"
SECOND_SNAPSHOT = "case-fixture-concurrent-test-only"
FIRST_ROWS = tuple((f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7))
SECOND_ROWS = tuple((f"SOURCE-{n:03d}", f"Revised synthetic concurrency case {n}") for n in range(1, 7))
WAIT = 8


class OperatorVerificationSnapshot(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch.object(import_job, "STATE_ROOT", folder / "state"))
        self.store, self.lease = import_job.store_paths("staff", create=True)
        with closing(import_job.open_state(self.store)) as db:
            db.executescript(import_job.SCHEMA)

    def complete(self, run_id, rows):
        with closing(import_job.open_state(self.store)) as db:
            with import_job.transaction(db):
                db.execute("INSERT INTO staff_requests VALUES (?, ?, ?)", (run_id, "local-fixture", "key-" + run_id))
                state = import_job.claim_staff_request(db, run_id)
            for n, cursor in enumerate(("after-002", "after-004", None)):
                state = import_job.apply_page(db, state, SourcePage(rows[2 * n:2 * n + 2], cursor))
            self.assertEqual((state.status, state.committed_pages), ("complete", 3))

    @staticmethod
    def operator_verify(run_id):
        out, err = StringIO(), StringIO()
        with (patch.object(sys, "argv", ["case_release", "verify", run_id]),
              redirect_stdout(out), redirect_stderr(err)):
            code = case_release.main()
        return code, out.getvalue(), err.getvalue()

    def test_inflight_verify_reports_one_snapshot_despite_replacement_commit(self):
        self.complete(FIRST_RUN, FIRST_ROWS)
        self.assertEqual(case_release.stage(FIRST_RUN)["snapshot"], FIRST_SNAPSHOT)
        first_receipt = {"run_id": FIRST_RUN, "snapshot": FIRST_SNAPSHOT, "records": 6, "verified": True}
        replacement_receipt = {"run_id": SECOND_RUN, "snapshot": SECOND_SNAPSHOT, "records": 6, "verified": True}

        # The shipped fixture is unchanged; only this isolated case models a new reference.
        with (patch.object(import_job, "SNAPSHOT", SECOND_SNAPSHOT),
              patch.object(case_release, "SNAPSHOT", SECOND_SNAPSHOT),
              patch.dict(import_job.RECONCILIATION_FIXTURES, {SECOND_SNAPSHOT: SECOND_ROWS})):
            self.complete(SECOND_RUN, SECOND_ROWS)
            materialized, commit_done = Event(), Event()
            original_read, original_validation = case_release.read_projection, case_release.verified_rows
            reads = []

            def track_projection():
                view = original_read()
                reads.append((view["run_id"], view["snapshot"]))
                return view

            def validate_after_commit(view):
                self.assertEqual((view["run_id"], view["snapshot"]), (FIRST_RUN, FIRST_SNAPSHOT))
                materialized.set()
                if not commit_done.wait(WAIT):
                    raise AssertionError("replacement did not commit while verify was paused")
                # This detached row must still hold all the old data, not a blend of before/after.
                validated = original_validation(view)
                self.assertEqual(validated, [list(row) for row in FIRST_ROWS])
                return validated

            with (patch.object(case_release, "read_projection", track_projection),
                  patch.object(case_release, "verified_rows", validate_after_commit),
                  ThreadPoolExecutor(max_workers=1) as pool):
                in_flight = pool.submit(self.operator_verify, FIRST_RUN)
                try:
                    self.assertTrue(materialized.wait(WAIT), "operator never materialized its SQLite row")
                    self.assertFalse(in_flight.done())
                    stage = case_release.stage(SECOND_RUN)  # Real writer and independent DB connection.
                    self.assertEqual(stage, {
                        "run_id": SECOND_RUN, "snapshot": SECOND_SNAPSHOT, "records": 6, "changed": True,
                    })
                    now = original_read()
                    self.assertEqual((now["run_id"], now["snapshot"]), (SECOND_RUN, SECOND_SNAPSHOT))
                    self.assertEqual(original_validation(now), [list(row) for row in SECOND_ROWS])
                finally:
                    commit_done.set()
                code, output, failure = in_flight.result(timeout=WAIT)
            self.assertEqual(reads, [(FIRST_RUN, FIRST_SNAPSHOT)])
            self.assertEqual((code, json.loads(output), failure), (0, first_receipt, ""))

            code, no_output, failure = self.operator_verify(FIRST_RUN)
            self.assertEqual((code, no_output, failure.strip()),
                             (1, "", "Preview stopped: current preview is not from the requested run"))
            code, output, failure = self.operator_verify(SECOND_RUN)
            self.assertEqual((code, json.loads(output), failure), (0, replacement_receipt, ""))
            self.assertFalse(self.lease.exists())
