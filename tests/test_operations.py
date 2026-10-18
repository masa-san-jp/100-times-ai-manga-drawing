import hashlib
import json
import os
import subprocess
import sys
import zipfile

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
    with zipfile.ZipFile(other.root / out["archive_path"]) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["missing_pages"] == [2]
        assert manifest["provider"] == "mock"
        assert manifest["pages"][0]["candidate_id"] == candidate["id"]
        with z.open("pages/0001.png") as f:
            im = Image.open(f)
            assert im.mode == "L" and im.size == (1410, 1920)
            assert im.getpixel((0, 0)) == 255
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
    start(cfg, pid, kind="extra")
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
