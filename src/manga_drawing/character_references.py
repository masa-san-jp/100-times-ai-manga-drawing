"""Character design sheets, backed by the shared durable batch job engine.

Internal projects isolate reference tasks from comic pages. Source sketches remain
immutable reference revisions; approved generated assets are a separate selection.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from importlib.resources import files
from pathlib import Path
import re
import unicodedata

from PIL import Image, ImageDraw, ImageFont

from . import assets
from .common import atomic_write, canonical, digest, require, timestamp, uid
from .db import connect, get, transaction
from .models import Draft

REFERENCE_RULES = (
    files("manga_drawing").joinpath("prompt_templates/character-original.txt").read_text().rstrip("\n")
)


def task_for(db, page_id):
    return db.execute("SELECT * FROM character_reference_tasks WHERE page_id=?", (page_id,)).fetchone()


def required_identity(metadata):
    name = metadata.get("name", "").strip()
    height = metadata.get("height_cm")
    require(
        name and height is not None, "character_identity", "人物の名前と身長（cm）を入力してください。", 422
    )
    return name, height


def labelled_image(content, metadata, configured_font):
    name, height = required_identity(metadata)
    identity = [*name.splitlines(), f"{Decimal(str(height)).normalize():f} cm"]
    image = assets.decode(content).convert("RGB")
    paths = [
        configured_font,
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    size = max(20, min(48, round(image.width / 28)))
    for path in [*filter(None, paths), None]:
        try:
            font = ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)
        except OSError:
            continue
        missing = bytes(font.getmask("\U0010ffff"))
        if all(
            bytes(font.getmask(char)) != missing for line in identity for char in line if not char.isspace()
        ):
            break
    else:
        require(
            False,
            "character_font",
            "名前の文字を描画できるフォントを MANGA_REFERENCE_FONT に指定してください。",
            422,
        )
    padding = max(12, size // 2)
    boxes = [font.getbbox(line) for line in identity]
    width = max(image.width, max(right - left for left, _, right, _ in boxes) + 2 * padding)
    heights = [max(size, bottom - top) for _, top, _, bottom in boxes]
    height = 2 * padding + sum(heights) + (len(heights) - 1) * (padding // 2)
    require(
        width * (image.height + height) <= 40_000_000,
        "character_image",
        "記載後の画像が40MPを超えます。",
        422,
    )
    result = Image.new("RGB", (width, image.height + height), "white")
    result.paste(image, ((width - image.width) // 2, 0))
    draw = ImageDraw.Draw(result)
    y = image.height + padding
    for line, (left, top, _, _), line_height in zip(identity, boxes, heights):
        draw.text((padding - left, y - top), line, fill="#b00000", font=font)
        y += line_height + padding // 2
    return assets.png(result)


def label_reference(cfg, db, revision, original_hash):
    prior = db.execute(
        "SELECT asset_hash FROM character_reference_labels WHERE source_revision_id=? AND original_hash=?",
        (revision["id"], original_hash),
    ).fetchone()
    if prior:
        return prior[0]
    content = assets.path_for(cfg, db, original_hash).read_bytes()
    labelled = assets.put(
        cfg, db, labelled_image(content, json.loads(revision["metadata_json"]), cfg.reference_font)
    )
    db.execute(
        "INSERT INTO character_reference_labels VALUES(?,?,?,?)",
        (revision["id"], original_hash, labelled, timestamp()),
    )
    return labelled


def relabel_matching_candidates(cfg, db, revision):
    metadata = json.loads(revision["metadata_json"])
    if not metadata.get("name", "").strip() or metadata.get("height_cm") is None:
        return False
    # Only reuse output generated from the current sketch. A changed sketch needs a new take.
    rows = db.execute(
        """SELECT DISTINCT c.raw_asset_hash FROM character_reference_tasks t
        JOIN rounds r ON r.page_id=t.page_id JOIN jobs j ON j.round_id=r.id
        JOIN candidates c ON c.job_id=j.id JOIN reference_revisions source
          ON source.id=json_extract(r.snapshot_json,'$.references[0].revision')
        WHERE t.reference_id=? AND source.asset_hashes_json=? AND c.availability='available'
          AND NOT EXISTS (SELECT 1 FROM character_reference_labels label
                          WHERE label.source_revision_id=? AND label.original_hash=c.raw_asset_hash)""",
        (revision["reference_id"], revision["asset_hashes_json"], revision["id"]),
    ).fetchall()
    for row in rows:
        label_reference(cfg, db, revision, row["raw_asset_hash"])
    return bool(rows)


def selected(db, ref):
    row = db.execute(
        "SELECT * FROM character_reference_selections WHERE reference_id=?", (ref["id"],)
    ).fetchone()
    if not row or row["source_revision_id"] != ref["current_revision_id"]:
        return None
    metadata = ref["metadata"]
    if not metadata.get("name", "").strip() or metadata.get("height_cm") is None:
        return None
    original_hash = ref["images"][0]
    if row["candidate_id"]:
        candidate = get(db, "candidates", row["candidate_id"])
        if candidate["availability"] != "available":
            return None
        original_hash = candidate["raw_asset_hash"]
    label = db.execute(
        """SELECT 1 FROM character_reference_labels
           WHERE source_revision_id=? AND original_hash=? AND asset_hash=?""",
        (ref["current_revision_id"], original_hash, row["asset_hash"]),
    ).fetchone()
    return dict(row) if label else None


def approved_images(db, ref):
    required_identity(ref["metadata"])
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
    required_identity(json.loads(revision["metadata_json"]))
    draft = Draft.model_validate_json(page["draft_json"])
    attachments = [
        {"role": "キャラクタースケッチ", "hash": h} for h in json.loads(revision["asset_hashes_json"])
    ]
    prompt = REFERENCE_RULES
    if draft.page_prompt:
        prompt += "\n\n追加・修正指示\n" + draft.page_prompt
    require(len(prompt) <= 20000, "prompt_limit", "送信する全文は20000文字までです。", 422)
    payload = {
        "prompt": prompt,
        "attachments": attachments,
        "settings": settings,
        "template_version": "character-reference-author-original-v1",
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
        if page:
            for candidate in page["candidates"]:
                label = current_candidate_label(db, ref["current_revision_id"], candidate)
                candidate["identity_labelled"] = bool(label)
                if label:
                    candidate["asset_hash"] = label[0]
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
                "height_cm": ref["metadata"].get("height_cm"),
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
                        c["identity_labelled"]
                        for c in ref["page"]["candidates"]
                        if c["round_id"] == ref["page"]["latest_round_id"]
                    )
                )
            ):
                continue
            required_identity(ref)
            page_ids.append(ref["page"]["id"])
        require(page_ids, "empty_plan", "生成対象がありません。人物ごとのボタンから再生成できます。", 422)
        plan = planning.make_plan(cfg, db, internal, {"kind": "extra", "page_ids": page_ids})
        if plan["excluded"]:
            require(False, "references", "人物の名前・身長と資料を確認してください。", 422)
        result = planning.insert_batch(db, plan, key, digest(request))
        planning.save_receipt(db, f"character-generate:{pid}", key, request, result)
        return result


def current_candidate_label(db, revision_id, candidate):
    return db.execute(
        """SELECT label.asset_hash FROM character_reference_labels label
           JOIN reference_revisions current ON current.id=label.source_revision_id
           JOIN reference_revisions original ON original.id=?
           WHERE label.source_revision_id=? AND label.original_hash=?
             AND original.reference_id=current.reference_id
             AND original.asset_hashes_json=current.asset_hashes_json""",
        (candidate["source_reference_revision"], revision_id, candidate["raw_asset_hash"]),
    ).fetchone()


def review_in(cfg, db, page, request):
    """Validate source version and update the separate approved-reference pointer."""
    task = task_for(db, page["id"])
    if not task:
        return
    ref = get(db, "reference_sets", task["reference_id"])
    if request["decision"] == "accept":
        row = db.execute(
            "SELECT c.*,json_extract(r.snapshot_json,'$.references[0].revision') source_reference_revision FROM candidates c JOIN jobs j ON j.id=c.job_id JOIN rounds r ON r.id=j.round_id WHERE c.id=? AND r.page_id=?",
            (request.get("candidate_id"), page["id"]),
        ).fetchone()
        require(row, "candidate", "この人物の候補を選んでください。", 422)
        revision = get(db, "reference_revisions", ref["current_revision_id"])
        required_identity(json.loads(revision["metadata_json"]))
        label = current_candidate_label(db, revision["id"], row)
        require(
            label,
            "character_identity",
            "現在の名前・身長が記載されたリファレンスを確認して採用してください。",
            422,
        )
        labelled_path = assets.path_for(cfg, db, label[0])
        require(
            digest_file(labelled_path) == label[0],
            "character_identity",
            "名前・身長を記載した画像の内容を確認できません。",
            422,
        )
        db.execute(
            "INSERT OR REPLACE INTO character_reference_selections VALUES(?,?,?,?,?)",
            (ref["id"], revision["id"], row["id"], label[0], timestamp()),
        )
    else:
        db.execute("DELETE FROM character_reference_selections WHERE reference_id=?", (ref["id"],))
    db.execute("UPDATE projects SET version=version+1 WHERE id=?", (ref["project_id"],))


def character_folder_key(db, pid, key):
    names = db.execute(
        "SELECT stable_key FROM reference_sets WHERE project_id=? AND kind='character'", (pid,)
    ).fetchall()
    return safe_character_key(key, [row[0] for row in names])


def safe_character_key(key, names):
    def safe(value):
        return re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", value).strip(" .")[:60] or "人物"

    name = safe(key)
    normalized = unicodedata.normalize("NFC", name).casefold()
    collision = sum(unicodedata.normalize("NFC", safe(other)).casefold() == normalized for other in names) > 1
    if name != key or collision:
        name += "-" + digest(key)[:8]
    return name


def digest_file(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def sync_files(cfg, prior_stamp=None):
    """Publish every reference take without mixing it with comic page output."""
    with cfg.write_lock(), connect(cfg) as db:
        rows = db.execute("""SELECT rs.id reference_id,rs.stable_key,rs.project_id,rs.current_revision_id,
          c.asset_hash original_display,COALESCE(label.asset_hash,c.asset_hash) asset_hash,
          label.asset_hash labelled_hash,c.id candidate_id,r.sequence,r.prompt_text,r.review_state,j.candidate_index,
          w.value work_folder,p.title,p.work_id,selection.candidate_id selected_id,
          (SELECT COUNT(*) FROM projects WHERE work_id=p.work_id AND purpose='comic') episode_count
          FROM character_reference_tasks t
          JOIN reference_sets rs ON rs.id=t.reference_id JOIN pages tp ON tp.id=t.page_id
          JOIN rounds r ON r.page_id=tp.id JOIN jobs j ON j.round_id=r.id JOIN candidates c ON c.job_id=j.id
          JOIN projects p ON p.id=rs.project_id JOIN system w ON w.key='work_folder:'||p.work_id
          JOIN reference_revisions current ON current.id=rs.current_revision_id
          LEFT JOIN reference_revisions original ON original.id=json_extract(r.snapshot_json,'$.references[0].revision')
          LEFT JOIN character_reference_labels label
            ON label.source_revision_id=rs.current_revision_id AND label.original_hash=c.raw_asset_hash
            AND original.asset_hashes_json=current.asset_hashes_json
          LEFT JOIN character_reference_selections selection
            ON selection.reference_id=rs.id AND selection.source_revision_id=rs.current_revision_id
            AND selection.asset_hash=label.asset_hash
          ORDER BY rs.project_id,rs.stable_key,r.sequence,j.candidate_index""").fetchall()
        names = {}
        for item in db.execute(
            "SELECT project_id,stable_key FROM reference_sets WHERE kind='character' ORDER BY project_id,stable_key"
        ):
            names.setdefault(item["project_id"], []).append(item["stable_key"])
        stamp = digest([names, [dict(row) for row in rows]])
        if stamp == prior_stamp:
            return stamp
        grouped = {}
        for row in rows:
            key = safe_character_key(row["stable_key"], names[row["project_id"]])
            base = Path(row["work_folder"]) / "キャラクターリファレンス"
            if row["episode_count"] > 1:
                from .export import readable_name

                base = base / readable_name(row["title"], row["project_id"])
            target = base / key
            target.mkdir(parents=True, exist_ok=True)
            suffix = f"_候補{row['candidate_index']:02d}" if row["candidate_index"] > 1 else ""
            filename = f"テイク{row['sequence']:02d}{suffix}.png"
            destination = target / filename
            if destination.exists() and row["labelled_hash"]:
                if digest_file(destination) != row["asset_hash"]:
                    filename = f"テイク{row['sequence']:02d}{suffix}_記載{row['labelled_hash'][:10]}.png"
                    destination = target / filename
            if not destination.exists():
                atomic_write(destination, assets.path_for(cfg, db, row["asset_hash"]).read_bytes())
            prompt_file = target / f"テイク{row['sequence']:02d}_生成指示.txt"
            if not prompt_file.exists():
                atomic_write(prompt_file, row["prompt_text"].encode())
            grouped.setdefault(target, []).append(
                {
                    "file": filename,
                    "take": row["sequence"],
                    "candidate": row["candidate_index"],
                    "status": "採用"
                    if row["selected_id"] == row["candidate_id"]
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
        return stamp
