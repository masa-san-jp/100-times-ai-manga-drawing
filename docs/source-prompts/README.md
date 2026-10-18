# 作者のプロンプト原文

## Notion版

出典：[男子全員の責任(再編版)-GenAI](https://app.notion.com/p/GenAI-2f2bb868d86780a38bb4d3cb6de6ea83)。2026-10-18にページを開いて取得しました。

- [文章形式のシステムプロンプト](notion-system-original.txt)
- [構造化されたシステムプロンプト](notion-system-structured-original.yaml)
- [取得元・ハッシュ記録](sources.json)

文言・記号・字下げを維持して保存しています。両形式を統合したり、独自の優先順位を追加していません。個別プロンプトの表示欄は4つとも「- ①」のひな形で、具体的なページ指示はありませんでした。Notion版2種類は資料として取り込んだもので、現時点ではAPIの既定指示へ自動適用していません。

## 記事版

出典：[Googleドキュメント](https://docs.google.com/document/d/1pzfVnZF5nxGzZ95Ugf1lwfbysnz1AsSdXJ-3pvQkFtw/edit)。同じ記事の[公開note版](https://note.com/masa_san_jp/n/n21a5c02f496f)にもプロンプト1が掲載されています。

[ページ1〜6とキャラクター生成の原文](article-originals.json)を収録しています。改行をLFへ統一し、コードブロック前後の空行のみ除去しました。誤字や表記の揺れは修正していません。

| 実装・作例 | 使用する原文 |
| --- | --- |
| 通常の漫画生成 | 記事のプロンプト1 |
| 作例001の1〜6ページ | 記事の対応するプロンプト1〜6をそれぞれ全文指定 |
| 作例001の7・8ページ | 固有の原文が記事にないため、プロンプト1を共通本文として再利用 |
| 通常のキャラクター生成 | 記事の英語のキャラクターデザインシート生成指示 |
| Notion版2種類 | 資料保存のみ。上記の本文と混在させない |

実装用の[漫画本文](../../src/manga_drawing/prompt_templates/comic-original.txt)・[キャラクター本文](../../src/manga_drawing/prompt_templates/character-original.txt)は原文との一致をテストします。作例のmanifestのprompt_overrideはそのままAPIのuser本文へ送信し、追加のシステム指示を重ねません。過去のテイクへ原文を差し込んで履歴を書き換えることもしません。
