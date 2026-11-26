# Enterprise Integration Lab

**English** | [日本語](README.ja.md)

A learning lab for integrating an enterprise identity provider, a protected API, and a resumable data import. The scenario uses fictional users and case records. No cloud account or AI model is required for the local lab.

## The exercise

1. Sign in with OIDC. A viewer can list records; an importer can also start an import.
2. Connect a simulated customer SAML identity provider through Keycloak. Keep the app's OIDC integration unchanged.
3. Fetch paginated records from a mock business API under a separate service identity. Interrupt the import, resume it, and verify no missing or duplicate records.

[Architecture and sequence diagrams](docs/architecture.md) · [Milestones and acceptance checks](docs/acceptance.md)

**Status:** M1 and M2 are implemented for local use. [Try OIDC and API authorization](docs/m1-local.md) or
[try customer SAML federation](docs/m2-local.md). [Read the paginated source as a service identity](docs/m3-service-identity.md) (M3a).
[Run and resume the SQLite import from the CLI](docs/m3-recovery.md) (M3b).
[Request an import and follow its durable status](docs/m3-staff-requests.md) (M3c); a local operator starts the queued run.
The local operator can [inspect the queue](docs/m3-staff-requests.md#inspect-the-queue-m3d) before starting or resuming a run (M3d).
They can also [cancel a request before it starts](docs/m3-staff-requests.md#cancel-a-queued-request-m3e)
without removing its history or changing imported records (M3e).
After a complete run, [reconcile the local destination against the fixed lab reference](docs/m3-recovery.md#reconcile-a-completed-import-m3f) (M3f).
Try the [no-server reconciliation walkthrough](docs/m3-recovery.md#try-reconciliation-without-running-services-m3g) (M3g).
