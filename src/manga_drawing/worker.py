from __future__ import annotations

import base64
from collections import Counter
import fcntl
import hashlib
import json
import os
import random
import threading
import time

from . import assets
from .common import DomainError, atomic_write, canonical, require, timestamp, uid
from .db import connect, get, transaction
from .planning import receipt, save_receipt
from .providers.base import RateLimited, SubmissionUnknown
from .providers.factory import create_provider
from .providers.gemini import ProviderRejected
from .reservations import reclaim_rejected, release_unaccepted


def failpoint(name):
    if os.environ.get("MANGA_CRASH_AT") == name:
        os._exit(73)


class Worker:
    def __init__(self, cfg, clock=time.time):
        from .prompts import provider_ready

        provider_ready(cfg, {})
        self.cfg, self.clock = cfg, clock
        self.provider = create_provider(cfg, clock)
        self.last_cleanup = 0.0

    def heartbeat(self):
        with transaction(self.cfg) as db:
            db.execute("INSERT OR REPLACE INTO system VALUES('heartbeat',?)", (str(time.time()),))

    def prepare(self):
        """Reserve a complete chunk atomically; insufficient allowance leaves every job queued."""
        with transaction(self.cfg) as db:
            external = db.execute(
                "SELECT COUNT(DISTINCT c.id) FROM chunks c JOIN attempts a ON a.chunk_id=c.id JOIN jobs j ON j.id=a.job_id JOIN rounds r ON r.id=j.round_id WHERE json_extract(r.snapshot_json,'$.settings.provider')=? AND (c.state IN ('submitting','submitted') OR (c.state='unknown' AND c.output_path IS NULL AND COALESCE(c.last_error,'') NOT LIKE 'gemini_batch_%'))",
                (self.cfg.provider,),
            ).fetchone()[0]
            if external >= self.cfg.max_external:
                return None
            for batch in db.execute("SELECT * FROM batches WHERE paused=0 ORDER BY created_at").fetchall():
                batch_provider = json.loads(batch["plan_json"])["pages"][0]["snapshot"]["settings"][
                    "provider"
                ]
                if batch_provider != self.cfg.provider:
                    continue
                rows = db.execute(
                    """SELECT j.*,r.snapshot_json,p.number FROM jobs j
                    JOIN rounds r ON r.id=j.round_id JOIN pages p ON p.id=r.page_id
                    WHERE j.batch_id=? AND j.state IN ('queued','retry_wait') AND j.retry_after<=?
                    ORDER BY p.number,j.candidate_index""",
                    (batch["id"], self.clock()),
                ).fetchall()
                chosen, estimated = [], 0
                for row in rows:
                    snap = json.loads(row["snapshot_json"])
                    size = len(canonical(snap).encode())
                    for attachment in snap["attachments"]:
                        size += (
                            db.execute(
                                "SELECT bytes FROM assets WHERE sha256=?", (attachment["hash"],)
                            ).fetchone()[0]
                            * 4
                            // 3
                            + 512
                        )
                    if chosen and (
                        len(chosen) >= self.cfg.chunk_count or estimated + size > self.cfg.chunk_bytes
                    ):
                        break
                    chosen.append(row)
                    estimated += size
                if not chosen:
                    continue
                cost = len(chosen) * batch["unit_reserve_micro"]
                if batch["spent_reserved_micro"] + cost > batch["cap_micro"]:
                    continue
                if self.cfg.provider == "gemini":
                    row = db.execute(
                        "SELECT value FROM system WHERE key='gemini_reserved_requests'"
                    ).fetchone()
                    reserved = int(row[0]) if row else 0
                    if (
                        self.cfg.live_request_limit is not None
                        and reserved + len(chosen) > self.cfg.live_request_limit
                    ):
                        continue
                    db.execute(
                        "INSERT OR REPLACE INTO system VALUES('gemini_reserved_requests',?)",
                        (str(reserved + len(chosen)),),
                    )
                cid = uid()
                db.execute(
                    "INSERT INTO chunks(id,batch_id,state,display_key,created_at) VALUES(?,?,'prepared',?,?)",
                    (cid, batch["id"], cid, timestamp()),
                )
                for row in chosen:
                    aid = uid()
                    db.execute(
                        "INSERT INTO attempts(id,job_id,chunk_id,request_key,state,send_reserved_micro,created_at) VALUES(?,?,?,?,'reserved',?,?)",
                        (aid, row["id"], cid, aid, batch["unit_reserve_micro"], timestamp()),
                    )
                    db.execute(
                        "UPDATE jobs SET state='running',active_attempt_id=? WHERE id=?", (aid, row["id"])
                    )
                db.execute(
                    "UPDATE batches SET spent_reserved_micro=spent_reserved_micro+? WHERE id=?",
                    (cost, batch["id"]),
                )
                return cid
        return None

    def requests(self, cid):
        with connect(self.cfg) as db:
            rows = db.execute(
                """SELECT a.id,a.job_id,r.snapshot_json,p.number,j.candidate_index,
                (SELECT COUNT(*) FROM attempts other WHERE other.job_id=a.job_id) attempt_number
                FROM attempts a JOIN jobs j ON j.id=a.job_id JOIN rounds r ON r.id=j.round_id
                JOIN pages p ON p.id=r.page_id WHERE a.chunk_id=? ORDER BY p.number,j.candidate_index""",
                (cid,),
            ).fetchall()
        for row in rows:
            yield {
                "key": row["id"],
                "page_number": row["number"],
                "candidate_index": row["candidate_index"],
                "attempt_number": row["attempt_number"],
                "snapshot": json.loads(row["snapshot_json"]),
            }

    def submit(self, cid):
        with connect(self.cfg) as db:
            chunk = get(db, "chunks", cid)
            if get(db, "batches", chunk["batch_id"])["paused"]:
                return
        if chunk["state"] not in ("prepared", "uploading"):
            return
        if self.cfg.provider == "gemini":
            with transaction(self.cfg) as db:
                reserved = db.execute(
                    "SELECT value FROM system WHERE key='gemini_reserved_requests'"
                ).fetchone()
                batch = get(db, "batches", chunk["batch_id"])
                if (
                    self.cfg.live_request_limit is not None
                    and reserved
                    and int(reserved[0]) > self.cfg.live_request_limit
                ) or batch["unit_reserve_micro"] < self.cfg.live_unit_micro:
                    db.execute(
                        "UPDATE chunks SET last_error='live_limit',next_poll_at=? WHERE id=?",
                        (self.clock() + 60, cid),
                    )
                    db.execute("UPDATE batches SET paused=1 WHERE id=?", (batch["id"],))
                    return
        path = self.cfg.root / "chunks" / f"{cid}.input.jsonl"
        try:
            h = self.provider.build_jsonl(path, self.requests(cid))
            with transaction(self.cfg) as db:
                db.execute(
                    "UPDATE chunks SET state='uploading',payload_path=?,payload_hash=? WHERE id=?",
                    (str(path.relative_to(self.cfg.root)), h, cid),
                )
            name = chunk["input_remote_name"] or self.provider.upload(path)
        except DomainError as exc:
            self.pre_submission_failure(cid, exc.code, terminal=True)
            return
        except OSError:
            self.pre_submission_failure(cid, "upload_unavailable")
            return
        with transaction(self.cfg) as db:
            latest = get(db, "chunks", cid)
            batch = get(db, "batches", latest["batch_id"])
            db.execute("UPDATE chunks SET input_remote_name=? WHERE id=?", (name, cid))
            if batch["paused"]:
                return
            if self.cfg.provider == "gemini":
                previous = db.execute("SELECT value FROM system WHERE key='gemini_last_submit_at'").fetchone()
                earliest = float(previous[0]) + 5 if previous else 0
                if self.clock() < earliest:
                    db.execute("UPDATE chunks SET next_poll_at=? WHERE id=?", (earliest, cid))
                    return
                db.execute(
                    "INSERT OR REPLACE INTO system VALUES('gemini_last_submit_at',?)", (str(self.clock()),)
                )
            db.execute("UPDATE chunks SET state='submitting',fetch_failures=0 WHERE id=?", (cid,))
            db.execute("UPDATE attempts SET state='submitting' WHERE chunk_id=? AND state='reserved'", (cid,))
        failpoint("before_submit")
        try:
            with connect(self.cfg) as db:
                model = json.loads(
                    db.execute(
                        "SELECT r.snapshot_json FROM attempts a JOIN jobs j ON j.id=a.job_id JOIN rounds r ON r.id=j.round_id WHERE a.chunk_id=? LIMIT 1",
                        (cid,),
                    ).fetchone()[0]
                )["settings"]["model"]
            remote = self.provider.submit(name, model, cid)
            failpoint("after_submit")
        except RateLimited:
            with transaction(self.cfg) as db:
                db.execute("UPDATE chunks SET state='failed',last_error='rate_limited' WHERE id=?", (cid,))
                for a in db.execute("SELECT * FROM attempts WHERE chunk_id=?", (cid,)).fetchall():
                    job = get(db, "jobs", a["job_id"])
                    count = db.execute(
                        "SELECT COUNT(*) FROM attempts WHERE job_id=?", (job["id"],)
                    ).fetchone()[0]
                    state = "retry_wait" if count < 3 + job["manual_allowance"] else "failed"
                    db.execute(
                        "UPDATE jobs SET state=?,retry_after=? WHERE id=? AND active_attempt_id=?",
                        (
                            state,
                            self.clock() + 30 * 2 ** (count - 1) + random.uniform(0, 10),
                            job["id"],
                            a["id"],
                        ),
                    )
                    db.execute(
                        "UPDATE attempts SET state='retryable_failed',error_code='rate_limited' WHERE id=?",
                        (a["id"],),
                    )
                release_unaccepted(self.cfg, db, cid, "rate_limited")
            return
        except ProviderRejected as exc:
            with transaction(self.cfg) as db:
                db.execute("UPDATE chunks SET state='failed',last_error=? WHERE id=?", (exc.code, cid))
                db.execute(
                    "UPDATE attempts SET state='terminal_failed',error_code=? WHERE chunk_id=?",
                    (exc.code, cid),
                )
                db.execute(
                    "UPDATE jobs SET state='failed' WHERE active_attempt_id IN (SELECT id FROM attempts WHERE chunk_id=?)",
                    (cid,),
                )
                db.execute("UPDATE batches SET paused=1 WHERE id=?", (chunk["batch_id"],))
                release_unaccepted(self.cfg, db, cid, "provider_rejected")
            return
        except (SubmissionUnknown, OSError):
            self.unknown(cid, "submission_unknown")
            return
        self.attach(cid, remote)

    def pre_submission_failure(self, cid, code, terminal=False):
        with transaction(self.cfg) as db:
            chunk = get(db, "chunks", cid)
            failures = chunk["fetch_failures"] + 1
            terminal = terminal or failures >= 3
            db.execute(
                "UPDATE chunks SET state=?,fetch_failures=?,last_error=?,next_poll_at=? WHERE id=?",
                (
                    "failed" if terminal else "uploading",
                    failures,
                    code,
                    self.clock() + 30 * 2 ** (failures - 1),
                    cid,
                ),
            )
            if terminal:
                db.execute(
                    "UPDATE attempts SET state='terminal_failed',error_code=? WHERE chunk_id=?", (code, cid)
                )
                db.execute(
                    "UPDATE jobs SET state='failed' WHERE active_attempt_id IN (SELECT id FROM attempts WHERE chunk_id=?)",
                    (cid,),
                )
                release_unaccepted(self.cfg, db, cid, "before_submit")

    def unknown(self, cid, reason):
        with transaction(self.cfg) as db:
            db.execute(
                "UPDATE chunks SET state='unknown',last_error=?,next_poll_at=? WHERE id=?",
                (reason, self.clock() + 60, cid),
            )
            db.execute(
                "UPDATE jobs SET state='unknown' WHERE active_attempt_id IN (SELECT id FROM attempts WHERE chunk_id=? AND state!='succeeded')",
                (cid,),
            )
            db.execute(
                "UPDATE attempts SET state='unknown',error_code=? WHERE chunk_id=? AND state NOT IN ('succeeded','abandoned')",
                (reason, cid),
            )

    def attach(self, cid, remote):
        with transaction(self.cfg) as db:
            if self.cfg.provider == "gemini":
                db.execute("INSERT OR REPLACE INTO system VALUES('gemini_connection_verified','1')")
            db.execute(
                "UPDATE chunks SET state='submitted',remote_name=?,next_poll_at=0,last_error=NULL WHERE id=?",
                (remote, cid),
            )
            db.execute(
                "UPDATE jobs SET state='running' WHERE active_attempt_id IN (SELECT id FROM attempts WHERE chunk_id=? AND state NOT IN ('succeeded','abandoned'))",
                (cid,),
            )
            db.execute(
                "UPDATE attempts SET state='submitted' WHERE chunk_id=? AND state NOT IN ('succeeded','abandoned')",
                (cid,),
            )

    def reconcile(self, chunk):
        try:
            matches = self.provider.find(chunk["display_key"])
        except OSError:
            self.unknown(chunk["id"], "reconcile_unavailable")
            return
        if len(matches) == 1:
            self.attach(chunk["id"], matches[0])
        else:
            self.unknown(chunk["id"], "no_match" if not matches else "multiple_matches")

    def ingest(self, cid, path):
        # Pass 1 validates the complete key set before ANY candidate commit.
        counts = Counter()
        with path.open() as f:
            for line in f:
                try:
                    row = self.provider.parse_line(line)
                    if isinstance(row.get("key"), str):
                        counts[row["key"]] += 1
                except (ValueError, AttributeError):
                    continue
        with connect(self.cfg) as db:
            attempts = {
                r["request_key"]: dict(r)
                for r in db.execute("SELECT * FROM attempts WHERE chunk_id=?", (cid,))
            }
        bad = {key for key in attempts if counts[key] != 1}
        for key in bad:
            with transaction(self.cfg) as db:
                a = get(db, "attempts", key)
                if a["state"] not in ("succeeded", "abandoned"):
                    db.execute(
                        "UPDATE attempts SET state='unknown',error_code='result_keys' WHERE id=?", (key,)
                    )
                    db.execute("UPDATE jobs SET state='unknown' WHERE active_attempt_id=?", (key,))
        with path.open() as f:
            for line in f:
                try:
                    result = self.provider.parse_line(line)
                except ValueError:
                    continue
                key = result.get("key") if isinstance(result, dict) else None
                if key not in attempts or key in bad:
                    continue
                with connect(self.cfg) as db:
                    current = get(db, "attempts", key)
                    job = get(db, "jobs", current["job_id"])
                if current["state"] in ("abandoned", "succeeded") or job["active_attempt_id"] != key:
                    continue
                error = result.get("error")
                images = result.get("images", [])
                if not error and (not isinstance(images, list) or len(images) != 1):
                    error = "no_image" if not images else "multiple_images"
                if not error:
                    try:
                        raw = base64.b64decode(images[0], validate=True)
                        assets.decode(raw)
                    except (ValueError, TypeError, DomainError):
                        error = "invalid_image"
                if error:
                    with transaction(self.cfg) as db:
                        db.execute(
                            "UPDATE attempts SET state='terminal_failed',error_code=?,usage_json=? WHERE id=?",
                            (str(error)[:200], canonical(result.get("usage")), key),
                        )
                        db.execute("UPDATE jobs SET state='failed' WHERE active_attempt_id=?", (key,))
                    continue
                with transaction(self.cfg) as db:
                    raw_h, display, thumb = assets.candidate_assets(self.cfg, db, raw)
                    # Sidecar is durable before candidate DB commit, allowing orphan recovery on restart.
                    atomic_write(
                        self.cfg.root / "chunks" / f"{key}.candidate.json",
                        canonical(
                            {"attempt_id": key, "raw": raw_h, "display": display, "thumbnail": thumb}
                        ).encode(),
                    )
                    failpoint("after_rename")
                    db.execute(
                        "INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?,?)",
                        (uid(), current["job_id"], key, display, thumb, raw_h, "available", timestamp()),
                    )
                    db.execute(
                        "UPDATE candidates SET availability='available' WHERE job_id=?", (current["job_id"],)
                    )
                    db.execute(
                        "UPDATE attempts SET state='succeeded',usage_json=? WHERE id=?",
                        (canonical(result.get("usage")), key),
                    )
                    db.execute("UPDATE jobs SET state='succeeded' WHERE active_attempt_id=?", (key,))
        with transaction(self.cfg) as db:
            db.execute(
                "UPDATE chunks SET state=?,last_error=?,next_poll_at=? WHERE id=?",
                ("unknown" if bad else "ingested", "result_keys" if bad else None, self.clock() + 60, cid),
            )

    def collect(self, chunk):
        try:
            status = self.provider.poll(chunk["remote_name"])
            if status["state"] == "terminal_unknown":
                self.unknown(chunk["id"], status.get("error", "batch_terminal_unknown"))
                return
            if status["state"] != "complete":
                with transaction(self.cfg) as db:
                    db.execute(
                        "UPDATE chunks SET next_poll_at=? WHERE id=?",
                        (self.clock() + self.cfg.poll_interval, chunk["id"]),
                    )
                return
            target = self.cfg.root / "chunks" / f"{chunk['id']}.result.jsonl"
            temp = target.with_suffix(".download")
            self.provider.fetch(status["result"], temp)
            temp.replace(target)
            with transaction(self.cfg) as db:
                db.execute(
                    "UPDATE chunks SET state='fetching',output_path=? WHERE id=?",
                    (str(target.relative_to(self.cfg.root)), chunk["id"]),
                )
            failpoint("after_download")
            self.ingest(chunk["id"], target)
        except (OSError, ValueError) as e:
            with transaction(self.cfg) as db:
                failures = get(db, "chunks", chunk["id"])["fetch_failures"] + 1
                db.execute(
                    "UPDATE chunks SET fetch_failures=?,next_poll_at=?,last_error=?,state=? WHERE id=?",
                    (
                        failures,
                        self.clock() + 30 * 2 ** (min(failures, 3) - 1),
                        type(e).__name__,
                        "failed" if failures >= 3 else "submitted",
                        chunk["id"],
                    ),
                )

    def tick(self):
        self.heartbeat()
        if time.time() - self.last_cleanup > 3600:
            from .maintenance import cleanup_staging

            cleanup_staging(self.cfg)
            self.last_cleanup = time.time()
        with connect(self.cfg) as db:
            chunks = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM chunks WHERE state NOT IN ('ingested','failed') ORDER BY created_at"
                )
            ]
        for chunk in chunks:
            with connect(self.cfg) as db:
                row = db.execute(
                    "SELECT r.snapshot_json FROM attempts a JOIN jobs j ON j.id=a.job_id JOIN rounds r ON r.id=j.round_id WHERE a.chunk_id=? LIMIT 1",
                    (chunk["id"],),
                ).fetchone()
                if row and json.loads(row[0])["settings"]["provider"] != self.cfg.provider:
                    continue
            if chunk["state"] in ("submitting", "unknown"):
                if (
                    chunk["last_error"] != "result_keys"
                    and not (chunk["last_error"] or "").startswith("gemini_batch_")
                    and chunk["next_poll_at"] <= self.clock()
                ):
                    self.reconcile(chunk)
            elif chunk["state"] in ("prepared", "uploading") and chunk["next_poll_at"] <= self.clock():
                self.submit(chunk["id"])
            elif chunk["state"] == "fetching" and chunk["output_path"]:
                self.ingest(chunk["id"], self.cfg.root / chunk["output_path"])
            elif chunk["state"] == "submitted" and chunk["next_poll_at"] <= self.clock():
                self.collect(chunk)
        for _ in range(self.cfg.max_external):
            cid = self.prepare()
            if not cid:
                break
            self.submit(cid)
        from .export import process_export, sync_rejected

        process_export(self.cfg)
        try:
            sync_rejected(self.cfg)
        except (DomainError, OSError, ValueError) as exc:
            with transaction(self.cfg) as db:
                db.execute(
                    "INSERT OR REPLACE INTO system VALUES('rejected_sync_error',?)", (type(exc).__name__,)
                )

    def check_assets(self):
        with transaction(self.cfg) as db:
            for c in db.execute("SELECT * FROM candidates WHERE availability='available'").fetchall():
                missing = False
                for field in ("asset_hash", "raw_asset_hash", "thumbnail_hash"):
                    try:
                        path = assets.path_for(self.cfg, db, c[field])
                        if hashlib.sha256(path.read_bytes()).hexdigest() != c[field]:
                            missing = True
                    except DomainError:
                        missing = True
                if missing:
                    db.execute("UPDATE candidates SET availability='unavailable' WHERE id=?", (c["id"],))
                    db.execute("UPDATE jobs SET state='failed' WHERE id=?", (c["job_id"],))
                    chunk = db.execute(
                        "SELECT chunk_id FROM attempts WHERE id=?", (c["attempt_id"],)
                    ).fetchone()
                    if chunk:
                        db.execute(
                            "UPDATE chunks SET state='submitted',next_poll_at=0 WHERE id=?", (chunk[0],)
                        )
                        db.execute("UPDATE attempts SET state='submitted' WHERE id=?", (c["attempt_id"],))
            # Orphan files are re-created from the durable result JSONL on the next tick.

    def run(self, once=False):
        lock = (self.cfg.root / "worker.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise DomainError("worker_running", "ワーカーはすでに起動しています。") from None
        stop = threading.Event()

        def beat():
            while not stop.wait(5):
                self.heartbeat()

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            reclaim_rejected(self.cfg)
            self.check_assets()
            while True:
                self.tick()
                if once:
                    break
                stop.wait(0.25)
        finally:
            stop.set()
            thread.join(timeout=6)
            lock.close()


def resolve(cfg, cid, request, key):
    provider = create_provider(cfg)
    with connect(cfg) as read_db:
        prior = receipt(read_db, f"resolve:{cid}", key, request)
        if prior:
            return prior
        original = get(read_db, "chunks", cid)
        source = read_db.execute(
            "SELECT r.snapshot_json FROM attempts a JOIN jobs j ON j.id=a.job_id JOIN rounds r ON r.id=j.round_id WHERE a.chunk_id=? LIMIT 1",
            (cid,),
        ).fetchone()
        require(
            source and json.loads(source[0])["settings"]["provider"] == cfg.provider,
            "provider_mismatch",
            "同じAPI設定で処理を照会してください。",
            422,
        )
    matches, remote = [], None
    if request.get("action") in ("attach", "reconcile"):
        matches = (
            [request.get("remote_name")]
            if request["action"] == "attach"
            else provider.find(original["display_key"])
        )
        if len(matches) == 1 and matches[0]:
            remote = provider.describe(matches[0])
    with transaction(cfg) as db:
        prior = receipt(db, f"resolve:{cid}", key, request)
        if prior:
            return prior
        chunk = get(db, "chunks", cid)
        require(chunk["state"] == request.get("expected_state"))
        action = request.get("action")
        if action == "resend":
            require(
                chunk["state"] == "unknown" and request.get("explicit_duplicate_risk"),
                "duplicate_risk",
                "成否不明の処理を再送する場合は重複の可能性を確認してください。",
                422,
            )
            for a in db.execute(
                "SELECT * FROM attempts WHERE chunk_id=? AND state='unknown'", (cid,)
            ).fetchall():
                db.execute("UPDATE attempts SET state='abandoned' WHERE id=?", (a["id"],))
                db.execute(
                    "UPDATE jobs SET state='queued' WHERE active_attempt_id=? AND state='unknown'", (a["id"],)
                )
            db.execute("UPDATE chunks SET state='failed',last_error='explicit_resend' WHERE id=?", (cid,))
        elif action in ("attach", "reconcile"):
            require(len(matches) == 1 and matches[0], "unknown", "外部処理を一意に確認できません。", 422)
            require(
                remote
                and remote["display_key"] == chunk["display_key"]
                and remote["model"] == json.loads(source[0])["settings"]["model"],
                "remote_mismatch",
                "外部処理の条件が一致しません。",
                422,
            )
            db.execute(
                "UPDATE chunks SET state='submitted',remote_name=?,next_poll_at=0 WHERE id=?",
                (matches[0], cid),
            )
            db.execute(
                "UPDATE jobs SET state='running' WHERE active_attempt_id IN (SELECT id FROM attempts WHERE chunk_id=? AND state='unknown')",
                (cid,),
            )
            db.execute("UPDATE attempts SET state='submitted' WHERE chunk_id=? AND state='unknown'", (cid,))
        elif action == "refetch":
            require(chunk["remote_name"], "remote_missing", "外部IDがありません。", 422)
            db.execute(
                "UPDATE chunks SET state='submitted',next_poll_at=0,fetch_failures=0 WHERE id=?", (cid,)
            )
        else:
            raise DomainError("action", "不明な解決操作です。")
        batch = get(db, "batches", chunk["batch_id"])
        db.execute(
            "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
            (uid(), batch["project_id"], action, cid, canonical(chunk), canonical(request), timestamp()),
        )
        result = {"chunk_id": cid}
        save_receipt(db, f"resolve:{cid}", key, request, result)
        return result
