"""Return Gemini reservations only when non-acceptance is positively known."""

import json
import re

from .common import canonical, require, timestamp, uid
from .db import get, transaction

REJECTED = re.compile(
    r"gemini_rejected_(400|401|403|404)(?:_(billing|mime_type|input_format|quota|api_key|precondition))?"
)


def release_unaccepted(cfg, db, cid, proof):
    """Use the caller's failure transaction; a durable receipt prevents double refunds."""
    if cfg.provider != "gemini":
        return 0
    receipt_key = "gemini_released:" + cid
    if db.execute("SELECT 1 FROM system WHERE key=?", (receipt_key,)).fetchone():
        return 0
    chunk = get(db, "chunks", cid)
    if chunk["state"] != "failed" or chunk["remote_name"] or chunk["output_path"]:
        return 0
    attempts = db.execute(
        "SELECT a.*,r.snapshot_json FROM attempts a JOIN jobs j ON j.id=a.job_id "
        "JOIN rounds r ON r.id=j.round_id WHERE a.chunk_id=?",
        (cid,),
    ).fetchall()
    if not attempts or any(
        json.loads(a["snapshot_json"])["settings"]["provider"] != "gemini" for a in attempts
    ):
        return 0
    if proof == "provider_rejected":
        valid = all(
            a["state"] == "terminal_failed" and REJECTED.fullmatch(a["error_code"] or "") for a in attempts
        )
    elif proof == "rate_limited":
        valid = all(a["state"] == "retryable_failed" and a["error_code"] == "rate_limited" for a in attempts)
    elif proof == "before_submit":
        # Only the build/upload failure path calls this; never inferred from a timeout.
        valid = all(a["state"] == "terminal_failed" for a in attempts)
    else:
        raise ValueError("Unknown non-acceptance proof")
    if not valid:
        return 0
    count = len(attempts)
    amount = sum(a["send_reserved_micro"] for a in attempts)
    batch = get(db, "batches", chunk["batch_id"])
    reserved = db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()
    require(
        reserved and int(reserved[0]) >= count and batch["spent_reserved_micro"] >= amount,
        "reservation_integrity",
        "送信枠の記録に不整合があります。",
    )
    db.execute(
        "UPDATE system SET value=? WHERE key='gemini_reserved_requests'", (str(int(reserved[0]) - count),)
    )
    db.execute(
        "UPDATE batches SET spent_reserved_micro=spent_reserved_micro-? WHERE id=?", (amount, batch["id"])
    )
    released = {"requests": count, "amount_micro": amount, "proof": proof}
    db.execute("INSERT INTO system VALUES(?,?)", (receipt_key, canonical(released)))
    db.execute(
        "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
        (
            uid(),
            batch["project_id"],
            "release_unaccepted",
            cid,
            canonical(
                {"reserved_requests": int(reserved[0]), "batch_reserved_micro": batch["spent_reserved_micro"]}
            ),
            canonical(released),
            timestamp(),
        ),
    )
    return count


def reclaim_rejected(cfg):
    """Upgrade existing known rejections without changing limits or retrying any job."""
    if cfg.provider != "gemini":
        return 0
    released = 0
    with transaction(cfg) as db:
        for chunk in db.execute(
            "SELECT id,last_error FROM chunks WHERE state='failed' AND remote_name IS NULL AND output_path IS NULL"
        ).fetchall():
            code = chunk["last_error"] or ""
            proof = (
                "provider_rejected"
                if REJECTED.fullmatch(code)
                else "rate_limited"
                if code == "rate_limited"
                else None
            )
            if proof:
                released += release_unaccepted(cfg, db, chunk["id"], proof)
    return released
