"""Check the actual local importer CLI through real Keycloak, HTTP and SQLite.

Requires only the M3 Keycloak Compose service; port 8001 must be free.
All names are newly generated fixture stores. This runner never changes an
existing store, starts a container, or prints credentials.
"""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "http://localhost:8001/v1/cases"


@contextmanager
def source_process(*, fail_page_two: bool, drift_page_two: bool = False, throttle_page_two: bool = False):
    with httpx.Client(timeout=0.3, trust_env=False) as probe:
        try:
            probe.get(SOURCE)
        except httpx.ConnectError:
            pass
        else:
            raise RuntimeError("port 8001 is in use; stop the other lab source first")
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "mock_source:source", "--host", "127.0.0.1", "--port", "8001",
             "--no-proxy-headers", "--no-access-log"],
            cwd=ROOT, env={**os.environ, "LAB_M3_FAIL_PAGE_TWO": str(int(fail_page_two)),
                           "LAB_M3_DRIFT_PAGE_TWO": str(int(drift_page_two)),
                           "LAB_M3_THROTTLE_PAGE_TWO": str(int(throttle_page_two))},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            finish = time.monotonic() + 8
            while time.monotonic() < finish and process.poll() is None:
                try:
                    if probe.get(SOURCE).status_code == 401:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("could not start the isolated local source")
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def run_cli(store, *args):
    return subprocess.run([sys.executable, "-m", "import_job", "--store", store, *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=30)


def command(store, *args, expected):
    output = run_cli(store, *args)
    if output.returncode != expected:
        raise AssertionError(f"command {args[0]} returned {output.returncode}: {output.stderr}")
    return json.loads(output.stdout)


def rows(store):
    path = ROOT / ".import-lab" / f"{store}.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT id, title FROM records ORDER BY id").fetchall()


def owned_store_files(*stores):
    paths = [
        ROOT / ".import-lab" / (store + extension)
        for store in stores for extension in (".sqlite3", ".lease.sqlite3")
    ]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise RuntimeError("test store already exists")
    return paths


def verify():
    suffix = uuid4().hex[:8]
    resumed_store, clean_store = f"test-resumed-{suffix}", f"test-clean-{suffix}"
    owned_files = owned_store_files(resumed_store, clean_store)
    expected_rows = [(f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7)]
    try:
        with source_process(fail_page_two=True):
            failed = command(resumed_store, "run", expected=1)
        assert failed["status"] == "failed" and failed["failure"] == "source_http_503"
        assert failed["committed_pages"] == 1 and failed["next_cursor"] == "after-002"
        assert failed["processed_records"] == failed["changed_records"] == 2
        assert rows(resumed_store) == expected_rows[:2]
        assert command(resumed_store, "status", failed["run_id"], expected=1) == failed
        with source_process(fail_page_two=False):
            resumed = command(resumed_store, "resume", failed["run_id"], expected=0)
            clean = command(clean_store, "run", expected=0)
            rerun = command(resumed_store, "run", expected=0)
        assert resumed["run_id"] == failed["run_id"]
        assert rerun["run_id"] != resumed["run_id"]
        for complete in (resumed, clean, rerun):
            assert complete["status"] == "complete" and complete["committed_pages"] == 3
            assert complete["processed_records"] == 6 and complete["next_cursor"] is None
            assert complete["failure"] is None
        assert resumed["changed_records"] == clean["changed_records"] == 6
        assert rerun["changed_records"] == 0
        actual = rows(resumed_store)
        assert actual == rows(clean_store) == expected_rows
        digest = hashlib.sha256(json.dumps(actual, separators=(",", ":")).encode()).hexdigest()
        print(json.dumps({
            "failure": failed["failure"], "saved_page": failed["committed_pages"],
            "saved_cursor": failed["next_cursor"], "saved_records": failed["processed_records"],
            "resumed_same_run": True, "completed_pages": resumed["committed_pages"],
            "clean_equals_resumed": True, "unique_records": len(actual),
            "rerun_changed_records": rerun["changed_records"], "records_sha256": digest,
        }))
    finally:
        for path in owned_files:
            path.unlink(missing_ok=True)


def verify_snapshot_drift():
    suffix = uuid4().hex[:8]
    drift_store, clean_store = f"test-drift-{suffix}", f"test-clean-{suffix}"
    owned_files = owned_store_files(drift_store, clean_store)
    expected_rows = [(f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7)]
    try:
        with source_process(fail_page_two=False, drift_page_two=True):
            failed = command(drift_store, "run", expected=1)
        run_id = failed["run_id"]
        assert failed["status"] == "failed" and failed["failure"] == "invalid_snapshot"
        assert failed["committed_pages"] == 1 and failed["next_cursor"] == "after-002"
        assert failed["processed_records"] == failed["changed_records"] == 2
        assert rows(drift_store) == expected_rows[:2]
        drift_store_path = owned_files[0]
        saved = drift_store_path.read_bytes()
        assert command(drift_store, "status", run_id, expected=1) == failed

        # Put the healthy API back first. Unlike a 503, changing the source back
        # must never turn a saved incompatible snapshot into a resumable run.
        with source_process(fail_page_two=False):
            refusal = run_cli(drift_store, "resume", run_id)
            assert refusal.returncode == 1 and not refusal.stdout
            assert refusal.stderr.strip() == "Import stopped: run is not safe to resume; inspect the recorded failure"
            complete = command(clean_store, "run", expected=0)
        assert command(drift_store, "status", run_id, expected=1) == failed
        assert drift_store_path.read_bytes() == saved
        assert rows(drift_store) == expected_rows[:2]
        assert complete["run_id"] != run_id and complete["status"] == "complete"
        assert complete["committed_pages"] == 3 and rows(clean_store) == expected_rows
        reconciled = command(clean_store, "reconcile", complete["run_id"], expected=0)
        assert reconciled["matches"] and reconciled["stored_records"] == 6
        print(json.dumps({"failure": failed["failure"], "saved_pages": failed["committed_pages"],
                          "saved_records": failed["processed_records"], "resume_refused": True,
                          "failed_store_unchanged": True, "new_store_records": reconciled["stored_records"],
                          "new_store_reconciled": reconciled["matches"]}))
    finally:
        for path in owned_files:
            path.unlink(missing_ok=True)


def verify_rate_limit():
    store = f"test-throttle-{uuid4().hex[:8]}"
    owned_files = owned_store_files(store)
    expected_rows = [(f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7)]
    try:
        with source_process(fail_page_two=False, throttle_page_two=True):
            failed = command(store, "run", expected=1)
            run_id = failed["run_id"]
            retry_at = failed["retry_not_before_unix"]
            assert failed["status"] == "failed" and failed["failure"] == "source_http_429"
            assert failed["committed_pages"] == 1 and failed["next_cursor"] == "after-002"
            assert failed["processed_records"] == 2 and rows(store) == expected_rows[:2]
            before = owned_files[0].read_bytes()
            assert command(store, "status", run_id, expected=1) == failed
            assert time.time() < retry_at, "test expired before it could exercise the early gate"
            refusal = run_cli(store, "resume", run_id)
            assert refusal.returncode == 1 and not refusal.stdout
            assert refusal.stderr.strip() == f"Import stopped: source rate limit: wait until Unix time {retry_at} before resuming"
            assert owned_files[0].read_bytes() == before

        # Wait only in this acceptance runner, not in the importer or a background worker.
        with source_process(fail_page_two=False):
            remaining = retry_at - time.time()
            assert remaining <= 10
            if remaining > 0:
                time.sleep(remaining + 0.05)
            resumed = command(store, "resume", run_id, expected=0)
        assert resumed["run_id"] == run_id and resumed["status"] == "complete"
        assert resumed["committed_pages"] == 3 and rows(store) == expected_rows
        assert "retry_not_before_unix" not in resumed
        report = command(store, "reconcile", run_id, expected=0)
        assert report["matches"] and report["stored_records"] == 6
        print(json.dumps({"failure": failed["failure"], "saved_pages": failed["committed_pages"],
                          "saved_records": failed["processed_records"], "early_resume_refused": True,
                          "early_attempt_store_unchanged": True, "same_run_resumed": True,
                          "reconciled_records": report["stored_records"]}))
    finally:
        for path in owned_files:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cases = {"temporary-outage": verify, "snapshot-drift": verify_snapshot_drift, "rate-limit": verify_rate_limit}
    cli.add_argument("--scenario", choices=tuple(cases), default="temporary-outage")
    cases[cli.parse_args().scenario]()
