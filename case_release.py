"""Stage an independently matched synthetic case set for the local read-only preview."""
import argparse
from contextlib import closing
import json
import sqlite3
import sys

from import_job import (
    CANCELLED_BEFORE_START, RECONCILIATION_FIXTURES, RUN_ID, SNAPSHOT, STAFF_STORE, UNFINISHED,
    open_state, reconcile_at, require_staff_request, store_paths, transaction,
)
from worker import LabError

PREVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS case_preview (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    run_id TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    rows_json TEXT NOT NULL
);
"""


def projection_row(db: sqlite3.Connection) -> sqlite3.Row | None:
    try:
        return db.execute(
            """SELECT run_id, snapshot, CASE WHEN length(rows_json) <= 2048 THEN rows_json END AS rows_json
               FROM case_preview WHERE slot = 1"""
        ).fetchone()
    except sqlite3.OperationalError as error:
        if (getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_ERROR
                or str(error) != "no such table: case_preview"):
            raise
        return None


def stage(run_id: str) -> dict:
    if not RUN_ID.fullmatch(run_id):
        raise LabError("provide a local staff import run ID", "invalid_run")
    path, _ = store_paths(STAFF_STORE, create=False)
    with closing(open_state(path)) as db, transaction(db):
        require_staff_request(db, run_id)
        if db.execute(f"SELECT 1 FROM import_runs r WHERE {UNFINISHED} LIMIT 1",
                      (CANCELLED_BEFORE_START, SNAPSHOT)).fetchone() is not None:
            raise LabError("there is unfinished import work; keep the current preview", "unfinished_import")
        latest = db.execute(
            """SELECT r.run_id FROM import_runs r JOIN staff_requests s USING (run_id)
               WHERE r.status = 'complete' ORDER BY r.rowid DESC LIMIT 1"""
        ).fetchone()
        if latest is None or latest["run_id"] != run_id:
            raise LabError("select the latest completed staff run", "not_latest_completed_run")
        report = reconcile_at(db, run_id)
        if not report["matches"]:
            raise LabError("current store does not match the fixed lab reference; keep the current preview",
                           "reconciliation_mismatch")
        # The verification and copy occur in the same short SQLite writer transaction.
        rows = [list(row) for row in db.execute("SELECT id, title FROM records ORDER BY id")]
        candidate = (run_id, report["snapshot"], json.dumps(rows, separators=(",", ":")))
        existing = projection_row(db)
        changed = existing is None or tuple(existing) != candidate
        if changed:
            db.execute(PREVIEW_SCHEMA)
            db.execute(
                """INSERT INTO case_preview VALUES (1, ?, ?, ?)
                   ON CONFLICT(slot) DO UPDATE SET run_id = excluded.run_id, snapshot = excluded.snapshot,
                       rows_json = excluded.rows_json""",
                candidate,
            )
        return {"run_id": run_id, "snapshot": report["snapshot"], "records": len(rows), "changed": changed}


def read_projection() -> sqlite3.Row | None:
    try:
        path, _ = store_paths(STAFF_STORE, create=False)
    except LabError as error:
        if error.code == "unknown_store":
            return None
        raise
    with closing(open_state(path, read_only=True)) as db:
        return projection_row(db)


def verified_rows(view: sqlite3.Row) -> list[list[str]]:
    try:
        fixture = RECONCILIATION_FIXTURES[view["snapshot"]]
        rows = json.loads(view["rows_json"])
        if (not RUN_ID.fullmatch(view["run_id"]) or rows != [list(row) for row in sorted(fixture)]):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise LabError("local case preview is inconsistent", "invalid_case_preview") from None
    return rows


def current() -> dict:
    view = read_projection()
    if view is None:
        return {"staged": False, "snapshot": None, "records": []}
    rows = verified_rows(view)
    # Do not reveal the associated staff request ID to unrelated case readers.
    return {"staged": True, "snapshot": view["snapshot"],
            "records": [{"id": record_id, "title": title} for record_id, title in rows]}


def verify(run_id: str) -> dict:
    """Check a specific already-staged copy; never restage or read the work table."""
    if not RUN_ID.fullmatch(run_id):
        raise LabError("provide a local staff import run ID", "invalid_run")
    view = read_projection()
    if view is None:
        raise LabError("no verified preview staged yet", "preview_not_staged")
    rows = verified_rows(view)
    if view["run_id"] != run_id:
        raise LabError("current preview is not from the requested run", "preview_run_mismatch")
    return {"run_id": run_id, "snapshot": view["snapshot"], "records": len(rows), "verified": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stage", help="stage the latest reconciled staff run locally").add_argument("run_id")
    commands.add_parser("verify", help="check the already-staged copy without changing it").add_argument("run_id")
    args = parser.parse_args()
    try:
        command = stage if args.command == "stage" else verify
        print(json.dumps(command(args.run_id)))
    except LabError as error:
        print(f"Preview stopped: {error}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("Preview stopped: local staff store unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
