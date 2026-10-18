from __future__ import annotations

import json
from importlib.resources import files

from . import assets
from .common import DomainError, digest, require
from .models import Draft
from .projects import references
from .providers.gemini import MODEL, SUPPORTED_MODELS

TEMPLATE_VERSION = "author-original-v2-page-notes"
RULES = files("manga_drawing").joinpath("prompt_templates/comic-original.txt").read_text().rstrip("\n")
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
    for ref in selected:
        attachments.extend({"role": ref["kind"], "key": ref["stable_key"], "hash": h} for h in ref["images"])
    require(len(attachments) <= 14, "image_limit", "添付画像は14枚までです。", 422)
    directions = "\n".join(
        f"{x['marker']} {x['text']}" for x in sorted(draft["directions"], key=lambda d: d["marker"])
    )
    sections = [draft["base_prompt"] if draft["base_prompt"] is not None else RULES]
    if draft["page_prompt"]:
        sections.append("このページの作画指示\n" + draft["page_prompt"])
    if directions:
        sections.append("演出\n" + directions)
    if draft["correction"]:
        sections.append("今回の修正\n" + draft["correction"])
    text = "\n\n".join(sections)
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
