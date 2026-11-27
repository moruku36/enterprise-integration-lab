# M3i exercise: Respect a source API cooldown

[English README](../README.md) · [日本語 README](../README.ja.md) · [Recovery runbook](m3-recovery.md)

**Question:** A provider rejects page two with HTTP 429. Can the operator return
to the **same run** without hammering the API or merging two versions?

With the [disposable M3 identity fixture](m3-service-identity.md#run-locally)
running and localhost port 8001 free, run:

```sh
.venv/bin/python tests/check_live_recovery.py --scenario rate-limit
```

On Windows use `.venv\Scripts\python.exe`. The runner uses only fictional
records, loopback services, and temporary stores. The source's optional rate
limit on page two returns 429 with `Retry-After: 8`; it does not change the
snapshot marker or relax source authorization. An early resume is refused
before fetching a token or contacting the source, and the failed store is
unchanged. After the wait and a switch to the healthy fixture, explicitly
resume the same ID: pages two and three commit, and all six records reconcile.

## Rules the exercise demonstrates

- Save the received throttle and minimum resume time together with the existing
  run's failure, without advancing its checkpoint. CLI `status` shows
  `retry_not_before_unix` (whole Unix seconds, rounded up). For a staff store,
  the local operator's `queue` shows `wait_rate_limit` and that deadline; after
  it passes, `resume_after_recovery` does **not** mean a worker was started.
- Only a **source GET** 429 with one supported numeric delay can enable this
  manual resume. The lab accepts 1–300 seconds in 1–3 digits. Missing,
  duplicate, zero, longer, malformed or HTTP-date values and identity-service
  429 do not enable resume. The [HTTP standard](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)
  also defines HTTP-date; this small lab deliberately does not implement it
  or shorten a provider's longer delay.
- The timer is a minimum based on the local clock. Resume still requires an
  explicit operator command, a healthy source, and the same snapshot. A fresh
  throttle can set a new deadline. This gate is per run, not a shared
  per-provider quota across stores or workers. Do not switch stores to evade
  the limit. There are no automatic requests.
- Store metadata is additive; existing stores need no migration. The older CLI
  ignores the optional side table and conservatively refuses source 429.
  This is durability after an error is recorded, not a guarantee if the host
  fails before recording it. A real external provider and hosted systems were
  not tested.

**日本語メモ：** 2ページ目が429を返したら、1ページ目の2件を残して停止します。
待ち時間を保存し、期限前の再開ではAPIにアクセスしません。回復後に運用担当者が
明示的に再開すると、同じ実行IDで残りを取り込み、6件を照合できます。待ち時間が
未指定・形式外なら、自動では再開しません。
