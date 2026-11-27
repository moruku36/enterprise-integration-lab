# M4a: Stage a verified preview of the imported cases

[English README](../README.md) · [日本語 README](../README.ja.md) · [Staff request runbook](m3-staff-requests.md)

**Question:** When can a completed integration safely serve its result to
staff? Completion, reconciliation and making a read copy are separate steps.

1. In the disposable lab, finish a staff-requested import using the [staff
   handoff](m3-staff-requests.md#try-the-staff-to-operator-handoff).
2. As the importer or viewer, open **Verified imported case preview** from
   the home page. It is empty until a local operator explicitly stages a copy.
3. As the local operator, stage the latest completed staff run. Replace
   `<run-id>` with its actual local lab ID:

   ```sh
   .venv/bin/python -m case_release stage <run-id>
   ```

   On Windows use `.venv\Scripts\python.exe`. The command does not connect
   to Keycloak or the source. It checks the work store against the six exact
   lab records and saves a separate copy in one SQLite transaction.
4. Refresh the preview: the six verified synthetic records now appear. The
   API equivalent is `GET /api/imported-cases` with the same staff access token
   and case-reader permission required by `GET /api/cases`. The existing
   `/api/cases` still serves its original M1 example.

A repeated stage for the same identical run is a no-op. A running or unresolved
failed import, an older run, or any mismatch (including the same row count with
a different value) leaves the prior preview alone. The display does not read
working rows directly. It remains an earlier verified copy even if the work
store changes; it is not proof the source is currently unchanged. The staff
view does not include the operator's run ID, another owner's request, input
payloads from an unverified source, or credentials.

## Recheck the already staged copy

Use the run ID from the operator's stage receipt. Rechecking needs no source
server or Keycloak:

```sh
.venv/bin/python -m case_release verify <run-id>
```

A match returns `run_id`, `snapshot`, `records: 6` and `verified: true`, with no
record titles or IDs. Repeating it returns the same receipt while that copy
is unchanged. A missing copy, a different current run, or a corrupt copy
returns nonzero instead. The check does not write, acquire a worker lease,
or consult the working table. It describes the local copy at read time;
it is not independent proof of who approved or first staged it.

Use `import_job --store staff reconcile <run-id>` to inspect the **current
working data**, and `case_release stage <run-id>` only when intending to create
or replace the read copy. It is expected that staging can refuse an
unmatched working dataset while verifying the existing copy still passes.

**日本語メモ：** 運用担当者は、作成時の実行IDを指定して、今表示されているコピーだけを
読み取りで再確認できます。取り込み先の作業データに不一致があっても、前回確認した
コピーが無事なら検証は通ります。再作成したり、外部APIを呼び出したりはしません。

## Verify while a replacement commits

Run the targeted offline operator test:

```sh
.venv/bin/python -m unittest discover -s tests -p test_operator_verify_read_snapshot.py -v
```

It pauses the real `verify` entry point after a SQLite read of the first
copy, then commits a different test-only copy through the normal stage
operation. The paused invocation checks all six values from that one read,
then reports the first ID, reference and count, even though it prints after
the other commit.
The next invocation rejects the first ID and verifies the new ID. A receipt
is therefore a point-in-time observation, not a lease or a promise that
the copy has stayed current after the command returns.

**日本語メモ：** 照合が読み取った直後に別のコピーが確定しても、その照合結果には
読み取った時点のIDと6件だけが使われます。確定後にもう一度実行すると、古いIDは
拒否され、新しいIDで確認できます。複数回のコマンドにわたって版を固定する機能
ではありません。

## Verify rollback without servers

After installing the lab's Python dependencies, run the no-server walkthrough:

```sh
.venv/bin/python examples/preview_rollback_walkthrough.py
```

The walkthrough makes its own disposable synthetic staff store. It runs the
real stage command with a **test-only** benign SQLite error after the new copy
has been written inside the transaction but before commit. An independent
reader still sees no first copy, or all six records from the prior copy if
replacing one. A fresh process after the stop sees the same state and unchanged
database bytes. The next clean invocation of the normal CLI stages six
matching records. It does not open your local lab state or contact an API.

This tests a controlled rollback, not sudden process termination, disk
failure, or power loss. It does not simulate an external identity or source.

**日本語メモ：** 最初の作成と作り直しを、確定（コミット）の直前で止める演習です。
別の読み取り処理には、初回なら何も表示されず、作り直しなら前回の6件が残ります。
新しいプロセスで開いても同じで、その後の通常の操作で6件を表示できます。
実際の停電やプロセスの強制終了を試したわけではありません。

## Observe two readers during a successful update

Run the targeted HTTP test with the same local Python environment:

```sh
.venv/bin/python -m unittest discover -s tests -p test_preview_reader_isolation.py -v
```

Two separate authorized test clients, a viewer and an importer, read the real
preview endpoint through the app in-process. The test temporarily models a
second valid synthetic snapshot with six changed titles. Readers continue to
see the whole first copy while two working records have changed, after the
second import completes but before staging, and after the stage write but
before commit. Both next reads after commit see exactly the six revised titles.
The test requires no hosted identity, source, or network listener. The
shipped lab still supports only its original fixed fixture; the second
snapshot is test data, not an application extension. There is no promise to
pin a client to one release across multiple GETs or across distributed nodes.

**日本語メモ：** 閲覧者と取り込み担当者が別々にAPIを読みます。新しい版を取り込んで
いる途中も、閲覧用コピーを書いてコミットする直前も、2人に見えるのは前回の6件です。
コミット後は2人とも更新後の6件を読みます。2つ目の版はテスト専用で、実際の
データ元や公開環境には接続しません。

This is a single-current-copy local teaching tool, not a live deploy,
publication to a third party, tenant boundary, durable audit history, or
production promotion approval. It handles only the fixed six synthetic
records. In a real integration, design explicit retention, provenance, and
privacy review with the data owner rather than reusing this fixture contract.

**日本語メモ：** 取り込みが完了しても、自動では閲覧画面に出しません。運用担当者が
正解データと照合してから、ローカルの閲覧用コピーを明示的に作ります。元データの
値が違った場合は、前回確認済みのコピーを残します。
