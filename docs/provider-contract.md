# プロバイダー契約

既定はmock。I15でGemini接続コードとSDKを使うオフライン契約試験を追加した。実アカウントでも初回2ページ＋修正1ページのBatch送信・結果取り込み・T14機能試験が成功した。作者による品質採否は別途。[実API検証結果](live-acceptance-report.md)を参照。

## ローカル契約

`providers/base.py`のBatchProviderがupload、submit、find、poll、fetch、parse_lineを定義する。mockのbuild_jsonlは各行にkey（attempt ID）、page_number、candidate_index、attempt_number、snapshotを保存する。snapshotにはプロンプト全文、順序付き画像hash、資料の版、モデルと出力設定を含む。

模擬結果の各行はkeyとusage、images（base64画像のリスト）またはerrorまたはtextを持つ。1要求1画像を成功とし、結果全体を2走査してキー重複・欠落を検査してから候補を登録する。これはローカル試験用schemaで、Geminiの実応答schemaではない。

## 永続化と障害

模擬外部状態はDATA_DIR/mock-provider/state.sqlite3、入力と結果はinputs/outputsへ保存する。ready_atを使ってpoll時に完了判定し、worker再起動でも同じIDを回収する。これらは完全バックアップに含まれる。

| scenario | 挙動 |
| --- | --- |
| success | 2秒後に試験PNGを返す |
| delay | 通常待ち時間の5倍 |
| 429 | 各要求の初回を明示拒否、枠があれば待機後に再試行 |
| reject | 要求を拒否 |
| timeout | 外部処理を永続化してから応答喪失、findで回収 |
| mixed | 偶数ページを拒否、奇数ページを成功 |
| duplicate / missing / unknown_key | 重複・欠落・不明のrequest keyを返す |
| corrupt / text_only / multiple | 不正画像・画像なし・複数画像を返す |

通常はmock_delay=2秒、poll=1秒。テストではConfigと注入した時計で待ち時間を短縮する。submit後不明、結果異常、画像再取得は新しい生成を自動で開始しない。

## Gemini接続

`providers/gemini.py`はgoogle-genai 2.31.0の公開APIを使う。入力画像をinlineDataとしてJSONLに固定し、Files upload → native Batch create → poll → 結果ファイルのストリーム取得を行う。モデルは`gemini-3-pro-image`、2K・3:4・1要求1画像。モデル情報取得で提供とBatch対応を確認済み。支払い設定後、実生成3要求と結果の取り込みに成功。

SDKの自動再試行は無効。429は明示拒否として待機し、送信応答喪失はunknownとして照合する。照合ゼロ件から未送信と断定しない。アップロードの事前障害は最大3回で停止する。ネットワーク待機中はDB書込ロックを保持しない。

送信は明示live設定・キー・累積要求上限・予算単位がすべて揃った場合のみ可能。[設定と制約](operations.md#gemini接続)を参照。外部処理間隔は最短5秒、pollは60秒。生成を伴うAPIはオフライン試験では一度も呼んでいない。

公開仕様の参照先：[Batch API](https://ai.google.dev/gemini-api/docs/batch-api)、[画像生成](https://ai.google.dev/gemini-api/docs/image-generation)、[Python SDK](https://googleapis.github.io/python-genai/)。ライブ受け入れ時にアカウントの提供モデル・実schema・料金を確認する。

未受理が確定したnative createの拒否と送信前の確定失敗は、Geminiの要求枠とバッチ予算予約を返却する。成否不明や受理後の失敗は保持する。返却は一度だけ監査記録付きで行い、送信履歴は削除しない。
