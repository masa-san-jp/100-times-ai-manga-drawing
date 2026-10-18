# 開発者向け手順

利用者の起動・制作手順は[利用手順](operations.md)を参照してください。この文書の模擬APIは開発テスト用で、試験画像を返します。

## 依存の導入

```sh
uv sync --locked --extra gemini
```

## 模擬APIを明示して起動する

リポジトリ直下から実行します。各コマンドでAPIと保存先を明示し、利用者の `.env` と実作品の保存先を変更せずに開発します。別のポートを使う場合はWebの `--port` を指定します。

```sh
MANGA_PROVIDER=mock MANGA_ENABLE_LIVE=0 MANGA_DATA_DIR=.data/mock uv run manga init
MANGA_PROVIDER=mock MANGA_ENABLE_LIVE=0 MANGA_DATA_DIR=.data/mock uv run manga web
```

別ターミナルで、同じディレクトリーから実行します。

```sh
MANGA_PROVIDER=mock MANGA_ENABLE_LIVE=0 MANGA_DATA_DIR=.data/mock uv run manga worker
```

[開発画面](http://127.0.0.1:8765)を開きます。通常コマンドはGeminiを選ぶため、模擬APIの開発では上記の指定を省略しません。

## 試験資料

```sh
uv run manga example --output /tmp/manga-example --pages 3
```

このコマンドは試験画像とmanifestを作成します。生成AIによる漫画作画は行いません。作成したフォルダーを開発画面で登録できます。

## 自動検証

```sh
uv run ruff check src tests scripts
uv run pytest -q
uv run playwright install chromium
MANGA_BROWSER_TESTS=1 uv run pytest tests/test_browser.py -q
uv run python scripts/benchmark.py
```

自動テストは一時保存先と模擬API、または注入したGoogle APIの代替クライアントを使います。Gemini実APIへ画像生成を依頼する試験とは分けています。ブラウザーの確認画像は `artifacts/screenshots` に保存します。

## 実APIの仕様・検証記録

- [API契約とモデル](provider-contract.md)
- [Gemini接続の実装・検証経緯](gemini-acceptance.md)
- [過去の少数ページによる実API検証結果](live-acceptance-report.md)
- [キャラクターリファレンス工程の検証](character-reference-acceptance.md)

実API用の起動・設定方法は[利用手順](operations.md#設定場所と確認)に集約しています。これらの過去の記録は、現在の設定や採用状態を変更するための手順ではありません。
