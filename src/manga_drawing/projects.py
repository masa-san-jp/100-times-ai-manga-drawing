from __future__ import annotations

import json
import time

from . import assets
from .common import DomainError, atomic_write, canonical, require, timestamp, uid
from .db import connect, get, transaction
from .models import Draft, Manifest


def create_import(cfg):
    token = uid()
    folder = cfg.root / "staging" / token
    folder.mkdir()
    atomic_write(folder / "meta.json", canonical({"created": time.time(), "files": {}}).encode())
    return token


def import_meta(cfg, token):
    # An UUID guard also prevents a token from being used as a filesystem path.
    import uuid

    try:
        require(str(uuid.UUID(token)) == token, "not_found", "登録が見つかりません。", 404)
        folder = cfg.root / "staging" / token
        meta = json.loads((folder / "meta.json").read_text())
        require(time.time() - meta["created"] < 86400, "expired", "登録の有効期限が切れました。", 422)
        return folder, meta
    except (ValueError, FileNotFoundError):
        raise DomainError("not_found", "登録が見つかりません。", 404) from None


def upload(cfg, token, path, content):
    path = assets.safe_path(path)
    assets.decode(content)
    with cfg.write_lock():
        folder, meta = import_meta(cfg, token)
        require(path not in meta["files"], "duplicate_path", "同じパスが重複しています。", 422)
        require(
            sum(meta["files"].values()) + len(content) <= 1024**3,
            "upload_size",
            "登録全体は1GiB以下にしてください。",
            413,
        )
        atomic_write(folder / "files" / path, content)
        meta["files"][path] = len(content)
        atomic_write(folder / "meta.json", canonical(meta).encode())


def validate_import(cfg, token, manifest, project_id=None):
    manifest = Manifest.model_validate(manifest).model_dump()
    folder, meta = import_meta(cfg, token)
    characters = {x["key"] for x in manifest["characters"]}
    scenes = {x["key"] for x in manifest["scenes"]}
    version = None
    if project_id:
        with connect(cfg) as db:
            project = get(db, "projects", project_id)
            version = project["version"]
            for row in db.execute(
                "SELECT kind,stable_key FROM reference_sets WHERE project_id=?", (project_id,)
            ):
                (characters if row["kind"] == "character" else scenes).add(row["stable_key"])
    errors = []
    for item in manifest["characters"] + manifest["scenes"]:
        for path in item["images"]:
            path = assets.safe_path(path)
            if path not in meta["files"]:
                errors.append({"reference": item["key"], "message": f"資料がありません: {path}"})
    for page in manifest["pages"]:
        for path in (page["name"], page["instructions"]):
            path = assets.safe_path(path)
            if path not in meta["files"]:
                errors.append({"page": page["number"], "message": f"画像がありません: {path}"})
        if set(page["characters"]) - characters or (page["scene"] and page["scene"] not in scenes):
            errors.append({"page": page["number"], "message": "未登録の人物またはシーンです。"})
    plan = {"manifest": manifest, "project_id": project_id, "version": version}
    atomic_write(folder / "plan.json", canonical(plan).encode())
    return {"token": token, "errors": errors, "pages": len(manifest["pages"]), "expected_version": version}


def commit_import(cfg, token, expected_version=None, work_id=None):
    with transaction(cfg) as db:
        prior = db.execute("SELECT value FROM system WHERE key=?", (f"import:{token}",)).fetchone()
        if prior:
            return {"project_id": prior[0]}
        if work_id:
            get(db, "works", work_id)
        folder, meta = import_meta(cfg, token)
        try:
            plan = json.loads((folder / "plan.json").read_text())
        except FileNotFoundError:
            raise DomainError("not_validated", "先に登録内容を検証してください。") from None
        manifest, pid = plan["manifest"], plan["project_id"]
        report = validate_import(cfg, token, manifest, pid)
        require(not report["errors"], "invalid_import", "不足資料を修正してください。", 422)
        if pid:
            project = get(db, "projects", pid)
            require(project["version"] == expected_version == plan["version"])
            count = db.execute("SELECT COUNT(*) FROM pages WHERE project_id=?", (pid,)).fetchone()[0]
            existing_numbers = {
                x[0] for x in db.execute("SELECT number FROM pages WHERE project_id=?", (pid,))
            }
            require(
                count + len({x["number"] for x in manifest["pages"]} - existing_numbers) <= 500,
                "page_limit",
                "1作品は500ページまでです。",
                422,
            )
        else:
            pid = uid()
            db.execute(
                "INSERT INTO projects(id,title,created_at) VALUES(?,?,?)",
                (pid, manifest["title"], timestamp()),
            )
        if work_id and not plan["project_id"]:
            db.execute("UPDATE projects SET work_id=? WHERE id=?", (work_id, pid))
            db.execute(
                "DELETE FROM works WHERE id=? AND NOT EXISTS(SELECT 1 FROM projects WHERE work_id=?)",
                (pid, pid),
            )
        mapped, originals = {}, {}
        for path in meta["files"]:
            originals[path], mapped[path] = assets.normalized(cfg, db, (folder / "files" / path).read_bytes())
        for kind, group in (("character", manifest["characters"]), ("scene", manifest["scenes"])):
            for ref in group:
                row = db.execute(
                    "SELECT id FROM reference_sets WHERE project_id=? AND kind=? AND stable_key=?",
                    (pid, kind, ref["key"]),
                ).fetchone()
                rid = row[0] if row else uid()
                if not row:
                    db.execute(
                        "INSERT INTO reference_sets(id,project_id,kind,stable_key) VALUES(?,?,?,?)",
                        (rid, pid, kind, ref["key"]),
                    )
                revision = uid()
                db.execute(
                    "INSERT INTO reference_revisions VALUES(?,?,?,?,?)",
                    (
                        revision,
                        rid,
                        canonical({k: v for k, v in ref.items() if k != "images"}),
                        canonical([mapped[p] for p in ref["images"]]),
                        timestamp(),
                    ),
                )
                db.execute("UPDATE reference_sets SET current_revision_id=? WHERE id=?", (revision, rid))
        for page in manifest["pages"]:
            existing = db.execute(
                "SELECT draft_json FROM pages WHERE project_id=? AND number=?", (pid, page["number"])
            ).fetchone()
            page_prompt = page["page_prompt"]
            if page_prompt is None:
                page_prompt = json.loads(existing[0]).get("page_prompt", "") if existing else ""
            draft = Draft(
                name_hash=mapped[page["name"]],
                instruction_hash=mapped[page["instructions"]],
                characters=page["characters"],
                scene=page["scene"],
                directions=page["directions"],
                page_prompt=page_prompt,
            )
            db.execute(
                """INSERT INTO pages(id,project_id,number,draft_json) VALUES(?,?,?,?)
                       ON CONFLICT(project_id,number) DO UPDATE SET draft_json=excluded.draft_json,version=pages.version+1""",
                (uid(), pid, page["number"], draft.model_dump_json()),
            )
        db.execute("UPDATE projects SET title=?,version=version+1 WHERE id=?", (manifest["title"], pid))
        db.execute(
            "INSERT INTO input_imports VALUES(?,?,?,?,?)",
            (
                token,
                pid,
                canonical(manifest),
                canonical(
                    {path: {"original": originals[path], "normalized": mapped[path]} for path in mapped}
                ),
                timestamp(),
            ),
        )
        # Keep receipt in the DB transaction, not in staging, so rollback is atomic.
        db.execute("INSERT OR REPLACE INTO system VALUES(?,?)", (f"import:{token}", pid))
        return {"project_id": pid}


def bump(db, page):
    db.execute("UPDATE pages SET version=version+1 WHERE id=?", (page["id"],))
    db.execute("UPDATE projects SET version=version+1 WHERE id=?", (page["project_id"],))


def patch_draft(db, page, patch):
    draft = Draft.model_validate(json.loads(page["draft_json"]) | patch).model_dump()
    for h in (draft["name_hash"], draft["instruction_hash"]):
        require(
            db.execute("SELECT 1 FROM assets WHERE sha256=?", (h,)).fetchone(),
            "invalid_asset",
            "登録済み画像を指定してください。",
            422,
        )
    db.execute("UPDATE pages SET draft_json=? WHERE id=?", (canonical(draft), page["id"]))
    bump(db, page)
    page["draft_json"] = canonical(draft)
    page["version"] += 1
    return page


def references(db, pid):
    out = []
    for row in db.execute(
        """SELECT r.*,v.metadata_json,v.asset_hashes_json FROM reference_sets r
                           JOIN reference_revisions v ON v.id=r.current_revision_id WHERE project_id=?
                           ORDER BY kind,stable_key""",
        (pid,),
    ):
        out.append(
            dict(row)
            | {"metadata": json.loads(row["metadata_json"]), "images": json.loads(row["asset_hashes_json"])}
        )
    return out


GENERATION_LABELS = {
    "not_requested": "生成未依頼（開始操作をしていません）",
    "queued": "依頼受付済み・API送信待ち",
    "paused": "依頼受付済み・送信開始を一時停止中",
    "sending": "APIへ送信中（受理は未確認）",
    "submitted": "API受理済み・生成結果待ち",
    "fetching": "生成完了・画像を取得して保存中",
    "retry_wait": "送信失敗・自動再試行の開始待ち",
    "complete": "生成完了・画像保存済み",
    "failed": "生成失敗・処理停止中",
    "unknown": "APIの受理・完了状況が不明（要確認）",
}


def generation_progress(db, page):
    rows = db.execute(
        """SELECT j.state,b.paused,c.state chunk_state FROM jobs j
        JOIN batches b ON b.id=j.batch_id LEFT JOIN attempts a ON a.id=j.active_attempt_id
        LEFT JOIN chunks c ON c.id=a.chunk_id WHERE j.round_id=? ORDER BY j.candidate_index""",
        (page["latest_round_id"],),
    ).fetchall()
    counts = {}
    for job in rows:
        state = job["state"]
        if state == "queued":
            phase = "paused" if job["paused"] else "queued"
        elif state == "running":
            phase = {
                "prepared": "queued",
                "uploading": "sending",
                "submitting": "sending",
                "submitted": "submitted",
                "fetching": "fetching",
                "unknown": "unknown",
                "failed": "failed",
            }.get(job["chunk_state"], "unknown")
        else:
            phase = {
                "succeeded": "complete",
                "retry_wait": "retry_wait",
                "failed": "failed",
                "unknown": "unknown",
            }.get(state, "unknown")
        if job["paused"] and (
            state in ("queued", "retry_wait") or job["chunk_state"] in ("prepared", "uploading")
        ):
            phase = "paused"
        counts[phase] = counts.get(phase, 0) + 1
    if not counts:
        counts = {"not_requested": 1}
    label = " / ".join(
        f"{GENERATION_LABELS[phase]}" + (f" {count}件" if len(rows) > 1 else "")
        for phase, count in counts.items()
    )
    return {
        "generation_counts": counts,
        "generation_label": label,
        "generation_sequence": page["latest_round"]["sequence"] if page["latest_round"] else None,
    }


def page_detail(db, page_id):
    page = get(db, "pages", page_id)
    page["draft"] = json.loads(page.pop("draft_json"))
    page["latest_round"] = get(db, "rounds", page["latest_round_id"]) if page["latest_round_id"] else None
    if page["latest_round"]:
        page["latest_round"]["snapshot"] = json.loads(page["latest_round"].pop("snapshot_json"))
    page["candidates"] = [
        dict(r)
        for r in db.execute(
            """SELECT c.*,r.id round_id,r.sequence,j.candidate_index,json_extract(r.snapshot_json,'$.settings.provider') provider
                        FROM candidates c JOIN jobs j ON j.id=c.job_id JOIN rounds r ON r.id=j.round_id
                        WHERE r.page_id=? ORDER BY r.sequence DESC,j.candidate_index LIMIT 100""",
            (page_id,),
        )
    ]
    page["jobs"] = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM jobs WHERE round_id=? ORDER BY candidate_index", (page["latest_round_id"],)
        )
    ]
    states = {j["state"] for j in page["jobs"]}
    if page["accepted_candidate_id"]:
        status = "accepted"
    elif page["latest_round"] and page["latest_round"]["review_state"] == "rejected":
        status = "rejected"
    elif any(
        c["round_id"] == page["latest_round_id"] and c["availability"] == "available"
        for c in page["candidates"]
    ):
        status = "review"
    elif states & {"queued", "running", "retry_wait"}:
        status = "running"
    elif "unknown" in states:
        status = "unknown"
    elif states:
        status = "error"
    else:
        status = "new"
    page["status"] = status
    page.update(generation_progress(db, page))
    return page


def page_list(db, pid, status=None, cursor=0, limit=40):
    # Query lightweight rows and bounded candidate metadata; never load image bytes.
    items = []
    for r in db.execute(
        "SELECT id FROM pages WHERE project_id=? AND number>? ORDER BY number", (pid, cursor)
    ):
        page = page_detail(db, r[0])
        if status and page["status"] != status:
            continue
        candidate = next((c for c in page["candidates"] if c["id"] == page["accepted_candidate_id"]), None)
        candidate = candidate or next(
            (c for c in page["candidates"] if c["round_id"] == page["latest_round_id"]), None
        )
        latest_candidates = [
            c
            for c in page["candidates"]
            if c["round_id"] == page["latest_round_id"] and c["availability"] == "available"
        ]
        items.append(
            {
                k: page[k]
                for k in ("id", "number", "version", "status", "latest_round_id", "accepted_candidate_id")
            }
            | {
                "thumbnail": candidate["thumbnail_hash"] if candidate else None,
                "generation_counts": page["generation_counts"],
                "generation_label": page["generation_label"],
                "generation_sequence": page["generation_sequence"],
                "review_candidate_id": latest_candidates[0]["id"] if len(latest_candidates) == 1 else None,
                "reviewable": bool(latest_candidates)
                and not any(
                    j["state"] in ("queued", "running", "retry_wait", "unknown") for j in page["jobs"]
                ),
            }
        )
        if len(items) == limit:
            break
    return {"items": items, "next_cursor": items[-1]["number"] if len(items) == limit else None}
