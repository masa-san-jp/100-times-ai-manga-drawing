from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from PIL import Image

from . import assets
from .common import DomainError, atomic_write, canonical, require, timestamp, uid
from .db import connect, get, transaction


def schedule(cfg, pid, request):
    with transaction(cfg) as db:
        project = get(db, "projects", pid)
        require(project["version"] == request["expected_version"])
        require(request["mode"] in ("complete", "accepted_only"), "mode", "書き出し方法が不正です。", 422)
        pages, missing = [], []
        for row in db.execute("SELECT * FROM pages WHERE project_id=? ORDER BY number", (pid,)):
            if not row["accepted_candidate_id"]:
                missing.append(row["number"])
                continue
            c = get(db, "candidates", row["accepted_candidate_id"])
            require(c["availability"] == "available", "missing_asset", "採用画像が欠損しています。", 422)
            assets.path_for(cfg, db, c["asset_hash"])
            job = get(db, "jobs", c["job_id"])
            pages.append(
                {
                    "number": row["number"],
                    "candidate_id": c["id"],
                    "round_id": job["round_id"],
                    "asset_hash": c["asset_hash"],
                    "raw_asset_hash": c["raw_asset_hash"],
                    "provider": json.loads(get(db, "rounds", job["round_id"])["snapshot_json"])["settings"][
                        "provider"
                    ],
                }
            )
        require(
            request["mode"] != "complete" or not missing, "missing_pages", f"未採用ページ: {missing}", 422
        )
        require(pages, "no_accepted", "採用済みページがありません。", 422)
        settings = json.loads(project["settings_json"])
        manifest = {
            "provider": cfg.provider,
            "project_id": pid,
            "title": project["title"],
            "pages": pages,
            "missing_pages": missing,
            "width": settings.get("width", 1410),
            "height": settings.get("height", 1920),
            "conversion": "grayscale-contain-white-center-lanczos",
        }
        provenance = {p["provider"] for p in pages}
        manifest["provider"] = next(iter(provenance)) if len(provenance) == 1 else "mixed"
        ident = uid()
        db.execute(
            "INSERT INTO exports(id,project_id,manifest_json,state,created_at) VALUES(?,?,?,'queued',?)",
            (ident, pid, canonical(manifest), timestamp()),
        )
        return {"export_id": ident}


def process_export(cfg):
    with cfg.write_lock():
        with connect(cfg) as db:
            row = db.execute(
                "SELECT * FROM exports WHERE state IN ('queued','running') ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return
            row = dict(row)
        final = cfg.root / "exports" / row["id"]
        temp = final.with_name(final.name + ".tmp")
        try:
            with transaction(cfg) as db:
                db.execute("UPDATE exports SET state='running' WHERE id=?", (row["id"],))
            manifest = json.loads(row["manifest_json"])
            width, height = manifest["width"], manifest["height"]
            # A restarted export rebuilds only its private staging folder.
            if temp.exists():
                shutil.rmtree(temp)
            (temp / "pages").mkdir(parents=True)
            with connect(cfg) as db:
                for page in manifest["pages"]:
                    with Image.open(assets.path_for(cfg, db, page["asset_hash"])) as source:
                        image = source.convert("L")
                        scale = min(width / image.width, height / image.height)
                        image = image.resize(
                            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                            Image.Resampling.LANCZOS,
                        )
                        canvas = Image.new("L", (width, height), "white")
                        canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
                        atomic_write(temp / "pages" / f"{page['number']:04d}.png", assets.png(canvas))
                atomic_write(temp / "manifest.json", canonical(manifest).encode())
            if final.exists():
                # Recovery after publication but before the DB completion commit.
                require(
                    (final / "manifest.json").read_text() == canonical(manifest),
                    "export_conflict",
                    "書き出し先の内容が一致しません。",
                )
                shutil.rmtree(temp)
            else:
                temp.replace(final)
            with transaction(cfg) as db:
                db.execute(
                    "UPDATE exports SET state='complete',archive_path=? WHERE id=?",
                    (str(final.relative_to(cfg.root)), row["id"]),
                )
        except (DomainError, OSError, ValueError) as e:
            with transaction(cfg) as db:
                db.execute(
                    "UPDATE exports SET state='failed',error=? WHERE id=?", (type(e).__name__, row["id"])
                )
            if temp.exists():
                shutil.rmtree(temp)


def output_folder(cfg, row):
    require(row["state"] == "complete", "not_ready", "書き出しが完了していません。", 409)
    require(
        row["archive_path"] == f"exports/{row['id']}",
        "legacy_export",
        "以前の圧縮形式です。画像をもう一度書き出してください。",
        409,
    )
    folder = cfg.root / row["archive_path"] / "pages"
    require(
        folder.is_dir()
        and folder.resolve().is_relative_to((cfg.root / "exports").resolve())
        and not folder.is_symlink()
        and not folder.parent.is_symlink(),
        "export_missing",
        "画像フォルダーが見つかりません。",
        404,
    )
    return folder


def open_folder(cfg, row):
    folder = output_folder(cfg, row)
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))
        else:
            command = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.run([command, str(folder)], check=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise DomainError(
            "open_folder", "フォルダーを開けませんでした。表示された保存先から開いてください。"
        ) from None
    return folder


def backup(cfg, output):
    output = Path(output).expanduser().resolve()
    require(
        output != cfg.root and cfg.root not in output.parents,
        "backup_path",
        "保存先の外にバックアップしてください。",
        422,
    )
    require(not output.exists(), "backup_exists", "新しいバックアップ先を指定してください。", 422)
    with cfg.write_lock():
        output.mkdir(parents=True)
        for relative in ("db.sqlite3", "mock-provider/state.sqlite3"):
            source = cfg.root / relative
            if source.exists():
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
                    src.backup(dst)
        for directory in (
            "assets",
            "chunks",
            "exports",
            "staging",
            "mock-provider/inputs",
            "mock-provider/outputs",
        ):
            if (cfg.root / directory).exists():
                shutil.copytree(cfg.root / directory, output / directory, dirs_exist_ok=True)
        shutil.copyfile(cfg.root / "secret", output / "secret")
        (output / "secret").chmod(0o600)
        atomic_write(
            output / "backup.json",
            canonical({"schema": 1, "created_at": timestamp(), "provider": cfg.provider}).encode(),
        )
    return str(output)


def restore(cfg, source):
    source = Path(source).expanduser().resolve()
    require(
        source != cfg.root and source not in cfg.root.parents and cfg.root not in source.parents,
        "restore_path",
        "バックアップと復元先は互いに独立したフォルダーにしてください。",
        422,
    )
    require((source / "backup.json").is_file(), "backup", "完全バックアップを指定してください。", 422)
    require(
        not cfg.root.exists() or not any(cfg.root.iterdir()),
        "restore_exists",
        "復元先は空のフォルダーにしてください。",
        422,
    )
    require(
        json.loads((source / "backup.json").read_text())["schema"] == 1,
        "backup",
        "未対応のバックアップです。",
        422,
    )
    shutil.copytree(source, cfg.root, dirs_exist_ok=True)
