from __future__ import annotations

import json
import csv
import io
import hashlib
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from PIL import Image

from . import assets
from .common import DomainError, atomic_write, canonical, require, timestamp, uid
from .db import connect, get, transaction


def readable_name(title, ident):
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", title).strip(" .")[:60]
    return f"{name or '名称未設定'}--{ident[:8]}"


def export_relative(row):
    manifest = json.loads(row["manifest_json"])
    relative = manifest.get("output_relative", f"exports/{row['id']}")
    path = Path(relative)
    require(
        not path.is_absolute()
        and ".." not in path.parts
        and path.parts[0] in (("exports", "生成結果") if manifest.get("work_folder") else ("exports",))
        and path.name == row["id"],
        "export_path",
        "書き出し先が不正です。",
        422,
    )
    return path


def public_manifest(manifest):
    return {key: value for key, value in manifest.items() if key != "work_folder"}


def export_target(cfg, row):
    manifest = json.loads(row["manifest_json"])
    base = (
        Path(manifest["work_folder"])
        if manifest.get("work_folder") and export_relative(row).parts[0] == "生成結果"
        else cfg.root
    )
    require(base.is_absolute(), "export_path", "作品フォルダーは絶対パスで指定してください。", 422)
    return base / export_relative(row)


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
                    "take": get(db, "rounds", job["round_id"])["sequence"],
                    "candidate_index": job["candidate_index"],
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
        work = get(db, "works", project["work_id"])
        manifest["work_id"] = work["id"]
        manifest["work_title"] = work["title"]
        manifest["output_relative"] = str(
            Path("exports")
            / readable_name(work["title"], work["id"])
            / readable_name(project["title"], pid)
            / ident
        )
        configured = db.execute(
            "SELECT value FROM system WHERE key=?", (f"work_folder:{work['id']}",)
        ).fetchone()
        if configured:
            manifest["work_folder"] = configured[0]
            siblings = db.execute("SELECT id,title FROM projects WHERE work_id=?", (work["id"],)).fetchall()
            manifest["publication_relative"] = "生成結果"
            if len(siblings) > 1:
                name = readable_name(project["title"], pid).rsplit("--", 1)[0]
                duplicates = sum(
                    readable_name(item["title"], item["id"]).rsplit("--", 1)[0] == name for item in siblings
                )
                if duplicates > 1:
                    name = readable_name(project["title"], pid)
                manifest["publication_relative"] = str(Path(name) / "生成結果")
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
        final = export_target(cfg, row)
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
                atomic_write(temp / "manifest.json", canonical(public_manifest(manifest)).encode())
            if final.exists():
                # Recovery after publication but before the DB completion commit.
                require(
                    (final / "manifest.json").read_text() == canonical(public_manifest(manifest)),
                    "export_conflict",
                    "書き出し先の内容が一致しません。",
                )
                shutil.rmtree(temp)
            else:
                temp.replace(final)
            if manifest.get("work_folder"):
                publish_flat(cfg, row, final)
            with transaction(cfg) as db:
                db.execute(
                    "UPDATE exports SET state='complete',archive_path=? WHERE id=?",
                    (
                        str(final.relative_to(cfg.root)),
                        row["id"],
                    ),
                )
        except (DomainError, OSError, ValueError) as e:
            with transaction(cfg) as db:
                db.execute(
                    "UPDATE exports SET state='failed',error=? WHERE id=?", (type(e).__name__, row["id"])
                )
            if temp.exists():
                shutil.rmtree(temp)


def candidate_filename(number, take, candidate=1):
    suffix = f"_候補{candidate:02d}" if candidate > 1 else ""
    return f"{number:04d}_テイク{take:02d}{suffix}.png"


def sync_rejected(cfg):
    with cfg.write_lock(), connect(cfg) as db:
        projects = db.execute(
            "SELECT p.*,s.value work_folder FROM projects p JOIN system s ON s.key='work_folder:'||p.work_id"
        ).fetchall()
        for project in projects:
            rows = db.execute(
                """SELECT p.number,r.sequence,j.candidate_index,c.id,c.asset_hash,r.rejection_reason
                FROM pages p JOIN rounds r ON r.page_id=p.id JOIN jobs j ON j.round_id=r.id
                JOIN candidates c ON c.job_id=j.id WHERE p.project_id=? AND r.review_state='rejected'
                AND (p.accepted_candidate_id IS NULL OR p.accepted_candidate_id!=c.id)
                ORDER BY p.number,r.sequence,j.candidate_index""",
                (project["id"],),
            ).fetchall()
            stamp = canonical([dict(row) for row in rows])
            key = f"rejected_files:{project['id']}"
            prior = db.execute("SELECT value FROM system WHERE key=?", (key,)).fetchone()
            if prior and prior[0] == stamp:
                continue
            base = Path(project["work_folder"])
            siblings = db.execute(
                "SELECT id,title FROM projects WHERE work_id=?", (project["work_id"],)
            ).fetchall()
            if len(siblings) > 1:
                name = readable_name(project["title"], project["id"]).rsplit("--", 1)[0]
                if sum(readable_name(x["title"], x["id"]).rsplit("--", 1)[0] == name for x in siblings) > 1:
                    name = readable_name(project["title"], project["id"])
                base = base / name
            target = base / "却下した候補"
            if not rows and not target.exists():
                db.execute("INSERT OR REPLACE INTO system VALUES(?,?)", (key, stamp))
                continue
            target.mkdir(parents=True, exist_ok=True)
            expected = {
                candidate_filename(row["number"], row["sequence"], row["candidate_index"]): dict(row)
                for row in rows
            }
            for filename, row in expected.items():
                destination = target / filename
                if destination.is_file():
                    existing_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
                    if existing_hash != row["asset_hash"]:
                        archive = cfg.root / "exports" / "rejected-history" / project["id"] / existing_hash
                        archive.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(destination), str(archive))
                atomic_write(destination, assets.path_for(cfg, db, row["asset_hash"]).read_bytes())
            old_rows = json.loads(prior[0]) if prior else []
            for old in old_rows:
                filename = candidate_filename(old["number"], old["sequence"], old["candidate_index"])
                old_file = target / filename
                if (
                    filename not in expected
                    and old_file.is_file()
                    and hashlib.sha256(old_file.read_bytes()).hexdigest() == old["asset_hash"]
                ):
                    archive = cfg.root / "exports" / "rejected-history" / project["id"] / old["asset_hash"]
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_file), str(archive))
            index = io.StringIO()
            writer = csv.writer(index, lineterminator="\n")
            writer.writerow(["ページ", "テイク", "候補", "却下理由", "ファイル名"])
            for filename, row in expected.items():
                writer.writerow(
                    [
                        row["number"],
                        row["sequence"],
                        row["candidate_index"],
                        row["rejection_reason"] or "",
                        filename,
                    ]
                )
            atomic_write(target / "候補一覧.csv", index.getvalue().encode("utf-8-sig"))
            db.execute("INSERT OR REPLACE INTO system VALUES(?,?)", (key, stamp))
        db.execute("DELETE FROM system WHERE key='rejected_sync_error'")


def publication_target(manifest):
    relative = Path(manifest.get("publication_relative", "生成結果"))
    require(
        not relative.is_absolute() and ".." not in relative.parts and relative.name == "生成結果",
        "export_path",
        "出力先が不正です。",
        422,
    )
    return Path(manifest["work_folder"]) / relative


def publish_flat(cfg, row, canonical_folder):
    manifest = json.loads(row["manifest_json"])
    target = publication_target(manifest)
    marker = target / "manifest.json"
    if marker.is_file() and json.loads(marker.read_text()).get("export_id") == row["id"]:
        return
    temp = target.with_name(f".生成結果-{row['id']}.tmp")
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    index = io.StringIO()
    writer = csv.writer(index, lineterminator="\n")
    writer.writerow(["ページ", "テイク", "候補", "ファイル名"])
    for page in manifest["pages"]:
        filename = candidate_filename(page["number"], page.get("take", 1), page.get("candidate_index", 1))
        shutil.copy2(canonical_folder / "pages" / f"{page['number']:04d}.png", temp / filename)
        writer.writerow([page["number"], page.get("take", 1), page.get("candidate_index", 1), filename])
    atomic_write(temp / "出力一覧.csv", index.getvalue().encode("utf-8-sig"))
    atomic_write(
        temp / "manifest.json", canonical(public_manifest(manifest) | {"export_id": row["id"]}).encode()
    )
    if target.exists():
        previous = cfg.root / "exports" / "published-history" / row["id"]
        require(not previous.exists(), "export_conflict", "以前の出力の保管先が重複しています。")
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(previous))
    try:
        temp.replace(target)
    except OSError:
        if not target.exists() and "previous" in locals() and previous.exists():
            shutil.move(str(previous), str(target))
        raise


def output_folder(cfg, row):
    require(row["state"] == "complete", "not_ready", "書き出しが完了していません。", 409)
    manifest = json.loads(row["manifest_json"])
    if manifest.get("work_folder"):
        published = publication_target(manifest)
        marker = published / "manifest.json"
        if marker.is_file() and json.loads(marker.read_text()).get("export_id") == row["id"]:
            require(not published.is_symlink(), "export_missing", "画像フォルダーが見つかりません。", 404)
            return published
    target = export_target(cfg, row)
    expected = str(target) if export_relative(row).parts[0] == "生成結果" else str(export_relative(row))
    require(
        row["archive_path"] == expected,
        "legacy_export",
        "以前の圧縮形式です。画像をもう一度書き出してください。",
        409,
    )
    folder = target / "pages"
    base = (
        Path(manifest["work_folder"]) / "生成結果"
        if export_relative(row).parts[0] == "生成結果"
        else cfg.root / "exports"
    )
    require(
        folder.is_dir()
        and folder.resolve().is_relative_to(base.resolve())
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
