"""Local fixture checks; SAML and OIDC processing is left to the real servers."""
from base64 import b64decode
from html.parser import HTMLParser
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
import httpx

import import_job
from app import COOKIE, ORIGIN, app
from keycloak.prepare_m2 import ACS, ALIAS, BROKER, CUSTOMER, MAPPINGS, build_realms


class FixtureTrust(unittest.TestCase):
    def test_realms_use_mutual_fixture_pins_and_allowlisted_group_roles(self):
        broker, customer = build_realms()
        keys = [r["components"]["org.keycloak.keys.KeyProvider"][0]["config"]
                for r in (broker, customer)]
        public_keys = []
        for key in keys:
            cert = x509.load_der_x509_certificate(b64decode(key["certificate"][0]))
            private = serialization.load_der_private_key(b64decode(key["privateKey"][0]), None)
            self.assertEqual(cert.public_key().public_numbers(), private.public_key().public_numbers())
            public_keys.append(cert.public_key().public_numbers())
        self.assertNotEqual(*public_keys)
        idp = broker["identityProviders"][0]["config"]
        client = customer["clients"][0]
        self.assertEqual(client["clientId"], idp["entityId"])
        self.assertEqual(idp["idpEntityId"], CUSTOMER)
        self.assertEqual(idp["signingCertificate"], keys[1]["certificate"][0])
        self.assertEqual(client["attributes"]["saml.signing.certificate"], keys[0]["certificate"][0])
        self.assertEqual(client["redirectUris"], [ACS])
        for name in ("validateSignature", "wantAssertionsSigned", "wantAuthnRequestsSigned"):
            self.assertEqual(idp[name], "true")
        for name in ("saml.client.signature", "saml.server.signature", "saml.assertion.signature"):
            self.assertEqual(client["attributes"][name], "true")
        self.assertEqual(
            [(m["config"]["attribute.value"], m["config"]["role"])
             for m in broker["identityProviderMappers"] if m["config"].get("role")], list(MAPPINGS),
        )
        self.assertEqual([c["protocol"] for c in broker["clients"]], ["openid-connect"])


class Page(HTMLParser):
    """Recognize the forms and links of the local fixture without interpreting SAML."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.links = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = (attrs, {})
            self.forms.append(self.current)
        elif tag == "input" and self.current and attrs.get("type") == "hidden" and attrs.get("name"):
            self.current[1][attrs["name"]] = attrs.get("value", "")
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


@unittest.skipUnless(os.environ.get("RUN_LOCAL_SAML") == "1", "start the M2 fixture and opt in")
class FederatedFlow(unittest.TestCase):
    def request(self, idp, method, url, **kwargs):
        destination = urlsplit(url)
        self.assertEqual((destination.scheme, destination.netloc), ("http", "localhost:8180"))
        # Mirror localhost's browser Secure-cookie exception only for this explicit loopback origin.
        cookie_request = Request(url)
        idp.cookies.jar.add_cookie_header(cookie_request)
        return idp.request(method, url, headers={"Cookie": cookie_request.get_header("Cookie", "")}, **kwargs)

    def sign_in(self, browser, username):
        login = browser.get("/login")
        self.assertEqual(login.status_code, 302)
        start_cookie = browser.cookies.get(COOKIE)
        auth_url = login.headers["location"]
        self.assertTrue(auth_url.startswith(BROKER + "/protocol/openid-connect/auth?"))
        params = parse_qs(urlsplit(auth_url).query)
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["code_challenge_method"], ["S256"])

        jar = CookieJar(policy=DefaultCookiePolicy(secure_protocols=("https", "wss", "http")))
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False, cookies=jar) as idp:
            local = self.request(idp, "GET", auth_url)
            self.assertEqual(local.status_code, 200)
            provider_links = [urljoin(str(local.url), href) for href in Page(local.text).links]
            selected = [url for url in provider_links if url.startswith(f"{BROKER}/broker/{ALIAS}/login?")]
            self.assertEqual(len(selected), 1, "Customer SAML option missing")
            broker = self.request(idp, "GET", selected[0])
            self.assertEqual(broker.status_code, 302)
            upstream = broker.headers["location"]
            self.assertTrue(upstream.startswith(f"{CUSTOMER}/protocol/saml?"))
            self.assertIn("SAMLRequest", parse_qs(urlsplit(upstream).query))
            customer = self.request(idp, "GET", upstream)
            self.assertEqual(customer.status_code, 200)
            forms = [form for form in Page(customer.text).forms if form[0].get("id") == "kc-form-login"]
            self.assertEqual(len(forms), 1, "Customer login form missing")
            attrs, fields = forms[0]
            self.assertTrue(attrs["action"].startswith(f"{CUSTOMER}/login-actions/"))
            customer = self.request(idp, "POST", attrs["action"], data={
                **fields, "username": username, "password": f"local-{username}-only",
            })
            self.assertEqual(customer.status_code, 200, "Customer did not return a SAML POST form")
            posts = [(attrs, fields) for attrs, fields in Page(customer.text).forms if "SAMLResponse" in fields]
            self.assertEqual(len(posts), 1, "Keycloak SAML POST form missing")
            attrs, fields = posts[0]
            self.assertEqual(urlsplit(attrs.get("action", ""))._replace(query="", fragment="").geturl(), ACS)
            out = self.request(idp, "POST", attrs["action"], data=fields)
            for _ in range(5):
                self.assertIn(out.status_code, (302, 303), f"Broker returned HTTP {out.status_code}")
                next_url = out.headers["location"]
                if next_url.startswith(ORIGIN + "/auth/callback?"):
                    break
                next_url = urljoin(str(out.url), next_url)
                self.assertTrue(next_url.startswith(BROKER + "/"))
                out = self.request(idp, "GET", next_url)
            else:
                self.fail("No OIDC callback after federation")
        completed = browser.get(next_url)
        self.assertEqual(completed.status_code, 303, "App did not accept OIDC callback")
        self.assertNotEqual(start_cookie, browser.cookies.get(COOKIE))

    def test_federated_group_mapping_and_denial(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(import_job, "STATE_ROOT", Path(directory) / "state"))
        for username, list_status, import_status in [
            ("customer-viewer", 200, 403),
            ("customer-importer", 200, 303),
            ("customer-unmapped", 403, 403),
        ]:
            with self.subTest(username=username), TestClient(app, base_url=ORIGIN, follow_redirects=False) as browser:
                self.sign_in(browser, username)
                home = browser.get("/")
                self.assertIn(f"List API: HTTP {list_status}", home.text)
                self.assertEqual("CASE-001" in home.text, list_status == 200)
                csrf = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)
                key = re.search(r'name="key" value="([a-f0-9]{32})"', home.text).group(1)
                response = browser.post("/import", data={"csrf": csrf, "key": key}, headers={"Origin": ORIGIN})
                self.assertEqual(response.status_code, import_status)
                if import_status == 303:
                    self.assertIn("queued", browser.get(response.headers["location"]).text)
