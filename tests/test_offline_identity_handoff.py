"""Offline authorization-to-recovery checks using the real API, CLI and SQLite."""
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt

from app import CLIENT, ORIGIN, app, jwks
from examples.identity_role_matrix import fixture_contract
import import_job
from keycloak.prepare_m2 import BROKER, build_realms
from service_profile import READ_SCOPE, SNAPSHOT, SOURCE, SOURCE_PATH, TOKEN_ENDPOINT

REQUEST_KEY = "fixture-owner-replay-key"
ROWS = tuple((f"SOURCE-{n:03d}", f"Fictional source case {n}") for n in range(1, 7))
PAGES = {None: (0, "after-002"), "after-002": (2, "after-004"), "after-004": (4, None)}


class OfflineIdentityHandoff(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.audience, profiles = fixture_contract(*build_realms())
        self.profiles = {entry.label: entry for entry in profiles}
        self.signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.enterContext(patch.object(jwks, "get_signing_key_from_jwt",
                          return_value=SimpleNamespace(key=self.signer.public_key())))

        def offline(*args, **kwargs):
            raise AssertionError("socket or DNS access is not allowed in this test")

        self.enterContext(patch.object(socket.socket, "connect", offline))
        self.enterContext(patch.object(socket, "getaddrinfo", offline))
        self.api = self.enterContext(TestClient(app, base_url=ORIGIN))

    def auth(self, label, subject, roles=None):
        profile = self.profiles[label]
        token = jwt.encode({
            "iss": BROKER, "aud": self.audience, "sub": subject, "typ": "Bearer", "azp": CLIENT,
            "exp": int(time.time()) + 120,
            "realm_access": {"roles": list(profile.roles if roles is None else roles)},
            # A fixture-only provenance marker, not a claim about Keycloak account linking.
            "fixture_auth_path": profile.path,
        }, self.signer, algorithm="RS256")
        return {"Authorization": "Bearer " + token, "Idempotency-Key": REQUEST_KEY}

    def cli(self, operation, run_id, *, code):
        out, err = StringIO(), StringIO()
        args = ["import_job", "--store", "staff", operation, run_id]
        with patch.object(sys, "argv", args), redirect_stdout(out), redirect_stderr(err):
            observed = import_job.main()
        self.assertEqual(observed, code, err.getvalue())
        self.assertEqual(err.getvalue(), "")
        return json.loads(out.getvalue())

    def worker_phase(self, operation, run_id, *, unavailable):
        calls = []

        def route(request: httpx.Request) -> httpx.Response:
            if request.url == httpx.URL(TOKEN_ENDPOINT) and request.method == "POST":
                body = parse_qs(request.content.decode("ascii"))
                self.assertEqual(body, {"grant_type": ["client_credentials"], "scope": [READ_SCOPE]})
                calls.append("workload-grant")
                return httpx.Response(200, json={"token_type": "Bearer", "access_token": "local.fixture.only"})
            if request.url.copy_with(query=None) != httpx.URL(SOURCE + SOURCE_PATH) or request.method != "GET":
                raise AssertionError("unexpected fixture route")
            if request.headers.get("authorization") != "Bearer local.fixture.only":
                raise AssertionError("staff credential was used or workload credential missing")
            cursor = request.url.params.get("cursor")
            self.assertIn(cursor, PAGES)
            calls.append(cursor)
            if unavailable and cursor == "after-002":
                return httpx.Response(503)
            offset, next_cursor = PAGES[cursor]
            data = [{"id": record_id, "title": title} for record_id, title in ROWS[offset:offset + 2]]
            return httpx.Response(200, json={"snapshot": SNAPSHOT, "records": data, "next_cursor": next_cursor})

        transport = httpx.MockTransport(route)

        def client(**options):
            return httpx.Client(transport=transport, **options)

        with (patch.object(import_job, "httpx", SimpleNamespace(Client=client)),
              patch.object(import_job, "fixture_secret", return_value="offline-fixture-placeholder")):
            result = self.cli(operation, run_id, code=int(unavailable))
        return result, calls

    def store_evidence(self):
        path, _ = import_job.store_paths("staff", create=False)
        with closing(import_job.open_state(path, read_only=True)) as db:
            return (db.execute("SELECT COUNT(*) FROM staff_requests").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0],
                    [tuple(item) for item in db.execute("SELECT id, title FROM records ORDER BY id")])

    def test_direct_and_federated_importers_replay_recover_and_reconcile_the_same_way(self):
        outcomes = []
        denied = [self.auth(label, "denied-" + label)
                  for label in ("viewer", "customer-viewer", "customer-unmapped")]
        for label in ("importer", "customer-importer"):
            with self.subTest(label=label), patch.object(import_job, "STATE_ROOT", self.folder / label):
                for candidate in denied:
                    self.assertEqual(self.api.post("/api/imports", headers=candidate).status_code, 403)
                self.assertFalse(import_job.STATE_ROOT.exists())

                owner = self.auth(label, "owner-" + label)
                accepted = self.api.post("/api/imports", headers=owner)
                self.assertEqual(accepted.status_code, 202)
                request = accepted.json()
                run_id, url = request["run_id"], request["status_url"]
                self.assertEqual((request["status"], request["processed_records"]), ("queued", 0))
                path, lease = import_job.store_paths("staff", create=False)
                queued_bytes = path.read_bytes()
                replay = self.api.post("/api/imports", headers=owner)
                self.assertEqual((replay.status_code, replay.json()), (200, request))
                self.assertEqual(self.store_evidence(), (1, 0, []))
                self.assertEqual(path.read_bytes(), queued_bytes)
                self.assertFalse(lease.exists())

                failed, failed_calls = self.worker_phase("run-request", run_id, unavailable=True)
                self.assertEqual(failed_calls, ["workload-grant", None, "after-002"])
                self.assertEqual((failed["run_id"], failed["failure"], failed["next_cursor"]),
                                 (run_id, "source_http_503", "after-002"))
                status = self.api.get(url, headers=owner)
                self.assertEqual(status.status_code, 200)
                self.assertEqual((status.json()["status"], status.json()["committed_pages"],
                                  status.json()["processed_records"]), ("failed", 1, 2))
                self.assertEqual(self.store_evidence(), (1, 1, list(ROWS[:2])))

                before = path.read_bytes(), lease.read_bytes()
                for candidate in denied:
                    self.assertEqual(self.api.post("/api/imports", headers=candidate).status_code, 403)
                    self.assertEqual(self.api.get(url, headers=candidate).status_code, 403)
                self.assertEqual((path.read_bytes(), lease.read_bytes()), before)

                complete, renewed_calls = self.worker_phase("resume", run_id, unavailable=False)
                self.assertEqual(renewed_calls, ["workload-grant", "after-002", "after-004"])
                self.assertEqual((complete["run_id"], complete["committed_pages"], complete["processed_records"]),
                                 (run_id, 3, 6))
                before = path.read_bytes(), lease.read_bytes()
                final = self.api.get(url, headers=owner)
                self.assertEqual(final.status_code, 200)
                self.assertEqual((final.json()["status"], final.json()["changed_records"]), ("complete", 6))
                replay = self.api.post("/api/imports", headers=owner)
                self.assertEqual((replay.status_code, replay.json()), (200, final.json()))
                verified = self.cli("reconcile", run_id, code=0)
                self.assertTrue(verified["matches"])
                self.assertEqual((path.read_bytes(), lease.read_bytes()), before)
                self.assertEqual(self.store_evidence(), (1, 1, list(ROWS)))
                outcomes.append([{k: v for k, v in stage.items() if k != "run_id"} for stage in
                                 (failed, complete, verified)])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0], outcomes[1])

    def test_canonical_subject_and_roles_scope_idempotence_not_fixture_login_path(self):
        roles = tuple(sorted(set(self.profiles["importer"].roles) & set(self.profiles["customer-importer"].roles)))
        self.assertEqual(roles, ("cases_importer",))
        with patch.object(import_job, "STATE_ROOT", self.folder / "canonical"):
            direct = self.auth("importer", "shared-synthetic-subject", roles)
            federated = self.auth("customer-importer", "shared-synthetic-subject", roles)
            other = self.auth("customer-importer", "different-synthetic-subject", roles)
            one = self.api.post("/api/imports", headers=direct)
            self.assertEqual(one.status_code, 202)
            replay = self.api.post("/api/imports", headers=federated)
            self.assertEqual((replay.status_code, replay.json()), (200, one.json()))
            self.assertEqual(self.store_evidence(), (1, 0, []))
            url = one.json()["status_url"]
            self.assertEqual(self.api.get(url, headers=federated).json(), one.json())
            self.assertEqual(self.api.get(url, headers=other).status_code, 404)
            distinct = self.api.post("/api/imports", headers=other)
            self.assertEqual(distinct.status_code, 202)
            self.assertNotEqual(distinct.json()["run_id"], one.json()["run_id"])
            self.assertEqual(self.store_evidence(), (2, 0, []))
