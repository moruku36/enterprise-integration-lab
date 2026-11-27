# M3b: Resume a paginated import from SQLite

[English](../README.md) · [日本語](../README.ja.md)

This exercise starts a standalone run with the local worker CLI. For a run created
by the staff app, see the [M3c operator handoff](m3-staff-requests.md). The standalone
[M3a read-only CLI](m3-service-identity.md) is still available.

## Reproduce a page-two outage

Prepare the local Keycloak service and Python environment from the [M3a runbook](m3-service-identity.md).
If you already have a source server on port 8001, stop it first. Run this source
process in a separate terminal:

```sh
LAB_M3_FAIL_PAGE_TWO=1 .venv/bin/python -m uvicorn mock_source:source --host 127.0.0.1 --port 8001 --no-proxy-headers --no-access-log
```

In another terminal, use a fresh store name and start an import:

```sh
.venv/bin/python -m import_job --store recovery-demo run
```

The command exits nonzero. It reports `failed`, `source_http_503`, one committed
page, two processed records and next cursor `after-002`. Keep the printed run ID.

Stop the faulting source process, then start it without the outage flag:

```sh
.venv/bin/python -m uvicorn mock_source:source --host 127.0.0.1 --port 8001 --no-proxy-headers --no-access-log
```

Replace `<run-id>` with the previous ID:

```sh
.venv/bin/python -m import_job --store recovery-demo status <run-id>
.venv/bin/python -m import_job --store recovery-demo resume <run-id>
.venv/bin/python -m import_job --store baseline-demo run
.venv/bin/python -m import_job --store recovery-demo run
```

The read-only `status` command still exits nonzero for a stored failure; it does
not create or change a run. The resumed run keeps its ID and completes all three
pages. The baseline uses a distinct store. Both stores should hold the same six
IDs and titles. The last command is a new run against the already-populated
recovery store: `processed_records` is 6 and `changed_records` is 0. It adds a run
record but leaves all six case records unchanged.

On Windows use `.venv\Scripts\python.exe`. For the faulting source set
`$env:LAB_M3_FAIL_PAGE_TWO = "1"` in its PowerShell terminal and remove that
environment variable before the healthy restart.

## When to resume

- Check the run ID, error category, committed page, and next cursor. The local
  database is `.import-lab/<store>.sqlite3`; all generated stores are gitignored.
- For a documented transient network or service failure, restore the local
  service and explicitly resume the same run. There is no automatic retry.
  A run left `running` by an interrupted process can resume once that process
  releases the store lease.
- A source 429 requires a supported `Retry-After` and a saved deadline.
  [Try the bounded cooldown exercise](m3-rate-limit.md) (M3i); no retry is automatic.
- Do not resume a 401, 403, changed snapshot, invalid or repeated cursor,
  duplicate record, or database error. Check configuration and data integrity
  first. The CLI rejects resuming a known nontransient failure. A separate store
  lets you start a fresh exercise without deleting the previous evidence.
- Do not move the database or remove the lease file while its importer is
  running. Keep these SQLite stores on the local filesystem.

The source supplies a fixed snapshot marker. The worker verifies it on every
page, uses a unique constraint on record IDs, and commits the row upserts and
the updated run checkpoint in one SQLite transaction. A separate local SQLite
lease prevents a second importer from working against the same store while
letting each successful page commit independently. Do not log tokens, secrets
or response payloads. This is not tenant isolation, a production queue, or a
test of sudden power loss.

## Reconcile a completed import (M3f)

The local operator can check the result after the source and identity service
are stopped. Use the same store and run ID as the earlier import:

```sh
.venv/bin/python -m import_job --store recovery-demo reconcile <run-id>
# Or: --store staff reconcile <run-id> for an operator-started staff request.
```

A matching result has `matches: true`, `checkpoint_ids_match: true`, six
expected and stored records, and zero missing, unexpected or value mismatches.
Exit status 0 means a match. A mismatch returns a summary and exits 1; an
unknown or incomplete run stops with an error instead of claiming a match.
A second successful import with zero changed records can still match.

The command compares the saved checkpoint IDs and the **present** SQLite
destination with an explicit reference for `case-fixture-v1`. It reads one
SQLite snapshot, does not start an import or obtain a worker lease or
credentials, and reports only counts, run and fixture identifiers, and match
flags. It does not expose row IDs or titles in the report, change any records,
contact the source, or repair data. A match does not prove the historical state
of the destination at completion or the current content of an external API.
Do not adapt the fixed reference into a production audit without defining an
independent source for ground truth and a retention model.

日本語メモ：`complete` は処理が最後まで進んだこと、`reconcile` は今ある
データが期待値と一致することです。件数が同じでもIDの欠損・余分なID・
値の違いを別々に数えます。照合はローカルで読み取りのみ。データを直したり
元のAPIへ問い合わせたりはしません。

## Try reconciliation without running services (M3g)

Once this lab's Python dependencies are installed, run the walkthrough without
Docker, Keycloak, or a source server:

```sh
.venv/bin/python examples/reconciliation_walkthrough.py
```

Windows: `.venv\Scripts\python.exe examples\reconciliation_walkthrough.py`.

The walkthrough makes its own SQLite stores in a temporary directory and
removes them at the end. It does not open your existing stores. It seeds an
interrupted run with one page and two rows, then feeds in the remaining synthetic
pages under the same run ID. It calls the real `status` and `reconcile` commands
against those stores; page delivery in this exercise is simulated in Python, not
an HTTP outage or a re-test of OIDC/SAML.

Predict the outcome before running it: both copies report six stored rows, and
the completed checkpoint is identical. Only the healthy copy matches. The
second copy reports one missing row, one unexpected row and one changed value.
A `complete` status on its own would not distinguish them. The original stores
are not modified, and the walkthrough verifies no read check changed even its
disposable stores.

日本語メモ：上のコマンド1つで、途中で止まった取り込みを合成データで
再開し、「6件で完了」と「正しい6件」の違いを確認できます。OIDCやSAML
を試す手順とは別です。既存データには触れません。

## Contrast a snapshot change with a temporary outage (M3h)

A provider can return **HTTP 200** even when the returned page no longer
belongs to the version used for page one. Retrying a temporary outage and
combining two different snapshot versions are not the same operation.

With the disposable M3 Keycloak fixture running, port 8001 free, and lab
dependencies installed, use the same local acceptance runner with a different
scenario:

```sh
.venv/bin/python tests/check_live_recovery.py --scenario snapshot-drift
```

The runner launches its own synthetic loopback source. Its fault is the changed
snapshot marker on page two, not an HTTP error, bad credential, or altered
record format. The real importer exits with `invalid_snapshot` and keeps only
page one (two rows). Even after the healthy source returns, explicit `resume` on
the same run is refused and the failed store is unchanged. The runner then
starts a separate store, completes and reconciles its six rows, and removes
only its temporary test stores. The default runner without `--scenario` still
demonstrates that a temporary HTTP 503 can be resumed under the same run ID.

For a manual local demonstration, set `LAB_M3_DRIFT_PAGE_TWO=1` on the source
process instead of the outage flag; the source refuses both options together.
The normal source returns the fixed snapshot and does not activate either
fault. This is a simulated marker change, not a test of a mutable production
API or a replacement for a real snapshot/version contract.

日本語メモ：2ページ目のHTTP応答が200でも、版の目印が変わったら別データとして
停止します。1ページ目の2件を保持し、元の版に戻っても同じ実行IDでは再開しません。
一時的な503なら同じIDで再開できます。演習はどちらも架空のデータのみです。

## Reference checks

The full M1/M2/M3 suite passed **16/16** on macOS ARM64 with local Keycloak
26.8.2 and Python 3.15.2. An offline SQLite check rejected the second write of
page two and confirmed both rows and the cursor rolled back together.

With Keycloak still running and port 8001 free, the separate integration check
launches the two disposable source sessions and runs the actual importer CLI:

```sh
.venv/bin/python tests/check_live_recovery.py
```

The reference run observed HTTP 503, durable page one, the same run ID on
resume, six exact SQLite rows matching a clean run, and no changed rows on a
rerun. This check deletes only its own temporary stores. Stop all test processes
and use `docker compose -f compose.m3.yaml down` when finished.

- [SQLite transaction control](https://www.sqlite.org/lang_transaction.html)
- [SQLite conditional UPSERT](https://www.sqlite.org/lang_upsert.html)
