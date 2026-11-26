"""Verify the real staff-to-operator handoff over loopback only.

Requires the disposable M3 Keycloak container. Ports 8000 and 8001 must be free.
All SQLite data is in a new temporary copy; the live lab store is never opened.
"""
from contextlib import closing, contextmanager
from html import unescape
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import httpx

from check_live_recovery import ROOT, source_process

sys.path.insert(0, str(ROOT))
from app import ORIGIN
from test_oidc import BrowserFlow
from test_saml import FederatedFlow, Page


@contextmanager
def app_process(root):
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.3):
            raise RuntimeError("port 8000 is in use")
    except ConnectionRefusedError:
        pass
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000",
         "--no-proxy-headers", "--no-access-log"],
        cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(timeout=0.4, trust_env=False) as probe:
            end = time.monotonic() + 8
            while time.monotonic() < end and process.poll() is None:
                try:
                    if probe.get(ORIGIN + "/").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("could not start the isolated staff app")
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def browser():
    return httpx.Client(base_url=ORIGIN, follow_redirects=False, trust_env=False, timeout=8)


def import_form(client):
    return next(fields for attrs, fields in Page(client.get("/").text).forms if attrs["action"] == "/import")


def post_form(client, form):
    return client.post("/import", data=form, headers={"Origin": ORIGIN})


def status(client, location):
    response = client.get(location)
    assert response.status_code == 200, response.status_code
    return json.loads(unescape(re.search(r"<pre>(.*?)</pre>", response.text).group(1)))


def worker(root, *args, expected):
    result = subprocess.run([sys.executable, "-m", "import_job", "--store", "staff", *args],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == expected, (args[0], result.returncode)
    return json.loads(result.stdout) if result.stdout else result.stderr


def verify():
    direct = BrowserFlow("test_callback_rejects_missing_transaction")
    federated = FederatedFlow("test_federated_group_mapping_and_denial")
    with TemporaryDirectory(prefix="enterprise-staff-check-") as tmp:
        root = Path(tmp).resolve()
        for module in ("app", "staff_requests", "import_job", "service_profile", "worker"):
            shutil.copy2(ROOT / f"{module}.py", root)
        (root / ".keycloak-m3-fixture").symlink_to(ROOT / ".keycloak-m3-fixture", target_is_directory=True)
        with app_process(root), browser() as first, browser() as viewer:
            direct.sign_in(first, "importer")
            form = import_form(first)
            assert first.post("/import", data=form).status_code == 403
            first_response = post_form(first, form)
            assert first_response.status_code == 303
            url = first_response.headers["location"]
            initial = status(first, url)
            run_id = initial["run_id"]
            assert url == "/import/status/" + run_id and initial["status"] == "queued"
            assert initial["processed_records"] == initial["committed_pages"] == 0
            assert first.get("/api/imports/" + run_id).status_code == 401  # cookie is not API auth
            direct.sign_in(viewer, "viewer")
            assert viewer.get(url).status_code == 403
            assert post_form(viewer, import_form(viewer)).status_code == 403

        with app_process(root), browser() as restored, browser() as other:
            direct.sign_in(restored, "importer")
            assert status(restored, url) == initial
            # A fresh app process/session, same owner and browser form idempotency key.
            replay = post_form(restored, {**import_form(restored), "key": form["key"]})
            assert (replay.status_code, replay.headers["location"]) == (303, url)
            federated.sign_in(other, "customer-importer")
            assert other.get(url).status_code == 404

            with source_process(fail_page_two=True):
                failed = worker(root, "run-request", run_id, expected=1)
                assert failed["run_id"] == run_id
                after_fail = status(restored, url)
                assert after_fail["status"] == "failed" and after_fail["failure"] == "source_http_503"
                assert after_fail["committed_pages"] == 1 and after_fail["processed_records"] == 2
            with source_process(fail_page_two=False):
                resumed = worker(root, "resume", run_id, expected=0)
                assert resumed["run_id"] == run_id
            complete = status(restored, url)
            assert complete["run_id"] == run_id and complete["status"] == "complete"
            assert complete["committed_pages"] == 3 and complete["processed_records"] == 6
            assert complete["changed_records"] == 6 and complete["failure"] is None
            assert "already started" in worker(root, "run-request", run_id, expected=1)
            final_replay = post_form(restored, {**import_form(restored), "key": form["key"]})
            assert final_replay.headers["location"] == url and status(restored, url) == complete

        reconciled = worker(root, "reconcile", run_id, expected=0)
        assert reconciled["run_id"] == run_id and reconciled["matches"]
        assert reconciled["checkpoint_ids_match"] and reconciled["stored_records"] == 6
        path = root / ".import-lab" / "staff.sqlite3"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            assert db.execute("SELECT run_id FROM staff_requests").fetchall() == [(run_id,)]
            assert db.execute("SELECT run_id FROM import_runs").fetchall() == [(run_id,)]
            assert db.execute("SELECT id, title FROM records ORDER BY id").fetchall() == [
                (f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7)
            ]
        print(json.dumps({
            "survived_app_restart": True, "same_request_same_run": True,
            "viewer_denied": True, "other_importer_cannot_read": True,
            "failure": after_fail["failure"], "saved_pages": after_fail["committed_pages"],
            "saved_records": after_fail["processed_records"], "same_run_resumed": True,
            "complete_pages": complete["committed_pages"], "complete_records": complete["processed_records"],
            "durable_requests": 1, "durable_runs": 1, "duplicate_execution_refused": True,
            "reconciled_current_destination": True,
        }))


if __name__ == "__main__":
    verify()
