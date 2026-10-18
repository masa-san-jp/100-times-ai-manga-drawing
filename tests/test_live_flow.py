"""Offline end-to-end rehearsal of the live test continuation; no external calls."""

import json
from pathlib import Path
import runpy

import pytest

from manga_drawing import projects, planning
from manga_drawing.db import connect, transaction
from manga_drawing.export import process_export, schedule
from manga_drawing.worker import Worker
from conftest import make_project, start

pytest_plugins = ["test_gemini"]  # Reuse the in-memory Google SDK fixture.

advance = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/live_acceptance_flow.py"))[
    "advance_once"
]


def prepare(live, tmp_path):
    cfg, fake, _ = live
    pid, _ = make_project(cfg, tmp_path, pages=2)
    with transaction(cfg) as db:
        db.execute("UPDATE projects SET title=? WHERE id=?", ("実API受け入れ試験（画質採否とは別）", pid))
    result, _ = start(cfg, pid)
    path = tmp_path / "record.json"
    path.write_text(json.dumps({"project_id": pid, "after_billing_batch_id": result["batch_id"]}))
    return cfg, fake, pid, path


def test_offline_live_flow_waits_revises_once_and_proves_baseline(live, tmp_path, clock):
    cfg, fake, pid, path = prepare(live, tmp_path)
    assert advance(cfg, path) == "waiting_initial"
    worker = Worker(cfg, clock)
    worker.tick()
    worker.tick()
    assert advance(cfg, path) == "waiting_revision"
    first = json.loads(path.read_text())
    assert advance(cfg, path) == "waiting_revision"  # Re-entry must not queue another paid request.
    assert first["revision_batch_id"] == json.loads(path.read_text())["revision_batch_id"]
    clock.advance(6)
    worker.tick()
    worker.tick()
    assert advance(cfg, path) == "waiting_export"
    process_export(cfg)
    assert advance(cfg, path) == "complete"
    completed = json.loads(path.read_text())["completed_at"]
    assert advance(cfg, path) == "complete"
    assert json.loads(path.read_text())["completed_at"] == completed
    record = json.loads(path.read_text())
    assert record["baseline_unchanged"] and record["successful_test_images"] == 3
    assert record["author_quality_review"] == "pending"
    assert len(record["accepted_attempts"]) == 3 and len(fake.creates) == 2
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
        page = projects.page_detail(db, record["baseline"]["page_id"])
        assert page["accepted_candidate_id"] == record["baseline"]["candidate_id"]
    assert Path(record["export_path"]).is_dir()


def test_offline_live_flow_stops_on_unknown_without_new_jobs(live, tmp_path):
    cfg, fake, _, path = prepare(live, tmp_path)
    with transaction(cfg) as db:
        db.execute("UPDATE jobs SET state='unknown'")
    with pytest.raises(Exception) as exc:
        advance(cfg, path)
    assert getattr(exc.value, "code", None) == "test_blocked"
    assert not fake.creates
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


@pytest.mark.parametrize("mode", ["accepted_only", "complete"])
def test_live_flow_rejects_stale_export_snapshot(live, tmp_path, clock, mode):
    cfg, _, pid, path = prepare(live, tmp_path)
    worker = Worker(cfg, clock)
    worker.tick()
    worker.tick()
    with connect(cfg) as db:
        pages = [projects.page_detail(db, r[0]) for r in db.execute("SELECT id FROM pages ORDER BY number")]
    for page in pages[: 1 if mode == "accepted_only" else 2]:
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
    with connect(cfg) as db:
        version = db.execute("SELECT version FROM projects WHERE id=?", (pid,)).fetchone()[0]
    old = schedule(cfg, pid, {"mode": mode, "expected_version": version})["export_id"]
    process_export(cfg)
    if mode == "complete":
        with connect(cfg) as db:
            page = projects.page_detail(db, pages[1]["id"])
        planning.review(
            cfg,
            page["id"],
            {
                "expected_version": page["version"],
                "latest_round_id": page["latest_round_id"],
                "decision": "reject",
                "clear_accepted": True,
            },
        )
    record = json.loads(path.read_text())
    record["export_id"] = old  # Simulate a record left by an interrupted older runner.
    path.write_text(json.dumps(record))
    assert advance(cfg, path) == "waiting_revision"
    clock.advance(6)
    worker.tick()
    worker.tick()
    assert advance(cfg, path) == "waiting_export"
    assert json.loads(path.read_text())["export_id"] != old
    process_export(cfg)
    assert advance(cfg, path) == "complete"


@pytest.mark.parametrize("mistake", ["initial_batch", "baseline", "revision_page"])
def test_live_flow_rejects_record_scope_mismatch_before_any_mutation(live, tmp_path, clock, mistake):
    cfg, _, pid, path = prepare(live, tmp_path)
    worker = Worker(cfg, clock)
    worker.tick()
    worker.tick()
    record = json.loads(path.read_text())
    if mistake == "initial_batch":
        other, _ = make_project(cfg, tmp_path / "other", pages=1)
        other_batch, _ = start(cfg, other)
        record["after_billing_batch_id"] = other_batch["batch_id"]
    elif mistake == "baseline":
        record["baseline"] = {
            "page_id": "wrong-page",
            "candidate_id": "wrong-candidate",
            "asset_hash": "wrong-hash",
        }
    else:
        record["revision_page_id"] = "wrong-page"
    path.write_text(json.dumps(record))
    with connect(cfg) as db:
        before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with pytest.raises(Exception) as exc:
        advance(cfg, path)
    assert getattr(exc.value, "code", None) == "test_scope"
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before
        assert db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 0


def test_recorded_live_response_schema_handles_jpeg_and_opaque_signature(live):
    import base64
    from io import BytesIO
    from PIL import Image

    fixture = Path(__file__).resolve().parents[1] / "docs/fixtures/gemini-live-response.json"
    assert fixture.stat().st_size < 8192  # Binary payloads and opaque signatures must stay omitted.
    response = json.loads(fixture.read_text())
    output = BytesIO()
    Image.new("RGB", (12, 16), "white").save(output, format="JPEG")
    image = response["response"]["candidates"][0]["content"]["parts"][0]["inlineData"]
    assert image["mimeType"] == "image/jpeg" and image["data"] == "<omitted>"
    image["data"] = base64.b64encode(output.getvalue()).decode()
    result = live[2].parse_line(json.dumps(response))
    assert len(result["images"]) == 1 and base64.b64decode(result["images"][0]) == output.getvalue()
    assert result["usage"]["totalTokenCount"] > 0
