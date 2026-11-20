# アプリの構成

`web.py` はローカルの FastAPI サーバーを提供します。作品の登録と編集は `projects.py`、生成プランと採否は `planning.py`、キャラクター設定画は `character_references.py` が担当します。依頼時に `prompts.py` が本文・資料・設定をスナップショットへ固定します。

`worker.py` は保存された依頼をまとめてプロバイダーに送信し、結果を確認して画像を回収します。`providers/` は模擬 API と Gemini Batch API を共通の契約で扱います。`export.py` は採用結果の出力、却下候補の同期、バックアップと復元を担当します。

| SQLite のテーブル | 保存するもの |
| --- | --- |
| `works`, `projects`, `pages` | 作品、話、ページと編集・採用状態 |
| `reference_sets`, `reference_revisions` | 人物とシーンの資料、資料の版 |
| `character_reference_tasks`, `character_reference_selections` | 設定画の作業ページと採用画像 |
| `plans`, `batches`, `rounds` | 送信前の計画、依頼単位、ページごとのテイクとスナップショット |
| `jobs`, `attempts`, `chunks` | 候補ごとの依頼、試行、外部バッチ |
| `assets`, `candidates`, `exports` | ハッシュで管理する画像、生成結果、書き出し履歴 |
| `review_events`, `audit_events`, `mutation_receipts`, `input_imports`, `system` | 採否と変更の記録、二重実行防止、登録記録、稼働情報と保存先 |

候補のジョブは `queued` で始まり、試行を確保すると `running`、画像保存が終わると `succeeded` になります。再試行可能な明示エラーは `retry_wait`、停止した失敗は `failed`、外部の受理や結果を確定できない状態は `unknown` として扱います。

複数の試行を束ねるチャンクは `prepared` → `uploading` → `submitting` → `submitted` → `fetching` → `ingested` と進みます。送信応答を失った場合は照合し、受理の可能性がある間は同じ要求を自動再送しません。試行は外部要求キーで結果と突き合わせます。`jobs.active_attempt_id` は現在の試行を指し、過去の試行は残ります。

画像は保存先のハッシュ付きファイルで管理します。更新は書き込みロックと SQLite のトランザクションで保護し、外部 API の待ち時間中は保持しません。
