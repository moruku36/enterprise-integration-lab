"""Build disposable local Keycloak imports and a fresh, uncommitted worker secret."""
import json
from os import open as os_open
import secrets

from keycloak.prepare_m2 import build_realms
from service_profile import FIXTURE_DIR, READ_SCOPE, SECRET_FILE, SOURCE_AUDIENCE, WORKER_CLIENT


def build_m3_realms(secret: str) -> tuple[dict, dict, dict]:
    broker, customer = build_realms()
    # A separate workload realm leaves the original browser client scopes and SAML trust untouched.
    workload = {
        "realm": "workload-lab", "enabled": True, "sslRequired": "none",
        "registrationAllowed": False, "resetPasswordAllowed": False, "accessTokenLifespan": 90,
        "clientScopes": [{
            "name": READ_SCOPE,
            "description": "Read this lab's fictional source records",
            "protocol": "openid-connect",
            "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
        }],
        "clients": [{
            "clientId": WORKER_CLIENT,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "clientAuthenticatorType": "client-secret",
            "secret": secret,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "fullScopeAllowed": False,
            "redirectUris": [],
            "defaultClientScopes": ["basic"],
            "optionalClientScopes": [READ_SCOPE],
            "attributes": {
                "access.token.lifespan": "90",
                "client_credentials.use_refresh_token": "false",
                "standard.token.exchange.enabled": "false",
            },
            "protocolMappers": [{
                "name": "Mock source audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": SOURCE_AUDIENCE,
                    "id.token.claim": "false",
                    "access.token.claim": "true",
                },
            }],
        }],
    }
    return broker, customer, workload


def main() -> None:
    secret = secrets.token_urlsafe(32)
    realms = build_m3_realms(secret)
    try:
        FIXTURE_DIR.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit("M3 fixture directory exists. Reuse it or move it aside to regenerate.") from None
    with open(SECRET_FILE, "x", encoding="ascii", opener=_private_opener) as handle:
        handle.write(secret + "\n")
    imports = FIXTURE_DIR / "realms"
    imports.mkdir(mode=0o755)
    for realm in realms:
        # Compose mounts only the realm imports, read-only. Keep the parent private.
        destination = imports / f"{realm['realm']}-realm.json"
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(realm, handle, indent=2)
            handle.write("\n")
        destination.chmod(0o644)
    print("Prepared three disposable local realms and the private worker credential.")


def _private_opener(path, flags):
    return os_open(path, flags, 0o600)


if __name__ == "__main__":
    main()
