"""Character design sheets, backed by the shared durable batch job engine.

Internal projects isolate reference tasks from comic pages. Source sketches remain
immutable reference revisions; approved generated assets are a separate selection.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata

from . import assets
from .common import atomic_write, canonical, digest, require, timestamp, uid
from .db import connect, get, transaction
from .models import Draft

REFERENCE_RULES = """添付のキャラクタースケッチをもとに、漫画作画用のキャラクターリファレンスシートを1枚作成してください。
白背景・モノクロ。原案の顔立ち、髪型、体格、衣装、特徴を忠実に保ち、別人に変えないでください。
同一人物の全身正面・側面・背面と顔の拡大を、重ならない見やすい配置で描いてください。
設定された身長を守り、全方向で同一の体格・衣装に統一してください。指定のない設定を勝手に追加しないでください。
漫画のコマ・吹き出し・背景演出は不要です。"""


def task_for(db, page_id):
    return db.execute("SELECT * FROM character_reference_tasks WHERE page_id=?", (page_id,)).fetchone()


def selected(db, ref):
    row = db.execute(
        "SELECT * FROM character_reference_selections WHERE reference_id=?", (ref["id"],)
    ).fetchone()
    if not row or row["source_revision_id"] != ref["current_revision_id"]:
        return None
    if row["candidate_id"]:
        candidate = get(db, "candidates", row["candidate_id"])
        if candidate["availability"] != "available":
            return None
    return dict(row)


def approved_images(db, ref):
    selection = selected(db, ref)
    require(
        selection,
        "character_reference_required",
        f"{ref['metadata']['name']}のキャラクターリファレンスを生成して採用してください。",
        422,
    )
    return [selection["asset_hash"]]


def ensure_tasks(db, pid):
    parent = get(db, "projects", pid)
    require(parent["purpose"] == "comic", "project", "作品を指定してください。", 422)
    internal = db.execute(
        "SELECT id FROM projects WHERE parent_project_id=? AND purpose='character_references'", (pid,)
    ).fetchone()
    if not internal:
        ident = uid()
        db.execute(
            "INSERT INTO projects(id,title,created_at,work_id,purpose,parent_project_id) VALUES(?,?,?,?,?,?)",
            (ident, "キャラクターリファレンス", timestamp(), parent["work_id"], "character_references", pid),
        )
    else:
        ident = internal[0]
    # Mirror lifecycle/work membership, so archived works cannot start new tasks.
    db.execute(
        "UPDATE projects SET lifecycle=?,work_id=? WHERE id=?",
        (parent["lifecycle"], parent["work_id"], ident),
    )
    from .projects import references

    for ref in references(db, pid):
        if ref["kind"] != "character":
            continue
        existing = db.execute(
            "SELECT page_id FROM character_reference_tasks WHERE reference_id=?", (ref["id"],)
        ).fetchone()
        if existing:
            continue
        page_id = uid()
        number = db.execute(
            "SELECT COALESCE(MAX(number),0)+1 FROM pages WHERE project_id=?", (ident,)
        ).fetchone()[0]
        draft = Draft(name_hash=ref["images"][0], instruction_hash=ref["images"][0])
        db.execute(
            "INSERT INTO pages(id,project_id,number,draft_json) VALUES(?,?,?,?)",
            (page_id, ident, number, draft.model_dump_json()),
        )
        db.execute("INSERT INTO character_reference_tasks VALUES(?,?)", (ref["id"], page_id))
    return ident


def make_snapshot(cfg, db, page, settings):
    task = task_for(db, page["id"])
    ref = get(db, "reference_sets", task["reference_id"])
    revision = get(db, "reference_revisions", ref["current_revision_id"])
    draft = Draft.model_validate_json(page["draft_json"])
    attachments = [
        {"role": "キャラクタースケッチ", "hash": h} for h in json.loads(revision["asset_hashes_json"])
    ]
    prompt = "\n\n".join(
        [REFERENCE_RULES, "人物設定\n" + revision["metadata_json"], "追加・修正指示\n" + draft.page_prompt]
    )
    require(len(prompt) <= 20000, "prompt_limit", "送信する全文は20000文字までです。", 422)
    payload = {
        "prompt": prompt,
        "attachments": attachments,
        "settings": settings,
        "template_version": "character-reference-v1",
        "task_type": "character_reference",
        "references": [{"id": ref["id"], "revision": ref["current_revision_id"]}],
    }
    total = len(prompt.encode()) + sum(
        (assets.path_for(cfg, db, a["hash"]).stat().st_size + 2) // 3 * 4 + 512 for a in attachments
    )
    require(total < 20 * 1024**2, "request_limit", "送信資料が20MiBを超えています。", 422)
    return payload, digest(payload), total


def list_references(db, pid):
    from .projects import page_detail, references

    items = []
    for ref in references(db, pid):
        if ref["kind"] != "character":
            continue
        task = db.execute(
            "SELECT page_id FROM character_reference_tasks WHERE reference_id=?", (ref["id"],)
        ).fetchone()
        page = page_detail(db, task[0]) if task else None
        selection = selected(db, ref)
        old_selection = db.execute(
            "SELECT source_revision_id FROM character_reference_selections WHERE reference_id=?", (ref["id"],)
        ).fetchone()
        problem = (
            "source_updated"
            if old_selection and old_selection[0] != ref["current_revision_id"]
            else "unavailable"
            if old_selection and not selection
            else None
        )
        items.append(
            {
                "id": ref["id"],
                "name": ref["metadata"]["name"],
                "key": ref["stable_key"],
                "sketch": ref["images"][0],
                "revision": ref["current_revision_id"],
                "selection": selection,
                "selection_problem": problem,
                "page": page,
            }
        )
    return items


def generate(cfg, pid, request, key):
    from . import planning

    with transaction(cfg) as db:
        prior = planning.receipt(db, f"character-generate:{pid}", key, request)
        if prior:
            return prior
        internal = ensure_tasks(db, pid)
        refs = list_references(db, pid)
        wanted = request.get("reference_ids")
        require(
            wanted is None or isinstance(wanted, list) and set(wanted) <= {r["id"] for r in refs},
            "references",
            "人物の指定が不正です。",
            422,
        )
        page_ids = []
        for ref in refs:
            if wanted is not None and ref["id"] not in wanted:
                continue
            # Bulk generate never replaces accepted or waiting-for-review results.
            if wanted is None and (
                ref["selection"]
                or ref["page"]["status"] in ("running", "unknown")
                or (
                    ref["page"]["status"] == "review"
                    and any(
                        c["source_reference_revision"] == ref["revision"]
                        for c in ref["page"]["candidates"]
                        if c["round_id"] == ref["page"]["latest_round_id"]
                    )
                )
            ):
                continue
            page_ids.append(ref["page"]["id"])
        require(page_ids, "empty_plan", "生成対象がありません。人物ごとのボタンから再生成できます。", 422)
        plan = planning.make_plan(cfg, db, internal, {"kind": "extra", "page_ids": page_ids})
        require(not plan["excluded"], "references", "資料を確認してください。", 422)
        result = planning.insert_batch(db, plan, key, digest(request))
        planning.save_receipt(db, f"character-generate:{pid}", key, request, result)
        return result


def review_in(db, page, request):
    """Validate source version and update the separate approved-reference pointer."""
    task = task_for(db, page["id"])
    if not task:
        return
    ref = get(db, "reference_sets", task["reference_id"])
    if request["decision"] == "accept":
        row = db.execute(
            "SELECT c.*,r.snapshot_json FROM candidates c JOIN jobs j ON j.id=c.job_id JOIN rounds r ON r.id=j.round_id WHERE c.id=? AND r.page_id=?",
            (request.get("candidate_id"), page["id"]),
        ).fetchone()
        require(row, "candidate", "この人物の候補を選んでください。", 422)
        source = json.loads(row["snapshot_json"])["references"][0]
        require(
            source["revision"] == ref["current_revision_id"],
            "stale_reference",
            "元資料が更新されています。リファレンスを再生成してください。",
            409,
        )
        db.execute(
            "INSERT OR REPLACE INTO character_reference_selections VALUES(?,?,?,?,?)",
            (ref["id"], source["revision"], row["id"], row["asset_hash"], timestamp()),
        )
    else:
        db.execute("DELETE FROM character_reference_selections WHERE reference_id=?", (ref["id"],))
    db.execute("UPDATE projects SET version=version+1 WHERE id=?", (ref["project_id"],))


def character_folder_key(db, pid, key):
    def safe(value):
        return re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", value).strip(" .")[:60] or "人物"

    name = safe(key)
    normalized = unicodedata.normalize("NFC", name).casefold()
    names = db.execute(
        "SELECT stable_key FROM reference_sets WHERE project_id=? AND kind='character'", (pid,)
    ).fetchall()
    collision = sum(unicodedata.normalize("NFC", safe(r[0])).casefold() == normalized for r in names) > 1
    if name != key or collision:
        name += "-" + digest(key)[:8]
    return name


def sync_files(cfg):
    """Publish every reference take without mixing it with comic page output."""
    with cfg.write_lock(), connect(cfg) as db:
        rows = db.execute("""SELECT rs.id reference_id,rs.stable_key,rs.project_id,rs.current_revision_id,
          c.asset_hash,c.id candidate_id,r.sequence,r.prompt_text,r.review_state,j.candidate_index,
          w.value work_folder,p.title,p.work_id FROM character_reference_tasks t
          JOIN reference_sets rs ON rs.id=t.reference_id JOIN pages tp ON tp.id=t.page_id
          JOIN rounds r ON r.page_id=tp.id JOIN jobs j ON j.round_id=r.id JOIN candidates c ON c.job_id=j.id
          JOIN projects p ON p.id=rs.project_id JOIN system w ON w.key='work_folder:'||p.work_id
          ORDER BY rs.stable_key,r.sequence,j.candidate_index""").fetchall()
        grouped = {}
        for row in rows:
            key = character_folder_key(db, row["project_id"], row["stable_key"])
            # A shared work may contain episode-specific character definitions.
            base = Path(row["work_folder"]) / "キャラクターリファレンス"
            if (
                db.execute(
                    "SELECT COUNT(*) FROM projects WHERE work_id=? AND purpose='comic'", (row["work_id"],)
                ).fetchone()[0]
                > 1
            ):
                from .export import readable_name

                base = base / readable_name(row["title"], row["project_id"])
            target = base / key
            target.mkdir(parents=True, exist_ok=True)
            suffix = f"_候補{row['candidate_index']:02d}" if row["candidate_index"] > 1 else ""
            filename = f"テイク{row['sequence']:02d}{suffix}.png"
            destination = target / filename
            if not destination.exists():
                atomic_write(destination, assets.path_for(cfg, db, row["asset_hash"]).read_bytes())
            prompt_file = target / f"テイク{row['sequence']:02d}_生成指示.txt"
            if not prompt_file.exists():
                atomic_write(prompt_file, row["prompt_text"].encode())
            selection = db.execute(
                "SELECT candidate_id FROM character_reference_selections WHERE reference_id=? AND source_revision_id=?",
                (row["reference_id"], row["current_revision_id"]),
            ).fetchone()
            grouped.setdefault(target, []).append(
                {
                    "file": filename,
                    "take": row["sequence"],
                    "candidate": row["candidate_index"],
                    "status": "採用"
                    if selection and selection[0] == row["candidate_id"]
                    else "要修正"
                    if row["review_state"] == "rejected"
                    else "未採用",
                    "asset_hash": row["asset_hash"],
                }
            )
        for target, items in grouped.items():
            content = canonical(items).encode()
            manifest = target / "候補一覧.json"
            if not manifest.exists() or manifest.read_bytes() != content:
                atomic_write(manifest, content)
