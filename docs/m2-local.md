# M2: Customer SAML federation

[English](../README.md) · [日本語](../README.ja.md)

## What stays the same

The web app still sends an OIDC Authorization Code request with PKCE (S256) to the
`integration-lab` realm. FastAPI never parses SAML. Keycloak brokers the login to a
second realm, `customer-lab`, acting as a fictional customer's SAML identity provider.
The broker validates SAML before issuing its own OIDC tokens. The Cases API still checks
the app realm's access token, audience, and roles.

## Run locally

Use Docker Compose and the same Python environment as [M1](m1-local.md). Stop the
disposable M1 Compose stack first if it is using port 8180. M2 uses a separate Compose
project, but reuses the same loopback port to keep the OIDC issuer unchanged.

```sh
.venv/bin/python keycloak/prepare_m2.py
docker compose -f compose.m2.yaml up -d
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
```

On Windows, use `.venv\Scripts\python.exe`. The first command is needed only once per
fixture: it creates two separate synthetic key pairs and writes both realm imports to
the gitignored `.keycloak-m2/` directory. Do not share or commit that directory. The
certificates are valid for 30 days. To refresh them, stop M2, move the generated
directory aside, rerun the generator, and start M2 again. M2 has no persistent data volume.

Open [http://localhost:8000](http://localhost:8000), choose **Sign in**, then choose
**Customer SAML (lab only)** on Keycloak's login page. Sign in using one of these
fictional customer accounts. Use a new private browser session between accounts;
**End app session** does not log out either Keycloak realm.

| Account | Fixture password | SAML `groups` | List cases | Request import |
| --- | --- | --- | --- | --- |
| `customer-viewer` | `local-customer-viewer-only` | `/customer-case-viewers` | 200 | 403 |
| `customer-importer` | `local-customer-importer-only` | `/customer-case-importers` | 200 | 303 to the queued run's status page |
| `customer-unmapped` | `local-customer-unmapped-only` | `/customer-unmapped` | 403 | 403 |

The unmapped account **can sign in** but receives no case privileges. The importer
role permits listing and queuing a request. See [M3c](m3-staff-requests.md) for the separate
operator step; the form does not run the import itself. The original M1 viewer and
importer can still sign in directly instead of selecting the customer provider.

## Trust configuration

| Setting | Local value |
| --- | --- |
| IdP entity ID (assertion issuer) | `http://localhost:8180/realms/customer-lab` |
| IdP sign-in URL | `http://localhost:8180/realms/customer-lab/protocol/saml` |
| Broker SP entity ID | `http://localhost:8180/realms/integration-lab` |
| Broker assertion consumer service (ACS) | `http://localhost:8180/realms/integration-lab/broker/customer-saml/endpoint` |
| SAML binding | SP-initiated Redirect request; POST response |
| Signing certificates | Each realm owns a different generated key. The customer verifies the signed request with the broker certificate. The broker pins the customer certificate and requires a signed assertion. No automatic metadata trust. |
| Group mapping | Keycloak emits the full group path in the `groups` attribute. Only the two exact values in the table map to a case role; mappings synchronize at sign-in. |

If sign-in fails, first check the loopback hostname and the SAML endpoints above.
Then check the issuer and that both sides imported the matching certificate pair from the
same generation. Do not disable signature validation. If sign-in works but the API
returns 403, check the signed group attribute name and full path against the explicit
role mappings. Reauthenticate after changing group membership. Previously issued app
access tokens can retain their old roles until expiry.

## Trace the fixture roles without servers

Once the local Python dependencies are installed, run:

```sh
.venv/bin/python -B examples/identity_role_matrix.py
```

The script reads the direct realm assignments and the generated customer group
and broker mappings from the checked-in fixture setup. It models only the
registered fixture's exact full group paths and case-role assignments. It
stops rather than predicting a result for an unsupported mapper or role shape.
Ephemeral, locally signed test access claims then exercise two read-only
endpoints of the real app in-process: `GET /api/cases` and
`GET /api/imports`. No realm files, store, assertion, token, cookie, key,
or password is written or emitted. Only fictional profile/group/role labels
and HTTP status codes appear.

For direct and federated viewers, case listing returns 200 and the
importer-only list returns 403. Both importers get 200 on both endpoints.
The unmapped customer fixture has no case roles and gets 403 on both.
The importer-only check reads a list, so its 200 does not create an import
request (the separate live browser submission returns 303). This is a
fixture model and an API-boundary check, not an actual SAML assertion,
OIDC sign-in/issuance, role sync at login, token revocation, or Keycloak
interop result. Use the live local exercise below for protocol behavior
when appropriate.

**日本語メモ：** 設定にあるグループ名とロールの対応を読み取り、アプリの読み取りAPI
を試します。閲覧者は案件だけ、取り込み担当者は依頼一覧も読めます。割り当てなしの
架空利用者は両方とも403です。認証サーバーとは通信せず、トークン・鍵・パスワード
も表示しません。SAMLやOIDCで実際にログインした結果とは別です。

## Verify

```sh
.venv/bin/python -m unittest discover -s tests -v
RUN_LOCAL_OIDC=1 RUN_LOCAL_SAML=1 .venv/bin/python -m unittest discover -s tests -v
```

The second command follows the local HTTP flow through the real Keycloak container
and checks OIDC PKCE, the SAML redirect and POST, the app's OIDC callback, and the
expected API status codes. The reference run on macOS ARM64 / Python 3.15.2 /
Keycloak 26.8.2 passed all six test methods, including direct M1 OIDC. The three
federated outcomes were also checked against running loopback Uvicorn. These
are HTTP-flow checks, not desktop-browser or Windows verification.

Set the two variables separately in PowerShell
(`$env:RUN_LOCAL_OIDC = "1"`, `$env:RUN_LOCAL_SAML = "1"`). Do not record raw
assertions, tokens, cookies, or login request URLs in notes or bug reports. Stop Uvicorn
and run `docker compose -f compose.m2.yaml down` when finished.

This is plain loopback HTTP, in-memory app sessions, synthetic data, and one simulated
customer. Production TLS, safe rotation procedures, tenant isolation, account lifecycle,
and federated logout are out of scope. Keep both services off the network.

Reference: [Keycloak identity brokering](https://www.keycloak.org/docs/latest/server_admin/#_identity_broker).
