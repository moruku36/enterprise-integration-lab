# M3a: Read a source API under a service identity

[English](../README.md) · [日本語](../README.ja.md)

The staff app and the worker use different trust profiles, even though the same
local Keycloak process hosts both. The existing `integration-lab` and
`customer-lab` realms stay unchanged. A third realm, `workload-lab`, issues client
credentials tokens for `import-worker`. The source app validates the workload
issuer, its signing keys, RS256, expiry, the sole audience `mock-source-api`, and
the authorized client. It then requires the exact scope `source.read`. This is
a Keycloak-specific token profile, not a claim of RFC 9068 compatibility.

| Credential | Staff Cases API | Read synthetic source |
| --- | --- | --- |
| None | 401 | 401 |
| Signed-in staff importer | 200; staff API queues with 202 | 401 |
| Worker, valid token without `source.read` | 401 | 403 |
| Worker, valid token with `source.read` | 401; import request 401 | 200 |

The source API exposes no mutating methods. The worker's audience is set
independently of its optional scope so a valid unscoped token reaches authorization
and gets **403**, rather than failing authentication with **401**. Staff users
cannot obtain or read the worker's credential through the app.

## Run locally

Use the Python environment from [M1](m1-local.md). Stop any other disposable lab
stack using port 8180 first. Keycloak stays on 8180, the staff app on 8000, and the
separate source app on 8001. Keep all three bound to loopback.

```sh
.venv/bin/python -m keycloak.prepare_m3
docker compose -f compose.m3.yaml up -d
```

The first command generates three realm imports and a new random fixture-only
secret under gitignored `.keycloak-m3-fixture/`. Run it only once per fixture.
The read-only Docker mount contains the realm imports, not the worker's separate
credential file. The workload realm import also contains that fixture secret so
Keycloak can authenticate the worker. Do not share or commit either file.
To reset the fixture, stop this Compose project, remove the generated directory,
then rerun the generator. There is no persistent Keycloak data volume.

Start the source server in a separate terminal, and then run the worker:

```sh
.venv/bin/python -m uvicorn mock_source:source --host 127.0.0.1 --port 8001 --no-proxy-headers --no-access-log
```

```sh
.venv/bin/python -m worker
```

Expect `read_complete`, three pages, and six unique IDs from `SOURCE-001` through
`SOURCE-006`. The worker requests an access token using the OAuth 2.0 Client
Credentials grant and calls `GET /v1/cases`. It follows the returned cursors,
validates each page, and prints only the count and synthetic IDs. The source
records are **not written anywhere**. Duplicate records, repeated cursors, or an
incomplete read fail rather than producing a success message.

## Verify and troubleshoot

For the full HTTP flow tests, also start the staff app in a separate terminal:

```sh
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
RUN_LOCAL_OIDC=1 RUN_LOCAL_SAML=1 RUN_LOCAL_M3=1 .venv/bin/python -m unittest discover -s tests -v
```

On Windows use `.venv\Scripts\python.exe` and set each of the three
`RUN_LOCAL_*` environment variables to `1` in PowerShell before the test.
Omit the variables to run offline, with no local servers. The M3a reference run passed 12/12 test methods on macOS ARM64 / Python 3.15.2 /
Keycloak 26.8.2. These are HTTP and CLI checks, not verification in a desktop browser or a live tenant.

If the token endpoint returns 401, check that the generated workload realm and
the worker file were created together and that no M1/M2 container is still
listening on 8180. If the source returns 401, check the token's intended issuer,
audience and client configuration. A 403 points to scope. Avoid logging the
token, secret or SAML assertion while troubleshooting. The worker does not retry
automatically. Stop the Uvicorn processes and run
`docker compose -f compose.m3.yaml down` after trying the lab.

The standalone [M3b worker](m3-recovery.md) persists and resumes an import.
[M3c](m3-staff-requests.md) adds durable staff requests and an explicit operator handoff.
This fixture is HTTP on loopback only; TLS,
workload credential rotation and production operations are not covered.

- [OAuth 2.0 Client Credentials grant](https://www.rfc-editor.org/rfc/rfc6749#section-4.4)
- [OAuth bearer resource errors](https://www.rfc-editor.org/rfc/rfc6750#section-3.1)
- [JWT profiles and audience validation](https://www.rfc-editor.org/rfc/rfc8725#section-3.12)
- [Keycloak service accounts](https://www.keycloak.org/docs/latest/server_admin/#_service_accounts)
