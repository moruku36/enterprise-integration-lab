# M1 exercise: Remove an import grant

[English README](../README.md) · [日本語 README](../README.ja.md) · [M1 setup](m1-local.md)

**Question:** A staff member no longer needs to request imports. Can they still
sign in and read cases after you remove only their import role?

Use a **disposable local copy** of this lab and the existing fictional OIDC test
accounts; never use this override with an existing Keycloak service, customer
realm, or real account. Stop any other lab container using port 8180 first. The
exercise does not use a real tenant, an OAuth admin credential, the M3 worker, or
an existing SQLite store. It compares *fresh sign-ins* from two fixture
deployments, not role propagation or session revocation in a live realm.

## 1. Confirm the baseline

From the repository root, with the Python dependencies from
[M1 setup](m1-local.md) installed:

```sh
docker compose -p enterprise-lab-m1-role-baseline up -d
# Wait for the disposable identity fixture to finish importing.
RUN_LOCAL_OIDC=1 .venv/bin/python -m unittest discover -s tests -p test_oidc.py -v
docker compose -p enterprise-lab-m1-role-baseline down
```

The test signs in as the fictional importer through OIDC Code + PKCE. Case
listing is **200**. Requesting an import actually saves a queued request and
returns **303** to its status page (the app API returns **202** on initial
acceptance). Logout ends that test's app session. The tests use disposable
SQLite state rather than any repository-local store.

## 2. Remove just the import entitlement

```sh
.venv/bin/python keycloak/prepare_m1_role_reduction.py
docker compose -p enterprise-lab-m1-role-reduced -f compose.yaml -f compose.m1-role-reduction.yaml up -d
# Wait for the viewer-only fixture to finish importing.
RUN_LOCAL_OIDC=1 RUN_LOCAL_OIDC_ROLE_REDUCTION=1 .venv/bin/python -m unittest discover -s tests -p test_oidc.py -v
docker compose -p enterprise-lab-m1-role-reduced -f compose.yaml -f compose.m1-role-reduction.yaml down
```

On PowerShell, set `$env:RUN_LOCAL_OIDC = "1"` and, for the second stage only,
`$env:RUN_LOCAL_OIDC_ROLE_REDUCTION = "1"`. Use `.venv\Scripts\python.exe`;
remove the two environment variables when finished.

The generator leaves the original JSON unchanged and refuses to overwrite a
previous generated directory. The separate Compose override mounts only the
generated JSON in place of the baseline import. The fictional `importer`
account now has `cases_viewer` only. Check the public result, not raw tokens:

| Fresh sign-in | List cases | Request an import | Durable effect |
| --- | --- | --- | --- |
| Baseline importer | 200 | 303 to status | One queued synthetic request |
| Importer account reduced to viewer | 200 | 403, `Importer role required` | No SQLite store created |

The denied operation is **403 after authentication**, not 401 (no acceptable
credential) and not the browser form's CSRF rejection. A role change need not
remove the ability to sign in. For a real deployment, define a separate
revocation requirement: this lab checks signed access tokens locally, and the
fixture's access-token lifespan is configured to 300 seconds. This exercise
does **not** measure remaining time on an already-issued token, test whether
one is invalidated immediately, or guarantee that ending an app session
ends IdP SSO. Treat those as distinct verification tasks before production use.

**日本語メモ：** 架空アカウントから「取り込みを依頼する」権限だけを外した
別の使い捨て環境で、新しくログインします。閲覧は200のまま、依頼は403となり、
取り込み依頼も保存されないことを確認します。元の設定や既存データには触れません。
この試験で分かるのは新規ログイン時の権限です。発行済みトークンの即時失効や
SSOセッションの終了は別の検証です。
