"""Run an offline reconciliation exercise against disposable synthetic SQLite files.

This does not call Keycloak, the source API, or the normal operator state directory.
"""
from argparse import ArgumentParser
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "0" * 30 + "f1"
ROWS = tuple((f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def operator(workspace: Path, store: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-E", "-B", "-m", "import_job", "--store", store, command, RUN_ID],
        cwd=workspace, capture_output=True, text=True, timeout=10,
    )


def report(workspace: Path, store: str, command: str, exit_code: int) -> dict:
    process = operator(workspace, store, command)
    require(process.returncode == exit_code and not process.stderr,
            f"unexpected {command} result in disposable {store} store")
    return json.loads(process.stdout)


def run_walkthrough(workspace: Path) -> dict:
    # Import from and run only against the disposable copy. Never write normal state.
    sys.path.insert(0, str(workspace))
    try:
        import import_job as job
        from worker import SourcePage

        require(job.STATE_ROOT == workspace / ".import-lab", "disposable store was not selected")
        clean, clean_lease = job.store_paths("healthy-demo", create=True)
        with closing(job.open_state(clean)) as db:
            db.executescript(job.SCHEMA)
            run = job.start_run(db, RUN_ID)
            job.apply_page(db, run, SourcePage(ROWS[:2], "after-002"))
            db.execute("UPDATE import_runs SET status = 'failed', failure = 'source_http_503' WHERE run_id = ?",
                       (RUN_ID,))

        failed_status = report(workspace, "healthy-demo", "status", 1)
        stopped = operator(workspace, "healthy-demo", "reconcile")
        require(failed_status["status"] == "failed" and failed_status["committed_pages"] == 1
                and failed_status["processed_records"] == 2, "unexpected saved checkpoint")
        require(stopped.returncode == 1 and not stopped.stdout
                and "requires a complete run" in stopped.stderr, "unfinished run was not refused")

        # Deliver the two remaining simulated pages to the same run without calling the real resume CLI.
        with closing(job.open_state(clean)) as db:
            run = job.resume_run(db, RUN_ID)
            run = job.apply_page(db, run, SourcePage(ROWS[2:4], "after-004"))
            job.apply_page(db, run, SourcePage(ROWS[4:], None))

        changed, changed_lease = job.store_paths("same-count-demo", create=True)
        shutil.copyfile(clean, changed)
        with closing(sqlite3.connect(changed)) as db, db:
            db.execute("UPDATE records SET title = ? WHERE id = ?", ("Synthetic different title", ROWS[0][0]))
            db.execute("DELETE FROM records WHERE id = ?", (ROWS[-1][0],))
            db.execute("INSERT INTO records VALUES (?, ?)", ("SYNTHETIC-EXTRA", "Synthetic extra row"))

        before = clean.read_bytes(), changed.read_bytes()
        good = report(workspace, "healthy-demo", "reconcile", 0)
        unchanged_status = report(workspace, "same-count-demo", "status", 0)
        bad = report(workspace, "same-count-demo", "reconcile", 1)
        require(good["matches"] and good["checkpoint_ids_match"]
                and good["expected_records"] == good["stored_records"] == 6, "healthy fixture did not match")
        require(not bad["matches"] and bad["checkpoint_ids_match"]
                and bad["expected_records"] == bad["stored_records"] == 6, "same-count mismatch was hidden")
        require([bad[key] for key in ("missing_records", "unexpected_records", "value_mismatches")] == [1, 1, 1],
                "mismatch categories did not separate the three differences")
        require(unchanged_status["status"] == "complete"
                and failed_status["run_id"] == good["run_id"] == unchanged_status["run_id"] == bad["run_id"],
                "run identity was not preserved")
        require(before == (clean.read_bytes(), changed.read_bytes())
                and not (clean_lease.exists() or changed_lease.exists()), "read-only check changed local state")
        return {
            "mode": "offline_synthetic",
            "interrupted_before_reconciliation": {"pages": 1, "records": 2, "reconciliation_refused": True},
            "completed": {"same_run_id": True, "expected_records": 6, "stored_records": 6, "matches": True},
            "same_count_difference": {
                "status": "complete", "stored_records": 6, "missing_records": bad["missing_records"],
                "unexpected_records": bad["unexpected_records"], "value_mismatches": bad["value_mismatches"],
                "matches": False,
            },
            "reconciliation_left_stores_unchanged": True,
        }
    finally:
        sys.path.remove(str(workspace))


def main() -> None:
    ArgumentParser(description=__doc__).parse_args()
    with TemporaryDirectory(prefix="eil-reconciliation-demo-") as directory:
        workspace = Path(directory).resolve()
        for name in ("import_job.py", "service_profile.py", "worker.py"):
            shutil.copyfile(ROOT / name, workspace / name)
        result = run_walkthrough(workspace)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
