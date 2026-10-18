from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time

from .db import connect


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
