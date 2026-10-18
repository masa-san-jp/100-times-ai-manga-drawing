from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import Field

from . import assets, planning, projects
from .common import DomainError, canonical, require, timestamp, uid
from .config import Config
from .db import connect, get, initialize, transaction
from .models import Character, Scene, Strict


class ImportValidation(Strict):
    manifest: dict
    project_id: str | None = None


class ImportCommit(Strict):
    work_id: str | None = None
    expected_version: int | None = None


class Plan(Strict):
    kind: Literal["initial", "revision", "extra"] = "initial"
    page_ids: list[str] | None = None
    candidate_count: int = Field(default=1, ge=1, le=3)
    settings: dict = Field(default_factory=dict)
    unit_reserve_micro: int = Field(default=1, gt=0)
    cap_micro: int | None = Field(default=None, ge=0)


class Start(Strict):
    plan_token: str


class Edit(Strict):
    expected_version: int
    patch: dict


class Review(Strict):
    expected_version: int
    latest_round_id: str
    decision: Literal["accept", "reject"]
    candidate_id: str | None = None
    reason: str = Field(default="", max_length=20000)
    restore: bool = False
    clear_accepted: bool = False


class Revise(Strict):
    expected_version: int
    latest_round_id: str
    draft_patch: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=20000)
    candidate_count: int = Field(default=1, ge=1, le=3)
    clear_accepted: bool = False
    unit_reserve_micro: int = Field(default=1, gt=0)
    cap_micro: int | None = Field(default=None, ge=0)


class Pause(Strict):
    paused: bool


class Budget(Strict):
    expected_cap: int
    new_cap: int = Field(ge=0)


class Retry(Strict):
    expected_state: str
    explicit_extra_attempt: bool = False


class Resolve(Strict):
    expected_state: str
    action: Literal["reconcile", "attach", "resend", "refetch"]
    remote_name: str | None = None
    explicit_duplicate_risk: bool = False


class ExportRequest(Strict):
    mode: Literal["complete", "accepted_only"]
    expected_version: int


def health(cfg):
    with connect(cfg) as db:
        heartbeat_row = db.execute("SELECT value FROM system WHERE key='heartbeat'").fetchone()
    reserved = 0
    verified = False
    with connect(cfg) as db:
        row = db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()
        reserved = int(row[0]) if row else 0
        verified_row = db.execute(
            "SELECT value FROM system WHERE key='gemini_connection_verified'"
        ).fetchone()
        verified = bool(verified_row and verified_row[0] == "1")
        pending = db.execute(
            "SELECT COUNT(*) FROM jobs j JOIN rounds r ON r.id=j.round_id WHERE j.state IN ('queued','retry_wait') AND json_extract(r.snapshot_json,'$.settings.provider')='gemini'"
        ).fetchone()[0]
    return {
        "provider": cfg.provider,
        "live_enabled": cfg.enable_live,
        "live_remaining_requests": max(0, cfg.live_request_limit - reserved),
        "live_queued_requests": pending,
        "live_limit_wait": cfg.provider == "gemini" and pending > max(0, cfg.live_request_limit - reserved),
        "real_api_connected": cfg.provider == "gemini" and verified,
        "live_connection_verified": cfg.provider == "gemini" and verified,
        "worker_running": bool(heartbeat_row and time.time() - float(heartbeat_row[0]) < 30),
    }


def create_app(cfg=None):
    cfg = cfg or Config.from_env()
    initialize(cfg)
    app = FastAPI(title="マンガ下書きバッチ", version="0.2.0")
    app.state.cfg = cfg
    package = Path(__file__).parent
    templates = Jinja2Templates(directory=package / "templates")
    templates.env.globals["runtime_provider"] = cfg.provider
    app.mount("/static", StaticFiles(directory=package / "static"), name="static")

    def csrf(session):
        return hmac.new(cfg.secret.encode(), session.encode(), hashlib.sha256).hexdigest()

    @app.middleware("http")
    async def security(request, call_next):
        host = request.headers.get("host", "")
        try:
            hostname = urlparse("http://" + host).hostname
        except ValueError:
            hostname = None
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"error": {"code": "host", "message": "localhost専用です。"}}, 403)
        session = request.cookies.get("manga_session", "")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            supplied = request.headers.get("x-csrf-token", "")
            if (
                (origin is not None and origin not in {"http://" + host, "https://" + host})
                or not session
                or not hmac.compare_digest(supplied, csrf(session))
            ):
                return JSONResponse(
                    {"error": {"code": "csrf", "message": "画面を再読み込みしてください。"}}, 403
                )
        response = await call_next(request)
        if not session:
            session = secrets.token_urlsafe(32)
            response.set_cookie("manga_session", session, httponly=True, samesite="strict")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        response.headers["Cache-Control"] = (
            "no-store" if not request.url.path.startswith("/static/") else "public, max-age=3600"
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message, "fields": exc.fields, "retryable": False}},
            exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "validation",
                    "message": "入力内容を確認してください。",
                    "fields": str(exc),
                    "retryable": False,
                }
            },
            422,
        )

    @app.get("/api/session")
    def session(request: Request):
        value = request.cookies.get("manga_session", "")
        return {"data": {"csrf": csrf(value) if value else None}}

    @app.get("/api/health")
    def health_api():
        return {"data": health(cfg)}

    @app.get("/")
    def home(request: Request, work: str | None = None):
        with connect(cfg) as db:
            items = [
                dict(r)
                for r in db.execute("""SELECT p.*, w.title work_title,
                (SELECT COUNT(*) FROM pages WHERE project_id=p.id) page_count,
                (SELECT COUNT(*) FROM pages WHERE project_id=p.id AND accepted_candidate_id IS NOT NULL) accepted_count
                FROM projects p JOIN works w ON w.id=p.work_id ORDER BY w.created_at DESC,p.created_at""")
            ]
            works = [dict(r) for r in db.execute("SELECT * FROM works ORDER BY created_at DESC")]
            if work:
                get(db, "works", work)
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={"projects": items, "project": None, "works": works, "selected_work": work},
        )

    @app.get("/projects/{pid}/settings")
    def settings(request: Request, pid: str):
        with connect(cfg) as db:
            project = get(db, "projects", pid)
            project["work_title"] = get(db, "works", project["work_id"])["title"]
            works = [dict(r) for r in db.execute("SELECT * FROM works ORDER BY created_at DESC")]
            project["references"] = projects.references(db, pid)
            project["settings"] = json.loads(project["settings_json"])
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={"project": project, "projects": [], "works": works},
        )

    @app.get("/projects/{pid}")
    def listing(request: Request, pid: str):
        with connect(cfg) as db:
            project = get(db, "projects", pid)
            latest_export = db.execute(
                "SELECT id FROM exports WHERE project_id=? AND state='complete' "
                "AND archive_path NOT LIKE '%.zip' ORDER BY created_at DESC LIMIT 1",
                (pid,),
            ).fetchone()
        return templates.TemplateResponse(
            request=request,
            name="pages.html",
            context={"project": project, "latest_export": latest_export[0] if latest_export else None},
        )

    @app.get("/pages/{page_id}")
    def detail(request: Request, page_id: str):
        with connect(cfg) as db:
            page = projects.page_detail(db, page_id)
            project = get(db, "projects", page["project_id"])
            previous_page = db.execute(
                "SELECT id,number FROM pages WHERE project_id=? AND number<? ORDER BY number DESC LIMIT 1",
                (page["project_id"], page["number"]),
            ).fetchone()
            next_page = db.execute(
                "SELECT id,number FROM pages WHERE project_id=? AND number>? ORDER BY number LIMIT 1",
                (page["project_id"], page["number"]),
            ).fetchone()
        return templates.TemplateResponse(
            request=request,
            name="detail.html",
            context={
                "page": page,
                "project": project,
                "previous_page": previous_page,
                "next_page": next_page,
            },
        )

    @app.get("/api/projects")
    def all_projects():
        with connect(cfg) as db:
            return {"data": [dict(x) for x in db.execute("SELECT * FROM projects ORDER BY created_at DESC")]}

    @app.post("/api/imports", status_code=201)
    def new_import():
        return {"data": {"token": projects.create_import(cfg)}}

    @app.put("/api/imports/{token}/files")
    async def put_file(token: str, path: str = Form(), file: UploadFile = File()):
        # Bound a single file. Total manifest ingestion never loads all images at once.
        content = bytearray()
        while block := await file.read(1024 * 1024):
            content.extend(block)
            if len(content) > assets.MAX_FILE:
                raise DomainError("file_size", "画像は20MiB以下にしてください。", 413)
        projects.upload(cfg, token, path, bytes(content))
        return {"data": {"path": path}}

    @app.post("/api/imports/{token}/validate")
    def validate(token: str, body: ImportValidation):
        try:
            return {"data": projects.validate_import(cfg, token, body.manifest, body.project_id)}
        except ValueError as e:
            raise DomainError("manifest", str(e)) from e

    @app.post("/api/imports/{token}/commit", status_code=201)
    def commit(token: str, body: ImportCommit):
        return {"data": projects.commit_import(cfg, token, body.expected_version, body.work_id)}

    @app.get("/api/projects/{pid}/pages")
    def pages(pid: str, status: str | None = None, cursor: int = 0, limit: int = 40):
        require(0 <= cursor and 1 <= limit <= 100, "pagination", "一覧の範囲が不正です。", 422)
        with connect(cfg) as db:
            get(db, "projects", pid)
            return {"data": projects.page_list(db, pid, status, cursor, limit)}

    @app.get("/api/pages/{page_id}")
    def page_api(page_id: str):
        with connect(cfg) as db:
            return {"data": projects.page_detail(db, page_id)}

    @app.get("/api/pages/{page_id}/history")
    def history(page_id: str, before: int = 2**31, limit: int = 20):
        require(1 <= limit <= 100, "pagination", "範囲が不正です。", 422)
        with connect(cfg) as db:
            get(db, "pages", page_id)
            rounds = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM rounds WHERE page_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?",
                    (page_id, before, limit),
                )
            ]
            for r in rounds:
                r["candidates"] = [
                    dict(c)
                    for c in db.execute(
                        "SELECT c.*,json_extract(r.snapshot_json,'$.settings.provider') provider FROM candidates c JOIN jobs j ON j.id=c.job_id JOIN rounds r ON r.id=j.round_id WHERE j.round_id=?",
                        (r["id"],),
                    )
                ]
            return {"data": rounds}

    @app.patch("/api/pages/{page_id}/draft")
    def draft(page_id: str, body: Edit):
        with transaction(cfg) as db:
            page = get(db, "pages", page_id)
            require(page["version"] == body.expected_version)
            projects.patch_draft(db, page, body.patch)
            return {"data": projects.page_detail(db, page_id)}

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, body: Edit):
        require(
            set(body.patch) <= {"title", "settings", "work_id", "work_title", "lifecycle"},
            "fields",
            "未定義の設定です。",
            422,
        )
        with transaction(cfg) as db:
            project = get(db, "projects", pid)
            require(project["version"] == body.expected_version)
            work_id = body.patch.get("work_id", project["work_id"])
            get(db, "works", work_id)
            lifecycle = body.patch.get("lifecycle", project["lifecycle"])
            require(
                lifecycle in ("active", "completed", "archived"), "lifecycle", "制作状態が不正です。", 422
            )
            if lifecycle == "completed":
                require(
                    not db.execute(
                        "SELECT 1 FROM pages WHERE project_id=? AND accepted_candidate_id IS NULL", (pid,)
                    ).fetchone(),
                    "unfinished",
                    "未採用のページがあります。完了にする前に採否を確認してください。",
                    422,
                )
                require(
                    not db.execute(
                        "SELECT 1 FROM jobs j JOIN rounds r ON r.id=j.round_id JOIN pages p ON p.id=r.page_id WHERE p.project_id=? AND j.state IN ('queued','running','retry_wait','unknown')",
                        (pid,),
                    ).fetchone(),
                    "unfinished",
                    "処理中または結果未確認の生成があります。",
                    422,
                )
            work_title = body.patch.get("work_title")
            if work_title is not None and work_id == project["work_id"]:
                require(
                    isinstance(work_title, str) and 0 < len(work_title.strip()) <= 200,
                    "title",
                    "作品名を入力してください。",
                    422,
                )
                current_work = get(db, "works", work_id)
                if current_work["title"] != work_title.strip():
                    db.execute("UPDATE works SET title=? WHERE id=?", (work_title.strip(), work_id))
                    db.execute(
                        "UPDATE projects SET version=version+1 WHERE work_id=? AND id!=?", (work_id, pid)
                    )
            db.execute("UPDATE projects SET work_id=?,lifecycle=? WHERE id=?", (work_id, lifecycle, pid))
            settings = json.loads(project["settings_json"]) | body.patch.get("settings", {})
            # Real provider may be saved as disabled configuration, but cannot plan/start.
            require(
                set(settings) <= {"provider", "model", "width", "height", "scenario"},
                "settings",
                "未定義の設定です。",
                422,
            )
            for field in ("width", "height"):
                require(
                    type(settings.get(field, 1)) is int and 1 <= settings.get(field, 1) <= 5000,
                    "dimensions",
                    "寸法は1〜5000pxです。",
                    422,
                )
            title = body.patch.get("title", project["title"])
            require(
                isinstance(title, str) and 0 < len(title) <= 200, "title", "作品名を入力してください。", 422
            )
            db.execute(
                "UPDATE projects SET title=?,settings_json=?,version=version+1 WHERE id=?",
                (title, canonical(settings), pid),
            )
            return {"data": get(db, "projects", pid)}

    @app.patch("/api/references/{rid}")
    def patch_reference(rid: str, body: dict):
        require(set(body) <= {"expected_revision", "metadata", "images"}, "fields", "未定義の項目です。", 422)
        with transaction(cfg) as db:
            ref = get(db, "reference_sets", rid)
            require(ref["current_revision_id"] == body.get("expected_revision"))
            revision = get(db, "reference_revisions", ref["current_revision_id"])
            metadata = json.loads(revision["metadata_json"]) | body.get("metadata", {})
            images = body.get("images", json.loads(revision["asset_hashes_json"]))
            require(
                isinstance(images, list) and len(images) <= (1 if ref["kind"] == "character" else 7),
                "images",
                "画像の枚数が不正です。",
                422,
            )
            try:
                validated = (Character if ref["kind"] == "character" else Scene).model_validate(
                    metadata | {"images": images}
                )
            except ValueError as e:
                raise DomainError("reference", str(e)) from e
            require(
                validated.key == ref["stable_key"],
                "reference_key",
                "人物やシーンのkeyは変更できません。",
                422,
            )
            metadata = validated.model_dump(exclude={"images"})
            for h in images:
                assets.path_for(cfg, db, h)
            ident = uid()
            db.execute(
                "INSERT INTO reference_revisions VALUES(?,?,?,?,?)",
                (ident, rid, canonical(metadata), canonical(images), timestamp()),
            )
            db.execute("UPDATE reference_sets SET current_revision_id=? WHERE id=?", (ident, rid))
            db.execute("UPDATE projects SET version=version+1 WHERE id=?", (ref["project_id"],))
            return {"data": {"revision_id": ident}}

    @app.post("/api/projects/{pid}/plans")
    def plan(pid: str, body: Plan):
        with transaction(cfg) as db:
            return {
                "data": planning.make_plan(
                    cfg, db, pid, body.model_dump(exclude_none=True, exclude_unset=True)
                )
            }

    @app.post("/api/projects/{pid}/batches", status_code=202)
    def start(pid: str, body: Start, idempotency_key: str = Header()):
        return {"data": planning.start(cfg, pid, body.plan_token, idempotency_key)}

    @app.post("/api/pages/{page_id}/review")
    def review(page_id: str, body: Review):
        return {"data": planning.review(cfg, page_id, body.model_dump())}

    @app.post("/api/pages/{page_id}/revise-and-generate", status_code=202)
    def revise(page_id: str, body: Revise, idempotency_key: str = Header()):
        return {
            "data": planning.revise_and_generate(
                cfg, page_id, body.model_dump(exclude_none=True, exclude_unset=True), idempotency_key
            )
        }

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str, body: Retry, idempotency_key: str = Header()):
        return {"data": planning.retry(cfg, job_id, body.model_dump(), idempotency_key)}

    @app.post("/api/batches/{bid}/pause")
    def pause(bid: str, body: Pause):
        with transaction(cfg) as db:
            get(db, "batches", bid)
            db.execute("UPDATE batches SET paused=? WHERE id=?", (int(body.paused), bid))
        return {"data": {"paused": body.paused}}

    @app.patch("/api/batches/{bid}/budget")
    def budget(bid: str, body: Budget):
        with transaction(cfg) as db:
            batch = get(db, "batches", bid)
            require(batch["cap_micro"] == body.expected_cap and body.new_cap >= batch["spent_reserved_micro"])
            plan_settings = json.loads(batch["plan_json"])["pages"][0]["snapshot"]["settings"]
            if plan_settings["provider"] == "gemini":
                require(
                    body.new_cap <= cfg.live_request_limit * batch["unit_reserve_micro"],
                    "live_limit",
                    "起動時の送信上限を超える枠は設定できません。",
                    422,
                )
            db.execute("UPDATE batches SET cap_micro=? WHERE id=?", (body.new_cap, bid))
            db.execute(
                "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)",
                (
                    uid(),
                    batch["project_id"],
                    "budget",
                    bid,
                    canonical({"cap": batch["cap_micro"]}),
                    canonical({"cap": body.new_cap}),
                    timestamp(),
                ),
            )
        return {"data": {"cap_micro": body.new_cap}}

    @app.post("/api/chunks/{cid}/resolve")
    def resolve(cid: str, body: Resolve, idempotency_key: str = Header()):
        from .worker import resolve

        return {"data": resolve(cfg, cid, body.model_dump(), idempotency_key)}

    @app.get("/api/projects/{pid}/progress")
    def progress(pid: str):
        with connect(cfg) as db:
            project = get(db, "projects", pid)
            counts = {}
            for row in db.execute("SELECT id FROM pages WHERE project_id=?", (pid,)):
                status = projects.page_detail(db, row[0])["status"]
                counts[status] = counts.get(status, 0) + 1
            batches = [
                dict(r)
                for r in db.execute(
                    "SELECT id,cap_micro,unit_reserve_micro,spent_reserved_micro,paused FROM batches WHERE project_id=? ORDER BY created_at DESC",
                    (pid,),
                )
            ]
            chunks = [
                dict(r)
                for r in db.execute(
                    "SELECT c.id,c.state,c.last_error,c.remote_name FROM chunks c JOIN batches b ON b.id=c.batch_id WHERE b.project_id=? AND c.state IN ('unknown','failed')",
                    (pid,),
                )
            ]
        return {
            "data": {
                "counts": counts,
                "batches": batches,
                "chunks": chunks,
                "project_version": project["version"],
                **health(cfg),
            }
        }

    @app.post("/api/projects/{pid}/exports", status_code=202)
    def export(pid: str, body: ExportRequest):
        from .export import schedule

        return {"data": schedule(cfg, pid, body.model_dump())}

    @app.get("/api/exports/{eid}")
    def export_status(eid: str):
        from .export import output_folder

        with connect(cfg) as db:
            row = get(db, "exports", eid)
        folder = (
            output_folder(cfg, row)
            if row["state"] == "complete" and not row["archive_path"].endswith(".zip")
            else None
        )
        return {
            "data": {
                "state": row["state"],
                "error": row["error"],
                "folder_path": str(folder) if folder else None,
                "open_url": f"/api/exports/{eid}/open" if folder else None,
            }
        }

    @app.post("/api/exports/{eid}/open")
    def open_export(eid: str):
        from .export import open_folder

        with connect(cfg) as db:
            row = get(db, "exports", eid)
        return {"data": {"folder_path": str(open_folder(cfg, row))}}

    @app.get("/api/exports/{eid}/download")
    def download(eid: str):
        with connect(cfg) as db:
            row = get(db, "exports", eid)
        require(row["state"] == "complete", "not_ready", "書き出しが完了していません。", 409)
        require(
            row["archive_path"].endswith(".zip"), "folder_export", "画像フォルダーを開いてください。", 409
        )
        return FileResponse(cfg.root / row["archive_path"], filename="manga-pages.zip")

    @app.get("/api/assets/{hash_}")
    def asset(hash_: str):
        with connect(cfg) as db:
            path = assets.path_for(cfg, db, hash_)
            mime = db.execute("SELECT mime FROM assets WHERE sha256=?", (hash_,)).fetchone()[0]
        return FileResponse(path, media_type=mime)

    @app.get("/api/pages/{page_id}/prompt")
    def preview(page_id: str):
        from .prompts import snapshot

        with connect(cfg) as db:
            snap, hash_, _ = snapshot(cfg, db, get(db, "pages", page_id))
        return {"data": {"prompt": snap["prompt"], "attachments": snap["attachments"], "hash": hash_}}

    return app
