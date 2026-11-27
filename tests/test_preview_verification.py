"""Exercise the new local verify command through fresh processes and disposable SQLite."""
from contextlib import closing
import json
import os
from pathlib import Path
from shutil import copyfile
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "c" * 32

SEED = r"""
from contextlib import closing
import sys
from import_job import SCHEMA, apply_page, claim_staff_request, open_state, store_paths, transaction
from worker import SourcePage

run_id = sys.argv[1]
path, _ = store_paths("staff", create=True)
with closing(open_state(path)) as db:
    db.executescript(SCHEMA)
    with transaction(db):
        db.execute("INSERT INTO staff_requests VALUES (?, ?, ?)", (run_id, "fixture-owner", "key-" + run_id))
        run = claim_staff_request(db, run_id)
    for page, cursor in enumerate(("after-002", "after-004", None)):
        rows = tuple((f"SOURCE-{n:03d}", f"Fictional source case {n}")
                     for n in (2 * page + 1, 2 * page + 2))
        run = apply_page(db, run, SourcePage(rows, cursor))
    assert run.status == "complete"
"""


class OperatorPreviewVerification(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(self.enterContext(TemporaryDirectory())).resolve()
        for name in ("case_release.py", "import_job.py", "service_profile.py", "worker.py"):
            copyfile(ROOT / name, self.workspace / name)
        self.state = self.workspace / ".import-lab"
        self.database = self.state / "staff.sqlite3"
        self.lease = self.state / "staff.lease.sqlite3"
        self.env = {key: os.environ[key] for key in ("PATH", "SystemRoot") if key in os.environ}
        self.env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def command(self, *args):
        return subprocess.run([sys.executable, "-B", *args], cwd=self.workspace, env=self.env, timeout=10,
                              capture_output=True, text=True, encoding="utf-8")

    def call(self, operation, run_id=RUN_ID, *, expected=0):
        answer = self.command("-m", "case_release", operation, run_id)
        self.assertEqual(answer.returncode, expected, answer.stderr)
        if expected:
            self.assertEqual(answer.stdout, "")
            return answer.stderr.strip()
        self.assertEqual(answer.stderr, "")
        return json.loads(answer.stdout)

    def prepare_staged(self):
        seed = self.command("-c", SEED, RUN_ID)
        self.assertEqual(seed.returncode, 0, seed.stderr)
        self.assertTrue(self.call("stage")["changed"])

    def test_repeat_verify_does_not_restage_or_consult_mutable_working_data(self):
        self.prepare_staged()
        expected = {"run_id": RUN_ID, "snapshot": "case-fixture-v1", "records": 6, "verified": True}
        initially = self.database.read_bytes()
        self.assertEqual(self.call("verify"), expected)
        self.assertEqual(self.call("verify"), expected)
        self.assertEqual(self.database.read_bytes(), initially)
        self.assertFalse(self.lease.exists())

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE records SET title = 'not a staged title' WHERE id = 'SOURCE-001'")
        changed_work = self.database.read_bytes()
        self.assertNotEqual(changed_work, initially)
        self.assertEqual(
            self.call("stage", expected=1),
            "Preview stopped: current store does not match the fixed lab reference; keep the current preview",
        )
        self.assertEqual(self.call("verify"), expected)
        self.assertEqual(self.database.read_bytes(), changed_work)
        self.assertFalse(self.lease.exists())

    def test_missing_preview_and_wrong_run_do_not_create_or_modify_state(self):
        self.assertEqual(self.call("verify", expected=1), "Preview stopped: no verified preview staged yet")
        self.assertFalse(self.state.exists())
        self.prepare_staged()
        before = self.database.read_bytes()
        self.assertEqual(self.call("verify", "d" * 32, expected=1),
                         "Preview stopped: current preview is not from the requested run")
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse(self.lease.exists())

    def test_inconsistent_preview_is_not_reported_as_verified(self):
        self.prepare_staged()
        with closing(sqlite3.connect(self.database)) as db, db:
            data = json.loads(db.execute("SELECT rows_json FROM case_preview WHERE slot = 1").fetchone()[0])
            data[0][1] = "unverified fixture output"
            self.assertEqual(len(data), 6)
            db.execute("UPDATE case_preview SET rows_json = ? WHERE slot = 1", (json.dumps(data),))
        before = self.database.read_bytes()
        self.assertEqual(self.call("verify", expected=1), "Preview stopped: local case preview is inconsistent")
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse(self.lease.exists())
