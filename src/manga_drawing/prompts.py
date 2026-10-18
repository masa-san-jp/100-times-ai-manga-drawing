from __future__ import annotations

import json

from . import assets
from .common import DomainError, canonical, digest, require
from .models import Draft
from .projects import references
from .providers.gemini import MODEL, SUPPORTED_MODELS

TEMPLATE_VERSION = "mono-v3-strict"
RULES = """あなたは作者の指定を忠実に作画へ変換する作画担当です。モノクロの漫画下書きを1ページ出力してください。
【指示の優先順位】
共通の作画条件を守り、同じ対象についての指定が矛盾するときは、今回の修正 > このページの文章指示 > 演出の明示指定 > 指示書画像 > ネーム画像の順で適用してください。
変更が明示された対象だけを変更し、指定されていない要素はネームの構成を保持してください。リファレンスは人物の造形・衣装の基準です。ポーズやコマ構成をリファレンスの配置へ置き換えないでください。
【保持するもの】
ネームのコマ数・順序・枠線・各コマの大きさ、吹き出しの位置・形・話者・セリフ、登場人物数・配置・向き・ポーズ・カメラアングルを、変更指示のない限り保持してください。
【全コマを描き直す】
ネームは構成の設計図です。ラフ線・未完成の顔・棒人間・仮置きの輪郭・走り書きをそのままコピーしたり貼り付けたりせず、全コマの人物・衣装・背景を同じ精度の明瞭な線画で描き直してください。
描き直しは構成変更の許可ではありません。指定された構成・表情・動作を維持し、添付の採用リファレンスに人物の顔・髪型・体格・衣装を統一してください。
【無断追加の禁止】
指示またはネームに存在しない集中線・効果線・スピード線・漫符（汗、怒り、驚き、ハート等）・擬音・擬態語・セリフ・吹き出し・人物・コマ・装飾を追加しないでください。「漫画らしさ」「迫力」「見栄え」を理由に演出を補わないでください。
背景はネーム・指示書に描かれた、または明示指定されたものだけを作画してください。空白を勝手な背景やトーンで埋めないでください。
【描画しないもの】
指示書の番号・矢印・色文字・注釈、資料のタイトル・説明ラベル・新しいページ番号・クレジットを描き込まないでください。ネームの作品本文にある文字と作画用注釈を区別してください。
【出力前の照合】
全コマについて、人物数・配置・向き・セリフ・吹き出し・指定演出・描き直しの精度を資料と照合してください。未指定の演出追加やラフの残存があれば修正してから出力してください。"""
SCENARIOS = {
    "success",
    "delay",
    "429",
    "reject",
    "timeout",
    "mixed",
    "duplicate",
    "missing",
    "unknown_key",
    "corrupt",
    "text_only",
    "multiple",
}


def provider_ready(cfg, settings):
    provider = settings.get("provider", cfg.provider)
    if provider != cfg.provider or provider not in ("mock", "gemini"):
        raise DomainError("provider_not_configured", "起動中アプリのAPI設定と一致しません。")
    if provider == "gemini":
        from .providers.gemini import ensure_configured

        ensure_configured(cfg)


def snapshot(cfg, db, page, overrides=None):
    project = db.execute("SELECT settings_json FROM projects WHERE id=?", (page["project_id"],)).fetchone()
    settings = (
        {
            "provider": cfg.provider,
            "model": "mock-batch-v1" if cfg.provider == "mock" else MODEL,
            "width": 1410,
            "height": 1920,
            "scenario": "success",
        }
        | json.loads(project[0])
        | (overrides or {})
    )
    provider_ready(cfg, settings)
    require(
        set(settings) <= {"provider", "model", "width", "height", "scenario"},
        "settings",
        "未定義の生成設定です。",
        422,
    )
    require(
        settings["model"] in ({"mock-batch-v1"} if cfg.provider == "mock" else SUPPORTED_MODELS),
        "model",
        "対応するモデルを指定してください。",
        422,
    )
    require(
        type(settings["width"]) is int
        and type(settings["height"]) is int
        and 1 <= settings["width"] <= 5000
        and 1 <= settings["height"] <= 5000,
        "dimensions",
        "出力寸法は1〜5000pxです。",
        422,
    )
    require(settings["scenario"] in SCENARIOS, "scenario", "不明な試験シナリオです。", 422)
    from .character_references import approved_images, make_snapshot, task_for

    if task_for(db, page["id"]):
        return make_snapshot(cfg, db, page, settings)
    draft = Draft.model_validate_json(page["draft_json"]).model_dump()
    refs = references(db, page["project_id"])
    selected = []
    for key in sorted(set(draft["characters"])):
        match = next((r for r in refs if r["kind"] == "character" and r["stable_key"] == key), None)
        require(match, "missing_reference", f"未登録の人物: {key}", 422)
        from .character_references import selected as selected_reference

        match["selection"] = selected_reference(db, match)
        match["images"] = approved_images(db, match)
        selected.append(match)
    if draft["scene"]:
        match = next((r for r in refs if r["kind"] == "scene" and r["stable_key"] == draft["scene"]), None)
        require(match, "missing_reference", "シーン資料がありません。", 422)
        selected.append(match)
    attachments = [
        {"role": "ネーム", "hash": draft["name_hash"]},
        {"role": "指示書", "hash": draft["instruction_hash"]},
    ]
    description = []
    for ref in selected:
        description.append(canonical(ref["metadata"]))
        attachments.extend({"role": ref["kind"], "key": ref["stable_key"], "hash": h} for h in ref["images"])
    require(len(attachments) <= 14, "image_limit", "添付画像は14枚までです。", 422)
    directions = "\n".join(
        f"{x['marker']} {x['text']}" for x in sorted(draft["directions"], key=lambda d: d["marker"])
    )
    text = "\n\n".join(
        [
            "資料\n" + "\n".join(description),
            "このページの作画指示（番号は指示書画像の番号と対応）\n" + draft["page_prompt"],
            "演出\n" + directions,
            "今回の修正\n" + draft["correction"],
            "添付一覧\n" + canonical(attachments),
        ]
    )
    if draft["prompt_override"] is not None:
        text = draft["prompt_override"]
    require(0 < len(text) <= 20000, "prompt_limit", "送信する全文は1〜20000文字にしてください。", 422)
    total = len(text.encode()) + len(RULES.encode())
    for item in attachments:
        path = assets.path_for(cfg, db, item["hash"])
        total += (path.stat().st_size + 2) // 3 * 4 + 512
    require(total < 20 * 1024**2, "request_limit", "1ページの送信資料が20MiBを超えます。", 422)
    payload = {
        "prompt": text,
        "system_prompt": RULES,
        "attachments": attachments,
        "settings": settings,
        "template_version": TEMPLATE_VERSION,
        "references": [
            {"id": r["id"], "revision": r["current_revision_id"]}
            | (
                {"candidate_id": r["selection"]["candidate_id"], "asset_hash": r["selection"]["asset_hash"]}
                if r.get("selection")
                else {}
            )
            for r in selected
        ],
    }
    return payload, digest(payload), total
