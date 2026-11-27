"""Verify the no-network mapping exercise and fail-closed fixture interpretation."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from examples.identity_role_matrix import FixtureShape, fixture_contract
from keycloak.prepare_m2 import build_realms

EXERCISE = Path(__file__).resolve().parents[1] / "examples" / "identity_role_matrix.py"
NO_NETWORK = r"""
import runpy
import sys

def guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        raise RuntimeError("no network in the offline identity exercise")
sys.addaudithook(guard)
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


class IdentityRoleMatrix(unittest.TestCase):
    def test_user_entrypoint_emits_only_deterministic_fixture_labels_and_statuses(self):
        with TemporaryDirectory() as directory:
            run = subprocess.run([sys.executable, "-B", "-c", NO_NETWORK, str(EXERCISE)], cwd=directory,
                                 capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])
        found = json.loads(run.stdout)
        expected = []
        for label, path, groups, roles, read, requests in (
            ("viewer", "direct_oidc", [], ["cases_viewer"], 200, 403),
            ("importer", "direct_oidc", [], ["cases_importer", "cases_viewer"], 200, 200),
            ("customer-viewer", "saml_broker_to_oidc", ["/customer-case-viewers"], ["cases_viewer"], 200, 403),
            ("customer-importer", "saml_broker_to_oidc", ["/customer-case-importers"], ["cases_importer"], 200, 200),
            ("customer-unmapped", "saml_broker_to_oidc", ["/customer-unmapped"], [], 403, 403),
        ):
            expected.append({"fixture_profile": label, "identity_path": path, "fixture_groups": groups,
                             "mapped_roles": roles, "list_cases_http": read,
                             "list_own_import_requests_http": requests})
        self.assertEqual(found, {"mode": "offline_fixture_and_api_only", "profiles": expected})

    def test_map_uses_fixture_assignments_and_stops_if_group_shape_is_not_modelled(self):
        broker, customer = build_realms()
        alternative = deepcopy(broker)
        viewer = next(user for user in alternative["users"] if user["username"] == "viewer")
        viewer["realmRoles"] = ["cases_viewer", "cases_importer"]
        audience, profiles = fixture_contract(alternative, customer)
        self.assertEqual(audience, "cases-api")
        self.assertEqual(profiles[0].roles, ("cases_importer", "cases_viewer"))

        different_emitter = deepcopy(customer)
        mapper = next(item for item in different_emitter["clients"][0]["protocolMappers"]
                      if item["protocolMapper"] == "saml-group-membership-mapper")
        mapper["config"]["full.path"] = "false"
        with self.assertRaisesRegex(FixtureShape, "SAML full group path shape"):
            fixture_contract(broker, different_emitter)

        unregistered_role = deepcopy(broker)
        rule = next(item for item in unregistered_role["identityProviderMappers"]
                    if item["identityProviderMapper"] == "saml-role-idp-mapper")
        rule["config"]["role"] = "fixture-role-not-registered"
        with self.assertRaisesRegex(FixtureShape, "exact SAML role map"):
            fixture_contract(unregistered_role, customer)

        unsupported_mapper = deepcopy(broker)
        unsupported_mapper["identityProviderMappers"][0]["identityProviderMapper"] = "another-role-mapper"
        with self.assertRaisesRegex(FixtureShape, "non-role SAML mapper"):
            fixture_contract(unsupported_mapper, customer)
