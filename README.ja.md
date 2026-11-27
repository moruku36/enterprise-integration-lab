# Enterprise Integration Lab

[English](README.md) | **日本語**

企業の認証基盤、権限付きのAPI、再開可能なデータ取り込みを、1つのアプリで学ぶための学習用ラボです。架空のユーザーと案件データだけを使い、クラウドアカウントやAIモデルなしでローカル実行できる構成です。

[手順と確認ポイントを順に試す](docs/learning-path.ja.md)。

## 学ぶ順番

1. **OIDCと認可**：閲覧者は一覧を見られ、取り込み担当者だけが依頼できます。実行はローカルの運用担当者が開始します。
2. **SAML連携**：顧客の認証基盤を模擬し、Keycloakの仲介を通して接続します。アプリ側のOIDC連携は変えません。
3. **APIとデータ連携**：専用のサービスIDで架空データを取得します。途中で止めても、欠損や二重登録なく再開できるかを確認します。

[設計と構成図（英語）](docs/architecture.md) · [実装順と合格条件（英語）](docs/acceptance.md)

**現在地：M1・M2をローカル向けに実装。** [OIDCログインとAPI権限を試す（英語）](docs/m1-local.md)・[顧客SAML連携を試す（英語）](docs/m2-local.md)。
[取り込み権限だけを外して、新規OIDCログインで確認する演習（日本語補足あり）](docs/m1-role-reduction.md)。
[サービスIDで3ページのデータを取得する（英語）](docs/m3-service-identity.md)（M3a）。
[CLIでSQLiteへの取り込みと障害からの再開を試す（英語）](docs/m3-recovery.md)（M3b）。
[画面で取り込みを依頼し、同じ実行IDで進捗を確認する（英語）](docs/m3-staff-requests.md)（M3c）。取り込みの開始はローカルの運用担当者がCLIで行います。
ローカルの運用担当者は、開始・再開の前に[待ち行列を確認できます（英語）](docs/m3-staff-requests.md#inspect-the-queue-m3d)（M3d）。
未開始の依頼だけを[取り消すこともできます（英語）](docs/m3-staff-requests.md#cancel-a-queued-request-m3e)（M3e）。
依頼の履歴や取り込み済みデータは削除されません。同じ依頼を再送しても、元のIDで取り消し済みと表示されます。
完了後、[取り込み先のデータを固定の正解と照合できます（英語・日本語補足）](docs/m3-recovery.md#reconcile-a-completed-import-m3f)（M3f）。
[サーバーを起動せず、コマンド1つで照合を体験できます](docs/m3-recovery.md#try-reconciliation-without-running-services-m3g)（M3g）。
[一時的な停止と、取り込み途中のデータの版違いを比べる演習](docs/m3-recovery.md#contrast-a-snapshot-change-with-a-temporary-outage-m3h)（M3h）。
[API側が429を返した場合、待ち時間を守って同じIDから再開する演習](docs/m3-rate-limit.md)（M3i）。
[再ログイン後に自分の取り込み依頼を見つける](docs/m3-staff-requests.md#find-your-recent-requests-m3j)（M3j）。
[照合が通ったレコードをローカルの閲覧用コピーにする](docs/m4-case-preview.md)（M4a）。
