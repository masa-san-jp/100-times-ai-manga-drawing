import hashlib
import json
import os
import sqlite3
import subprocess
import sys

from PIL import Image
import pytest

from manga_drawing import planning, projects
from manga_drawing.assets import decode
from manga_drawing.common import DomainError
from manga_drawing.config import Config
from manga_drawing.db import connect, get, initialize, transaction
from manga_drawing.export import backup, process_export, restore, schedule
from manga_drawing.worker import Worker

from conftest import make_project, page_rows, run, start


def accept_first(cfg, pid):
    p = page_rows(cfg, pid)[0]
    with connect(cfg) as db:
        c = projects.page_detail(db, p["id"])["candidates"][0]
    planning.review(
        cfg,
        p["id"],
        dict(
            expected_version=p["version"],
            latest_round_id=p["latest_round_id"],
            decision="accept",
            candidate_id=c["id"],
        ),
    )
    return c


def test_backup_restore_and_export(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)
    worker = Worker(cfg, clock)
    worker.tick()  # remote submitted, not collected
    location = backup(cfg, tmp_path / "backup")
    other = Config(tmp_path / "restored", mock_delay=0)
    restore(other, location)
    initialize(other)
    run(other, clock)
    candidate = accept_first(other, pid)
    with connect(other) as db:
        p = get(db, "projects", pid)
    with pytest.raises(DomainError):
        schedule(other, pid, {"mode": "complete", "expected_version": p["version"]})
    result = schedule(other, pid, {"mode": "accepted_only", "expected_version": p["version"]})
    process_export(other)
    with connect(other) as db:
        out = get(db, "exports", result["export_id"])
    folder = other.root / out["archive_path"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["missing_pages"] == [2]
    assert manifest["provider"] == "mock"
    assert manifest["pages"][0]["candidate_id"] == candidate["id"]
    assert [p.name for p in (folder / "pages").iterdir()] == ["0001.png"]
    with Image.open(folder / "pages/0001.png") as im:
        assert im.mode == "L" and im.size == (1410, 1920)
        assert im.getpixel((0, 0)) == 255
    assert not list((other.root / "exports").glob("*.zip"))
    assert Worker(other, clock).provider.call_count() == 1
    assert page_rows(other, pid)[0]["accepted_candidate_id"] == candidate["id"]


@pytest.mark.parametrize(
    "field,corrupt", [("asset_hash", True), ("raw_asset_hash", False), ("thumbnail_hash", False)]
)
def test_missing_asset_recovered_without_generation(cfg, tmp_path, clock, field, corrupt):
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    worker = run(cfg, clock)
    c = accept_first(cfg, pid)
    with connect(cfg) as db:
        path = (
            cfg.root
            / db.execute("SELECT relative_path FROM assets WHERE sha256=?", (c[field],)).fetchone()[0]
        )
    if corrupt:
        path.write_bytes(b"broken")
    else:
        path.unlink()
    worker.check_assets()
    run(cfg, clock, 2)
    assert worker.provider.call_count() == 1
    assert page_rows(cfg, pid)[0]["accepted_candidate_id"] == c["id"]
    with connect(cfg) as db:
        assert get(db, "candidates", c["id"])["availability"] == "available"
        assert get(db, "jobs", c["job_id"])["state"] == "succeeded"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == c[field]
    decode(path.read_bytes())


@pytest.mark.parametrize("point", ["before_submit", "after_submit", "after_download", "after_rename"])
def test_real_process_crash_recovery(cfg, tmp_path, clock, point):
    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)
    script = """
from pathlib import Path
import sys
from manga_drawing.config import Config
from manga_drawing.worker import Worker
cfg=Config(Path(sys.argv[1]),mock_delay=0)
w=Worker(cfg)
w.run(once=True)
w.run(once=True)
"""
    env = dict(os.environ, MANGA_CRASH_AT=point)
    child = subprocess.run(
        [sys.executable, "-c", script, str(cfg.root)], env=env, timeout=20, capture_output=True
    )
    assert child.returncode == 73, child.stderr.decode()
    worker = run(cfg, clock, 5)
    if point == "before_submit":
        # No external match is not proof of non-acceptance; requires explicit resolution.
        with connect(cfg) as db:
            assert {r[0] for r in db.execute("SELECT state FROM jobs")} == {"unknown"}
        assert worker.provider.call_count() == 0
    else:
        with connect(cfg) as db:
            assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2
            assert {r[0] for r in db.execute("SELECT state FROM jobs")} == {"succeeded"}
        assert worker.provider.call_count() == 1


def test_retry_does_not_create_second_active_round(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1, {"scenario": "reject"})
    start(cfg, pid)
    run(cfg, clock)
    with connect(cfg) as db:
        old = dict(db.execute("SELECT * FROM jobs").fetchone())
    start(cfg, pid, kind="extra", page_ids=[p["id"] for p in page_rows(cfg, pid)])
    with pytest.raises(DomainError):
        planning.retry(cfg, old["id"], {"expected_state": "failed"}, "retry-old")


def test_real_provider_disabled(cfg, tmp_path, client):
    pid, _ = make_project(cfg, tmp_path, 1)
    with transaction(cfg) as db:
        db.execute(
            "UPDATE projects SET settings_json=? WHERE id=?", (json.dumps({"provider": "gemini"}), pid)
        )
    response = client.post(f"/api/projects/{pid}/plans", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_not_configured"
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_http_security_and_pagination(cfg, tmp_path, client):
    pid, _ = make_project(cfg, tmp_path, 100)
    result = client.get(f"/api/projects/{pid}/pages").json()["data"]
    assert len(result["items"]) == 40
    assert all("draft" not in p and "candidates" not in p for p in result["items"])
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/imports", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/imports", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.get("/api/assets/unknown").status_code == 404
    assert client.post(f"/api/projects/{pid}/plans", json={"candidate_count": 4}).status_code == 422
    assert len(client.get("/openapi.json").json()["paths"]) >= 25


def test_import_receipt_survives_expiry(cfg, tmp_path):
    pid, token = make_project(cfg, tmp_path)
    meta = cfg.root / "staging" / token / "meta.json"
    payload = json.loads(meta.read_text())
    payload["created"] = 0
    meta.write_text(json.dumps(payload))
    assert projects.commit_import(cfg, token)["project_id"] == pid


def test_doctor_and_staging_cleanup(cfg, tmp_path):
    from manga_drawing.maintenance import cleanup_staging, doctor

    pid, token = make_project(cfg, tmp_path, 1)
    assert doctor(cfg)["ok"]
    assert cleanup_staging(cfg, now=4_000_000_000) == 1
    assert projects.commit_import(cfg, token)["project_id"] == pid
    with connect(cfg) as db:
        asset = dict(db.execute("SELECT * FROM assets LIMIT 1").fetchone())
    (cfg.root / asset["relative_path"]).write_bytes(b"broken")
    assert "asset_corrupt:" + asset["sha256"] in doctor(cfg)["issues"]


def test_result_key_errors_do_not_resubmit_or_repoll_forever(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1, {"scenario": "duplicate"})
    start(cfg, pid)
    worker = run(cfg, clock, 3)
    with connect(cfg) as db:
        c = get(db, "chunks", db.execute("SELECT id FROM chunks").fetchone()[0])
    clock.advance(1000)
    worker.tick()
    with connect(cfg) as db:
        current = get(db, "chunks", c["id"])
    assert current["state"] == "unknown"
    assert current["next_poll_at"] == c["next_poll_at"]
    assert worker.provider.call_count() == 1


def test_chunk_splitting_and_single_worker_lock(cfg, tmp_path, clock):
    from dataclasses import replace

    pid, _ = make_project(cfg, tmp_path, 5)
    start(cfg, pid)
    small = replace(cfg, chunk_count=2, max_external=2)
    worker = run(small, clock, 8)
    assert worker.provider.call_count() == 3
    script = """
from pathlib import Path
import sys
from manga_drawing.config import Config
from manga_drawing.worker import Worker
Worker(Config(Path(sys.argv[1]))).run()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(cfg.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connect(cfg) as db:
                h = db.execute("SELECT value FROM system WHERE key='heartbeat'").fetchone()
            if h and time.time() - float(h[0]) < 1:
                time.sleep(0.5)
                break
            time.sleep(0.05)
        with pytest.raises(DomainError, match="すでに起動"):
            Worker(cfg).run(once=True)
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_folder_export_open_button_and_reloaded_history(cfg, client, tmp_path, clock, monkeypatch):
    from manga_drawing import export as outputs

    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    run(cfg, clock)
    accept_first(cfg, pid)
    with connect(cfg) as db:
        version = get(db, "projects", pid)["version"]
    response = client.post(
        f"/api/projects/{pid}/exports", json={"mode": "complete", "expected_version": version}
    )
    eid = response.json()["data"]["export_id"]
    assert client.post(f"/api/exports/{eid}/open").status_code == 409
    process_export(cfg)
    result = client.get(f"/api/exports/{eid}").json()["data"]
    with connect(cfg) as db:
        row = get(db, "exports", eid)
    folder = outputs.output_folder(cfg, row)
    assert folder.parent.parent == cfg.root / "作品"
    assert folder.name == "生成結果"
    assert result["folder_path"] == str(folder) and folder.is_dir()
    assert "download_url" not in result
    opened = []
    monkeypatch.setattr(outputs.subprocess, "run", lambda args, **kwargs: opened.append(args))
    assert client.post(result["open_url"]).status_code == 200
    assert opened[0][-1] == str(folder)  # Fixed argument vector, never a shell command.
    assert client.get(f"/api/exports/{eid}/download").status_code == 404
    html = client.get(f"/projects/{pid}").text
    assert f'data-export-id="{eid}"' in html and "ZIP" not in html
    assert "/api/exports/{eid}/download" not in client.get("/openapi.json").json()["paths"]
    with transaction(cfg) as db:
        db.execute("UPDATE exports SET archive_path='exports/legacy.zip' WHERE id=?", (eid,))
    legacy = client.get(f"/api/exports/{eid}")
    assert legacy.status_code == 200
    assert legacy.json()["data"]["open_url"] is None
    listing = client.get(f"/projects/{pid}")
    assert listing.status_code == 200
    assert f'data-export-id="{eid}"' not in listing.text


def test_failed_folder_export_does_not_publish_partial_pages(cfg, tmp_path, clock, monkeypatch):
    from manga_drawing import export as outputs

    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)
    run(cfg, clock)
    for page in page_rows(cfg, pid):
        with connect(cfg) as db:
            detail = projects.page_detail(db, page["id"])
        planning.review(
            cfg,
            page["id"],
            {
                "expected_version": page["version"],
                "latest_round_id": page["latest_round_id"],
                "decision": "accept",
                "candidate_id": detail["candidates"][0]["id"],
            },
        )
    with connect(cfg) as db:
        version = get(db, "projects", pid)["version"]
    eid = schedule(cfg, pid, {"mode": "complete", "expected_version": version})["export_id"]
    original = outputs.atomic_write
    calls = []

    def fail_second(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk error")
        return original(path, data)

    monkeypatch.setattr(outputs, "atomic_write", fail_second)
    process_export(cfg)
    with connect(cfg) as db:
        relative = outputs.export_relative(get(db, "exports", eid))
    assert not (cfg.root / relative).exists()
    assert not (cfg.root / relative).with_name(eid + ".tmp").exists()
    with connect(cfg) as db:
        assert get(db, "exports", eid)["state"] == "failed"
        db.execute("UPDATE exports SET state='queued' WHERE id=?", (eid,))
    monkeypatch.setattr(outputs, "atomic_write", original)
    process_export(cfg)
    folder = cfg.root / relative
    assert sorted(p.name for p in (folder / "pages").iterdir()) == ["0001.png", "0002.png"]
    # Recovery after folder publication but before DB completion reuses the same snapshot.
    with connect(cfg) as db:
        db.execute("UPDATE exports SET state='running' WHERE id=?", (eid,))
    process_export(cfg)
    with connect(cfg) as db:
        assert get(db, "exports", eid)["state"] == "complete"
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_detail_navigation_stays_in_episode_and_skips_number_gaps(cfg, client, tmp_path):
    pid, _ = make_project(cfg, tmp_path, 3)
    other, _ = make_project(cfg, tmp_path, 1)
    pages = page_rows(cfg, pid)
    other_page = page_rows(cfg, other)[0]
    with connect(cfg) as db:
        db.execute("UPDATE pages SET number=8 WHERE id=?", (pages[2]["id"],))
    response = client.get(f"/pages/{pages[1]['id']}")
    assert response.status_code == 200
    assert f"/pages/{pages[0]['id']}" in response.text
    assert f"/pages/{pages[2]['id']}" in response.text
    assert "次のページ（8）" in response.text
    assert f"/pages/{other_page['id']}" not in response.text


def test_export_is_inside_configured_work_folder(cfg, client, tmp_path, clock):
    from manga_drawing import export as outputs

    pid, _ = make_project(cfg, tmp_path, 1)
    work = tmp_path / "公開作品"
    work.mkdir()
    with connect(cfg) as db:
        version = get(db, "projects", pid)["version"]
    response = client.patch(
        f"/api/projects/{pid}", json={"expected_version": version, "patch": {"work_folder": str(work)}}
    )
    assert response.status_code == 200
    start(cfg, pid)
    run(cfg, clock)
    accept_first(cfg, pid)
    with connect(cfg) as db:
        version = get(db, "projects", pid)["version"]
    eid = schedule(cfg, pid, {"expected_version": version, "mode": "complete"})["export_id"]
    process_export(cfg)
    with connect(cfg) as db:
        row = get(db, "exports", eid)
    folder = outputs.output_folder(cfg, row)
    assert folder == work / "生成結果"
    assert (folder / "0001_テイク01.png").is_file()
    public = json.loads((folder / "manifest.json").read_text())
    assert "work_folder" not in public
    assert str(tmp_path) not in (folder / "manifest.json").read_text()
    with connect(cfg) as db:
        db.execute("UPDATE exports SET state='running' WHERE id=?", (eid,))
    process_export(cfg)
    with connect(cfg) as db:
        assert get(db, "exports", eid)["state"] == "complete"


@pytest.mark.parametrize("add_episode", [False, True])
def test_rejected_candidates_are_named_and_restored_without_loss(cfg, tmp_path, clock, add_episode):
    from manga_drawing.export import sync_rejected

    pid, _ = make_project(cfg, tmp_path, 1)
    work = tmp_path / "公開作品"
    work.mkdir()
    with connect(cfg) as db:
        db.execute("INSERT OR REPLACE INTO system VALUES(?,?)", (f"work_folder:{pid}", str(work)))
    start(cfg, pid)
    run(cfg, clock)
    item = page_rows(cfg, pid)[0]
    with connect(cfg) as db:
        candidate = projects.page_detail(db, item["id"])["candidates"][0]
    planning.review(
        cfg,
        item["id"],
        {
            "expected_version": item["version"],
            "latest_round_id": item["latest_round_id"],
            "decision": "reject",
            "reason": "人物の向き",
        },
    )
    sync_rejected(cfg)
    path = work / "却下した候補" / "0001_テイク01.png"
    assert path.is_file()
    assert "人物の向き" in (path.parent / "候補一覧.csv").read_text(encoding="utf-8-sig")
    if add_episode:
        second, _ = make_project(cfg, tmp_path, 1)
        old_path = path
        old_index = path.parent / "候補一覧.csv"
        old_index.write_text("利用者の追記")
        extra = path.parent / "利用者の資料.txt"
        extra.write_text("保存する資料")
        with transaction(cfg) as db:
            db.execute("UPDATE projects SET work_id=?,title=? WHERE id=?", (pid, "第2話", second))
            title = get(db, "projects", pid)["title"]
        sync_rejected(cfg)
        path = work / title / "却下した候補" / path.name
        assert path.is_file()
        assert "人物の向き" in (path.parent / "候補一覧.csv").read_text(encoding="utf-8-sig")
        assert not old_path.exists()
        assert old_index.read_text() == "利用者の追記"
        assert extra.read_text() == "保存する資料"
    sync_rejected(cfg)
    with connect(cfg) as db:
        item = get(db, "pages", item["id"])
    planning.review(
        cfg,
        item["id"],
        {
            "expected_version": item["version"],
            "latest_round_id": item["latest_round_id"],
            "decision": "accept",
            "candidate_id": candidate["id"],
        },
    )
    sync_rejected(cfg)
    assert not path.exists()
    assert (cfg.root / "exports" / "rejected-history" / pid / candidate["asset_hash"]).is_file()


def test_managed_work_folder_restore_uses_new_machine_path(cfg, tmp_path, clock):
    from manga_drawing import export as outputs

    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    run(cfg, clock)
    accept_first(cfg, pid)
    with connect(cfg) as db:
        version = get(db, "projects", pid)["version"]
        old_folder = db.execute("SELECT value FROM system WHERE key=?", (f"work_folder:{pid}",)).fetchone()[0]
    eid = schedule(cfg, pid, {"expected_version": version, "mode": "complete"})["export_id"]
    process_export(cfg)
    location = backup(cfg, tmp_path / "移行用バックアップ")
    restored = Config(tmp_path / "別のマシン")
    restored.root.mkdir()
    broken = tmp_path / "broken-backup"
    broken.mkdir()
    (broken / "backup.json").write_text('{"schema": 1}')
    (broken / "db.sqlite3").write_text("not a database")
    with pytest.raises(sqlite3.DatabaseError):
        restore(restored, broken)
    assert list(restored.root.iterdir()) == []
    restore(restored, location)
    with pytest.raises(DomainError, match="復元先は空"):
        restore(restored, location)
    initialize(restored)
    with connect(restored) as db:
        new_folder = db.execute("SELECT value FROM system WHERE key=?", (f"work_folder:{pid}",)).fetchone()[0]
        row = get(db, "exports", eid)
    assert new_folder != old_folder
    assert outputs.output_folder(restored, row) == restored.root / "作品" / "サンプル作品" / "生成結果"
    assert (outputs.output_folder(restored, row) / "0001_テイク01.png").is_file()


def test_saving_settings_keeps_managed_folder(cfg, client, tmp_path):
    pid, _ = make_project(cfg, tmp_path, 1)
    with connect(cfg) as db:
        version = get(db, "projects", pid)["version"]
        folder = db.execute("SELECT value FROM system WHERE key=?", (f"work_folder:{pid}",)).fetchone()[0]
    saved = client.patch(
        f"/api/projects/{pid}",
        json={"expected_version": version, "patch": {"work_folder": folder, "title": "名前を変更"}},
    )
    assert saved.status_code == 200
    with connect(cfg) as db:
        assert db.execute("SELECT value FROM system WHERE key=?", (f"managed_work_folder:{pid}",)).fetchone()
    reset = client.patch(
        f"/api/projects/{pid}",
        json={"expected_version": saved.json()["data"]["version"], "patch": {"work_folder": ""}},
    )
    assert reset.status_code == 200
    assert reset.json()["data"]["work_folder"] == folder


def test_connect_closes_connection(cfg):
    with connect(cfg) as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")


def test_maintenance_import_does_not_load_web():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import manga_drawing.maintenance; "
            "assert not {'fastapi', 'manga_drawing.web'} & sys.modules.keys()",
        ],
        check=True,
    )


def test_idle_worker_starts_no_write_transactions_and_skips_reference_files(
    cfg, tmp_path, clock, monkeypatch
):
    import sqlite3

    from manga_drawing import character_references as characters

    pid, _ = make_project(cfg, tmp_path, 1, ready_references=False)
    characters.generate(cfg, pid, {}, "idle-test")
    worker = Worker(cfg, clock)
    worker.tick()
    worker.tick()
    original_connect = sqlite3.connect
    statements = []

    def trace(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    def fail_file_write(*args, **kwargs):
        pytest.fail("unchanged reference files must be skipped")

    monkeypatch.setattr(sqlite3, "connect", trace)
    monkeypatch.setattr(characters, "atomic_write", fail_file_write)
    assert not worker.tick()
    assert statements and not any(sql.startswith("BEGIN") for sql in statements)
    monkeypatch.undo()

    with connect(cfg) as db:
        ref = characters.list_references(db, pid)[0]
        folder = db.execute("SELECT value FROM system WHERE key=?", (f"work_folder:{pid}",)).fetchone()[0]
    from pathlib import Path

    index = Path(folder) / "キャラクターリファレンス/a/候補一覧.json"
    assert json.loads(index.read_text())[0]["status"] == "未採用"
    page = ref["page"]
    planning.review(
        cfg,
        page["id"],
        {
            "expected_version": page["version"],
            "latest_round_id": page["latest_round_id"],
            "decision": "accept",
            "candidate_id": page["candidates"][0]["id"],
        },
    )
    worker.tick()
    assert json.loads(index.read_text())[0]["status"] == "採用"


def test_restore_partial_copy_failure_keeps_destination_empty(cfg, tmp_path, monkeypatch):
    from manga_drawing import export

    source = backup(cfg, tmp_path / "backup")
    destination = Config(tmp_path / "restored")
    destination.root.mkdir()

    def interrupted_copy(source, target):
        target.mkdir()
        (target / "partial").write_text("partial restore")
        raise OSError("copy interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(export.shutil, "copytree", interrupted_copy)
        with pytest.raises(OSError, match="copy interrupted"):
            restore(destination, source)
    assert list(destination.root.iterdir()) == []
    assert not list(tmp_path.glob(".manga-restore-*"))
    restore(destination, source)
    assert (destination.root / "db.sqlite3").is_file()


def test_asset_urls_follow_contents_and_api_uses_package_version(cfg, monkeypatch):
    import hashlib
    from pathlib import Path

    from fastapi.testclient import TestClient

    from manga_drawing import __version__, web

    def render():
        with TestClient(web.create_app(cfg), base_url="http://127.0.0.1") as client:
            assert client.get("/openapi.json").json()["info"]["version"] == __version__
            return client.get("/").text

    script_path = Path(web.__file__).parent / "static/app.js"
    original = script_path.read_bytes()
    first_hash = hashlib.sha256(original).hexdigest()[:12]
    assert f"app.js?v={first_hash}" in render()
    replacement = original + b"\n"
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: replacement if p == script_path else read_bytes(p))
    changed_hash = hashlib.sha256(replacement).hexdigest()[:12]
    assert f"app.js?v={changed_hash}" in render()
