from __future__ import annotations

import hashlib
import hmac
import json
import math
import time

from .common import DomainError, canonical, digest, require, timestamp, uid
from .db import get, transaction
from .projects import bump, page_detail, patch_draft
from .prompts import provider_ready, snapshot

ACTIVE = ("queued", "running", "retry_wait", "unknown")


def valid_key(key):
    require(isinstance(key, str) and 1 <= len(key) <= 200, "idempotency_key", "操作キーが必要です。", 422)


def token(cfg, ident):
    return ident + "." + hmac.new(cfg.secret.encode(), ident.encode(), hashlib.sha256).hexdigest()


def read_plan(cfg, db, value):
    ident = value.split(".")[0]
    require(hmac.compare_digest(value, token(cfg, ident)), "invalid_plan", "計画が無効です。", 422)
    row = get(db, "plans", ident)
    require(
        row["expires"] > time.time(), "expired_plan", "計画が失効しました。再度生成を開始してください。", 422
    )
    return json.loads(row["payload_json"])


def make_plan(cfg, db, pid, request):
    project = get(db, "projects", pid)
    require(
        project["lifecycle"] == "active", "inactive", "作品設定で制作中に戻してから生成してください。", 409
    )
    provider_ready(cfg, json.loads(project["settings_json"]) | request.get("settings", {}))
    count = request.get("candidate_count", 1)
    unit = request.get("unit_reserve_micro", cfg.live_unit_micro if cfg.provider == "gemini" else 1)
    require(type(count) is int and 1 <= count <= 3, "candidate_count", "候補数は1〜3です。", 422)
    require(type(unit) is int and unit > 0, "budget", "送信単位は正整数で指定してください。", 422)
    if cfg.provider == "gemini":
        require(unit >= cfg.live_unit_micro, "budget", "起動時に指定した予算単位より小さくできません。", 422)
    kind = request.get("kind", "initial")
    require(kind in ("initial", "revision", "extra"), "kind", "不明な生成操作です。", 422)
    wanted = request.get("page_ids")
    if wanted is not None:
        require(
            isinstance(wanted, list) and len(wanted) == len(set(wanted)),
            "page_ids",
            "ページ指定が不正です。",
            422,
        )
    selected, excluded = [], []
    chunk_sizes = []
    for row in db.execute("SELECT * FROM pages WHERE project_id=? ORDER BY number", (pid,)):
        page = dict(row)
        if wanted is not None and page["id"] not in wanted:
            continue
        round_ = get(db, "rounds", page["latest_round_id"]) if page["latest_round_id"] else None
        if kind == "initial" and round_:
            continue
        if kind == "revision" and (
            page["accepted_candidate_id"] or not round_ or round_["review_state"] != "rejected"
        ):
            continue
        if db.execute(
            """SELECT 1 FROM jobs j JOIN rounds r ON r.id=j.round_id
                       WHERE r.page_id=? AND j.state IN ('queued','running','retry_wait','unknown')""",
            (page["id"],),
        ).fetchone():
            raise DomainError("active_round", f"{page['number']}ページは処理中です。", 409)
        try:
            snap, hash_, estimate = snapshot(cfg, db, page, request.get("settings"))
            require(estimate <= cfg.chunk_bytes, "chunk_size", "1要求がチャンク上限を超えています。", 422)
        except DomainError as e:
            if e.code == "character_reference_required":
                raise
            excluded.append({"page_id": page["id"], "number": page["number"], "message": e.message})
            continue
        selected.append(
            {
                "page_id": page["id"],
                "number": page["number"],
                "version": page["version"],
                "snapshot": snap,
                "input_hash": hash_,
                "estimated_bytes": estimate,
            }
        )
        chunk_sizes.extend([estimate] * count)
    require(selected, "empty_plan", "生成できる対象ページがありません。", 422)
    if wanted is not None:
        owned = {r[0] for r in db.execute("SELECT id FROM pages WHERE project_id=?", (pid,))}
        require(set(wanted) <= owned, "page_ids", "別作品のページは指定できません。", 422)
    chunks, n, size = 1, 0, 0
    for estimate in chunk_sizes:
        if n and (n == cfg.chunk_count or size + estimate > cfg.chunk_bytes):
            chunks, n, size = chunks + 1, 0, 0
        n, size = n + 1, size + estimate
    cap = request.get("cap_micro", len(selected) * count * unit)
    require(type(cap) is int and cap >= 0, "budget", "送信枠は0以上の整数にしてください。", 422)
    value = {
        "project_id": pid,
        "project_version": project["version"],
        "kind": kind,
        "candidate_count": count,
        "pages": selected,
        "excluded": excluded,
        "unit_reserve_micro": unit,
        "cap_micro": cap,
        "chunks": chunks,
        "waves": math.ceil(chunks / cfg.max_external),
        "jobs": len(selected) * count,
    }
    if cfg.provider == "gemini" and cfg.live_request_limit is not None:
        reserved_row = db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()
        reserved = int(reserved_row[0]) if reserved_row else 0
        require(
            len(selected) * count <= cfg.live_request_limit - reserved,
            "live_limit",
            "試験用の累積生成回数に達しました。.envのMANGA_LIVE_MAX_REQUESTSを空欄にすると通常利用に戻せます。",
            422,
        )
        require(
            len(selected) * count <= cfg.live_request_limit and cap <= cfg.live_request_limit * unit,
            "live_limit",
            "この実行の送信数または予算枠が起動時の上限を超えます。",
            422,
        )
    ident = uid()
    db.execute("INSERT INTO plans VALUES(?,?,?,?)", (ident, pid, canonical(value), time.time() + 900))
    return value | {"plan_token": token(cfg, ident)}


def replay_batch(db, pid, key, payload_hash):
    valid_key(key)
    row = db.execute("SELECT * FROM batches WHERE project_id=? AND idempotency_key=?", (pid, key)).fetchone()
    if row:
        require(
            row["payload_hash"] == payload_hash, "idempotency_conflict", "同じ操作キーの内容が異なります。"
        )
        return {"batch_id": row["id"], "replayed": True}


def insert_batch(db, plan, key, payload_hash):
    pid = plan["project_id"]
    require(
        get(db, "projects", pid)["lifecycle"] == "active",
        "inactive",
        "制作中に戻してから生成してください。",
        409,
    )
    require(get(db, "projects", pid)["version"] == plan["project_version"])
    batch_id = uid()
    db.execute(
        """INSERT INTO batches(id,project_id,kind,idempotency_key,payload_hash,plan_json,
               cap_micro,unit_reserve_micro,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            batch_id,
            pid,
            plan["kind"],
            key,
            payload_hash,
            canonical(plan),
            plan["cap_micro"],
            plan["unit_reserve_micro"],
            timestamp(),
        ),
    )
    for item in plan["pages"]:
        page = get(db, "pages", item["page_id"])
        require(page["version"] == item["version"])
        for reference in item["snapshot"]["references"]:
            current = get(db, "reference_sets", reference["id"])
            require(
                current["current_revision_id"] == reference["revision"],
                "stale_reference",
                "人物・シーン資料が更新されています。生成内容を再確認してください。",
                409,
            )
        require(
            not db.execute(
                """SELECT 1 FROM jobs j JOIN rounds r ON r.id=j.round_id
                WHERE r.page_id=? AND j.state IN ('queued','running','retry_wait','unknown')""",
                (page["id"],),
            ).fetchone()
        )
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM rounds WHERE page_id=?", (page["id"],)
        ).fetchone()[0]
        rid = uid()
        db.execute(
            """INSERT INTO rounds(id,page_id,sequence,input_hash,snapshot_json,prompt_text,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
            (
                rid,
                page["id"],
                sequence,
                item["input_hash"],
                canonical(item["snapshot"]),
                item["snapshot"]["prompt"],
                timestamp(),
            ),
        )
        for i in range(plan["candidate_count"]):
            db.execute(
                "INSERT INTO jobs(id,batch_id,round_id,candidate_index) VALUES(?,?,?,?)",
                (uid(), batch_id, rid, i + 1),
            )
        db.execute("UPDATE pages SET latest_round_id=? WHERE id=?", (rid, page["id"]))
        bump(db, page)
    return {"batch_id": batch_id, "replayed": False}


def start(cfg, pid, plan_token, key):
    payload_hash = digest({"plan_token": plan_token})
    with transaction(cfg) as db:
        provider_ready(cfg, json.loads(get(db, "projects", pid)["settings_json"]))
        prior = replay_batch(db, pid, key, payload_hash)
        if prior:
            return prior
        plan = read_plan(cfg, db, plan_token)
        require(plan["project_id"] == pid)
        return insert_batch(db, plan, key, payload_hash)


def review_in(db, page, request):
    require(page["version"] == request["expected_version"])
    require(page["latest_round_id"] == request["latest_round_id"])
    require(page["latest_round_id"], "no_round", "審査する候補がありません。", 422)
    rid = page["latest_round_id"]
    decision = request["decision"]
    candidate_id = request.get("candidate_id")
    if decision == "accept":
        require(candidate_id, "candidate", "候補を選んでください。", 422)
        candidate = db.execute(
            """SELECT c.*,r.page_id,r.id round_id FROM candidates c
                    JOIN jobs j ON j.id=c.job_id JOIN rounds r ON r.id=j.round_id WHERE c.id=?""",
            (candidate_id,),
        ).fetchone()
        require(
            candidate and candidate["page_id"] == page["id"] and candidate["availability"] == "available",
            "candidate",
            "このページの有効な候補を選んでください。",
            422,
        )
        require(
            candidate["round_id"] == rid or request.get("restore"),
            "old_candidate",
            "過去候補の採用を明示してください。",
            422,
        )
        rid = candidate["round_id"]
        db.execute("UPDATE pages SET accepted_candidate_id=? WHERE id=?", (candidate_id, page["id"]))
        db.execute("UPDATE rounds SET review_state='accepted' WHERE id=?", (rid,))
    elif decision == "reject":
        require(
            not page["accepted_candidate_id"] or request.get("clear_accepted"),
            "clear_accepted",
            "採用を解除して却下することを明示してください。",
            422,
        )
        db.execute("UPDATE pages SET accepted_candidate_id=NULL WHERE id=?", (page["id"],))
        db.execute(
            "UPDATE rounds SET review_state='rejected',rejection_reason=? WHERE id=?",
            (request.get("reason", ""), rid),
        )
        candidate_id = None
    else:
        raise DomainError("decision", "採用または却下を指定してください。")
    db.execute(
        "INSERT INTO review_events VALUES(?,?,?,?,?,?,?)",
        (uid(), page["id"], rid, candidate_id, decision, request.get("reason", ""), timestamp()),
    )
    from .character_references import review_in as reference_review

    reference_review(db, page, request)
    bump(db, page)


def review(cfg, page_id, request):
    with transaction(cfg) as db:
        review_in(db, get(db, "pages", page_id), request)
        return page_detail(db, page_id)


def revise_and_generate(cfg, page_id, request, key):
    with transaction(cfg) as db:
        page = get(db, "pages", page_id)
        provider_ready(cfg, json.loads(get(db, "projects", page["project_id"])["settings_json"]))
        prior = replay_batch(db, page["project_id"], key, digest(request | {"page_id": page_id}))
        if prior:
            return prior
        require(page["version"] == request["expected_version"])
        review_in(db, page, request | {"decision": "reject"})
        page = get(db, "pages", page_id)
        patch_draft(db, page, request.get("draft_patch", {}))
        plan = make_plan(
            cfg,
            db,
            page["project_id"],
            {
                "kind": "revision",
                "page_ids": [page_id],
                **{
                    k: request[k]
                    for k in ("candidate_count", "cap_micro", "unit_reserve_micro")
                    if k in request
                },
            },
        )
        return insert_batch(db, plan, key, digest(request | {"page_id": page_id}))


def receipt(db, scope, key, payload):
    valid_key(key)
    row = db.execute(
        "SELECT * FROM mutation_receipts WHERE scope=? AND idempotency_key=?", (scope, key)
    ).fetchone()
    if row:
        require(row["payload_hash"] == digest(payload))
        return json.loads(row["response_json"])


def save_receipt(db, scope, key, payload, response):
    db.execute(
        "INSERT INTO mutation_receipts VALUES(?,?,?,?,?)",
        (scope, key, digest(payload), canonical(response), timestamp()),
    )


def retry(cfg, job_id, request, key):
    with transaction(cfg) as db:
        prior = receipt(db, f"retry:{job_id}", key, request)
        if prior:
            return prior
        job = get(db, "jobs", job_id)
        require(job["state"] == request.get("expected_state") == "failed")
        require(
            not db.execute("SELECT 1 FROM candidates WHERE job_id=?", (job_id,)).fetchone(),
            "candidate_exists",
            "候補画像の欠損は追加生成で対応してください。",
            422,
        )
        round_ = get(db, "rounds", job["round_id"])
        require(
            not db.execute(
                """SELECT 1 FROM jobs j JOIN rounds r ON r.id=j.round_id
            WHERE r.page_id=? AND r.id<>? AND j.state IN ('queued','running','retry_wait','unknown')""",
                (round_["page_id"], job["round_id"]),
            ).fetchone(),
            "active_round",
            "同じページの別の版が処理中です。",
        )
        count = db.execute("SELECT COUNT(*) FROM attempts WHERE job_id=?", (job_id,)).fetchone()[0]
        if count >= 3 + job["manual_allowance"]:
            require(
                request.get("explicit_extra_attempt"), "attempt_limit", "追加の試行を明示してください。", 422
            )
            db.execute("UPDATE jobs SET manual_allowance=manual_allowance+1 WHERE id=?", (job_id,))
        db.execute("UPDATE jobs SET state='queued',retry_after=0 WHERE id=?", (job_id,))
        response = {"job_id": job_id}
        save_receipt(db, f"retry:{job_id}", key, request, response)
        return response
