"""Offline staff request/idempotency checks using real SQLite and signed local tokens."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt

from app import AUDIENCE, CLIENT, COOKIE, ISSUER, ORIGIN, Session, app, jwks, sessions
import import_job
from import_job import MAX_QUEUED, STAFF_STORE, apply_page, claim_staff_request, open_state, store_paths, transaction
import staff_requests
from worker import SourcePage

KEY = "fixed-staff-request-key"


class StaffRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(import_job, "STATE_ROOT", Path(directory) / "state"))
        self.enterContext(patch.object(
            jwks, "get_signing_key_from_jwt", return_value=SimpleNamespace(key=self.key.public_key()),
        ))
        self.client = self.enterContext(TestClient(app, base_url=ORIGIN, follow_redirects=False))

    def token(self, subject, role="cases_importer", **extra):
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": subject, "typ": "Bearer",
                  "azp": CLIENT, "exp": int(time.time()) + 120, "realm_access": {"roles": [role]}}
        return jwt.encode(claims | extra, self.key, algorithm="RS256")

    def request(self, subject, role="cases_importer", key=KEY, **kwargs):
        headers = {"Authorization": "Bearer " + self.token(subject, role)}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.post("/api/imports", headers=headers, **kwargs)

    def status(self, url, subject, role="cases_importer", **claims):
        return self.client.get(url, headers={"Authorization": "Bearer " + self.token(subject, role, **claims)})

    def test_replay_keeps_one_durable_id_and_owner_controls_read(self):
        created = self.request("owner-a")
        body = created.json()
        self.assertEqual((created.status_code, body["status"]), (202, "queued"))
        self.assertEqual(created.headers["location"], body["status_url"])
        self.assertEqual(created.headers["cache-control"], "no-store")
        self.assertEqual(set(body), {"run_id", "status", "status_url", "committed_pages", "processed_records",
                                     "changed_records", "failure"})
        path, _ = store_paths(STAFF_STORE, create=False)
        with closing(open_state(path, read_only=True)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0], 0)
        # A fresh client and a fresh DB connection recover the original result.
        with TestClient(app, base_url=ORIGIN) as fresh:
            replay = fresh.post("/api/imports", headers={
                "Authorization": "Bearer " + self.token("owner-a"), "Idempotency-Key": KEY,
            })
            self.assertEqual((replay.status_code, replay.json()), (200, body))
        url = body["status_url"]
        self.assertEqual(self.status(url, "owner-a").json(), body)
        self.assertEqual(self.status(url, "another-importer").status_code, 404)
        self.assertEqual(self.status(url, "owner-a", "cases_viewer").status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.status(url, "worker", iss="http://localhost:8180/realms/workload-lab").status_code, 401)
        other = self.request("another-importer")
        self.assertEqual(other.status_code, 202)
        self.assertNotEqual(other.json()["run_id"], body["run_id"])

    def test_denials_and_read_only_status_never_create_a_store(self):
        url = "/api/imports/" + "0" * 32
        self.assertEqual(self.status(url, "owner-a").status_code, 404)
        self.assertEqual(self.status("/api/imports/not-an-id", "owner-a").status_code, 404)
        self.assertEqual(self.request("owner-a", "cases_viewer").status_code, 403)
        self.assertEqual(self.request("").status_code, 401)
        self.assertEqual(self.request("owner-a", key=None).status_code, 400)
        self.assertEqual(self.request("owner-a", key="short").status_code, 400)
        self.assertEqual(self.request("owner-a", content=b"anything").status_code, 400)
        self.assertEqual(self.client.post("/api/imports", headers=[
            ("Authorization", "Bearer " + self.token("owner-a")),
            ("Idempotency-Key", KEY), ("Idempotency-Key", KEY),
        ]).status_code, 400)
        self.assertFalse(import_job.STATE_ROOT.exists())

    def test_concurrent_identical_requests_insert_once_and_pending_queue_is_bounded(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: staff_requests.accept("owner-a", KEY), range(8)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({result["run_id"] for result, _ in results}), 1)
        for n in range(MAX_QUEUED - 1):
            self.assertEqual(self.request("owner-a", key=f"another-request-{n}").status_code, 202)
        self.assertEqual(self.request("owner-a", key="one-extra-request").status_code, 409)
        self.assertEqual(self.request("owner-a").status_code, 200)

    def test_only_an_explicit_claim_changes_status_and_reuse_does_not_rerun(self):
        run_id = self.request("owner-a").json()["run_id"]
        url = "/api/imports/" + run_id
        path, _ = store_paths(STAFF_STORE, create=False)
        with closing(open_state(path)) as db:
            with transaction(db):
                state = claim_staff_request(db, run_id)
            self.assertEqual(self.status(url, "owner-a").json()["status"], "running")
            for n in range(3):
                next_cursor = f"after-{2 * (n + 1):03d}" if n < 2 else None
                state = apply_page(db, state, SourcePage(tuple(
                    (f"SOURCE-{i:03d}", f"Synthetic case {i}") for i in (2 * n + 1, 2 * n + 2)
                ), next_cursor))
            self.assertEqual(state.run_id, run_id)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 6)
            with self.assertRaisesRegex(import_job.LabError, "already started"), transaction(db):
                claim_staff_request(db, run_id)
        complete = self.status(url, "owner-a")
        self.assertEqual((complete.status_code, complete.json()["status"]), (200, "complete"))
        self.assertEqual(complete.json()["processed_records"], 6)
        self.assertEqual(self.request("owner-a").json(), complete.json())

    def test_cancelled_request_replays_same_id_owner_only_and_frees_queue_slot(self):
        body = self.request("owner-a").json()
        for n in range(MAX_QUEUED - 1):
            self.assertEqual(self.request("owner-a", key=f"another-request-{n}").status_code, 202)
        self.assertEqual(self.request("owner-a", key="replacement-key-01").status_code, 409)
        path, _ = store_paths(STAFF_STORE, create=False)
        with closing(open_state(path)) as db:
            import_job.cancel_staff_request(db, body["run_id"])
        cancelled = {**body, "status": "cancelled"}
        url = body["status_url"]
        self.assertEqual(self.status(url, "owner-a").json(), cancelled)
        self.assertEqual(self.status(url, "different-importer").status_code, 404)
        self.assertEqual(self.status(url, "owner-a", "cases_viewer").status_code, 403)
        replay = self.request("owner-a")
        self.assertEqual((replay.status_code, replay.json()), (200, cancelled))
        self.assertEqual(replay.headers["location"], url)
        replacement = self.request("owner-a", key="replacement-key-01")
        self.assertEqual((replacement.status_code, replacement.json()["status"]), (202, "queued"))
        self.assertNotEqual(replacement.json()["run_id"], body["run_id"])
        self.assertEqual(self.request("owner-a", key="replacement-key-02").status_code, 409)

    def test_case_preview_uses_existing_reader_roles_and_never_creates_a_store(self):
        url = "/api/imported-cases"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.status(url, "worker", iss="http://localhost:8180/realms/workload-lab").status_code, 401)
        self.assertEqual(self.status(url, "fixture-unmapped", "not-a-case-reader").status_code, 403)
        for given_role in ("cases_viewer", "cases_importer"):
            response = self.status(url, "synthetic-case-reader", given_role)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"staged": False, "snapshot": None, "records": []})
        self.assertFalse(import_job.STATE_ROOT.exists())

    def test_recent_empty_is_read_only_and_requires_staff_importer(self):
        url = "/api/imports"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.status(url, "owner-a", "cases_viewer").status_code, 403)
        self.assertEqual(self.status(url, "worker", iss="http://localhost:8180/realms/workload-lab").status_code, 401)
        reply = self.status(url, "owner-a")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json(), {"limit": 10, "requests": []})
        self.assertEqual(reply.headers["cache-control"], "no-store")
        self.assertFalse(import_job.STATE_ROOT.exists())

    def test_recent_applies_owner_before_limit_and_exposes_only_safe_status(self):
        old = self.request("owner-a").json()
        own = [old]
        path, lease = store_paths(STAFF_STORE, create=False)
        with closing(open_state(path)) as db:
            import_job.cancel_staff_request(db, old["run_id"])
            for i in range(1, 14):
                item = self.request("owner-a", key=f"private-fixture-key-{i}").json()
                own.append(item)
                import_job.cancel_staff_request(db, item["run_id"])
            other = self.request("owner-b").json()
            own.append(self.request("owner-a", key="private-final-fixture-key").json())
        saved = path.read_bytes()
        # Neither a query string nor an alternate importer can impersonate the owner.
        reply = self.status("/api/imports?staff_sub=owner-b", "owner-a")
        self.assertEqual(reply.status_code, 200)
        items = reply.json()["requests"]
        self.assertEqual([item["run_id"] for item in items], [item["run_id"] for item in reversed(own[-10:])])
        self.assertEqual(items[0]["status"], "queued")
        self.assertTrue(all(item["status"] == "cancelled" for item in items[1:]))
        self.assertEqual(self.status("/api/imports?staff_sub=owner-a", "owner-b").json()["requests"], [other])
        self.assertIn("status_url", items[0])
        self.assertEqual(set(items[0]), set(old))
        for unwanted in ("private-fixture-key", "owner-a", "owner-b", other["run_id"]):
            self.assertNotIn(unwanted, reply.text)
        self.assertEqual(path.read_bytes(), saved)
        self.assertFalse(lease.exists())

    def test_recent_invalid_store_does_not_claim_an_empty_history(self):
        path, _ = store_paths(STAFF_STORE, create=True)
        path.write_bytes(b"broken lab fixture, no confidential content")
        reply = self.status("/api/imports", "owner-a")
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(reply.json(), {"detail": "Local import store unavailable"})

    def test_browser_post_redirects_to_the_same_status_and_keeps_csrf(self):
        cookie = "test-browser-session"
        sessions[cookie] = Session(time.monotonic() + 60, {
            "access_token": self.token("owner-a"), "csrf": "fake-csrf-value",
        })
        self.addCleanup(sessions.pop, cookie, None)
        self.client.cookies.set(COOKIE, cookie)
        page = self.client.get("/").text
        key = re.search(r'name="key" value="([a-f0-9]{32})"', page).group(1)
        form = {"csrf": "fake-csrf-value", "key": key}
        self.assertEqual(self.client.post("/import", data=form).status_code, 403)
        self.assertFalse(import_job.STATE_ROOT.exists())
        form_post = self.client.post("/import", data=form, headers={"Origin": ORIGIN})
        self.assertEqual(form_post.status_code, 303)
        self.assertTrue(form_post.headers["location"].startswith("/import/status/"))
        replay = self.client.post("/import", data=form, headers={"Origin": ORIGIN})
        self.assertEqual(replay.headers["location"], form_post.headers["location"])
        overview = self.client.get("/imports")
        self.assertEqual(overview.status_code, 200)
        self.assertIn(f'href="{form_post.headers["location"]}"', overview.text)
        self.assertIn("queued", overview.text)
        self.assertNotIn("fake-csrf-value", overview.text)
        status = self.client.get(form_post.headers["location"])
        self.assertEqual(status.status_code, 200)
        self.assertIn("queued", status.text)
