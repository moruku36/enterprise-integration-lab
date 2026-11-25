# Milestones and acceptance checks

[English README](../README.md) · [日本語 README](../README.ja.md)

Use local containers and synthetic data throughout. See the [M1 runbook](m1-local.md) for the runnable checks; M2 and M3 remain planned.

| Identity | List cases | Request import | Read mock API |
| --- | --- | --- | --- |
| Unauthenticated | No | No | No |
| Viewer | Yes | No | No |
| Importer | Yes | Yes | No |
| Import worker | No | No | Yes (read scope only) |

## M1: OIDC and API authorization

- [x] Start a local Keycloak realm and FastAPI with Docker Compose. Sign in with Authorization Code + PKCE (S256).
- [x] Unauthenticated API request returns 401; viewer lists the fixtures but receives 403 when requesting import; importer reaches a harmless stub.
- [x] The API rejects expired credentials, an ID token, and an access token meant for another audience.
- [ ] Learning exercise: predict what changes when a test account loses its importer role; obtain a new session/token and verify the result. Document the remaining lifetime of previously issued tokens separately.

## M2: Customer SAML federation

- [ ] Add a separate customer realm. Record the trust configuration, verify a successful sign-in through SAML, and confirm the app still uses OIDC.
- [ ] Compare allowed-group and unmapped-group test accounts. Confirm the latter can gain no case access.
- [ ] Document the first checks when onboarding fails: configured endpoints, issuer and signing certificate, then group attributes and role mapping.

## M3: API, data and recovery

- [ ] Use a separate service identity to read a three-page synthetic dataset. Record expected record IDs and counts.
- [ ] Simulate a temporary error on page two. Confirm the run reports failure with the last committed checkpoint and does not claim success.
- [ ] Resume and compare with a clean run: identical record IDs and values, no duplicates. Run again and confirm the stored records are unchanged.
- [ ] Write a short operator runbook: what to inspect, when to resume, and when not to retry. Record success and failure evidence without including credentials or raw tokens.
