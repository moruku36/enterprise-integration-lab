"""Staff-facing local queue, with no source calls and no workload credential reads."""
from contextlib import closing
import sqlite3
from uuid import uuid4

from import_job import MAX_QUEUED, SCHEMA, STAFF_STORE, open_state, read_run, store_paths, transaction
from worker import LabError


def _status(db: sqlite3.Connection, run_id: str) -> dict:
    try:
        run = read_run(db, run_id)
    except LabError as error:
        if error.code != "unknown_run":
            raise
        return {
            "run_id": run_id, "status": "queued", "committed_pages": 0,
            "processed_records": 0, "changed_records": 0, "failure": None,
        }
    return {
        "run_id": run_id, "status": run.status, "committed_pages": run.committed_pages,
        "processed_records": len(run.seen_ids), "changed_records": run.changed_records, "failure": run.failure,
    }


def accept(staff_sub: str, request_key: str) -> tuple[dict, bool]:
    path, _ = store_paths(STAFF_STORE, create=True)
    with closing(open_state(path)) as db:
        db.executescript(SCHEMA)
        with transaction(db):
            row = db.execute(
                "SELECT run_id FROM staff_requests WHERE staff_sub = ? AND request_key = ?",
                (staff_sub, request_key),
            ).fetchone()
            if row is not None:
                return _status(db, row["run_id"]), False
            pending = db.execute(
                "SELECT COUNT(*) FROM staff_requests s LEFT JOIN import_runs r USING (run_id) "
                "WHERE r.run_id IS NULL",
            ).fetchone()[0]
            if pending >= MAX_QUEUED:
                raise LabError("local staff queue full", "queue_full")
            run_id = uuid4().hex
            db.execute("INSERT INTO staff_requests VALUES (?, ?, ?)", (run_id, staff_sub, request_key))
            return _status(db, run_id), True


def status(staff_sub: str, run_id: str) -> dict:
    path, _ = store_paths(STAFF_STORE, create=False)
    with closing(open_state(path, read_only=True)) as db:
        row = db.execute(
            "SELECT 1 FROM staff_requests WHERE staff_sub = ? AND run_id = ?",
            (staff_sub, run_id),
        ).fetchone()
        if row is None:
            raise LabError("staff request not found", "unknown_request")
        return _status(db, run_id)
