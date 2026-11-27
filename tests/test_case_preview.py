"""History and authorization checks for the local verified synthetic read projection."""
from contextlib import closing, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import case_release
import import_job
import staff_requests
from worker import LabError, SourcePage


class CasePreview(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch.object(import_job, "STATE_ROOT", directory / "state"))

    def start(self, key):
        request, _ = staff_requests.accept("synthetic-owner", key)
        path, _ = import_job.store_paths("staff", create=False)
        run_id = request["run_id"]
        with closing(import_job.open_state(path)) as db, import_job.transaction(db):
            import_job.claim_staff_request(db, run_id)
        return path, run_id

    def complete(self, key):
        path, run_id = self.start(key)
        with closing(import_job.open_state(path)) as db:
            run = import_job.read_run(db, run_id)
            for page, cursor in enumerate(("after-002", "after-004", None)):
                page_rows = tuple((f"SOURCE-{i:03d}", f"Fictional source case {i}")
                                  for i in (2 * page + 1, 2 * page + 2))
                run = import_job.apply_page(db, run, SourcePage(page_rows, cursor))
        return path, run_id

    def test_completing_does_not_stage_and_an_explicit_stage_is_idempotent(self):
        self.assertFalse(case_release.current()["staged"])
        self.assertFalse(import_job.STATE_ROOT.exists())
        path, run_id = self.complete("first-fictional-request")
        self.assertEqual(case_release.current(), {"staged": False, "snapshot": None, "records": []})
        _, lease = import_job.store_paths("staff", create=False)
        output = StringIO()
        with patch.object(sys, "argv", ["case_release", "stage", run_id]), redirect_stdout(output):
            self.assertEqual(case_release.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "run_id": run_id, "snapshot": "case-fixture-v1", "records": 6, "changed": True,
        })
        expected = [{"id": f"SOURCE-{i:03d}", "title": f"Fictional source case {i}"} for i in range(1, 7)]
        state = case_release.current()
        self.assertEqual(state, {"staged": True, "snapshot": "case-fixture-v1", "records": expected})
        self.assertNotIn(run_id, json.dumps(state))
        before = path.read_bytes()
        self.assertFalse(case_release.stage(run_id)["changed"])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(lease.exists())

    def test_unfinished_run_never_replaces_the_prior_preview(self):
        path, run_id = self.complete("first-fictional-request")
        case_release.stage(run_id)
        previous = case_release.current()
        _, started = self.start("second-fictional-request")
        before = path.read_bytes()
        with self.assertRaisesRegex(LabError, "unfinished import"):
            case_release.stage(started)
        self.assertEqual(case_release.current(), previous)
        self.assertEqual(path.read_bytes(), before)

    def test_old_run_and_same_count_mismatch_cannot_replace_the_preview(self):
        _, old_id = self.complete("first-fictional-request")
        case_release.stage(old_id)
        path, new_id = self.complete("second-fictional-request")
        previous = case_release.current()
        with self.assertRaisesRegex(LabError, "latest completed staff run"):
            case_release.stage(old_id)
        with closing(import_job.open_state(path)) as db:
            db.execute("UPDATE records SET title = 'Unverified synthetic title' WHERE id = 'SOURCE-001'")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 6)
        before = path.read_bytes()
        with self.assertRaisesRegex(LabError, "does not match"):
            case_release.stage(new_id)
        self.assertEqual(case_release.current(), previous)
        self.assertEqual(path.read_bytes(), before)

    def test_bad_projection_is_refused_not_rendered(self):
        path, run_id = self.complete("first-fictional-request")
        case_release.stage(run_id)
        with closing(import_job.open_state(path)) as db:
            db.execute("UPDATE case_preview SET rows_json = '[1, 2, 3]' WHERE slot = 1")
        with self.assertRaisesRegex(LabError, "preview is inconsistent"):
            case_release.current()
