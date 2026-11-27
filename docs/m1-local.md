# M1: OIDC sign-in and API authorization

[English](../README.md) · [日本語](../README.ja.md)

## Run locally

Requires Docker Compose and Python (verified on 3.15.2). Use only the synthetic accounts already in the
realm fixture. Ports 8000 and 8180 are bound to loopback; do not expose this development
configuration over a network.

```sh
docker compose up -d
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`. Open
[http://localhost:8000](http://localhost:8000), not the numeric IP: the callback and
issuer are fixed to `localhost`. Give Keycloak a few seconds to import the realm.

| Synthetic account | Fixture password | List | Request import |
| --- | --- | --- | --- |
| `viewer` | `local-viewer-only` | 200 | 403 |
| `importer` | `local-importer-only` | 200 | 303 to the queued run's status page |

Use a new private browser window or browser profile to switch users. **End app session**
clears the app cookie, not the upstream Keycloak SSO session. M1 originally accepted a stub;
on the current branch [M3c](m3-staff-requests.md) saves a request but does not start it automatically.

## What to inspect

- In the Network panel, find the redirect to the identity service. Confirm the request uses
  `response_type=code` and `code_challenge_method=S256`. Do not copy codes, tokens or cookies
  into notes. Authlib handles state, PKCE and ID-token validation; the API validates the separate
  access token and checks roles.
- Both fixture users should be able to list cases. The fixed audience mapper is scoped to
  `lab-web`: it places `cases-api` in both access tokens, not in ID tokens. A viewer's import
  denial is therefore **403 after authentication**, not a 401 caused by the wrong audience.
- In the [role reduction exercise](m1-role-reduction.md), predict and verify the
  change from import allowed to 403 in a fresh sign-in while listing stays
  available. The disposable override leaves the original realm fixture unchanged.

## Automated checks

```sh
.venv/bin/python -m unittest discover -s tests -v
RUN_LOCAL_OIDC=1 .venv/bin/python -m unittest discover -s tests -v
```

The first command uses local signed fixtures and skips the real identity provider flows.
The second uses the running local Keycloak container and drives the login flow over HTTP.
The local reference run on macOS ARM64 passed four test methods with Keycloak 26.8.2,
including the signed token matrix and both users. Uvicorn’s loopback login redirect was
also checked. The login test follows the HTTP flow; a desktop browser and Windows setup
were not independently verified.

In PowerShell, set `$env:RUN_LOCAL_OIDC = "1"` before invoking Python. Finish with
`docker compose down` after stopping Uvicorn.

## Scope

M1 is a single-process learning lab: in-memory app sessions, Keycloak development mode,
plain loopback HTTP, fixed audience and test accounts. Do not reuse these credentials or
copy this setup into production. For SAML, use the separate [M2 fixture](m2-local.md).
See the [M3a fixture](m3-service-identity.md) for the standalone worker identity and
mock business API. Resumable import, federated logout and production operations are
not covered by M1.
