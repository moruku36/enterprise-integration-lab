# Enterprise Integration Lab

**English** | [日本語](README.ja.md)

A learning lab for integrating an enterprise identity provider, a protected API, and a resumable data import. The scenario uses fictional users and case records. No cloud account or AI model is required for the planned local lab.

## The exercise

1. Sign in with OIDC. A viewer can list records; an importer can also start an import.
2. Connect a simulated customer SAML identity provider through Keycloak. Keep the app's OIDC integration unchanged.
3. Fetch paginated records from a mock business API under a separate service identity. Interrupt the import, resume it, and verify no missing or duplicate records.

[Architecture and sequence diagrams](docs/architecture.md) · [Milestones and acceptance checks](docs/acceptance.md)

**Status:** M1 is implemented for local use. [Run the OIDC and API authorization exercise](docs/m1-local.md).
SAML and data import are planned; the current import endpoint is a stub.
