# M3c: Request an import and follow one durable run

[English](../README.md) · [日本語](../README.ja.md)

An importer can now queue the fixed synthetic snapshot and open its status page.
The staff app records the request, but does not sign in to the source or launch a
background worker. A local operator starts that exact run with the separate
workload identity.

## Try the staff-to-operator handoff

1. Prepare the disposable Keycloak realms from the [M3a runbook](m3-service-identity.md)
   and start the staff app and mock source on loopback.
2. Sign in as the synthetic importer at [http://localhost:8000](http://localhost:8000).
   Click **Request import**. The app redirects to a status page that shows a run
   ID and `queued`. Copy the run ID. Refresh the status page, not the form, to
   see what has changed.
3. In a local operator terminal, start that exact queued request. Replace
   `<run-id>` with the one from the status page:

   ```sh
   .venv/bin/python -m import_job run-request <run-id>
   .venv/bin/python -m import_job --store staff status <run-id>
   ```

4. Refresh the staff status page. If all three pages succeeded, it shows
   `complete` and six processed records. The viewer cannot request a run or
   read status; a different importer cannot read this run by knowing its URL.

## Find your recent requests (M3j)

If the browser closes or the app restarts and you did not save the run ID,
sign in again as the same importer and open **Recent import requests** on the
home page. Select the original ID to continue tracking it. Use this view
instead of submitting a new form just to recover the old ID.

The API equivalent is read-only `GET /api/imports` with the existing staff
bearer authentication. It returns `limit: 10` and an array called `requests`.
Each entry has the same safe fields and status URL as the individual status
response, including queued, cancelled and completed requests. These are the
last ten accepted **for the authenticated subject**, newest acceptance first,
not by last status change. The exercise has no pagination or timestamp
contract. A missing store yields an empty list without creating a store; an
unreadable store fails instead of pretending there are no requests.

A viewer is denied. Another signed-in importer sees only their own requests,
not yours; a browser cookie by itself does not authorize a direct API call.
The list does not include owners, idempotency keys, source data, or the local
operator's cooldown details. Reading it does not launch a worker, change any
request, or examine another tenant. This lab still simulates one customer.

**日本語メモ：** 画面を閉じて依頼IDを忘れても、同じ担当者でログインして
「Recent import requests」から自分の最新10件を開けます。再依頼は不要です。
一覧で他の担当者の依頼は見えず、閲覧権限だけの利用者は一覧を開けません。

## Inspect the queue (M3d)

An operator can discover the pending IDs without copying them from a staff
session. The command needs only local filesystem read access and works with the
app, source and Keycloak stopped:

```sh
.venv/bin/python -m import_job queue
```

The result lists `queued` run IDs in ID order (not FIFO), plus `blocking_run`
when a saved run is unfinished. A missing staff store means the queue is empty;
an unreadable or inconsistent store is an error, not an empty queue. The view
omits staff identities, request keys, records and credentials, and reads a
point-in-time snapshot without claiming the worker lease or changing any
status. It does not add a staff endpoint.

- No blocking run: manually choose a queued ID and use `run-request`.
- `check_worker`: the saved status is `running`, not proof a process is alive.
  Check whether the previous worker is still active before resuming.
- `resume_after_recovery`: the prior failure is in the retryable allowlist.
  Confirm the dependency recovered and explicitly resume that same ID.
- `wait_rate_limit`: a bounded source 429 deadline has not elapsed; see
  `retry_not_before_unix` and [the cooldown exercise](m3-rate-limit.md).
- `inspect_failure`: do not retry before inspecting the saved failure with
  `--store staff status <run-id>`.

These are hints about the saved state, not permission to proceed. The worker
still verifies its lease and run state when an operator issues a command.

## Cancel a queued request (M3e)

If an import is no longer wanted, only the local operator can cancel it, and
only before a worker claims it. Copy the exact queued ID from the queue or
staff status page. Use a disposable request to try the exercise:

```sh
.venv/bin/python -m import_job cancel-request <run-id>
.venv/bin/python -m import_job queue
.venv/bin/python -m import_job --store staff status <run-id>
```

The selected ID leaves the queue and its status becomes `cancelled` with zero
committed pages. Refresh the original staff status URL to see it. Repeating the
cancellation or submitting the same request key keeps the original ID and
status. A later import needs a new request key; neither start nor resume accepts
the cancelled ID. Running, failed and completed imports cannot be cancelled.

Cancellation and claim both take a short SQLite writer transaction. If they
race, the first committed decision wins: a cancellation prevents the source
call; a claim prevents cancellation. This does not stop a running process, and
it does not change the import records, the lease, the app's roles, or its HTTP
routes. At rest a cancelled row is a zero-checkpoint record with a reserved
nonretryable disposition. This preserves the existing SQLite schema and stops
the preceding M3d worker from restarting that ID. M3d reports it as a blocking
failure, not as cancelled; do not use M3d for normal operation on a store that
has cancellations.

日本語メモ：未開始の依頼だけをローカルCLIから取り消せます。先に開始が確定した依頼には効きません。
担当者は元のURLで取り消しを確認でき、同じキーの再送では依頼が復活しません。再依頼には新しいキーを使います。

## Recover the same run

For the page-two exercise, start the source with `LAB_M3_FAIL_PAGE_TWO=1` as in
the [M3b runbook](m3-recovery.md) before the operator begins. The status page
should report `failed`, `source_http_503` and one committed page (two records).
After restoring the local source, run:

```sh
.venv/bin/python -m import_job --store staff resume <run-id>
```

The original URL now shows `complete` with the same ID, three committed pages
and six records. `run-request` refuses a run that already started; it is not a
retry command. No automatic retries are performed. After a nontransient
checkpoint failure, stop and inspect the preserved state as described in the
[M3b runbook](m3-recovery.md); do not force a new run against that store.

Windows uses `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

## API contract and local limits

- Use a staff bearer access token to send bodyless `POST /api/imports` with one
  `Idempotency-Key` header (16 to 64 letters, digits, underscores or hyphens).
  The first accepted request returns `202`; reuse by the same authenticated
  subject returns `200`, the original ID, and its current state. A different
  key creates a different request. The browser includes a key in each displayed
  form, so double submission of the same form is safe.
- The `Location` header and returned `status_url` identify
  `GET /api/imports/<run-id>`. It requires the same importer subject and current
  importer role. A non-owner importer receives the same `404` as an unknown ID.
  A worker token is not a staff token. Status never includes the request key,
  subject, source token or records.
- The request, run and checkpoints persist in the ignored local
  `.import-lab/staff.sqlite3` store; app sessions do not persist across restarts.
  At most eight requests can await a worker; an already-accepted key still
  replays when the queue is full. A single local worker holds the existing
  store lease. Staff status is read-only and only the operator can start or
  resume work. Direct CLI exercises continue to use separate named stores.
- There is no automatic dispatch, cancellation of running work, tenant isolation, or production
  durability claim. Keep the lab local and use only the simulated identities.

## Compare both import identities without servers

Run the targeted offline handoff tests:

```sh
.venv/bin/python -B -m unittest discover -s tests -p test_offline_identity_handoff.py -v
```

The offline identity matrix supplies the registered direct and federated
roles. Separate disposable stores start from the same empty state. With
each authorized importer the real app accepts one request (202); replay with
the same key returns the same ID and does not dispatch work (200). The actual
operator command follows an in-process synthetic workload grant and paging
transport. A page-two 503 leaves one committed page and two records. Explicit
resume keeps the same run ID, starts from page two, and reconciles six exact
records. Direct/federated viewers and the unmapped account get 403 on the
tested write and status operations without changing either store or lease.

A second fixture deliberately supplies the same issuer, subject and allowed
role on both input paths. At the app boundary they are the same owner for
replay; a different subject gets a different request and cannot read the
first one's URL. That does not link real accounts, establish they share a
subject in Keycloak, or test a live login, token grant, or source API.

**日本語メモ：** 直接ログイン相当とSAML経由相当の架空の権限を使い、同じ条件で
依頼の再送、途中停止、同じIDでの再開を比べます。両方で6件を照合でき、
閲覧者や未割り当て利用者の操作ではDBが変わりません。同一人物かどうかは、
認証基盤がアプリに渡す利用者IDで判定する想定で、実アカウントの統合は試しません。

## Verify without using your staff store

With only the M3 Keycloak container already running and ports 8000 and 8001
free, the checker creates a separate temporary store and starts and stops its
own app and source processes:

```sh
.venv/bin/python tests/check_live_staff.py
```

It checks the real local OIDC and SAML flows, replay after app restart, denied
access for another importer, owner-scoped listing after app restart (M3j),
a page-two failure and same-run resume, and exact
readback of one request, one run and six distinct rows. It does not read or
modify the regular staff store. The reference run on macOS ARM64, Python 3.15.2
and Keycloak 26.8.2 passed this check and all 21 suite tests, including live
OIDC, SAML and workload-token checks. Desktop browsers and Windows were not
independently tested. Finish by stopping any other lab processes you
started and running `docker compose -f compose.m3.yaml down`.
