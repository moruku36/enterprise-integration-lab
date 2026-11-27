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
- [x] [Learning exercise](m1-role-reduction.md): with a fresh OIDC login, removing the synthetic import role still allows listing (200), but writing a request returns 403 and creates no store.
- [ ] Separate follow-up: decide the revocation requirement and measure the behavior of previously issued tokens. The fixture is configured for a 300-second access-token lifetime; the fresh sign-in exercise does not measure revocation.

## M2: Customer SAML federation

- [x] Add a separate customer realm. Record the trust configuration, verify a successful sign-in through SAML, and confirm the app still uses OIDC.
- [x] Compare allowed-group and unmapped-group test accounts. Confirm the latter can gain no case access.
- [x] [Trace the fixture roles offline](m2-local.md#trace-the-fixture-roles-without-servers): derive direct and federated role assignments from the fixture configuration, then check the real read-only API gates with synthetic access claims.
- [x] Document the first checks when onboarding fails: configured endpoints, issuer and signing certificate, then group attributes and role mapping.

## M3: API, data and recovery

- [x] Use a separate service identity to read a three-page synthetic dataset. Confirm six IDs: `SOURCE-001` through `SOURCE-006`.
- [x] Confirm that a valid workload token without read scope gets 403, and that neither staff nor worker tokens cross into the other API.
- [x] Simulate a temporary error on page two. Confirm the run reports failure with the last committed checkpoint and does not claim success.
- [x] Resume and compare with a clean run: identical record IDs and values, no duplicates. Run again and confirm the stored records are unchanged.
- [x] Write a short operator runbook: what to inspect, when to resume, and when not to retry. Record success and failure evidence without including credentials or raw tokens.
- [x] Persist a staff request and return its run ID and status URL. Replay after an app restart yields the same run; another importer cannot read it.
- [x] Start that request explicitly as the local operator, recover from page-two failure on the same run, and verify the staff view and six stored records.
- [x] [Compare direct and federated handoff offline](m3-staff-requests.md#compare-both-import-identities-without-servers): same request/replay/recovery effects from equivalent starting stores, unchanged denied stores, and scoped replay by validated fixture subject rather than login-path label.
- [x] Inspect the pending IDs and any blocking run from the local operator CLI without a workload credential, acquiring the worker lease, or starting any work.
- [x] Cancel an unclaimed request from the local operator CLI with no source calls or record writes. Verify one claim-or-cancel result under contention, refusal after claim, and owner-only status plus idempotent replay of the cancelled ID.
- [x] Reconcile a completed run offline against the fixed six-record lab reference. With the same destination row count, detect a missing ID, an unexpected ID, and a wrong value separately. Detect checkpoint ID disagreement separately from destination disagreement.
- [x] Run the no-server walkthrough from a separate working directory. It demonstrates the incomplete-run refusal, the completed match and the same-count mismatch via the real CLI, without retaining any of its synthetic stores.
- [x] [Contrast snapshot drift with an outage](m3-recovery.md#contrast-a-snapshot-change-with-a-temporary-outage-m3h): HTTP 200 with a changed page-two marker stops after two rows and refuses resume. Preserve the failed store while reconciling six rows in a separate fresh store.
- [x] [Honor a bounded source 429 cooldown](m3-rate-limit.md): persist the minimum resume time, refuse an early manual resume, and later reconcile six rows under the same run ID.
- [x] [Find an existing request after a fresh sign-in](m3-staff-requests.md#find-your-recent-requests-m3j): recover the original ID in a bounded read-only owner list, hide it from a different importer and a viewer, and reflect its later completion.

## M4a: Serve a verified local case preview

- [x] [Stage an explicitly reconciled copy](m4-case-preview.md) only from the latest completed staff run. No rows are served before staging, and an unfinished or mismatching run cannot replace the prior copy. The existing viewer role can read the copy but does not get the run ID.
- [x] [Inject a controlled pre-commit rollback](m4-case-preview.md#verify-rollback-without-servers) during first stage and replacement: independent readers and a fresh process see only the prior state, and a clean CLI retry stages all six records.
- [x] [Observe a successful preview swap](m4-case-preview.md#observe-two-readers-during-a-successful-update) with two authorized offline HTTP clients: both see the whole previous copy until commit, then six genuinely revised synthetic records.
- [x] [Recheck the exact staged copy without restaging](m4-case-preview.md#recheck-the-already-staged-copy): repeat the local receipt, distinguish it from mismatching working data, and refuse absent, wrong, or corrupt copies without modifying the database.
- [x] [Verify during a replacement commit](m4-case-preview.md#verify-while-a-replacement-commits): a paused operator reads exactly once, reports the whole copy it read even after a new commit, and a later read refuses the old ID.
