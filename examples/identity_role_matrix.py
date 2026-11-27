"""Show how the registered lab identities reach the app authorization boundary offline."""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import CLIENT, ORIGIN, app, jwks
import import_job
from keycloak.prepare_m2 import ALIAS, BROKER, PROFILE, build_realms

CASE_ROLES = {"cases_viewer", "cases_importer"}
DIRECT_USERS = ("viewer", "importer")
FEDERATED_USERS = ("customer-viewer", "customer-importer", "customer-unmapped")


class FixtureShape(ValueError):
    """The offline model does not cover this fixture declaration."""


@dataclass(frozen=True)
class Profile:
    label: str
    path: str
    groups: tuple[str, ...]
    roles: tuple[str, ...]


def one(options, label):
    found = list(options)
    if len(found) != 1:
        raise FixtureShape(f"expected one {label} in the synthetic fixture")
    return found[0]


def supported(condition, label):
    if not condition:
        raise FixtureShape(f"unsupported {label} in the synthetic fixture")


def fixture_contract(broker: dict, customer: dict) -> tuple[str, list[Profile]]:
    """Model only the checked-in fixture's direct assignment and exact group rules."""
    try:
        supported((broker["realm"], customer["realm"]) == ("integration-lab", "customer-lab"), "realms")
        supported(broker.get("enabled") is True and customer.get("enabled") is True, "disabled realms")
        declared_roles = broker["roles"]["realm"]
        registered = {role["name"] for role in declared_roles}
        supported(registered == CASE_ROLES and len(declared_roles) == len(registered)
                  and not any(role.get("composite") or role.get("composites") for role in declared_roles)
                  and not broker.get("defaultRoles") and not broker.get("defaultRole"), "registered case roles")

        client = one((entry for entry in broker["clients"] if entry.get("clientId") == CLIENT), "OIDC app client")
        supported(client.get("protocol") == "openid-connect" and "roles" in client["defaultClientScopes"],
                  "OIDC role scope")
        audience_config = one((mapper["config"] for mapper in client["protocolMappers"]
                               if mapper.get("protocolMapper") == "oidc-audience-mapper"), "OIDC audience mapper")
        audience = audience_config["included.custom.audience"]
        supported(isinstance(audience, str) and bool(audience)
                  and audience_config.get("access.token.claim") == "true"
                  and audience_config.get("id.token.claim") == "false", "OIDC audience mapper")

        provider = one((item for item in broker["identityProviders"] if item.get("alias") == ALIAS),
                       "customer provider")
        supported(provider.get("providerId") == "saml" and provider.get("enabled") is True, "customer provider")
        source_client = one((entry for entry in customer["clients"] if entry.get("clientId") == BROKER),
                            "customer broker registration")
        supported(source_client.get("protocol") == "saml", "customer protocol")
        group_config = one((item["config"] for item in source_client["protocolMappers"]
                            if item.get("protocolMapper") == "saml-group-membership-mapper"), "SAML group emitter")
        supported(group_config.get("attribute.name") == "groups" and group_config.get("full.path") == "true"
                  and group_config.get("single") == "true", "SAML full group path shape")

        group_paths = {"/" + item["name"] for item in customer["groups"]}
        group_roles: dict[str, str] = {}
        for mapper in broker["identityProviderMappers"]:
            config = mapper["config"]
            if mapper.get("identityProviderMapper") != "saml-role-idp-mapper":
                supported(mapper.get("identityProviderAlias") == ALIAS
                          and mapper.get("identityProviderMapper") == "saml-user-attribute-idp-mapper"
                          and config.get("attribute.name") in PROFILE
                          and config.get("user.attribute") == config.get("attribute.name")
                          and "role" not in config, "non-role SAML mapper")
                continue
            group, role = config["attribute.value"], config["role"]
            supported(mapper.get("identityProviderAlias") == ALIAS and config.get("attribute.name") == "groups"
                      and group in group_paths and group not in group_roles and role in registered, "exact SAML role map")
            group_roles[group] = role
        supported(set(group_roles.values()) == registered, "SAML role coverage")

        def enabled_user(realm, label):
            entry = one((item for item in realm["users"] if item.get("username") == label), "named test account")
            supported(entry.get("enabled") is True, "disabled test account")
            return entry

        profiles = []
        for label in DIRECT_USERS:
            assignments = enabled_user(broker, label)["realmRoles"]
            supported(isinstance(assignments, list) and set(assignments) <= registered, "direct role assignment")
            profiles.append(Profile(label, "direct_oidc", (), tuple(sorted(set(assignments)))))
        for label in FEDERATED_USERS:
            groups = enabled_user(customer, label)["groups"]
            supported(isinstance(groups, list) and set(groups) <= group_paths, "federated fixture group")
            selected = tuple(sorted(set(groups)))
            roles = tuple(sorted({group_roles[group] for group in selected if group in group_roles}))
            profiles.append(Profile(label, "saml_broker_to_oidc", selected, roles))
        return audience, profiles
    except (KeyError, TypeError, AttributeError):
        raise FixtureShape("incomplete offline identity fixture declarations") from None


def exercise() -> dict:
    # The builder's synthetic keys/passwords exist only in memory. Never serialize either realm object.
    audience, profiles = fixture_contract(*build_realms())
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    results = []
    with (TemporaryDirectory(prefix="eil-identity-matrix-") as work,
          patch.object(import_job, "STATE_ROOT", Path(work).resolve() / "unused-state"),
          patch.object(jwks, "get_signing_key_from_jwt", return_value=SimpleNamespace(key=signing_key.public_key())),
          TestClient(app, base_url=ORIGIN) as api):
        for profile in profiles:
            token = jwt.encode({
                "iss": BROKER, "aud": audience, "sub": "synthetic-" + profile.label, "typ": "Bearer",
                "azp": CLIENT, "exp": int(time.time()) + 120, "realm_access": {"roles": list(profile.roles)},
            }, signing_key, algorithm="RS256")
            headers = {"Authorization": "Bearer " + token}
            cases_status = api.get("/api/cases", headers=headers).status_code
            imports_status = api.get("/api/imports", headers=headers).status_code
            results.append({
                "fixture_profile": profile.label, "identity_path": profile.path, "fixture_groups": list(profile.groups),
                "mapped_roles": list(profile.roles), "list_cases_http": cases_status,
                "list_own_import_requests_http": imports_status,
            })
        if import_job.STATE_ROOT.exists():
            raise RuntimeError("read-only offline check unexpectedly created state")
    return {"mode": "offline_fixture_and_api_only", "profiles": results}


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        report = exercise()
    except FixtureShape as error:
        raise SystemExit(f"Offline matrix stopped: {error}.") from None
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
