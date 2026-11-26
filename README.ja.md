# Enterprise Integration Lab

[English](README.md) | **日本語**

企業の認証基盤、権限付きのAPI、再開可能なデータ取り込みを、1つのアプリで学ぶための学習用ラボです。架空のユーザーと案件データだけを使い、クラウドアカウントやAIモデルなしでローカル実行できる構成です。

## 学ぶ順番

1. **OIDCと認可**：閲覧者は一覧を見られ、取り込み担当者だけが取り込みを開始できます。
2. **SAML連携**：顧客の認証基盤を模擬し、Keycloakの仲介を通して接続します。アプリ側のOIDC連携は変えません。
3. **APIとデータ連携**：専用のサービスIDで架空データを取得します。途中で止めても、欠損や二重登録なく再開できるかを確認します。

[設計と構成図（英語）](docs/architecture.md) · [実装順と合格条件（英語）](docs/acceptance.md)

**現在地：M1・M2をローカル向けに実装。** [OIDCログインとAPI権限を試す（英語）](docs/m1-local.md)・[顧客SAML連携を試す（英語）](docs/m2-local.md)。
[サービスIDで3ページのデータを取得する（英語）](docs/m3-service-identity.md)（M3a）。
[CLIでSQLiteへの取り込みと障害からの再開を試す（英語）](docs/m3-recovery.md)（M3b）。
[画面で取り込みを依頼し、同じ実行IDで進捗を確認する（英語）](docs/m3-staff-requests.md)（M3c）。取り込みの開始はローカルの運用担当者がCLIで行います。
ローカルの運用担当者は、開始・再開の前に[待ち行列を確認できます（英語）](docs/m3-staff-requests.md#inspect-the-queue-m3d)（M3d）。
未開始の依頼だけを[取り消すこともできます（英語）](docs/m3-staff-requests.md#cancel-a-queued-request-m3e)（M3e）。
依頼の履歴や取り込み済みデータは削除されません。同じ依頼を再送しても、元のIDで取り消し済みと表示されます。
完了後、[取り込み先のデータを固定の正解と照合できます（英語・日本語補足）](docs/m3-recovery.md#reconcile-a-completed-import-m3f)（M3f）。
[サーバーを起動せず、コマンド1つで照合を体験できます](docs/m3-recovery.md#try-reconciliation-without-running-services-m3g)（M3g）。
