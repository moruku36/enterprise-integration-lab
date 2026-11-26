"""Integration checks against the local Keycloak fixture, never a real tenant."""
import os
import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from html import unescape
from http.cookiejar import CookieJar, DefaultCookiePolicy
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt

import import_job
from app import AUDIENCE, CLIENT, COOKIE, ISSUER, ORIGIN, app, jwks, sessions


class ApiValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def test_missing_token(self):
        response = TestClient(app, base_url=ORIGIN).get("/api/cases")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_signed_fixture_validation_matrix(self):
        base = {"iss": ISSUER, "aud": AUDIENCE, "sub": "fixture", "typ": "Bearer",
                "azp": CLIENT, "exp": int(time.time()) + 120,
                "realm_access": {"roles": ["cases_viewer"]}}
        variants = [
            ("viewer", {}, 200),
            ("expired", {"exp": int(time.time()) - 120}, 401),
            ("wrong audience", {"aud": "other-api"}, 401),
            ("wrong issuer", {"iss": "https://other.example.invalid"}, 401),
            ("ID token type", {"typ": "ID"}, 401),
            ("wrong authorized party", {"azp": "other-client"}, 401),
            ("no role", {"realm_access": {"roles": []}}, 403),
        ]
        client = TestClient(app, base_url=ORIGIN)
        with patch.object(jwks, "get_signing_key_from_jwt", return_value=SimpleNamespace(key=self.key.public_key())):
            for label, claims, expected in variants:
                with self.subTest(label=label):
                    token = jwt.encode(base | claims, self.key, algorithm="RS256")
                    response = client.get("/api/cases", headers={"Authorization": "Bearer " + token})
                    self.assertEqual(response.status_code, expected)


@unittest.skipUnless(os.environ.get("RUN_LOCAL_OIDC") == "1", "start the local identity fixture and opt in")
class BrowserFlow(unittest.TestCase):
    def sign_in(self, browser, username):
        start = browser.get("/login")
        self.assertEqual(start.status_code, 302)
        self.assertIn("HttpOnly", start.headers["set-cookie"])
        pre_auth = browser.cookies.get(COOKIE)
        auth_url = start.headers["location"]
        self.assertTrue(auth_url.startswith(ISSUER + "/protocol/openid-connect/auth?"))
        params = parse_qs(urlparse(auth_url).query)
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertTrue(params.get("nonce") and params.get("state"))
        # Emulate modern browsers' Secure-cookie exception for HTTP localhost.
        # This jar is private to the fixture; URLs are checked and redirects are never followed.
        jar = CookieJar(policy=DefaultCookiePolicy(secure_protocols=("https", "wss", "http")))
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False, cookies=jar) as idp:
            login_page = idp.get(auth_url)
            self.assertEqual(login_page.status_code, 200)
            action = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', login_page.text)
            self.assertIsNotNone(action, "fixture login form missing")
            login_url = unescape(action.group(1))
            self.assertTrue(login_url.startswith(ISSUER + "/login-actions/"))
            local_form = Request(login_url)
            jar.add_cookie_header(local_form)
            login = idp.post(
                login_url, data={"username": username, "password": f"local-{username}-only"},
                headers={"Cookie": local_form.get_header("Cookie", "")},
            )
            self.assertIn(login.status_code, (302, 303))
        redirect = login.headers["location"]
        self.assertTrue(redirect.startswith(ORIGIN + "/auth/callback?"))
        response = browser.get(redirect)
        self.assertEqual(response.status_code, 303, response.text)
        self.assertNotEqual(pre_auth, browser.cookies.get(COOKIE))
        self.assertNotIn(pre_auth, sessions)

    def test_users_have_distinct_roles_after_real_oidc(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(import_job, "STATE_ROOT", Path(directory) / "state"))
        for account, import_status in [("viewer", 403), ("importer", 303)]:
            with self.subTest(account=account):
                with TestClient(app, base_url=ORIGIN, follow_redirects=False) as browser:
                    self.sign_in(browser, account)
                    home = browser.get("/")
                    self.assertIn("List API: HTTP 200", home.text)
                    csrf = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)
                    key = re.search(r'name="key" value="([a-f0-9]{32})"', home.text).group(1)
                    self.assertEqual(browser.post("/import", data={"csrf": csrf, "key": key}).status_code, 403)
                    response = browser.post("/import", data={"csrf": csrf, "key": key}, headers={"Origin": ORIGIN})
                    self.assertEqual(response.status_code, import_status)
                    if import_status == 303:
                        self.assertIn("queued", browser.get(response.headers["location"]).text)
                    self.assertEqual(browser.post("/logout", data={"csrf": csrf}, headers={"Origin": ORIGIN}).status_code, 303)
                    self.assertIn("Sign in", browser.get("/").text)

    def test_callback_rejects_missing_transaction(self):
        with TestClient(app, base_url=ORIGIN, follow_redirects=False) as browser:
            self.assertEqual(browser.get("/auth/callback").status_code, 400)
