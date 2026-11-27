"""Verify separate token profiles, read-only scope, and the real CLI source path."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt

from app import COOKIE, ORIGIN, app, sessions
from keycloak.prepare_m3 import build_m3_realms
import mock_source
from mock_source import keys, source
from service_profile import ISSUER, READ_SCOPE, SNAPSHOT, SOURCE, SOURCE_AUDIENCE, SOURCE_PATH, WORKER_CLIENT
import test_oidc
from worker import LabError, fetch_all, fixture_secret, request_token


def authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class SourcePolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True)
        cls.jwk = jwt.PyJWK.from_dict({**public, "kid": "fixture-key", "alg": "RS256"})

    def test_claims_and_scope_are_checked_separately(self):
        base = {"iss": ISSUER, "aud": SOURCE_AUDIENCE, "sub": "synthetic-worker", "typ": "Bearer",
                "azp": WORKER_CLIENT, "scope": READ_SCOPE, "exp": int(time.time()) + 120}
        profiles = (
            ("worker", {}, 200),
            ("wrong issuer", {"iss": "https://other.example.invalid"}, 401),
            ("wrong audience", {"aud": "cases-api"}, 401),
            ("mixed audiences", {"aud": [SOURCE_AUDIENCE, "cases-api"]}, 401),
            ("wrong client", {"azp": "lab-web"}, 401),
            ("wrong token type", {"typ": "ID"}, 401),
            ("expired", {"exp": int(time.time()) - 120}, 401),
            ("blank subject", {"sub": ""}, 401),
            ("no scope", {"scope": ""}, 403),
            ("other scope", {"scope": READ_SCOPE + ".extra"}, 403),
            ("wrong scope shape", {"scope": [READ_SCOPE]}, 403),
        )
        with TestClient(source, base_url=SOURCE) as client:
            self.assertEqual(client.get(SOURCE_PATH).status_code, 401)
            self.assertEqual(client.get(SOURCE_PATH, headers=authorization("not-a-jwt")).status_code, 401)
            with patch.object(keys, "get_signing_key", return_value=self.jwk):
                for label, claims, expected in profiles:
                    with self.subTest(label=label):
                        token = jwt.encode(base | claims, self.key, algorithm="RS256",
                                           headers={"kid": "fixture-key"})
                        response = client.get(SOURCE_PATH, headers=authorization(token))
                        self.assertEqual(response.status_code, expected)
                        self.assertEqual(response.headers["cache-control"], "no-store")
                        if expected == 403:
                            self.assertEqual(response.headers["www-authenticate"],
                                             f'Bearer error="insufficient_scope", scope="{READ_SCOPE}"')
                        if expected != 200:
                            self.assertNotIn("records", response.json())
                token = jwt.encode(base, self.key, algorithm="RS256", headers={"kid": "fixture-key"})
                self.assertEqual(client.get(SOURCE_PATH, params={"cursor": "unknown"},
                                            headers=authorization(token)).status_code, 400)
                self.assertEqual(client.post(SOURCE_PATH, headers=authorization(token)).status_code, 405)

    def test_idp_unavailable_does_not_grant_access(self):
        token = jwt.encode({"sub": "fixture"}, self.key, algorithm="RS256", headers={"kid": "fixture-key"})
        with (TestClient(source, base_url=SOURCE) as client,
              patch.object(keys, "get_signing_key", side_effect=jwt.PyJWKClientConnectionError)):
            response = client.get(SOURCE_PATH, headers=authorization(token))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("records", response.json())

    def test_fixture_does_not_grant_browser_or_user_permissions_to_worker(self):
        broker, customer, workload = build_m3_realms("synthetic-validator-not-a-real-secret")
        self.assertEqual([realm["realm"] for realm in (broker, customer, workload)],
                         ["integration-lab", "customer-lab", "workload-lab"])
        self.assertNotIn("clientScopes", broker)
        (web,), (worker,) = broker["clients"], workload["clients"]
        self.assertEqual(worker["clientId"], WORKER_CLIENT)
        self.assertTrue(worker["serviceAccountsEnabled"])
        for flag in ("publicClient", "standardFlowEnabled", "implicitFlowEnabled",
                     "directAccessGrantsEnabled", "fullScopeAllowed"):
            self.assertIs(worker[flag], False, flag)
        self.assertEqual(worker["optionalClientScopes"], [READ_SCOPE])
        self.assertNotIn(READ_SCOPE, web.get("optionalClientScopes", []))
        self.assertEqual(worker["redirectUris"], [])


class PagingChecks(unittest.TestCase):
    def test_http_200_with_a_changed_page_two_marker_is_not_a_complete_read(self):
        with (patch.object(mock_source, "FAIL_PAGE_TWO", False),
              patch.object(mock_source, "DRIFT_PAGE_TWO", True)):
            pages = [mock_source.read_cases(cursor) for cursor in (None, "after-002", "after-004")]
        self.assertEqual([page["snapshot"] for page in pages], [SNAPSHOT, "case-fixture-drift-demo", SNAPSHOT])
        self.assertEqual(pages[1]["next_cursor"], "after-004")
        attempted = []

        def send(request):
            attempted.append(request.url.params.get("cursor"))
            return httpx.Response(200, json=pages[len(attempted) - 1])

        with httpx.Client(transport=httpx.MockTransport(send)) as client:
            with self.assertRaisesRegex(LabError, "page 2: source snapshot changed") as refused:
                fetch_all(client, "opaque-fixture")
        self.assertEqual(refused.exception.code, "invalid_snapshot")
        self.assertEqual(attempted, [None, "after-002"])

    def test_incomplete_or_repeated_page_is_never_a_complete_read(self):
        def page(next_cursor):
            return {"snapshot": SNAPSHOT, "records": [{"id": f"SOURCE-{n:03d}", "title": "Synthetic case"} for n in (1, 2)],
                    "next_cursor": next_cursor}

        for label, pages, error in (
            ("early end", [httpx.Response(200, json=page(None))], "invalid source cursor"),
            ("page 2 unavailable", [httpx.Response(200, json=page("p2")), httpx.Response(503)],
             "page 2: source returned HTTP 503"),
            ("duplicate rows", [httpx.Response(200, json=page("p2")),
                                httpx.Response(200, json=page("p3"))], "duplicate source record"),
        ):
            with self.subTest(label=label):
                replies = iter(pages)
                with httpx.Client(transport=httpx.MockTransport(lambda request: next(replies))) as client:
                    with self.assertRaisesRegex(LabError, error):
                        fetch_all(client, "opaque-fixture")


@unittest.skipUnless(os.environ.get("RUN_LOCAL_M3") == "1", "opt in and run the local IdP and both Uvicorn apps")
class LiveServiceIdentity(unittest.TestCase):
    def test_real_grant_keeps_worker_cases_and_browser_source_separate(self):
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            secret = fixture_secret()
            read_token = request_token(client, secret)
            no_scope = request_token(client, secret, scoped=False)
            # Same issuer/client/audience, but only the scoped token may read.
            self.assertEqual(client.get(SOURCE + SOURCE_PATH).status_code, 401)
            self.assertEqual(client.get(SOURCE + SOURCE_PATH, headers=authorization(no_scope)).status_code, 403)
            self.assertEqual(client.get(SOURCE + SOURCE_PATH, headers=authorization(read_token)).status_code, 200)
            self.assertEqual(client.post(SOURCE + SOURCE_PATH, headers=authorization(read_token)).status_code, 405)
            self.assertEqual(client.get(ORIGIN + "/api/cases", headers=authorization(read_token)).status_code, 401)
            self.assertEqual(client.post(ORIGIN + "/api/imports", headers=authorization(read_token)).status_code, 401)
            with TestClient(app, base_url=ORIGIN, follow_redirects=False) as browser:
                # The normal public browser client obtains the actual claims from local Keycloak.
                test_oidc.BrowserFlow("test_callback_rejects_missing_transaction").sign_in(browser, "importer")
                session_id = browser.cookies.get(COOKIE)
                try:
                    token = sessions[session_id].data["access_token"]
                    self.assertEqual(client.get(ORIGIN + "/api/cases", headers=authorization(token)).status_code, 200)
                    self.assertEqual(client.get(SOURCE + SOURCE_PATH, headers=authorization(token)).status_code, 401)
                finally:
                    sessions.pop(session_id, None)

    def test_cli_reads_exact_fixture_records_through_live_http(self):
        output = subprocess.run([sys.executable, "-m", "worker"], capture_output=True, text=True, timeout=25)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(json.loads(output.stdout), {
            "status": "read_complete", "pages": 3, "count": 6,
            "record_ids": [f"SOURCE-{n:03d}" for n in range(1, 7)],
        })
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            rows = fetch_all(client, request_token(client, fixture_secret()))
        self.assertEqual(rows, [
            {"id": f"SOURCE-{n:03d}", "title": f"Fictional source case {n}"} for n in range(1, 7)
        ])
