from __future__ import annotations

import json

from . import assets
from .common import DomainError, canonical, digest, require
from .models import Draft
from .projects import references
from .providers.gemini import MODEL, SUPPORTED_MODELS

TEMPLATE_VERSION = "mono-v2"
RULES = """マンガの清書に使うモノクロの下書きを1ページ生成してください。
ネームの枠線と吹き出しの位置・形を最優先で保持してください。
視線誘導、人物配置、カメラアングル、演出に従ってください。
人物の造形と身長は添付の資料を参照してください。
背景はページの演出に従って補ってください。
指示書の番号・矢印・色文字を出力へ描き込まないでください。"""
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
    draft = Draft.model_validate_json(page["draft_json"]).model_dump()
    refs = references(db, page["project_id"])
    selected = []
    for key in sorted(set(draft["characters"])):
        match = next((r for r in refs if r["kind"] == "character" and r["stable_key"] == key), None)
        require(match, "missing_reference", f"未登録の人物: {key}", 422)
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
            RULES,
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
    total = len(text.encode())
    for item in attachments:
        path = assets.path_for(cfg, db, item["hash"])
        total += (path.stat().st_size + 2) // 3 * 4 + 512
    require(total < 20 * 1024**2, "request_limit", "1ページの送信資料が20MiBを超えます。", 422)
    payload = {
        "prompt": text,
        "attachments": attachments,
        "settings": settings,
        "template_version": TEMPLATE_VERSION,
        "references": [{"id": r["id"], "revision": r["current_revision_id"]} for r in selected],
    }
    return payload, digest(payload), total
