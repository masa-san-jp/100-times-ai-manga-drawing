# プロバイダー契約

v1はmockのみを実装する。Gemini APIに接続したという実績はない。Geminiのモデル、SDK引数、実schema、利用枠、期限の確認は後続I15で行う。

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

## 実APIプレースホルダー

providers/gemini.pyはProviderNotConfiguredを返す。plan/startとworkerの構成検査で実APIを拒否し、ジョブ、送信枠、SDK、ネットワークを起動しない。google-genaiはv1の依存に含めない。
