# 実装と検証の状態

2026年10月18日。I01〜I14を実装・検証済み。I15は接続コードと少数の実API機能試験を完了。支払い設定後、初回2ページ＋修正1ページのT14が成功。作者の品質採否と実請求明細の照合は別途。GitHub Issueのclose状態ではなく、このリポジトリ内の作業結果を記録する。

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
| I12 出力と復元 | 完了 | export.py。採用snapshot、比率保持、連番PNGのフォルダー出力、模擬外部状態を含むバックアップ |
| I13 受入 | 完了 | 全40試験成功、900ジョブの性能測定、ローカル安全性試験 |
| I14 引き渡し | 完了 | AIエージェント2体による内部確認（人間レビュー未実施）、受け入れ記録、起動と運用手順 |
| I15 実API | 接続・少数live機能検証完了 | 2ページ一括生成＋1ページ修正、非対象の採用hash不変、フォルダー出力を実証。上限3維持。[実API検証結果](live-acceptance-report.md)。作者品質採否・実請求明細照合は別途 |

[受け入れ結果](acceptance-report.md)、[実装レビュー](implementation-review.md)、[運用手順](operations.md)を参照する。

現在の全体検証は103試験成功（Chromiumを含む）。旧ZIP時点の実DB・出力をAIエージェント2体が内部確認（人間レビュー未実施）。過去の40試験という記録はv1モック版完成時点の証拠。

フォルダー出力への変更を実施。全体試験は105件成功、追加ブラウザ試験1件は登録完了待機を修正して再実行成功。既存の採用画像2枚も追加生成なしで通常のフォルダーへ出力し、画面のボタンから開く実操作を確認した。今回、新たなAIレビュー依頼は行っていない。
