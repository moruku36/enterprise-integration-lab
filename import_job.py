"""Durable, single-worker import of the lab's fixed synthetic source snapshot."""
import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
from math import ceil
from pathlib import Path
import re
import sqlite3
from stat import S_ISREG
import sys
from time import time
from uuid import uuid4

import httpx

from service_profile import SNAPSHOT
from worker import PAGE_LIMIT, PAGE_SIZE, LabError, SourcePage, check_progress, fetch_page, fixture_secret, request_token

STATE_ROOT = Path(__file__).resolve().parent / ".import-lab"
STORE_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
RUN_ID = re.compile(r"[a-f0-9]{32}")
REQUEST_KEY = re.compile(r"[A-Za-z0-9_-]{16,64}")
STAFF_STORE = "staff"
MAX_QUEUED = 8
CANCELLED_BEFORE_START = "cancelled_before_start"
# Keep the M3d SQLite schema: older workers see a nonretryable failed run,
# so they never start or resume a cancelled request even if rolled back.
UNFINISHED = """
    r.status != 'complete' AND NOT (
        r.status = 'failed' AND r.failure IS ? AND r.snapshot = ?
        AND r.committed_pages = 0 AND r.next_cursor IS NULL
        AND r.seen_ids = '[]' AND r.seen_cursors = '[]' AND r.changed_records = 0
        AND EXISTS (SELECT 1 FROM staff_requests s0 WHERE s0.run_id = r.run_id)
    )
"""
RETRYABLE = frozenset({
    "identity_unavailable", "source_unavailable",
    "token_http_502", "token_http_503", "token_http_504",
    "source_http_502", "source_http_503", "source_http_504",
})

# Add an optional side table without changing the existing import_runs schema.
# An older CLI ignores this table and still refuses to resume source_http_429.
SOURCE_THROTTLED = "source_http_429"
RETRY_GATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_retry_gates (
    run_id TEXT PRIMARY KEY NOT NULL,
    retry_not_before_unix INTEGER NOT NULL CHECK (
        typeof(retry_not_before_unix) = 'integer' AND retry_not_before_unix > 0
    )
);
"""

# Independent expected data for the fixed lab snapshot. Do not derive it from
# the source implementation, stored checkpoint, or SQLite destination.
RECONCILIATION_FIXTURES = {
    "case-fixture-v1": (
        ("SOURCE-001", "Fictional source case 1"),
        ("SOURCE-002", "Fictional source case 2"),
        ("SOURCE-003", "Fictional source case 3"),
        ("SOURCE-004", "Fictional source case 4"),
        ("SOURCE-005", "Fictional source case 5"),
        ("SOURCE-006", "Fictional source case 6"),
    ),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY NOT NULL,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    snapshot TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'failed', 'complete')),
    committed_pages INTEGER NOT NULL CHECK (committed_pages BETWEEN 0 AND 3),
    next_cursor TEXT,
    seen_ids TEXT NOT NULL,
    seen_cursors TEXT NOT NULL,
    changed_records INTEGER NOT NULL,
    failure TEXT
);
CREATE TABLE IF NOT EXISTS staff_requests (
    run_id TEXT PRIMARY KEY NOT NULL,
    staff_sub TEXT NOT NULL,
    request_key TEXT NOT NULL,
    UNIQUE(staff_sub, request_key)
);
"""


@dataclass(frozen=True)
class Run:
    run_id: str
    snapshot: str
    status: str
    committed_pages: int
    next_cursor: str | None
    seen_ids: tuple[str, ...]
    seen_cursors: tuple[str, ...]
    changed_records: int
    failure: str | None
    retry_not_before_unix: int | None = None

    def result(self) -> dict:
        result = {
            "run_id": self.run_id, "snapshot": self.snapshot, "status": self.status,
            "committed_pages": self.committed_pages, "next_cursor": self.next_cursor,
            "processed_records": len(self.seen_ids), "changed_records": self.changed_records,
            "failure": self.failure,
        }
        if self.retry_not_before_unix is not None:
            result["retry_not_before_unix"] = self.retry_not_before_unix
        return result


def store_paths(name: str, *, create: bool) -> tuple[Path, Path]:
    if not STORE_NAME.fullmatch(name):
        raise LabError("invalid store name; use 1 to 32 lowercase letters, digits or hyphens")
    if create:
        STATE_ROOT.mkdir(mode=0o700, exist_ok=True)
    paths = (STATE_ROOT / f"{name}.sqlite3", STATE_ROOT / f"{name}.lease.sqlite3")
    if STATE_ROOT.is_symlink() or any(path.is_symlink() for path in paths):
        raise LabError("refusing a linked local import store")
    if not create:
        try:
            mode = paths[0].stat().st_mode
        except FileNotFoundError:
            raise LabError("unknown import store", "unknown_store") from None
        if not S_ISREG(mode):
            raise LabError("local import store is not a regular file", "invalid_store")
    return paths


@contextmanager
def import_lease(lease_path: Path):
    """Separate SQLite reservation spans the run without holding the data transaction open."""
    with closing(sqlite3.connect(lease_path, isolation_level=None, timeout=0)) as lease:
        try:
            lease.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY:
                raise LabError("another worker owns this local store", "store_busy") from None
            raise
        try:
            yield
        finally:
            lease.rollback()


@contextmanager
def transaction(db: sqlite3.Connection):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise


def open_state(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None) if read_only else (
        sqlite3.connect(path, isolation_level=None)
    )
    connection.row_factory = sqlite3.Row
    return connection


def read_run(db: sqlite3.Connection, run_id: str) -> Run:
    try:
        row = db.execute(
            """SELECT r.*, g.retry_not_before_unix FROM import_runs r
               LEFT JOIN import_retry_gates g USING (run_id) WHERE r.run_id = ?""", (run_id,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        # Older stores (and stores with no saved throttle) are readable without migration.
        if (getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_ERROR
                or str(error) != "no such table: import_retry_gates"):
            raise
        row = db.execute("SELECT r.*, NULL AS retry_not_before_unix FROM import_runs r WHERE run_id = ?",
                         (run_id,)).fetchone()
    if row is None:
        raise LabError("run ID not found in this store", "unknown_run")
    try:
        ids_data = json.loads(row["seen_ids"])
        cursors_data = json.loads(row["seen_cursors"])
        if not (isinstance(ids_data, list) and isinstance(cursors_data, list)
                and all(isinstance(value, str) for value in ids_data + cursors_data)):
            raise ValueError
        seen_ids = tuple(ids_data)
        seen_cursors = tuple(cursors_data)
        pages = row["committed_pages"]
        cancelled = row["failure"] == CANCELLED_BEFORE_START
        retry_at = row["retry_not_before_unix"]
        if retry_at is not None and (type(retry_at) is not int or retry_at <= 0
                                     or row["status"] != "failed" or row["failure"] != SOURCE_THROTTLED):
            raise ValueError
        if (row["snapshot"] != SNAPSHOT or not 0 <= pages <= PAGE_LIMIT
                or len(seen_ids) != pages * PAGE_SIZE or len(seen_ids) != len(set(seen_ids))
                or len(seen_cursors) != min(pages, PAGE_LIMIT - 1)
                or len(seen_cursors) != len(set(seen_cursors))
                or (pages == PAGE_LIMIT) != (row["status"] == "complete")
                or row["next_cursor"] != (seen_cursors[-1] if 0 < pages < PAGE_LIMIT else None)
                or (cancelled and (row["status"] != "failed" or pages != 0
                                  or row["changed_records"] != 0
                                  or row["seen_ids"] != "[]" or row["seen_cursors"] != "[]"))):
            raise ValueError
    except (TypeError, ValueError):
        raise LabError("stored checkpoint is inconsistent; inspect the store", "invalid_checkpoint") from None
    return Run(
        row["run_id"], row["snapshot"], "cancelled" if cancelled else row["status"], pages, row["next_cursor"],
        seen_ids, seen_cursors, row["changed_records"], None if cancelled else row["failure"], retry_at,
    )


def start_run(db: sqlite3.Connection, run_id: str | None = None) -> Run:
    if db.execute(
        f"SELECT 1 FROM import_runs r WHERE {UNFINISHED} LIMIT 1",
        (CANCELLED_BEFORE_START, SNAPSHOT),
    ).fetchone():
        raise LabError("unfinished run exists; inspect it and explicitly resume if eligible")
    run_id = uuid4().hex if run_id is None else run_id
    db.execute(
        """INSERT INTO import_runs VALUES (?, ?, 'running', 0, NULL, '[]', '[]', 0, NULL)""",
        (run_id, SNAPSHOT),
    )
    return read_run(db, run_id)


def require_staff_request(db: sqlite3.Connection, run_id: str) -> None:
    if not db.execute("SELECT 1 FROM staff_requests WHERE run_id = ?", (run_id,)).fetchone():
        raise LabError("staff request not found", "unknown_request")


def claim_staff_request(db: sqlite3.Connection, run_id: str) -> Run:
    require_staff_request(db, run_id)
    existing = db.execute("SELECT failure FROM import_runs WHERE run_id = ?", (run_id,)).fetchone()
    if existing is not None:
        if existing["failure"] == CANCELLED_BEFORE_START:
            read_run(db, run_id)  # Refuse a damaged marker as an inconsistent checkpoint.
            raise LabError("request was cancelled before it started", "request_cancelled")
        raise LabError("request already started; use status or explicit resume", "request_already_started")
    return start_run(db, run_id)


def cancel_staff_request(db: sqlite3.Connection, run_id: str) -> Run:
    """Persist the terminal decision without acquiring the worker lease or talking to the source."""
    # The same writer reservation protects the existence check and insert as a claim.
    with transaction(db):
        require_staff_request(db, run_id)
        if db.execute("SELECT 1 FROM import_runs WHERE run_id = ?", (run_id,)).fetchone():
            state = read_run(db, run_id)
            if state.status == "cancelled":
                return state
            raise LabError("only a queued request can be cancelled", "request_already_started")
        db.execute(
            """INSERT INTO import_runs VALUES (?, ?, 'failed', 0, NULL, '[]', '[]', 0, ?)""",
            (run_id, SNAPSHOT, CANCELLED_BEFORE_START),
        )
        return read_run(db, run_id)


def resume_run(db: sqlite3.Connection, run_id: str) -> Run:
    with transaction(db):
        state = read_run(db, run_id)
        if state.status == "cancelled":
            raise LabError("request was cancelled before it started", "request_cancelled")
        if state.status == "complete":
            raise LabError("run is already complete")
        retry_at = state.retry_not_before_unix
        if state.status == "failed":
            if retry_at is not None:
                if time() < retry_at:
                    raise LabError(f"source rate limit: wait until Unix time {retry_at} before resuming",
                                   "source_retry_wait")
            elif state.failure not in RETRYABLE:
                raise LabError("run is not safe to resume; inspect the recorded failure")
        # If the previous worker was interrupted, the caller holds the store lease.
        db.execute("UPDATE import_runs SET status = 'running', failure = NULL WHERE run_id = ?", (run_id,))
        if retry_at is not None:
            db.execute("DELETE FROM import_retry_gates WHERE run_id = ?", (run_id,))
    return read_run(db, run_id)


def record_failure(db: sqlite3.Connection, run_id: str, error: LabError | sqlite3.Error) -> None:
    reason = error.code if isinstance(error, LabError) else "database_error"
    retry_delay = error.retry_after_seconds if isinstance(error, LabError) and reason == SOURCE_THROTTLED else None
    with transaction(db):
        result = db.execute("UPDATE import_runs SET status = 'failed', failure = ? "
                            "WHERE run_id = ? AND status = 'running'", (reason, run_id))
        if result.rowcount and retry_delay is not None:
            db.execute(RETRY_GATE_SCHEMA)
            # Round up so operator resume never starts before the full advertised delay.
            db.execute("INSERT INTO import_retry_gates VALUES (?, ?)", (run_id, ceil(time()) + retry_delay))


def apply_page(db: sqlite3.Connection, state: Run, page: SourcePage) -> Run:
    page_ids = check_progress(page, state.seen_ids, state.seen_cursors)
    next_ids = (*state.seen_ids, *page_ids)
    next_cursors = (*state.seen_cursors, page.next_cursor) if page.next_cursor is not None else state.seen_cursors
    committed_pages = state.committed_pages + 1
    if (len(page.rows) != PAGE_SIZE or not 1 <= committed_pages <= PAGE_LIMIT
            or (page.next_cursor is None) != (committed_pages == PAGE_LIMIT)):
        raise LabError("invalid page transition", "invalid_checkpoint")
    status = "complete" if page.next_cursor is None else "running"
    with transaction(db):
        changed = db.executemany(
            """INSERT INTO records (id, title) VALUES (?, ?)
               ON CONFLICT(id) DO UPDATE SET title = excluded.title
               WHERE records.title <> excluded.title""",
            page.rows,
        ).rowcount
        result = db.execute(
            """UPDATE import_runs SET status = ?, committed_pages = ?, next_cursor = ?,
                      seen_ids = ?, seen_cursors = ?, changed_records = changed_records + ?
               WHERE run_id = ? AND committed_pages = ? AND status = 'running' AND next_cursor IS ?""",
            (status, committed_pages, page.next_cursor, json.dumps(next_ids), json.dumps(next_cursors),
             changed, state.run_id, state.committed_pages, state.next_cursor),
        )
        if result.rowcount != 1:
            raise LabError("checkpoint changed while applying a page", "invalid_checkpoint")
    return read_run(db, state.run_id)


def run_import(db: sqlite3.Connection, state: Run) -> Run:
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as transport:
            token = request_token(transport, fixture_secret())
            while state.status != "complete":
                page = fetch_page(transport, token, state.next_cursor, state.committed_pages + 1)
                state = apply_page(db, state, page)
    except (LabError, sqlite3.Error) as error:
        record_failure(db, state.run_id, error)
    return read_run(db, state.run_id)


def inspect_staff_queue() -> dict:
    """Read one snapshot without acquiring a lease or starting/resuming a run."""
    try:
        path, _ = store_paths(STAFF_STORE, create=False)
    except LabError as error:
        if error.code != "unknown_store":
            raise
        return {"queued": [], "blocking_run": None}

    with closing(open_state(path, read_only=True)) as db:
        # Both lists and the checkpoint must come from the same read snapshot.
        db.execute("BEGIN")
        queued = [
            row["run_id"] for row in db.execute(
                """SELECT s.run_id FROM staff_requests s LEFT JOIN import_runs r USING (run_id)
                   WHERE r.run_id IS NULL ORDER BY s.run_id LIMIT ?""",
                (MAX_QUEUED + 1,),
            )
        ]
        blockers = db.execute(
            f"""SELECT r.run_id, s.run_id AS request_id
               FROM import_runs r LEFT JOIN staff_requests s USING (run_id)
               WHERE {UNFINISHED} LIMIT 2""",
            (CANCELLED_BEFORE_START, SNAPSHOT),
        ).fetchall()
        if (len(queued) > MAX_QUEUED or len(blockers) > 1
                or any(not RUN_ID.fullmatch(run_id) for run_id in queued)):
            raise LabError("staff queue state is inconsistent; inspect the store", "invalid_queue")

        blocking_run = None
        if blockers:
            blocker = blockers[0]
            run_id = blocker["run_id"]
            if blocker["request_id"] is None or not RUN_ID.fullmatch(run_id):
                raise LabError("staff queue state is inconsistent; inspect the store", "invalid_queue")
            run = read_run(db, run_id)
            if run.status == "running":
                # A saved 'running' row alone cannot tell us whether a worker is alive.
                next_action = "check_worker"
            elif run.retry_not_before_unix is not None:
                next_action = "wait_rate_limit" if time() < run.retry_not_before_unix else "resume_after_recovery"
            elif run.failure in RETRYABLE:
                next_action = "resume_after_recovery"
            else:
                next_action = "inspect_failure"
            blocking_run = {
                "run_id": run_id, "status": run.status, "committed_pages": run.committed_pages,
                "next_action": next_action,
            }
            if run.retry_not_before_unix is not None:
                blocking_run["retry_not_before_unix"] = run.retry_not_before_unix
        return {"queued": queued, "blocking_run": blocking_run}


def reconcile_import(path: Path, run_id: str) -> dict:
    """Compare checkpoint IDs and present destination data with a fixed reference."""
    with closing(open_state(path, read_only=True)) as db:
        # The checkpoint and both destination queries share one read snapshot.
        db.execute("BEGIN")
        return reconcile_at(db, run_id)


def reconcile_at(db: sqlite3.Connection, run_id: str) -> dict:
    """Evaluate reconciliation from the caller's consistent SQLite transaction."""
    run = read_run(db, run_id)
    if run.status != "complete" or run.failure is not None:
        raise LabError("reconciliation requires a complete run; inspect its status", "run_not_complete")
    if run.snapshot not in RECONCILIATION_FIXTURES:
        raise LabError("no fixed reference for this snapshot", "unknown_reference")
    expected = dict(RECONCILIATION_FIXTURES[run.snapshot])
    ids = tuple(expected)
    placeholders = ", ".join("?" for _ in ids)
    stored = dict(db.execute(f"SELECT id, title FROM records WHERE id IN ({placeholders})", ids))
    stored_count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    missing_count = len(expected) - len(stored)
    unexpected_count = stored_count - len(stored)
    value_mismatches = sum(stored[case_id] != expected[case_id] for case_id in stored)
    checkpoint_ids_match = set(run.seen_ids) == expected.keys()
    return {
        "run_id": run.run_id,
        "snapshot": run.snapshot,
        "expected_records": len(expected),
        "stored_records": stored_count,
        "missing_records": missing_count,
        "unexpected_records": unexpected_count,
        "value_mismatches": value_mismatches,
        "checkpoint_ids_match": checkpoint_ids_match,
        "matches": checkpoint_ids_match and not (missing_count or unexpected_count or value_mismatches),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", help="local store; staff commands default to staff, others to demo")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="start a new standalone run")
    commands.add_parser("run-request", help="start a queued staff request").add_argument("run_id")
    commands.add_parser("cancel-request", help="cancel a staff request that has not started").add_argument("run_id")
    commands.add_parser("queue", help="read queued staff run IDs and any blocking run")
    for command, description in (("resume", "continue from the last committed page"),
                                 ("status", "show a saved checkpoint without changing it"),
                                 ("reconcile", "compare a completed run and current store with the fixed fixture")):
        commands.add_parser(command, help=description).add_argument("run_id")
    args = parser.parse_args()
    if args.command not in {"run", "queue"} and not RUN_ID.fullmatch(args.run_id):
        parser.error("provide the 32-character run ID printed by the CLI")
    staff_commands = {"queue", "run-request", "cancel-request"}
    store = args.store or (STAFF_STORE if args.command in staff_commands else "demo")
    if args.command in staff_commands and store != STAFF_STORE:
        parser.error(f"{args.command} uses the staff store")
    if args.command == "run" and store == STAFF_STORE:
        parser.error("the staff store uses run-request, not standalone run")
    try:
        if args.command == "queue":
            print(json.dumps(inspect_staff_queue()))
            return 0
        path, lease = store_paths(store, create=args.command == "run")
        if args.command == "reconcile":
            report = reconcile_import(path, args.run_id)
            print(json.dumps(report))
            return int(not report["matches"])
        if args.command == "status":
            with closing(open_state(path, read_only=True)) as db:
                state = read_run(db, args.run_id)
        elif args.command == "cancel-request":
            with closing(open_state(path)) as db:
                state = cancel_staff_request(db, args.run_id)
        else:
            with import_lease(lease), closing(open_state(path)) as db:
                db.executescript(SCHEMA)
                if args.command == "run":
                    state = start_run(db)
                elif args.command == "run-request":
                    with transaction(db):
                        state = claim_staff_request(db, args.run_id)
                else:
                    state = resume_run(db, args.run_id)
                state = run_import(db, state)
    except LabError as error:
        print(f"Import stopped: {error}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print("Import stopped: local store unavailable; inspect its last saved status", file=sys.stderr)
        return 1
    print(json.dumps(state.result()))
    return int(state.status == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
