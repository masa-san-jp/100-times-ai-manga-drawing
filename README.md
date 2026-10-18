# 100 TIMES AI MANGA DRAWING

<img width="604" height="281" alt="スクリーンショット 2026-03-07 9 36 01" src="https://github.com/user-attachments/assets/8454abc8-dc4c-4476-9b1c-2c1e1606af51" />

**[note版を読む：生成AIを使って「マンガを描く」を100倍高速化する](https://note.com/masa_san_jp/n/n21a5c02f496f)**

制作の背景や具体的な制作プロセス、完成原稿と実際に使ったプロンプトを紹介しています。

## Theme

- 生成AIを使って、「マンガを描く」を100倍高速化する

## Overview

- マンガ制作のプロセスを分析、構造化して、生成AIに置き換えることで合理化・高速化できる部分を特定、実際に置き換えることで生産性の向上を実現する。
- 実際に自分のマンガ作品の制作プロセスに適合し、再現可能な体系として示す。

## モック版を起動する

Python 3.12以上とuvを使用します。初回に依存を導入し、Webを起動します。

```sh
uv sync --locked
uv run manga init
uv run manga web
```

別ターミナルで `uv run manga worker` を実行し、[ローカル画面](http://127.0.0.1:8765)を開きます。APIキーは不要です。

`uv run manga example --output /tmp/manga-example --pages 3` でサンプル資料を作成し、画面からフォルダーを登録できます。モック候補は試験画像であり、実際のAI作画ではありません。詳しくは[起動と運用手順](docs/operations.md)を参照してください。Gemini用の設定はリポジトリ直下の`.env`に記入します（[テンプレート](.env.example)、[設定手順](docs/operations.md#設定場所と確認)）。

作品を登録すると、アプリの保存先に `作品/作品名/` を自動作成します。採用画像は「生成結果」、却下画像は「却下した候補」にテイク番号付きで保存します。ページ一覧の「作品フォルダーを開く」から確認でき、保存場所の追加設定やZIPの解凍は不要です。

画像生成の標準モデルは **Gemini Nano Banana 2.1（`gemini-nano-banana-2.1`）** です。Batch APIで処理します。旧モデルで登録済みの生成要求は、そのモデルのまま回収・復旧できます。

## Outline

- 1.完成原稿とプロンプト
- 2.制作の背景
- 3.マンガ制作を100倍高速化する
- 4.どのように100倍高速化を実現するか
- 5.まとめ

## Output

![441C93B0-6DA4-4049-821D-B462B49B07C5](https://github.com/user-attachments/assets/525499b9-a1cd-4f6c-9ce8-3472b03ac426)

- 冊子版「100 TIMES AI MANGA DRAWING」
  - 発行：2026年3月7日
  - 2026年3月7日（土）技研AIマーケット @技研ベース にて頒布
- [note版「100 TIMES AI MANGA DRAWING」を読む](https://note.com/masa_san_jp/n/n21a5c02f496f)
  - 公開：2026年3月7日
 
## 公開作例

[作例001の制作資料](examples/example-001/README.md)：キャラクタースケッチ4枚、ネームと作画指示書各8ページ、およびアプリ登録用の設定を収録しています。

## 仕様と実装計画

モック版のアプリケーションを実装しました。Geminiの実APIでも、2ページの一括生成と1ページだけの修正再生成、採用結果の保持、採用画像のフォルダー出力を検証しました。作画品質の採否は作者が行います。下書きの試験画像でバッチ処理・採否・修正再生成を確認できます。

- [起動・複数作品と連載の管理・保存方法](docs/operations.md)
- [実装結果とIssue別の状態](docs/implementation-status.md)
- [受け入れ結果](docs/acceptance-report.md)
- [Gemini接続の実装・検証経緯](docs/gemini-acceptance.md)
- [実APIによる少数ページの検証結果](docs/live-acceptance-report.md)
- [独立した実装レビュー](docs/implementation-review.md)
- [実装担当者への引き継ぎ](docs/implementation-handoff.md)
- [仕様設計の骨子](docs/software-design.md)
- [実装仕様](docs/implementation-spec.md)
- [Issue駆動の実装計画](docs/implementation-plan.md)
- [独立レビュー記録](docs/design-review.md)

## 用語としての用例

「100 TIMES AI MANGA DRAWING の工程整理を使い、ネームと作画指示を分けて下描きを試す」のように、マンガ制作へ生成AIを組み込むワークフローを指します。「100倍」は本企画のテーマであり、すべての工程・環境に対する性能保証ではありません。

## 技術的背景

このリポジトリには制作方法と原稿草案に加えて、ローカルで動く下書き生成パイプラインと、模擬API・Gemini接続を収録しています。[Claude版草案](docs/claude-draft.md) では、人間のネーム、レイアウト指示、キャラクターデザインシート、プロンプトを組み合わせて画像生成へ渡す方法を説明しています。[Gemini版草案](docs/gemini-draft.md) とあわせ、草案の記述と公開済み成果物を区別して読んでください。

## 歴史的背景と展開

本READMEの Output に記録した通り、2026年3月7日に冊子版を頒布し、note版を公開しています。[READMEの整備履歴](https://github.com/masa-san-jp/100-times-ai-manga-drawing/commit/9adb72e7e1605e063826a6293bb3c042590c5783) は同年3月8日です。ソフトウェアの実装状況は上記のIssue別の実装結果で管理します。
