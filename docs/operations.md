# モック版の起動と運用

## 起動

Python 3.12以上とuvを使う。初回だけ `uv sync --locked` で依存を導入する。その後のアプリ利用はインターネットとAPIキーを必要としない。

```sh
uv run manga init
uv run manga web
```

別のターミナルでワーカーを起動する。

```sh
uv run manga worker
```

ブラウザで http://127.0.0.1:8765 を開く。保存先はmacOSでは `~/Library/Application Support/manga-drawing`、Linuxでは `~/.local/share/manga-drawing`。変更する場合は両ターミナルで同じ `MANGA_DATA_DIR` を設定する。クラウド同期フォルダーやネットワークドライブは保存先にしない。

Webを閉じてもワーカーは動く。ワーカーが停止したら同じコマンドで再起動する。ロックを削除して二重起動しない。`Ctrl+C`で停止でき、送信済みの模擬処理は次回に回収する。

## 試す

```sh
uv run manga example --output /tmp/manga-example --pages 3
```

作品登録でこのフォルダーを選ぶ。manifest.jsonと画像3枚を検証して登録し、未生成ページを生成する。サンプルはプログラムで作った試験画像であり、漫画原稿やAI作画ではない。

ページ詳細で候補を採用するか、修正内容を書いて「却下して保存」「修正して再生成」を使う。一覧から要修正ページだけをまとめて再生成できる。全文編集、過去候補、障害時の操作は詳細領域に置いている。

## 入力と更新

manifestの形式は `uv run manga example` の出力を参考にする。PNG/JPEG、1ファイル20MiB、登録1回1GiB、画像40MP、作品500ページまで。フォルダー相対パスで指定する。ZIP入力は非対応。サブフォルダーがある作品はフォルダー選択を使う。

作品設定から同じ作品へ再登録すると同番号ページのdraftを更新し、採用と生成履歴は保持する。共通人物資料の変更は次回の生成だけに反映する。原稿画像の差し替えは再登録、人物とシーンの説明は資料の詳細から編集する。

## エラーへの対処

| 表示 | 操作 |
| --- | --- |
| ワーカー停止中 | 別ターミナルでmanga workerを起動 |
| 送信枠不足 | 一覧の詳細から送信件数を追加。モック版の枠は試験用で、課金はない |
| 結果確認が必要 | 外部処理の再照合を使う。0件でも勝手に再送しない |
| 結果キー異常 | 同じ結果の再取得を試す。解決しなければ重複の可能性を理解して明示再送 |
| 生成の確定失敗 | 詳細で再試行。演出を変える場合は却下して修正再生成 |
| 編集競合 | 未保存の文字を退避し、再読み込み後に他の変更を確認して入力し直す |
| 画像欠損 | doctorとバックアップを確認。ワーカーは同じ外部結果から再取得する。採用を自動変更しない |

通信応答が失われた生成操作はsessionStorageに同じ操作キーとpayloadを保持する。元の操作ボタンを再度押すと同じ要求を照合する。タブを閉じるとsessionStorageは失われるため、その場合は一覧で処理結果を確認してから追加生成する。

## 書き出しとバックアップ

採用画像を全ページまたは採用済み分だけZIPに書き出す。グレースケールPNGを比率保持・白余白で指定寸法へ変換し、原本を変更しない。manifest.jsonに採用IDとhash、未採用ページ、provider=mockを記録する。

```sh
uv run manga doctor
uv run manga backup --output /absolute/path/new-backup
```

バックアップ先は新しいフォルダーを指定する。DB、原稿、生成結果、模擬外部状態を整合した形で保存する。DBファイル単独のコピーで代替しない。

復元はWebとワーカーを停止して、空の保存先に対して実行する。

```sh
MANGA_DATA_DIR=/absolute/path/restored uv run manga restore --source /absolute/path/new-backup
MANGA_DATA_DIR=/absolute/path/restored uv run manga doctor
```

## 開発時の検証

```sh
uv run ruff check src tests scripts
uv run pytest -q
uv run playwright install chromium
MANGA_BROWSER_TESTS=1 uv run pytest tests/test_browser.py -q
uv run python scripts/benchmark.py
```

ブラウザ試験はlocalhostの一時ポートを使い、スクリーンショットをartifacts/screenshotsへ保存する。CIも模擬APIだけを使う。実API、カラー、複数ユーザー、外部公開は未対応。


## Gemini接続

既定はmock。実接続コードの導入は`uv sync --locked --extra gemini`。起動するWebとworkerの両方に、同一の環境変数を設定する。

| 変数 | 内容 |
| --- | --- |
| MANGA_PROVIDER | gemini |
| MANGA_ENABLE_LIVE | 1（実送信を明示的に有効化） |
| GEMINI_API_KEY | 利用するアカウントのキー。履歴やリポジトリへ保存しない |
| MANGA_LIVE_MAX_REQUESTS | 保存先全体の累積要求予約数の上限。正整数 |
| MANGA_LIVE_UNIT_RESERVE_MICRO_USD | 1要求の予算予約単位。正整数、micro USD |
| MANGA_DATA_DIR | 保存先。mockとは別の専用ディレクトリーを推奨 |

上限・予算単位の値をアプリが推測して実送信することはない。初回live検証は少数の要求と明示した予算で行い、実schema・出力・利用明細を照合する。現時点でその検証は未実施。

要求上限は日次やバッチごとではなくDATA_DIR単位の累積値。予約は失敗・unknown・事前障害でも返却しない。画面の残数は予約可能数であり成功画像数ではない。複数バッチの合計が上限に達すると待機し、上限縮小後の準備済みチャンクも停止する。再送は追加予約を消費する。

予算単位は内部の予約額で、Googleの請求額を保証する金額上限ではない。実料金・利用枠は実行前に利用アカウントで確認する。SDKの自動再試行は無効。送信の成否不明時は自動再送せず照合し、重複リスクを明示した操作でのみ再送する。

既存の候補には生成元を表示し、ZIPにも記録する。起動providerを切り替えても旧providerのジョブを別APIへ送らない。キーは環境変数で受け取り、DB・バックアップ・画面へ保存しない。
