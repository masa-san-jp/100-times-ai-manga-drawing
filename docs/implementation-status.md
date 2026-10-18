# v1モック版の実装結果

2026年10月18日。I01〜I14を実装・検証済み。I15は接続コードとオフライン検証を追加。実APIのBatch createはHTTP400で拒否され、T14は未完了。GitHub Issueのclose状態ではなく、このリポジトリ内の作業結果を記録する。

| Issue | 結果 | 主な実装と証拠 |
| --- | --- | --- |
| I01 起動基盤 | 完了 | pyproject.toml、uv.lock、cli.py、CI。init/web/worker/doctor/backup/restore/example |
| I02 模擬API | 完了 | providers/mock.py。永続外部ID、試験PNG、遅延と障害scenario、Gemini無効化 |
| I03 保存 | 完了 | migrations、db.py、assets.py。hash保存、原子的ファイル更新、FKとCAS |
| I04 登録 | 完了 | projects.py、imports API、examples/minimal。検証、一括登録、再登録、冪等receipt |
| I05 プロンプト | 完了 | prompts.py。固定テンプレート、入力版、全文編集、hash検証 |
| I06 計画と枠 | 完了 | planning.py。固定計画、期限、冪等キー、送信枠、再試行 |
| I07 バッチ | 完了 | worker.py。チャンク、同時外部枠、独立起動、pause、heartbeat |
| I08 審査 | 完了 | review/revise API。採否、過去候補、原子的修正再生成 |
| I09 復旧 | 完了 | unknown照合、2走査結果検査、画像欠損回復、実プロセス停止試験 |
| I10 UI | 完了 | Jinja2とJS/CSSの3画面、ライト/ダーク、1280/360px確認 |
| I11 結合 | 完了 | Chromiumの競合、応答喪失、再生成、入力保持試験 |
| I12 出力と復元 | 完了 | export.py。採用snapshot、比率保持、ZIP、模擬外部状態を含むバックアップ |
| I13 受入 | 完了 | 全40試験成功、900ジョブの性能測定、ローカル安全性試験 |
| I14 引き渡し | 完了 | 独立実装レビュー2名PASS、受け入れ記録、起動と運用手順 |
| I15 実API | 接続実装・offline検証済み／live未完 | SDK契約試験に加えて実接続を試行。モデル情報取得は成功、生成3要求はHTTP400で拒否。T14未完了。[検証記録](gemini-acceptance.md) |

[受け入れ結果](acceptance-report.md)、[実装レビュー](implementation-review.md)、[運用手順](operations.md)を参照する。
