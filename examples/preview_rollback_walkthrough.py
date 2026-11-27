"""Rehearse initial/replacement preview rollback with no servers or existing stores."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
FIRST_ID = "1" * 32
SECOND_ID = "2" * 32

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
    assert run.status == "complete" and run.committed_pages == 3
"""

READ = r"""
from contextlib import closing
import json

from case_release import current, projection_row
from import_job import open_state, store_paths

path, _ = store_paths("staff", create=False)
with closing(open_state(path, read_only=True)) as db:
    row = projection_row(db)
    exists = db.execute("SELECT 1 FROM sqlite_schema WHERE name = 'case_preview'").fetchone() is not None
print(json.dumps({"provenance": row["run_id"] if row else None, "preview": current(), "table_exists": exists}))
"""

FAULT = r"""
from contextlib import closing, contextmanager
import json
import sqlite3
import sys

import case_release
from import_job import open_state, store_paths

candidate, previous = sys.argv[1], sys.argv[2] or None
saved_transaction = case_release.transaction
asserted = False
rows = [[f"SOURCE-{n:03d}", f"Fictional source case {n}"] for n in range(1, 7)]

@contextmanager
def abort_before_commit(db):
    global asserted
    with saved_transaction(db):
        yield
        # The real stage SQL has finished. A reader outside that transaction
        # must still see only the last committed state.
        pending = case_release.projection_row(db)
        assert pending["run_id"] == candidate and json.loads(pending["rows_json"]) == rows
        path, _ = store_paths("staff", create=False)
        with closing(open_state(path, read_only=True)) as observer:
            visible = case_release.projection_row(observer)
            if previous is None:
                assert visible is None
            else:
                assert visible["run_id"] == previous and json.loads(visible["rows_json"]) == rows
        asserted = True
        raise sqlite3.OperationalError("fixture rollback before commit")

case_release.transaction = abort_before_commit
sys.argv = ["case_release", "stage", candidate]
exit_code = case_release.main()
assert asserted, "fault did not occur after the staging SQL"
raise SystemExit(exit_code)
"""


def execute(workspace: Path, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot") if key in os.environ}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", *args], cwd=workspace, env=env, timeout=12,
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode != expected:
        raise RuntimeError(f"fixture child returned {result.returncode}: {result.stderr.strip()}")
    return result


def verify_case(workspace: Path, candidate: str, previous: str | None) -> dict:
    execute(workspace, "-c", SEED, candidate)
    store = workspace / ".import-lab" / "staff.sqlite3"
    if not store.is_file():
        raise RuntimeError("disposable staff store not present")
    before_bytes = store.read_bytes()
    before = json.loads(execute(workspace, "-c", READ).stdout)
    assert before["table_exists"] is (previous is not None) and before["provenance"] == previous

    refused = execute(workspace, "-c", FAULT, candidate, previous or "", expected=1)
    assert not refused.stdout
    assert refused.stderr.strip() == "Preview stopped: local staff store unavailable"
    # Reopen in a new process after the faulting stage process has exited.
    after = json.loads(execute(workspace, "-c", READ).stdout)
    assert after == before and store.read_bytes() == before_bytes
    assert not (workspace / ".import-lab" / "staff.lease.sqlite3").exists()

    retry = json.loads(execute(workspace, "-m", "case_release", "stage", candidate).stdout)
    assert retry == {"run_id": candidate, "snapshot": "case-fixture-v1", "records": 6, "changed": True}
    current = json.loads(execute(workspace, "-c", READ).stdout)
    assert current["table_exists"] and current["provenance"] == candidate
    expected = [{"id": f"SOURCE-{n:03d}", "title": f"Fictional source case {n}"} for n in range(1, 7)]
    assert current["preview"] == {"staged": True, "snapshot": "case-fixture-v1", "records": expected}
    return {"independent_reader_saw_previous_state": True, "previous_record_count": len(before["preview"]["records"]),
            "restart_preserved_state_and_bytes": True, "clean_retry_record_count": 6}


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    if not __debug__:
        raise SystemExit("Run the rollback walkthrough without Python optimization (-O).")
    with TemporaryDirectory(prefix="eil-preview-rollback-demo-") as directory:
        workspace = Path(directory).resolve()
        for module in ("case_release.py", "import_job.py", "service_profile.py", "worker.py"):
            shutil.copyfile(ROOT / module, workspace / module)
        first = verify_case(workspace, FIRST_ID, None)
        replacement = verify_case(workspace, SECOND_ID, FIRST_ID)
    print(json.dumps({"mode": "offline_synthetic_precommit_rollback",
                      "initial_stage": first, "replace_prior_preview": replacement}, indent=2))


if __name__ == "__main__":
    main()
