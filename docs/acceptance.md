# Milestones and acceptance checks

[English README](../README.md) · [日本語 README](../README.ja.md)

Use local containers and synthetic data throughout. See the [M1 runbook](m1-local.md) and
[M2 runbook](m2-local.md) for staff flows, and the [M3a runbook](m3-service-identity.md) for the service identity. The [M3b runbook](m3-recovery.md) covers the standalone worker and recovery. The [M3c runbook](m3-staff-requests.md) covers staff requests and status.

| Identity | List cases | Request import | Read mock API |
| --- | --- | --- | --- |
| Unauthenticated | No | No | No |
| Viewer | Yes | No | No |
| Importer | Yes | Yes | No |
| Import worker | No | No | Yes (read scope only) |

## M1: OIDC and API authorization

- [x] Start a local Keycloak realm and FastAPI with Docker Compose. Sign in with Authorization Code + PKCE (S256).
- [x] Unauthenticated API request returns 401; viewer lists the fixtures but receives 403 when requesting import; importer reaches a harmless stub in M1 (replaced by the durable request in M3c).
- [x] The API rejects expired credentials, an ID token, and an access token meant for another audience.
- [ ] Learning exercise: predict what changes when a test account loses its importer role; obtain a new session/token and verify the result. Document the remaining lifetime of previously issued tokens separately.

## M2: Customer SAML federation

- [x] Add a separate customer realm. Record the trust configuration, verify a successful sign-in through SAML, and confirm the app still uses OIDC.
- [x] Compare allowed-group and unmapped-group test accounts. Confirm the latter can gain no case access.
- [x] Document the first checks when onboarding fails: configured endpoints, issuer and signing certificate, then group attributes and role mapping.

## M3: API, data and recovery

- [x] Use a separate service identity to read a three-page synthetic dataset. Confirm six IDs: `SOURCE-001` through `SOURCE-006`.
- [x] Confirm that a valid workload token without read scope gets 403, and that neither staff nor worker tokens cross into the other API.
- [x] Simulate a temporary error on page two. Confirm the run reports failure with the last committed checkpoint and does not claim success.
- [x] Resume and compare with a clean run: identical record IDs and values, no duplicates. Run again and confirm the stored records are unchanged.
- [x] Write a short operator runbook: what to inspect, when to resume, and when not to retry. Record success and failure evidence without including credentials or raw tokens.
- [x] Persist a staff request and return its run ID and status URL. Replay after an app restart yields the same run; another importer cannot read it.
- [x] Start that request explicitly as the local operator, recover from page-two failure on the same run, and verify the staff view and six stored records.
- [x] Inspect the pending IDs and any blocking run from the local operator CLI without a workload credential, acquiring the worker lease, or starting any work.
- [x] Cancel an unclaimed request from the local operator CLI with no source calls or record writes. Verify one claim-or-cancel result under contention, refusal after claim, and owner-only status plus idempotent replay of the cancelled ID.
- [x] Reconcile a completed run offline against the fixed six-record lab reference. With the same destination row count, detect a missing ID, an unexpected ID, and a wrong value separately. Detect checkpoint ID disagreement separately from destination disagreement.
- [x] Run the no-server walkthrough from a separate working directory. It demonstrates the incomplete-run refusal, the completed match and the same-count mismatch via the real CLI, without retaining any of its synthetic stores.
