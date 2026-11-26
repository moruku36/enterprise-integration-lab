"""Build offline, disposable two-realm fixtures. Never use these keys in production."""
from base64 import b64encode
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parent
BROKER = "http://localhost:8180/realms/integration-lab"
CUSTOMER = "http://localhost:8180/realms/customer-lab"
ALIAS = "customer-saml"
ACS = f"{BROKER}/broker/{ALIAS}/endpoint"
NAME_FORMAT = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"
PROFILE = ("firstName", "lastName", "email")
MAPPINGS = (
    ("/customer-case-viewers", "cases_viewer"),
    ("/customer-case-importers", "cases_importer"),
)
UNMAPPED = "/customer-unmapped"


def signing_key(name: str) -> tuple[dict, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    public = b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")
    private = b64encode(key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )).decode("ascii")
    return {
        "name": "fixture-signing",
        "providerId": "rsa",
        "config": {
            "priority": ["200"], "enabled": ["true"], "active": ["true"],
            "algorithm": ["RS256"], "privateKey": [private], "certificate": [public],
        },
    }, public


def incoming_mapper(name: str, kind: str, config: dict) -> dict:
    return {
        "name": name,
        "identityProviderAlias": ALIAS,
        "identityProviderMapper": kind,
        "config": {"syncMode": "INHERIT", **config},
    }


def outgoing_mapper(name: str, kind: str, config: dict) -> dict:
    return {
        "name": name,
        "protocol": "saml",
        "protocolMapper": kind,
        "config": {"attribute.name": name, "attribute.nameformat": "Basic", **config},
    }


def account(label: str, groups: list[str]) -> dict:
    user = f"customer-{label}"
    return {
        "username": user,
        "enabled": True,
        "firstName": "Fixture",
        "lastName": label.capitalize(),
        "email": f"{user}@example.invalid",
        "groups": groups,
        "credentials": [{"type": "password", "value": f"local-{user}-only", "temporary": False}],
    }


def build_realms() -> tuple[dict, dict]:
    broker = json.loads((ROOT / "integration-lab-realm.json").read_text(encoding="utf-8"))
    broker_key, broker_cert = signing_key("enterprise-m2-fixture-broker")
    customer_key, customer_cert = signing_key("enterprise-m2-fixture-customer")
    broker["components"] = {"org.keycloak.keys.KeyProvider": [broker_key]}
    broker["identityProviders"] = [{
        "alias": ALIAS, "displayName": "Customer SAML (lab only)", "providerId": "saml",
        "enabled": True, "trustEmail": False, "storeToken": False, "linkOnly": False,
        "firstBrokerLoginFlowAlias": "first broker login",
        "config": {
            "syncMode": "FORCE", "entityId": BROKER, "idpEntityId": CUSTOMER,
            "singleSignOnServiceUrl": f"{CUSTOMER}/protocol/saml",
            "nameIDPolicyFormat": NAME_FORMAT, "allowCreate": "true",
            "principalType": "SUBJECT", "validateSignature": "true",
            "wantAssertionsSigned": "true", "wantAuthnRequestsSigned": "true",
            "signatureAlgorithm": "RSA_SHA256", "postBindingResponse": "true",
            "signingCertificate": customer_cert, "useMetadataDescriptorUrl": "false",
        },
    }]
    broker["identityProviderMappers"] = [
        incoming_mapper(role, "saml-role-idp-mapper",
                        {"attribute.name": "groups", "attribute.value": group, "role": role})
        for group, role in MAPPINGS
    ] + [
        incoming_mapper(attr, "saml-user-attribute-idp-mapper",
                        {"attribute.name": attr, "user.attribute": attr})
        for attr in PROFILE
    ]
    customer = {
        "realm": "customer-lab", "enabled": True, "sslRequired": "none",
        "registrationAllowed": False, "resetPasswordAllowed": False,
        "components": {"org.keycloak.keys.KeyProvider": [customer_key]},
        "groups": [{"name": group[1:]} for group, _ in MAPPINGS] + [{"name": UNMAPPED[1:]}],
        "users": [account("viewer", [MAPPINGS[0][0]]),
                  account("importer", [MAPPINGS[1][0]]),
                  account("unmapped", [UNMAPPED])],
        "clients": [{
            "clientId": BROKER, "name": "Integration Lab Broker", "protocol": "saml", "enabled": True,
            "redirectUris": [ACS],
            "attributes": {
                "saml_assertion_consumer_url_post": ACS,
                "saml_name_id_format": "persistent", "saml_force_name_id_format": "true",
                "saml.client.signature": "true", "saml.signing.certificate": broker_cert,
                "saml.server.signature": "true", "saml.assertion.signature": "true",
                "saml.signature.algorithm": "RSA_SHA256", "saml.force.post.binding": "true",
                "saml.authnstatement": "true", "saml.assertion.lifespan": "120",
            },
            "protocolMappers": [
                outgoing_mapper("groups", "saml-group-membership-mapper",
                                {"single": "true", "full.path": "true"}),
                *(outgoing_mapper(attr, "saml-user-property-mapper", {"user.attribute": attr})
                  for attr in PROFILE),
            ],
        }],
    }
    return broker, customer


def main() -> None:
    target = ROOT.parent / ".keycloak-m2"
    realms = build_realms()
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit("Fixture directory already exists. Reuse it or move it aside to regenerate.") from None
    for realm in realms:
        # Individual 0644 files in a private directory can be mounted read-only by a
        # Keycloak container with a different UID. Keep the directory private.
        destination = target / f"{realm['realm']}-realm.json"
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(realm, handle, indent=2)
            handle.write("\n")
        destination.chmod(0o644)
    print("Prepared two disposable Keycloak realm imports in .keycloak-m2/.")


if __name__ == "__main__":
    main()
