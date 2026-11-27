# A guided run through the lab

**English** · [日本語](learning-path.ja.md) · [Repository README](../README.md)

Work through one question at a time. Use the fictional accounts and records in
a disposable local checkout. No cloud account, real identity provider, or
external source is required. The local realm profiles share port 8180: stop
one profile before starting another. Keep the apps bound to loopback.

## 1. Start without Docker: does "complete" mean correct?

Run the [offline reconciliation walkthrough](m3-recovery.md#try-reconciliation-without-running-services-m3g).
Predict the outcome before running it: two copies can show six stored rows
and a completed run, yet one fails because an ID is missing, an extra ID is
present and a title differs. The walkthrough uses its own temporary stores.

## 2. Follow the identity, then check what it can do

- [M1: OIDC and API authorization](m1-local.md). Sign in as the fictional
  viewer and importer. Both can read cases. Only the importer can request
  work. A valid token lacking a role gets 403; a missing or invalid token
  gets 401.
- [Remove only the import entitlement](m1-role-reduction.md). Compare fresh
  sign-ins into two separate disposable fixtures. The reduced account still
  reads cases and cannot queue a request. This does not test revocation of
  previously issued tokens, a live role edit, or an SSO sign-out.
- [M2: SAML federation](m2-local.md). Trace the simulated customer's SAML
  sign-in to the broker and the app's OIDC connection to the broker. Compare
  mapped and unmapped groups. The app does not add its own SAML parser.
  [Run the offline role matrix](m2-local.md#trace-the-fixture-roles-without-servers)
  to separate configuration-to-role reasoning from an actual federated sign-in.

## 3. Follow the request across people and services

Use the [M3 staff handoff](m3-staff-requests.md) to queue an import, close the
app session, then find the same ID in **Recent import requests** after another
sign-in. A different importer cannot see that entry. Only the local operator
starts the run. The separate [workload identity](m3-service-identity.md)
reads the source; its token is not a staff credential. [Compare direct and federated handoff offline](m3-staff-requests.md#compare-both-import-identities-without-servers)
for the same replay, recovery and record counts without a live sign-in.

Compare these three synthetic page-two conditions after page one is saved:

| Source reply | What to expect |
| --- | --- |
| [Temporary HTTP 503](m3-recovery.md#reproduce-a-page-two-outage) | After restoring the source, explicitly resume the same run from its checkpoint. |
| [HTTP 200, different snapshot marker](m3-recovery.md#contrast-a-snapshot-change-with-a-temporary-outage-m3h) | Checkpoint and two records stay put. The same run must not resume; do not combine pages of two versions. |
| [Source HTTP 429 with a supported delay](m3-rate-limit.md) | The local CLI saves the minimum retry time and refuses an early resume. After the time passes, the operator explicitly resumes the same run. No background retries or provider-wide quota enforcement. |

## 4. Verify and stage the staff read copy

Follow the [M4a local preview](m4-case-preview.md): after a successful import,
open the preview **before** staging and verify it is empty in a fresh lab.
Run the local staging command after reconciliation, then refresh it as a
viewer. The six matching records are now visible under the existing reader
permission. Work in progress and mismatches cannot replace a previously
verified copy. Staging changes only the local preview, not an external
deployment. It is not proof the external source is unchanged. To see why a
partial copy never appears, [try the controlled rollback](m4-case-preview.md#verify-rollback-without-servers)
without Docker or an active lab. The [two-reader update check](m4-case-preview.md#observe-two-readers-during-a-successful-update)
shows when a committed update becomes visible. Use [read-only verification](m4-case-preview.md#recheck-the-already-staged-copy),
not staging again, to confirm the exact copy from an earlier operator receipt.
[Check an overlapping replacement](m4-case-preview.md#verify-while-a-replacement-commits)
to see what a single point-in-time receipt can and cannot prove.

Use the documented checkers for repeatable evidence. They were verified with
temporary fixtures on macOS ARM64; native desktop browser, Windows,
production identity, live datasets and multi-tenant behavior were not
independently verified.
