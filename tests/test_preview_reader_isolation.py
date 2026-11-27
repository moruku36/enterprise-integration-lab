"""Verify two authorized offline HTTP readers across a second synthetic preview commit."""
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt

from app import AUDIENCE, CLIENT, ISSUER, ORIGIN, app, jwks
import case_release
import import_job
from worker import SourcePage

FIRST_RUN = "1" * 32
SECOND_RUN = "2" * 32
FIRST_SNAPSHOT = "case-fixture-v1"
SECOND_SNAPSHOT = "case-fixture-v2-test-only"
FIRST_RECORDS = tuple((f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7))
SECOND_RECORDS = tuple((f"SOURCE-{n:03d}", f"Revised fictional case {n}") for n in range(1, 7))
CURSORS = ("after-002", "after-004", None)


class PreviewReaderIsolation(unittest.TestCase):
    def setUp(self):
        temp = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch.object(import_job, "STATE_ROOT", temp / "state"))
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.enterContext(patch.object(jwks, "get_signing_key_from_jwt",
                          return_value=SimpleNamespace(key=self.key.public_key())))
        self.path, _ = import_job.store_paths("staff", create=True)
        with closing(import_job.open_state(self.path)) as db:
            db.executescript(import_job.SCHEMA)

    def reader(self, subject, role):
        token = jwt.encode({
            "iss": ISSUER, "aud": AUDIENCE, "sub": subject, "typ": "Bearer", "azp": CLIENT,
            "exp": int(time.time()) + 90, "realm_access": {"roles": [role]},
        }, self.key, algorithm="RS256")
        return self.enterContext(TestClient(app, base_url=ORIGIN, headers={"Authorization": "Bearer " + token}))

    def assert_readers_see(self, readers, snapshot, rows):
        expected = {"staged": True, "snapshot": snapshot, "records": [
            {"id": record_id, "title": title} for record_id, title in rows
        ]}
        for reader in readers:
            response = reader.get("/api/imported-cases")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json(), expected)

    def start(self, run_id):
        with closing(import_job.open_state(self.path)) as db, import_job.transaction(db):
            db.execute("INSERT INTO staff_requests VALUES (?, ?, ?)", (run_id, "fixture-owner", "key-" + run_id))
            import_job.claim_staff_request(db, run_id)

    def put_page(self, run_id, rows, page):
        with closing(import_job.open_state(self.path)) as db:
            run = import_job.read_run(db, run_id)
            return import_job.apply_page(db, run, SourcePage(rows[2 * page:2 * (page + 1)], CURSORS[page]))

    def stage(self, run_id, *, succeeds=True):
        out, err = StringIO(), StringIO()
        with (patch.object(sys, "argv", ["case_release", "stage", run_id]),
              redirect_stdout(out), redirect_stderr(err)):
            code = case_release.main()
        self.assertEqual(code, int(not succeeds), err.getvalue())
        if succeeds:
            self.assertEqual(err.getvalue(), "")
            return json.loads(out.getvalue())
        self.assertEqual(out.getvalue(), "")
        return err.getvalue()

    def test_two_authorized_readers_only_see_whole_committed_snapshots(self):
        readers = (self.reader("fixture-viewer", "cases_viewer"), self.reader("fixture-importer", "cases_importer"))
        self.start(FIRST_RUN)
        for page in range(3):
            self.put_page(FIRST_RUN, FIRST_RECORDS, page)
        self.assertEqual(self.stage(FIRST_RUN), {
            "run_id": FIRST_RUN, "snapshot": FIRST_SNAPSHOT, "records": 6, "changed": True,
        })
        self.assert_readers_see(readers, FIRST_SNAPSHOT, FIRST_RECORDS)

        # Only this test simulates an independent, supported second reference.
        # The shipped integration still accepts case-fixture-v1 alone.
        with (patch.object(import_job, "SNAPSHOT", SECOND_SNAPSHOT),
              patch.object(case_release, "SNAPSHOT", SECOND_SNAPSHOT),
              patch.dict(import_job.RECONCILIATION_FIXTURES, {SECOND_SNAPSHOT: SECOND_RECORDS})):
            self.start(SECOND_RUN)
            self.put_page(SECOND_RUN, SECOND_RECORDS, 0)
            with closing(import_job.open_state(self.path, read_only=True)) as work:
                self.assertEqual([tuple(row) for row in work.execute("SELECT id, title FROM records ORDER BY id")],
                                 list(SECOND_RECORDS[:2] + FIRST_RECORDS[2:]))
            self.assert_readers_see(readers, FIRST_SNAPSHOT, FIRST_RECORDS)
            self.assertIn("unfinished import work", self.stage(SECOND_RUN, succeeds=False))
            for page in (1, 2):
                self.put_page(SECOND_RUN, SECOND_RECORDS, page)
            self.assert_readers_see(readers, FIRST_SNAPSHOT, FIRST_RECORDS)

            writer = case_release.transaction
            observations = []

            @contextmanager
            def observe_before_commit(db):
                with writer(db):
                    yield
                    private = case_release.projection_row(db)
                    self.assertEqual((private["run_id"], private["snapshot"]), (SECOND_RUN, SECOND_SNAPSHOT))
                    # Real endpoint calls now read through independent SQLite connections while the
                    # stage write is made but not committed. Neither reader may see a partial release.
                    self.assert_readers_see(readers, FIRST_SNAPSHOT, FIRST_RECORDS)
                    observations.append("first-still-visible")

            with patch.object(case_release, "transaction", observe_before_commit):
                result = self.stage(SECOND_RUN)
            self.assertEqual(observations, ["first-still-visible"])
            self.assertEqual(result, {"run_id": SECOND_RUN, "snapshot": SECOND_SNAPSHOT, "records": 6, "changed": True})
            self.assert_readers_see(readers, SECOND_SNAPSHOT, SECOND_RECORDS)
