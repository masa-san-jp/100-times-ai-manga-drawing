from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import shutil
import sqlite3
import tempfile
import time

from .db import connect


def live_check(cfg):
    """Read-only T14 readiness check; never constructs an SDK client or initializes storage."""
    try:
        sdk_version = version("google-genai")
    except PackageNotFoundError:
        sdk_version = None
    reserved = 0
    active_jobs = 0
    database_readable = True
    database = cfg.root / "db.sqlite3"
    if database.exists():
        try:
            db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                row = db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()
                reserved = int(row[0]) if row else 0
                active_jobs = db.execute(
                    "SELECT COUNT(*) FROM jobs j JOIN rounds r ON r.id=j.round_id "
                    "WHERE j.state IN ('queued','running','retry_wait','unknown') "
                    "AND json_extract(r.snapshot_json,'$.settings.provider')='gemini'"
                ).fetchone()[0]
            finally:
                db.close()
            database_readable = reserved >= 0
        except (sqlite3.Error, ValueError):
            database_readable = False
    checks = {
        "MANGA_PROVIDER=gemini": cfg.provider == "gemini",
        "MANGA_ENABLE_LIVE=1": cfg.enable_live,
        "GEMINI_API_KEY": bool(cfg.gemini_api_key),
        "MANGA_LIVE_MAX_REQUESTS": cfg.live_request_limit > 0,
        "MANGA_LIVE_UNIT_RESERVE_MICRO_USD": cfg.live_unit_micro > 0,
        "google-genai": sdk_version is not None,
        "database_readable": database_readable,
        "no_existing_live_jobs": database_readable and active_jobs == 0,
        "remaining_requests_at_least_3": database_readable and cfg.live_request_limit - reserved >= 3,
    }
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "missing": [name for name, ok in checks.items() if not ok],
        "required_requests": 3,
        "reserved_requests": reserved,
        "existing_live_jobs": active_jobs,
        "sdk_version": sdk_version,
        "data_dir": str(cfg.root),
        "api_requests_made": 0,
        "scope": "local_configuration_only; credentials_and_model_not_verified",
    }


def cleanup_staging(cfg, now=None):
    now = time.time() if now is None else now
    removed = 0
    with cfg.write_lock():
        for path in (cfg.root / "staging").iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                meta = json.loads((path / "meta.json").read_text())
            except (ValueError, FileNotFoundError):
                continue
            if now - meta["created"] >= 86400:
                shutil.rmtree(path)
                removed += 1
    return removed


def doctor(cfg):
    from .web import health

    report = health(cfg) | {"data_dir": str(cfg.root), "issues": []}
    try:
        with tempfile.TemporaryFile(dir=cfg.root) as f:
            f.write(b"probe")
            f.flush()
    except OSError:
        report["issues"].append("data_dir_not_writable")
    try:
        from .prompts import provider_ready

        provider_ready(cfg, {})
    except Exception:
        report["issues"].append("provider_not_configured")
    with connect(cfg) as db:
        report["database"] = db.execute("PRAGMA integrity_check").fetchone()[0]
        if report["database"] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            report["issues"].append("database_integrity")
        for asset in db.execute("SELECT * FROM assets"):
            path = cfg.root / asset["relative_path"]
            if not path.is_file():
                report["issues"].append("asset_missing:" + asset["sha256"])
                continue
            with path.open("rb") as file:
                hash_ = hashlib.file_digest(file, "sha256").hexdigest()
            if hash_ != asset["sha256"]:
                report["issues"].append("asset_corrupt:" + asset["sha256"])
    report["ok"] = not report["issues"]
    return report
