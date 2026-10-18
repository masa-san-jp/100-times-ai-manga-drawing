# マンガ下書き生成システム 実装仕様

状態：v1モック版の引き継ぎ仕様  
更新日：2026年10月18日  
対象：初期リリース v1 モック版

本仕様を実装上の規範とし、作業順と完了条件は[実装計画](implementation-plan.md)、背景は[設計の骨子](software-design.md)を参照する。競合時は本仕様を優先する。MUSTは必須、SHOULDは理由をIssueに記録して変更可能とする。以下の「実装する」は将来の要求であり、実装済みを意味しない。

## 1 目的と設計決定

数十〜数百ページのモノクロ漫画について、ネーム、指示書、人物資料から下書きを一括生成する。作者がページごとに採用または却下し、プロンプトを修正して却下ページだけを再生成する。原稿のページ設計と清書は作者が担う。

| 決定 | v1の仕様と理由 |
| --- | --- |
| 利用環境 | 単一ユーザー、単一マシン、localhost専用。macOSとLinuxを対象とする |
| Web | Python 3.12以上、FastAPI、Jinja2、素のJavaScriptとCSS。SPAとNodeビルドを不要にする |
| 永続化 | Python標準sqlite3、SQLite WAL、ローカルファイル。Redis、Celery、ORM、外部DBを使わない |
| 実行 | Webと独立したワーカー1プロセス。プロバイダー側が並列生成する |
| 生成 | v1は永続状態を持つ模擬Batch APIで完成させる。Gemini接続は同じ境界の無効なプレースホルダーとし、後続I15で接続する |
| 初期モデル | mock-batch-v1。将来のGemini候補はgemini-3-pro-image、2K、3:4。未検証の実モデル設定で外部送信できない |
| 候補数 | 標準1、操作ごとに1〜3。3枚生成を全ページの初期値にしない |
| UI | 作品設定、ページ一覧、ページ詳細の3画面。ジョブ構造は詳細領域に収める |
| 対象外 | カラー、複数ユーザー、外部公開、共同編集、同期画像生成、AIによる自動採否、ストーリー自動生成 |
| 補助機能 | 人物資料の生成はv1から外し、既存シートの登録で完結させる |

v1の完成は模擬APIで動作するアプリを指す。実際のAI作画、Gemini SDKへの接続、実アカウントでの検証は後続I15に分け、v1の開始や完成を妨げない。画面ヘッダーに「模擬API」を常時表示し、候補と書き出しmanifestにもprovider=mockを記録する。模擬画像を実際のAI生成結果として扱わない。

骨子から具体化した選択として、生成処理はプロバイダーの非同期バッチに統一する。UIを閉じてもワーカーが動いていれば進む。マシン停止中も送信済みの処理はプロバイダー側で進み、再起動時に回収する。結果はチャンク完了後に表示し、1ページずつ即時に返ることは要求しない。再生成も同方式で待ち時間を伴う。

2026年10月18日に確認した[公式Batch API](https://ai.google.dev/gemini-api/docs/batch-api)は通常実行の50%の料金、完了目標24時間を示している。この条件はI15でGeminiを有効にした後の生成開始要約にのみ表示する。v1モック版は「模擬API・課金なし・実際のAI作画は行わない」を表示する。将来同期モードを足すための汎用エンジンは作らない。

## 2 構成と実行契約

```text
ブラウザ → FastAPI/Jinja → application services → SQLiteとDATA_DIR
                                        ↑
                                worker tick loop
                                        ↓
                               模擬Batch API（将来Geminiへ交換）
```

実装パッケージはsrc/manga_drawing、テストはtests、CSSとJSはstatic、HTMLはtemplatesに置く。モジュールはconfig、db、assets、projects、prompts、planning、worker、review、export、web、providers/gemini、providers/mockに分ける。providers/geminiはv1ではProviderNotConfiguredを返す接続口のみとし、SDKの推測実装をしない。google-genaiの依存追加と固定はI15で行う。モジュール間でアプリケーションサービスを呼び、HTTPハンドラーやプロバイダーから直接審査状態を書き換えない。

起動コマンドを以下で提供する。

```sh
uv sync --locked
uv run manga init
uv run manga web
uv run manga worker
uv run manga doctor
uv run manga backup --output /absolute/path/backup
```

webは127.0.0.1:8765、単一ASGIプロセス。workerは同じDATA_DIRを使い、OSのファイルロックで多重起動を拒否する。SQLiteは各処理で接続しforeign_keys=ON、busy_timeout=5000、journal_mode=WAL。ネットワークI/O中にDBトランザクションを保持しない。データフォルダーはネットワークドライブとクラウド同期フォルダーを避け、初期値はユーザーのローカルアプリデータ領域とする。ロックファイルを削除して強制起動する機能は設けない。

doctorはDBマイグレーション、保存先の書き込み、ワーカーheartbeat、設定、秘密情報の存在を検査する。通常は外部APIを呼ばない。ワーカー未起動はUI上部に表示する。ワーカーは独立した小さなheartbeatスレッドで5秒ごとに時刻を更新し、ネットワーク取得中も継続する。30秒以上経過したら停止中と表示する。業務処理の並列ワーカーは増やさない。

## 3 データと不変条件

IDはUUID、時刻はUTCのISO 8601、金額はUSDの整数microUSDとする。DB変更は番号付きSQL migrationで管理し、initと起動時に適用する。ダウングレードは対象外、更新前バックアップを提供する。

| テーブル | 必須フィールドと制約 |
| --- | --- |
| projects | id、title、settings_json、version、created_at |
| assets | sha256主キー、relative_path、mime、bytes、width、height。ファイル内容は不変 |
| references | id、project_id、kind(character/scene)、stable_key、current_revision_id。作品内kindとstable_keyはunique |
| reference_revisions | id、reference_id、metadata_json、asset_hashes_json、created_at。不変 |
| pages | id、project_id、number、draft_json、version、latest_round_id、accepted_candidate_id。作品内numberはuniqueで正整数 |
| rounds | id、page_id、sequence、input_hash、snapshot_json、prompt_text、review_state(pending/accepted/rejected)、rejection_reason、created_at。ページ内sequenceはunique。不変入力と審査状態を分離 |
| batches | id、project_id、kind(initial/revision/extra)、idempotency_key、payload_hash、plan_json、cap_micro、unit_reserve_micro、spent_reserved_micro、paused、created_at。作品内keyはunique |
| jobs | id、batch_id、round_id、candidate_index、state、active_attempt_id、retry_after。roundとcandidate_indexはunique |
| chunks | id、batch_id、attempt_no、state、display_key、payload_path、payload_hash、remote_name、input_remote_name、output_path、next_poll_at、last_error。display_keyはunique |
| attempts | id、job_id、chunk_id、request_key、state、send_reserved_micro、error_code、usage_json、created_at。request_keyはunique |
| candidates | id、job_id、attempt_id、asset_hash、thumbnail_hash、raw_asset_hash、availability、created_at。job_idはunique |
| review_events | id、page_id、round_id、candidate_id(nullable)、decision、reason、created_at。追記のみ |
| exports | id、project_id、manifest_json、archive_path、state、created_at |
| mutation_receipts | scope、idempotency_key、payload_hash、response_json、created_at。scopeとkeyはunique。成功した手動retryとresolve等の冪等応答を保存 |
| audit_events | id、project_id、action、target_id、before_json、after_json、created_at。予算増枠と明示再送を追記 |

snapshot_jsonには資料の版と内容ハッシュ、ページ演出、修正、出力設定、モデルプロファイルの版、テンプレートの版、画像の送信順を含める。metadataはJSONのスキーマをPydanticで定義し、未定義フィールドを拒否する。

必須の不変条件は以下とする。

- 採用候補は同じページの保存完了した候補に限る。採用画像は自動で更新しない。
- 同じページには非終端ジョブを含む生成ラウンドを同時に2つ作らない。409を返し、重複課金を防ぐ。
- 一度キュー登録したラウンドの入力・プロンプト・設定を更新しない。編集はdraftだけに反映する。
- 古いラウンドの完了・審査要求が新しいラウンドを暗黙に置き換えない。
- 保存済み成功ジョブを再送しない。追加生成は明示操作により新ラウンドと新ジョブを作る。
- 結果到着とユーザーの合格は別の出来事である。画像の見た目を自動採点しない。

referencesの更新はexpected_current_revision_idでCASする。ページdraft、共通設定、資料、審査、ページ順の変更時はprojects.versionも同じtransactionで増やす。生成結果や進捗の更新ではprojects.versionを増やさず、一覧のprogress versionを別に返す。exportはproject versionを検査した同じtransactionで採用一覧を固定する。

SQLiteのFKに加え、同一ページ等の複合条件を同じBEGIN IMMEDIATEトランザクションで検証する。pages.versionによる楽観ロックはdraft保存、審査、生成登録すべてに使う。比較後の更新行数0は409とする。

## 4 入力登録と大量ページの取り込み

ブラウザのフォルダー選択または複数ファイル選択で相対パス付きファイルを送る。サーバー側で任意のローカルパスを読み込むAPIは作らない。大量登録はmanifest.jsonを添えたフォルダーを標準とする。ZIPの入力展開はv1対象外とし、展開攻撃と依存を減らす。

```json
{
  "schema_version": 1,
  "title": "作品例",
  "characters": [{"key":"tohma","name":"Tohma","height_cm":175,"images":["characters/tohma.png"]}],
  "scenes": [{"key":"classroom","description":"昼の教室。夏服。","images":[]}],
  "pages": [{"number":1,"name":"pages/001/name.png","instructions":"pages/001/instructions.png","characters":["tohma"],"scene":"classroom","directions":[{"marker":"①","text":"Tohmaを見上げる構図。"}]}]
}
```

ファイルごとに最大20MiB、総アップロード1GiB、画像最大40メガピクセル、ページ最大500。画像はPNG/JPEGのみ。実体をPillowで開いて検証し、拡張子だけで判断しない。EXIF orientationを反映した送信用画像を別assetとして作り、元ファイルは保存する。パスは正規化して相対パスのみ許可し、絶対パス、..、重複パス、シンボリックリンク相当を拒否する。

アップロードは1ファイルずつストリーミングし、ローカルのstaging領域へ保存する。import tokenでmanifestと紐付ける。検証プレビューで不足と警告をページごとに返し、確定時に登録する。重複内容はSHA-256で再利用する。manifestを修正して再登録する場合はページ番号でdraft更新し、versionの不一致を409とする。過去のラウンドや採用は維持する。

登録後は画面から人物資料、演出、ページ画像を変更できる。登録の失敗で既存作品の一部だけを更新しない。staging tokenは24時間で失効し、参照されないstagingファイルのみ掃除する。作品や生成履歴の自動削除はしない。

## 5 プロンプトの構築

追加のLLM呼び出しは使わず、固定テンプレートから決定的に構築する。送信順は共通ルール→人物とシーンの説明→番号順の演出→修正指示→添付一覧→ネーム→指示書→stable_key順の人物資料→シーン資料。画像の直前に役割と人物名をテキストで付す。

共通ルールには枠線・吹き出しの保持、視線誘導、人物の造形と身長、背景の指定、注釈を描かないこと、モノクロ下書きを含める。セリフ本文の自動抽出・OCRはしない。番号はテキスト上の重複を検証するが、指示書画像との対応を自動認識したと主張しない。

修正指示は「今回の修正」として末尾に1回だけ挿入する。draftの修正欄は前回の修正欄を初期値として編集する累積メモとし、履歴を自動で連結しない。却下理由は記録専用で、明示的に修正欄へコピーした場合のみ送信する。

全文編集モードは構築済み全文を初期値にし、保存した全文が送信テキストの全体になる。二重にテンプレートを追加しない。画像とAPI設定は全文編集でも変わらない。通常編集へ戻す操作では、全文上書きを解除する旨を表示する。双方を同時に適用しない。全文上書き中に演出や人物説明を変更しても送信テキストには反映しないため、UIに警告する。画像の差し替えは反映されるので、送信プレビューではテキストと添付一覧を合わせて表示する。

input_hashはcanonical JSON（UTF-8、キー順序固定、不要空白なし）のSHA-256。プロンプト全文、添付の順と内容ハッシュ、モデルプロファイル、出力設定を含める。UI上の選択状態、審査理由、時刻、候補番号は含めない。候補番号が違っても生成ジョブは別であり、ハッシュ一致を理由に候補を統合しない。

## 6 計画とコスト制御

操作をplan→startに分ける。planは対象ページ、使用するversion、固定入力、候補数、送信数、入力不足、コスト枠を返す。通常UIでは「対象120ページ・各1候補・最大120件・非同期」を生成開始の直前に示す。これが通常の生成操作であり、追加の承認画面を重ねない。

startはplan token（15分有効）、Idempotency-Key、request payload hashを受け取り、対象のversionを全件再検証して1トランザクションでラウンド・ジョブ・バッチを登録する。1ページでも版が変わっていたら全件409、再planとする。未準備ページはplanで除外理由と一覧を表示し、その確定済み対象だけをstartへ渡す。

バッチと成功操作のkeyは履歴と同じ期間保持し、自動で期限切れにしない。plan tokenは署名済みで固定入力とversionを指し、失効時は新しいplanを作る。すでに成功したkeyの照合はtoken失効やversion検査より先に行う。

同じkeyと同じpayloadの再要求は同じbatch IDを返す。違うpayloadは409。二重クリック、ブラウザ再送、API timeoutでもジョブを増やさない。

コストを抑えるため、候補数default=1、追加の自動品質審査、検索grounding、会話履歴、自動モデル切替を使わない。既存の人物資料を再利用し、送信に必要な人物だけ添付する。

### 6.1 コスト枠の仕様

公式料金を[料金表](https://ai.google.dev/gemini-api/docs/pricing)で確認してプロファイルへ転記し、確認日を保存する。2026年10月18日確認のgemini-3-pro-image Batchでは1K/2K画像出力は1枚0.067 USDで、入力とテキスト等は別課金である。100ページ1候補の画像出力部分は6.70 USD、3候補は20.10 USDとなる。これは総額や上限の見積もりではない。

v1の実装は厳密な課金額上限を保証しない。ユーザー設定の「1送信あたりの予算枠 U」と「この実行の予算枠 B」で、再試行を含む送信件数の上限を保証する。予算枠は請求額と区別して表示する。Uは入力画像数と出力設定を踏まえた保守的な値を初期設定時に入力し、未設定なら実APIの開始を無効にする。価格の自動取得や正確に見える推定請求額は実装しない。

送信前にチャンク全件分のattemptを単一BEGIN IMMEDIATEで予約し、累積予約額+チャンク要求数×U>Bならチャンク全体を未送信で待機させる。部分予約や暗黙の再分割はしない。予約は成功、失敗、送信後不明のいずれでも戻さない。明らかにネットワーク送信前に失敗したattemptだけ解除できる。送信枠上限floor(B/U)と残り枠を表示する。usageは取得できた範囲で記録し、取得不能を0円としない。

初期バッチのBの標準値は対象ジョブ数×U。自動再試行用の追加枠は詳細設定から指定できる。新規送信枠を使い切ったら自動で支出を増やさず「送信枠不足」を表示し、ユーザーがBを増やすか停止する。追加枠の変更は監査ログに残す。

### 6.2 対象選択

initialはラウンドのない未生成ページ、revisionは最新ラウンドが却下のページ、extraはユーザーが明示選択したページが対象。失敗ジョブの再試行は新バッチやラウンドを作らず、元のbatch/jobへ新attemptを追加し、元バッチの予算枠を使う。再試行では不変入力を再利用し、修正を反映したい場合はページを却下してrevisionとして新ラウンドを作る。合格済みページはrevisionに含めない。extraで合格ページを選んでも採用候補を保ち、表示は「採用済み・追加生成中」とする。

## 7 非同期バッチ処理

### 7.1 プロバイダーの境界

```text
validate(profile, snapshots) -> validation report
build_jsonl(attempts) -> immutable file + hash
upload(file) -> remote input name
submit(input_name, model, display_key) -> remote batch name
find(display_key) -> zero / one / multiple matches
poll(remote_name) -> normalized state + result file reference
fetch(result_reference, local_temp_path) -> downloaded file
parse_line(line) -> request_key + image/text/usage/error
```

以下のGemini固有契約はI15で実装・検証する将来要件であり、v1の完成条件ではない。v1では同じローカルprotocolを模擬APIで満たす。

公式SDKでgenerateContent形式のJSONLを作り、request keyをattempt IDとする。送信はgoogle-genaiのbatches.create、状態取得はbatches.get、照合はbatches.list、ファイル操作はfilesで実装する。SDKの実引数と結果schemaはI15で固定し、依存をuv.lockに記録する。SDKの送信系の暗黙リトライを無効化し、アプリ側に一本化する。照合の検索範囲はページネーションですべて追い、display_keyの完全一致のみ使う。

モデルプロファイルは生成対応、Batch対応、画像参照上限、文字と画像を含む要求の最大bytes、設定名を持つ。初期値は総画像最大14、人物資料最大5、生成は2K/3:4。アプリ側の保守的な要求上限はJSONLの1行20MiB、文字20000字、各人物資料1枚。実際のSDK/APIとの適合をI15で検証し、超えるページは送信前にエラーとする。勝手な人物資料の省略や縮小はしない。[画像生成の公式仕様](https://ai.google.dev/gemini-api/docs/image-generation)を根拠としてプロファイルを管理する。

### 7.2 模擬APIとプレースホルダー

v1のprovider既定値はmock。模擬APIはupload、submit、find、poll、fetch、parseを実装し、外部処理を表す状態をworkerのメモリとは別のローカルSQLiteまたはファイルに永続化する。worker停止後もfindとpollで同じ外部IDを回収できる。実ネットワークやAPIキーは使わない。模擬外部状態はDATA_DIR/mock-provider/state.sqlite3、入力と結果は同配下のinputs/outputsへ置く。scenarioとready_atを外部処理レコードへ固定保存し、実時刻から完了可能か判断する。通常モックは2秒後に完了し、workerは1秒ごとにpollする。テスト時の仮想時計とその必要な状態も同配下で管理する。

模擬結果はページ番号、候補番号、attempt IDから識別できるPNGとし、画像decode・サムネイル・審査・書き出しを省略せず通す。作画の再現性を保証する画像ではなく、状態と入力条件を検証する試験画像である。scenario設定により成功、遅延、429、拒否、submit受理後timeout、混在成否、重複/不足key、画像破損を再現する。テストでは仮想時計を注入し、24時間の実待機を不要にする。

モック版でも入力の固定、プロンプト全文、hash、ジョブ、予算予約、復旧の本体コードは本番予定のものを使う。U=1 microUSD相当の試験単位、B=予定ジョブ数を無課金の初期値として内部的に使用し、通常UIには「模擬API・課金なし・残り送信件数」と表示する。金額を実費として表示しない。T08では任意の正整数U/Bを注入し境界を試す。

実APIが選択された場合はplan/startより前の構成検査でprovider_not_configuredを返し、ジョブ登録、予算予約、SDK起動、外部通信を一切行わない。UIの実API選択は無効化して「後続対応」と表示する。存在しないAPIキーやダミーURLへ送信する実装はしない。

### 7.3 チャンクとワーカー

1チャンクは最大1000要求かつ1GiBのJSONLで分割する。JSONLを逐次書き込み、ファイル全体をメモリに展開しない。上限を超える1要求は登録不可。最大同時外部バッチは8。将来のGeminiはpoll間隔60秒、外部送信開始は最低5秒間隔。通常モックはpoll間隔1秒、submit間隔0秒とする。すべて設定値とし、UIでは詳細設定に置く。標準1候補の数百ページを可能な限り1〜少数の外部バッチへまとめ、20要求などの小分けは障害試験のみに使う。planには実際の分割数とceil(分割数/同時外部バッチ数)の投入波数を表示する。24時間は1外部バッチの目標であり、作品全体の完了保証ではない。実アカウントのキュー上限に当たった場合は既知の拒否として待機し、設定値を勝手に増やさない。I15でSDKと利用枠を検証し、アカウント側がこれより低い場合は根拠付きでプロファイルの上限を引き下げる。

ローカルワーカーは1つで十分とし、分散ロックやワーカープールは作らない。

ワーカーは各tickで既存バッチの照会、結果回収、未送信チャンクの提出をそれぞれ進める。プロバイダーI/Oはタイムアウトを設定し、全チャンクを巨大なメモリ上のリストにしない。完了チャンクの候補は取り込み直後から審査できる。停止・再起動してもremote_nameがある処理は照会から再開する。

### 7.4 状態と復旧

job状態はqueued / running / retry_wait / succeeded / failed / unknown。chunk状態はprepared / uploading / submitting / submitted / fetching / ingested / failed / unknown。成功と失敗は各要求の結果で判断し、外部バッチ全体の成功だけで全要求を成功にしない。

外部バッチの受理後は対応jobをrunning、attemptをsubmittedにする。

attempt状態はreserved / submitting / submitted / succeeded / retryable_failed / terminal_failed / unknown / abandonedとする。succeeded、retryable_failed、terminal_failed、abandonedは終端、unknownは解決待ちである。retryable_failedからの再試行は新attemptを作り、旧attemptは更新しない。

| 要求の結果とjobの状態 | 再実行の規則 |
| --- | --- |
| 受理前であることが確実な失敗、明示429 | job=retry_wait、attempt=retryable_failed。枠と上限内だけ自動で新attempt |
| 画像なし、拒否、入力不正 | job=failed、attempt=terminal_failed。自動再試行なし。手動再試行または修正して新round |
| 生成後の画像破損、結果のキー異常 | まず同じ結果の再取得。キー異常はunknown、破損の確定はfailed。自動で課金生成しない |
| 成否不明 | job=unknown、attempt=unknown。resolve経由の明示再送のみ |
| 画像を正常保存 | job=succeeded、attempt=succeeded。通常の再試行対象外 |
| 成功後にローカル画像を喪失 | 採用候補を保持してunavailableと表示。再取得不能ならjob=failed、候補は利用不可。明示の再生成のみ |

手動再試行はfailedの同一jobに対しPOST retryで新attemptを追加する。retryとresolveの成功応答はmutation_receiptsに状態更新と同じtransactionで保存し、同じscope/key/payloadならその応答を返す。異なるpayloadは409とする。unknownはresolve経由、succeededはextra経由とし入口を分ける。全attempt数の自動上限3を超える手動再試行は追加送信を明示し、手動上限を1回ずつ増やす。取り込んだ候補を失ったjobは同じjobで再生成せずextraへ案内する。candidateの利用可否はavailability=available/unavailableで保持する。

外部チャンクがfailedになっても、各jobを機械的にretry_waitへ落とさない。結果の回収後に要求ごとの確定状態で上表に分類する。出力downloadはAPIで再取得可能な期限内に最大3回（30/60/120秒待ち）を自動試行し、回復しなければchunk=failedとして「結果の再取得」を提示する。この再取得は同じremote_nameに対して行い、生成を伴わない。期限切れで成否を確定できない要求はunknownとする。

| 障害点 | 必須動作 |
| --- | --- |
| ファイル生成中の停止 | preparedを再構築。外部生成はまだない |
| アップロードの失敗や不明 | 重複アップロードは生成課金と区別。既知名を再利用し、未知の一時ファイルは後片付け対象に記録 |
| submit呼び出し前 | submittingとdisplay_key、予算予約を先に永続化 |
| submit中の通信切断またはローカル停止 | unknownにし、同じdisplay_keyを照合。直ちに再提出しない |
| 照合結果が1件 | remote_nameを保存して照会を継続 |
| 照合0件または複数件 | 不明を維持。0件は未送信の証明にならない。60秒間隔で照会し、10分後は手動解決を表示 |
| pollやdownloadの通信失敗 | 同じremote_nameと結果を再取得。新しい生成はしない |
| 既知の要求単位エラー | 下記分類で再試行または失敗確定 |
| 出力取得後の停止 | ローカル結果ファイルを再解析して重複なく取り込む |
| 外部バッチの失敗や期限切れ | 取得可能な結果を先に回収。成功が確認できた要求は再送しない。要求の成否不明分はunknown |

unknownには「外部処理を再照合」「外部IDを指定して照合」「重複生成の可能性を確認して再送」を詳細領域で提供する。外部IDを指定した場合は存在とモデルとdisplay_keyを検証する。再送を選んだ場合は新attemptと新しい予算予約を作り、元attemptをabandonedとして記録する。後から元の結果が見つかっても現在の採用を変えず、診断履歴に隔離する。

要求の明示的な429はRetry-Afterを優先し、なければ30秒×2^(再試行回数)＋0〜10秒のjitter。ネットワーク送信前の接続失敗も同じ。試行上限は初回を含め3回。5xxやtimeoutなど受理の可能性がある送信エラーはunknownとし、自動再送しない。認証・課金設定・モデル不一致はバッチをpauseしてエラー表示。ポリシー拒否、入力不正、画像なしは要求単位でfailed。自動再試行は予算枠がある場合だけ行う。

pauseは新規submitと再試行を止め、送信済み結果のpollと取得は続ける。v1では外部バッチのキャンセルUIを作らず、pauseの説明を「新しい送信を停止」とする。

## 8 画像保存と整合性

DATA_DIRにはdb.sqlite3、assets/sha256、chunks、exports、staging、logsを置く。元画像、生成画像の原本、表示と書き出し用画像を分ける。候補の表示画像はPillowでグレースケールPNGに変換し、サムネイルは長辺320pxで作る。ネームや指示書の色は入力の意味を持つため、送信時にグレースケール化しない。

raw responseは秘密情報を除いたローカル結果ファイルとして保存する。結果JSONLは2段階で逐次解析する。第1走査で全行のrequest key、行位置、出現数をローカル索引に記録し、未知キー、重複キー、不足キーを検出する。この時点で候補を成功登録しない。第2走査でキーが一意に対応する要求だけを取り込む。未知キーの行は隔離し、既知の正常行の回収は続ける。重複キーと不足キーのjobはunknownとして、結果再取得で解決しなければ手動対応とする。再取得時も同じ2段階を行い、既存の成功候補は重複登録しない。JSON構文破損は行単位で隔離し、対応不明の要求は不足キーとして扱う。1要求1最終画像を契約とし、複数最終画像はprotocol errorとしてfailedにし、同じ結果の再取得で解決しなければ手動対応とする。thinking画像は候補にしない。画像なしのテキスト応答はfailedとし理由を表示する。

保存順序はtemp書き込み→画像decodeと寸法検証→fsync→同一ファイルシステム内rename→candidate登録とjob成功のDB commit。manifest sidecarにattempt IDとsha256を保存する。起動時に整合検査し、DB未登録の完成ファイルは同じattemptなら取り込み直す。欠損画像のjobを成功のまま表示しない。回収可能なら外部結果を再取得し、不能なら候補をunavailable、jobをfailedとして手動対応を示す。採用IDは消さず、書き出しを止める。課金生成を自動でやり直さない。

サイズ調整は書き出し時だけ行う。標準は1410×1920px、scale=min(目標幅/元幅,目標高さ/元高さ)で縦横比を保持し、LANCZOSでリサイズ後、白い余白を中央配置する。切り取りと縦横別倍率の変形は禁止。原本は変更しない。

## 9 審査と表示状態

採用操作はcandidate ID、expected page version、expected latest round IDを受ける。現在ラウンドの候補は即時採用できる。過去候補の採用はrestore=trueを明示し、最新ラウンドを置き換えずaccepted_candidate_idのみ更新する。進行中の新ラウンドがあっても過去候補の採用は可能で、後着結果は採用を変えない。

却下操作はexpected latest round IDを必須とし、最新ラウンドに対してのみ実行する。別のラウンドの候補は却下対象にできない。採用を持つページで却下する場合は、その採用を解除する旨を表示し、明示操作で解除してから要修正にする。採用なしの却下では解除は不要。

表示の主状態は以下の順で決める。副状態として生成進捗とエラー件数を添える。

1. accepted_candidate_idがあれば「採用済み」。追加生成や失敗があっても保持する。
2. 最新roundがrejectedなら「要修正」。進行中は「再生成中」を併記する。
3. 最新roundに利用可能な候補があれば「確認待ち」。残り候補の生成中も審査できる。
4. queued、running、retry_waitのジョブがあれば「生成中」。unknownもあれば「結果確認が必要」を併記する。
5. 上記に該当せずunknownがあれば「結果確認が必要」、全ジョブがfailedで候補がなければ「エラー」。それぞれ解決操作または再試行を示す。
6. ラウンドがなければ「未生成」。

新ラウンド登録時にlatest_round_idを変更し、そのroundのreview_stateはpendingとする。旧roundの却下は履歴として残る。新roundに候補がまだなくても、過去候補は詳細から閲覧できる。

## 10 HTTP契約とUI

同一オリジンのHTMLとJSON APIを提供する。JSONのエラーは{error:{code,message,fields,retryable}}、成功は{data:...}。作成は201、開始済みは200、非同期開始は202、入力不正は422、競合は409、容量超過は413。versionはレスポンスに含める。

| API | 入力と動作 |
| --- | --- |
| POST /api/imports | staging tokenを作成 |
| PUT /api/imports/{token}/files | multipartのpathとfile。1ファイルずつ保存 |
| POST /api/imports/{token}/validate | manifestと更新対象project ID。不足と計画を返す |
| POST /api/imports/{token}/commit | 検証済みimport plan token、expected project version。作品とdraftを登録 |
| GET /api/projects/{id}/pages | status、cursor、limit（default40/max100）。page number順、サムネイルだけ返す |
| GET /api/pages/{id} | draft、version、最新round、候補、採用、進捗。履歴は別ページング |
| PATCH /api/projects/{id} | expected_versionと共通設定。新しい版だけ更新 |
| PATCH /api/references/{id} | expected revisionと人物またはシーン設定、登録済asset参照。新しい資料版を作る |
| PATCH /api/pages/{id}/draft | expected_versionと演出、修正、資料参照、全文上書き。版を増やす |
| POST /api/projects/{id}/plans | kind、page IDsまたはfilter、candidate count、設定。固定入力とplan token |
| POST /api/projects/{id}/batches | plan tokenとIdempotency-Key。生成登録 |
| POST /api/pages/{id}/revise-and-generate | expected_version、latest round、draft patch、reason、candidate count、予算設定、clear_accepted、Idempotency-Key。修正と却下と新生成登録を原子的確定 |
| POST /api/jobs/{id}/retry | expected state、Idempotency-Key、explicit_extra_attempt。元batchの予算枠で新attemptを待機登録 |
| POST /api/pages/{id}/review | expected_version、latest round、accept/reject、candidate、reason、restore |
| POST /api/batches/{id}/pause | true/false。新規送信の停止と再開 |
| PATCH /api/batches/{id}/budget | expected budget、new cap。増枠を記録 |
| POST /api/chunks/{id}/resolve | reconcile/attach/resend/refetch、expected state、Idempotency-Key。resendにはexplicit_duplicate_risk=true |
| GET /api/projects/{id}/progress | 件数、各batch、heartbeat、更新version。送信プロンプト全文を毎回返さない |
| POST /api/projects/{id}/exports | mode=complete/accepted_only、expected project version。採用スナップショットからZIPを作る |
| GET /api/exports/{id} | stateと同一オリジンのdownload URL |
| GET /api/assets/{hash} | DBにあるassetのみ。任意パスは受けない |

参照資料の新規追加はimport経由、画像の差し替えも小さなimportを使う。APIを増やすより既存の検証と保存を再利用する。エンドポイントの具体例とschemaはFastAPIのOpenAPIにも生成する。

### 10.1 三つの画面

作品設定ではフォルダー登録、入力不足、候補数、生成開始を中心に表示する。モデル、予算枠、チャンク設定は詳細設定。生成開始の近くに対象件数と非即時実行の説明を置く。

一覧は状態フィルター、ページ順のサムネイル、選択、要修正ページの一括再生成、書き出し。スクロールに応じ40ページずつ追加する。チェックボックスの全選択は「表示分」と「フィルター該当全件」を区別し、planに確定ID一覧を持たせる。

詳細はネームと候補の比較、指示書の切替、候補の拡大、採用、修正入力を配置する。「却下して保存」と「修正して再生成」を用意し、後者は専用POSTで、draft保存・旧ラウンド却下・入力固定・新round/job/batch登録を1つのBEGIN IMMEDIATEで原子的に行う。planと同じ検証関数を使い、外部I/Oを含めない。採用中のページは解除の明示がなければ拒否する。Idempotency-Keyは最初に照合し、成功済みならversion照合より先に既存batchを返す。失敗時は全変更をrollbackして入力を画面に保持し、同じkeyで再実行できる。通常の「却下して保存」は別操作として残す。画面のボタンは実行中に無効化する。

修正の未保存状態は可視化し、ページ移動時に保存か破棄を選べる。5秒pollで進捗領域とサムネイルを更新し、入力欄、スクロール位置、候補の選択を再描画で失わない。非表示タブはpollを止め、復帰時に即取得する。ジョブIDや予算予約の内部用語は通常の画面に出さない。

### 10.2 デザインとアクセシビリティ

[デジタル庁デザインシステムの基本デザイン](https://design.digital.go.jp/dads/foundations/)を参考に、白とグレー、青い主要操作、直線的な配置、角丸4px、1pxの境界線、最小限の影で構成する。本文はsystem-uiと日本語sans-serif、16px、line-height=1.6。余白は4/8/16/24/32pxの尺度を使う。デザインシステム準拠認定を主張しない。

テーマはCSS変数で背景、面、文字、境界、主要操作、フォーカス、状態色を切り替える。system/light/darkの3択をヘッダーの単一コントロールに置き、選択をlocalStorageに保存する。未指定時はprefers-color-scheme。原稿と指示書の画素はテーマで変えない。

文字は通常4.5:1、大きな文字3:1、操作境界とフォーカスは3:1以上を目標とする。状態は文字を必須とし、色のみに依存しない。ボタンと入力はラベルを持ち、すべてキーボードで操作できる。360px幅では比較を縦並びにする。デスクトップ1280px幅で左右比較できる。大きなUIフレームワークや外部フォント配信は使わない。

## 11 書き出しと運用

書き出し開始時に採用候補、ページ順、寸法を固定する。実行中に採用が変わっても内容を混ぜない。completeは全ページ採用が必要で不足時422、accepted_onlyは採用分と不足一覧を明示する。ZIP内はpages/0001.png、manifest.jsonとする。manifestにcandidate ID、round ID、元画像hash、変換方法、欠落ページを記録し、秘密情報を入れない。

ZIPはtempへストリーミングし、完成後に公開する。Web要求はexportをDBに登録して202を返し、ワーカーがチャンク処理の間に実行する。1個のZIPを丸ごとメモリに保持しない。

バックアップはワーカーの新規操作を止め、進行中の取り込みとimport commitを待ち、アプリ全体の書き込みロック下でSQLite backup APIと参照ファイルをコピーする。mock-providerのDB、inputs、outputs、scenario、時計状態もバックアップ対象に含める。模擬APIの状態更新も同じアプリ書き込みロックを取得し、ロック順はアプリ書き込みロック→mock DB transactionで統一する。バックアップ中はこのロック下で本体DBとmock DBをそれぞれbackup APIで取得し、参照ファイルをコピーする。ローカル模擬APIは別プロセスで勝手に更新せず、poll時に保存済みready_atから結果を確定する。ネットワーク結果を待って無期限に止めず、バックアップ中は外部結果を次tickまで保留する。復元はWebとワーカーを停止して実施し、doctorで整合を確認する。DB単独のコピーを完全バックアップと呼ばない。

localhost専用でもHostとOriginを検証し、書き込みには起動時に発行したセッションとCSRF tokenを使う。外部オリジンのCORS許可はしない。アップロード名をHTMLとして出さず、原稿テキストはエスケープする。DATA_DIRのログにAPIキー、Authorization、base64画像、プロンプト全文を出さない。診断にはIDとエラー分類だけを使い、詳細資料はローカル保存から参照する。

## 12 検証と完成の判定

料金の発生しないmockをv1の唯一の動作プロバイダーおよび通常のテストとCIの標準とする。ライブAPI検証は明示的なliveフラグ、APIキー、送信予算枠がある場合だけ実行する。CIが勝手に有料生成しない。

| ID | 必須シナリオと合格基準 |
| --- | --- |
| T01 | 100ページ×3候補、300ページ×1候補、300ページ×3候補をmockで生成し、それぞれ300/300/900件で欠損・重複なし |
| T02 | 同じstartを並行再送してbatchとjobが増えない。異なるpayloadを同じkeyで送ると409 |
| T03 | submit直前、受理直後、結果download後、rename後、DB commit前で停止し、再起動後に成功済みの再生成と二重取り込みがない |
| T04 | unknownの照合が0/1/複数のケースで規定通り。mockの呼出し履歴で受理後timeoutの自動再submitがない。SDKの暗黙再送検査はI15へ分離 |
| T05 | 100ページのうち7ページを却下して修正し、再生成対象がその7ページのみ。採用93ページのIDと画像hashが不変 |
| T06 | 全文編集、累積修正、資料差替えの送信内容とinput_hashが正しい。実行中のdraft編集が送信済み条件を変えない |
| T07 | 旧結果の後着、過去候補の採用、審査のversion競合で最新採用を壊さない |
| T08 | 初回と再試行の予約合計がBを超えず、unknownも予約保持。予算不足後もpollと回収は進む |
| T09 | 画像破損、文字のみ、未知/重複/不足key、複数画像、混在成否を成功扱いしない |
| T10 | 両テーマ、360/1280px幅、キーボード、未保存修正、poll中の入力保持をブラウザで確認 |
| T11 | 一括importのパス逸脱・容量超過・同番号重複を拒否。書き出しはページ順と寸法が正しく原本を保持 |
| T12 | 別の空DATA_DIRへバックアップ復元し、採用と入力と結果hashが一致、ワーカー再開可能 |
| T13 | 300ページ一覧の最初の応答は40件、サムネイル以外を先読みしない。mock負荷試験でRSS 512MiB以下を目標に測定環境と結果を記録 |
| T14 | liveで最小2ページを送信し、複数参照の伝達とBatch結果の取り込みを確認。1ページを却下し修正再生成して、もう1ページの採用が不変。SDK transport spyで隠れたsubmit再送がない |

v1モック版はT01〜T13およびI01〜I14の完了条件を満たして完成とする。T14のlive検証は後続I15の条件であり、未実行でもv1モック版は完成と判定できる。完了報告は「モック版完成・実API未接続」と表記する。実API対応版の完成はI15とT14の成功後に別途判定する。生成品質の採否は作者が行い、テスト通過を作画品質の保証としない。


## I15追補：未受理確定時の要求枠返却（2026年10月18日）

ユーザー指示により、実APIで未受理と確定した送信前失敗・native createの400/401/403/404/429は、要求数とバッチ予算の予約を返却する。unknown・5xx・timeout・受理後の失敗は保持する。返却は状態確定と同じtransactionでreceipt・監査記録を残して一度だけ行う。過去の試行履歴は削除しない。旧版が保持した明確な拒否も同じ条件で回復する。外部障壁の解消後、元の上限のまま再開できることを受入条件とする。認証・課金エラーでの無限自動再送はしない。
