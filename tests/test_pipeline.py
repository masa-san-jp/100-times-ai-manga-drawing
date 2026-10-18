import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from manga_drawing import planning, projects
from manga_drawing.common import DomainError, uid
from manga_drawing.db import connect, get, transaction
from manga_drawing.prompts import snapshot
from manga_drawing.worker import Worker, resolve

from conftest import make_project, page_rows, run, start


@pytest.mark.parametrize("pages,candidates", [(100, 3), (300, 1), (300, 3)])
def test_large_batch(cfg, tmp_path, clock, pages, candidates):
    pid, _ = make_project(cfg, tmp_path, pages)
    start(cfg, pid, candidate_count=candidates)
    worker = run(cfg, clock)
    with connect(cfg) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM jobs WHERE state='succeeded'").fetchone()[0]
            == pages * candidates
        )
        assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == pages * candidates
    assert worker.provider.call_count() == 1


def test_idempotent_parallel_and_conflict(cfg, tmp_path):
    pid, _ = make_project(cfg, tmp_path)
    with transaction(cfg) as db:
        plan = planning.make_plan(cfg, db, pid, {})
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: planning.start(cfg, pid, plan["plan_token"], "same-key"), range(2)))
    assert replies[0]["batch_id"] == replies[1]["batch_id"]
    with pytest.raises(DomainError):
        planning.start(cfg, pid, "other-token", "same-key")
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_plan_cas_and_snapshot(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1)
    page = page_rows(cfg, pid)[0]
    with transaction(cfg) as db:
        plan = planning.make_plan(cfg, db, pid, {})
        projects.patch_draft(db, page, {"correction": "keep panel borders"})
    with pytest.raises(DomainError):
        planning.start(cfg, pid, plan["plan_token"], uid())
    start(cfg, pid)
    with transaction(cfg) as db:
        page = get(db, "pages", page["id"])
        snap1, hash1, _ = snapshot(cfg, db, page)
        projects.patch_draft(db, page, {"correction": "changed after queue"})
        round_ = get(db, "rounds", page["latest_round_id"])
        assert "keep panel borders" in round_["prompt_text"]
        assert "changed after queue" not in round_["prompt_text"]
        snap2, hash2, _ = snapshot(cfg, db, page)
        assert hash1 != hash2
        projects.patch_draft(db, page, {"prompt_override": "EXACT OVERRIDE"})
        snap3, _, _ = snapshot(cfg, db, page)
        assert snap3["prompt"] == "EXACT OVERRIDE"
        assert snap3["attachments"] == snap1["attachments"] == snap2["attachments"]


def test_seven_rejected_only(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 100)
    start(cfg, pid)
    run(cfg, clock)
    accepted = {}
    for i, page in enumerate(page_rows(cfg, pid)):
        with connect(cfg) as db:
            detail = projects.page_detail(db, page["id"])
        candidate = detail["candidates"][0]
        decision = "reject" if i < 7 else "accept"
        planning.review(
            cfg,
            page["id"],
            {
                "expected_version": detail["version"],
                "latest_round_id": detail["latest_round_id"],
                "decision": decision,
                "candidate_id": candidate["id"],
                "reason": "fix face",
            },
        )
        if i >= 7:
            accepted[page["id"]] = (candidate["id"], candidate["asset_hash"])
        else:
            with transaction(cfg) as db:
                p = get(db, "pages", page["id"])
                projects.patch_draft(db, p, {"correction": "keep balloon at original position"})
    _, plan = start(cfg, pid, kind="revision")
    assert len(plan["pages"]) == 7
    run(cfg, clock)
    for page in page_rows(cfg, pid)[7:]:
        assert page["accepted_candidate_id"] == accepted[page["id"]][0]
        with connect(cfg) as db:
            assert (
                get(db, "candidates", page["accepted_candidate_id"])["asset_hash"] == accepted[page["id"]][1]
            )
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 107


def test_atomic_revision_replay_and_rollback(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    run(cfg, clock)
    page = page_rows(cfg, pid)[0]
    request = {
        "expected_version": page["version"],
        "latest_round_id": page["latest_round_id"],
        "draft_patch": {"correction": "new direction"},
        "candidate_count": 4,
    }
    with pytest.raises(DomainError):
        planning.revise_and_generate(cfg, page["id"], request, "revision-key")
    current = page_rows(cfg, pid)[0]
    assert current["version"] == page["version"] and json.loads(current["draft_json"])["correction"] == ""
    request["candidate_count"] = 1
    first = planning.revise_and_generate(cfg, page["id"], request, "revision-key")
    second = planning.revise_and_generate(cfg, page["id"], request, "revision-key")
    assert first["batch_id"] == second["batch_id"]
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 2


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("reject", "failed"),
        ("mixed", "mixed"),
        ("duplicate", "unknown"),
        ("missing", "unknown"),
        ("unknown_key", "unknown"),
        ("corrupt", "failed"),
        ("text_only", "failed"),
        ("multiple", "failed"),
    ],
)
def test_provider_bad_results(cfg, tmp_path, clock, scenario, expected):
    pid, _ = make_project(cfg, tmp_path, 2, {"scenario": scenario})
    start(cfg, pid)
    run(cfg, clock)
    with connect(cfg) as db:
        states = [r[0] for r in db.execute("SELECT state FROM jobs")]
        if expected == "mixed":
            assert sorted(states) == ["failed", "succeeded"]
        else:
            assert set(states) == {expected}
        if expected != "mixed":
            assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_timeout_reconcile_does_not_resubmit(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 2, {"scenario": "timeout"})
    start(cfg, pid)
    worker = Worker(cfg, clock)
    worker.tick()
    assert worker.provider.call_count() == 1
    clock.advance(61)
    run(cfg, clock, 3)
    assert worker.provider.call_count() == 1
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE state='succeeded'").fetchone()[0] == 2


def test_unknown_zero_many_and_manual_resend(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    worker = Worker(cfg, clock)
    cid = worker.prepare()
    with transaction(cfg) as db:
        db.execute("UPDATE chunks SET state='unknown' WHERE id=?", (cid,))
        db.execute("UPDATE attempts SET state='unknown' WHERE chunk_id=?", (cid,))
        db.execute("UPDATE jobs SET state='unknown'")
        chunk = get(db, "chunks", cid)
    worker.reconcile(chunk)
    with connect(cfg) as db:
        assert get(db, "chunks", cid)["state"] == "unknown"
    assert worker.provider.call_count() == 0
    with worker.provider.db() as external:
        for n in range(2):
            external.execute(
                "INSERT INTO remote VALUES(?,?,?,?,?,?,?)",
                (f"duplicate-{n}", cid, "mock-batch-v1", "unused", 0, None, "running"),
            )
    worker.reconcile(chunk)
    with connect(cfg) as db:
        assert get(db, "chunks", cid)["last_error"] == "multiple_matches"
    with worker.provider.db() as external:
        external.execute("DELETE FROM remote")
    request = {"expected_state": "unknown", "action": "resend", "explicit_duplicate_risk": True}
    resolve(cfg, cid, request, "resolve-key")
    resolve(cfg, cid, request, "resolve-key")
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts WHERE state='abandoned'").fetchone()[0] == 1
    # Reservation of the abandoned attempt is retained; new submission needs another allowance.
    with transaction(cfg) as db:
        db.execute("UPDATE batches SET cap_micro=2")
    run(cfg, clock)
    assert worker.provider.call_count() == 1


def test_budget_retry_pause(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 2, {"scenario": "429"})
    reply, _ = start(cfg, pid, cap_micro=2)
    worker = Worker(cfg, clock)
    worker.tick()
    clock.advance(100)
    worker.tick()
    assert worker.provider.call_count() == 1
    with transaction(cfg) as db:
        b = get(db, "batches", reply["batch_id"])
        assert b["spent_reserved_micro"] == 2
        db.execute("UPDATE batches SET cap_micro=4,paused=1")
    worker.tick()
    assert worker.provider.call_count() == 1
    with transaction(cfg) as db:
        db.execute("UPDATE batches SET paused=0")
    run(cfg, clock)
    assert worker.provider.call_count() == 2
    with connect(cfg) as db:
        assert get(db, "batches", reply["batch_id"])["spent_reserved_micro"] == 4
        assert {r[0] for r in db.execute("SELECT state FROM jobs")} == {"succeeded"}


def test_review_old_candidate_preserves_acceptance(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    run(cfg, clock)
    page = page_rows(cfg, pid)[0]
    with connect(cfg) as db:
        old = projects.page_detail(db, page["id"])["candidates"][0]
    start(cfg, pid, kind="extra", page_ids=[p["id"] for p in page_rows(cfg, pid)])
    p = page_rows(cfg, pid)[0]
    planning.review(
        cfg,
        p["id"],
        {
            "expected_version": p["version"],
            "latest_round_id": p["latest_round_id"],
            "decision": "accept",
            "candidate_id": old["id"],
            "restore": True,
        },
    )
    run(cfg, clock)
    assert page_rows(cfg, pid)[0]["accepted_candidate_id"] == old["id"]
    with connect(cfg) as db:
        listed = projects.page_list(db, pid)["items"][0]
        assert listed["preview_take"] == 1
        assert listed["generation_sequence"] == 2
    with pytest.raises(DomainError):
        planning.review(
            cfg,
            p["id"],
            {"expected_version": p["version"], "latest_round_id": p["latest_round_id"], "decision": "reject"},
        )


def test_import_receipt_and_paths(cfg, tmp_path):
    pid, token = make_project(cfg, tmp_path)
    assert projects.commit_import(cfg, token)["project_id"] == pid
    from manga_drawing.assets import safe_path

    for path in ("../secret", "/tmp/a", "a/../b", "a\\b", "a//b", "C:/test"):
        with pytest.raises(DomainError):
            safe_path(path)
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    from manga_drawing.models import Manifest
    from manga_drawing.assets import decode
    from pydantic import ValidationError
    from PIL import Image
    from manga_drawing.assets import png

    manifest = {
        "schema_version": 1,
        "title": "Duplicate",
        "pages": [
            {"number": 1, "name": "x", "instructions": "x"},
            {"number": 1, "name": "x", "instructions": "x"},
        ],
    }
    with pytest.raises(ValidationError):
        Manifest.model_validate(manifest)
    with pytest.raises(DomainError):
        decode(b"not-a-png")
    with pytest.raises(DomainError):
        decode(b"x" * (20 * 1024**2 + 1))
    oversized = Image.new("1", (8000, 5001))
    with pytest.raises(DomainError):
        decode(png(oversized))


@pytest.mark.parametrize(
    "job_state,chunk_state,paused,expected",
    [
        ("queued", None, 0, "queued"),
        ("queued", None, 1, "paused"),
        ("running", "prepared", 0, "queued"),
        ("running", "uploading", 0, "sending"),
        ("running", "submitting", 0, "sending"),
        ("running", "submitted", 0, "submitted"),
        ("running", "fetching", 0, "fetching"),
        ("retry_wait", None, 0, "retry_wait"),
        ("unknown", "unknown", 0, "unknown"),
        ("failed", "failed", 0, "failed"),
    ],
)
def test_explicit_generation_states(cfg, tmp_path, clock, job_state, chunk_state, paused, expected):
    pid, _ = make_project(cfg, tmp_path, 1)
    item = page_rows(cfg, pid)[0]
    with connect(cfg) as db:
        assert projects.page_detail(db, item["id"])["generation_counts"] == {"not_requested": 1}
    start(cfg, pid)
    if chunk_state:
        Worker(cfg, clock).prepare()
    with connect(cfg) as db:
        db.execute("UPDATE jobs SET state=?", (job_state,))
        db.execute("UPDATE batches SET paused=?", (paused,))
        if chunk_state:
            db.execute("UPDATE chunks SET state=?", (chunk_state,))
        page = projects.page_detail(db, item["id"])
        assert page["generation_counts"] == {expected: 1}
        assert page["generation_label"] == projects.GENERATION_LABELS[expected]


def test_page_prompt_applies_to_first_generation_and_freezes_history(cfg, tmp_path):
    pid, token = make_project(cfg, tmp_path, 1)
    item = page_rows(cfg, pid)[0]
    with transaction(cfg) as db:
        page = get(db, "pages", item["id"])
        projects.patch_draft(db, page, {"page_prompt": "① 横顔。② 教室を背景にする。"})
    start(cfg, pid)
    with connect(cfg) as db:
        frozen = db.execute("SELECT snapshot_json FROM rounds").fetchone()[0]
        assert "① 横顔。② 教室を背景にする。" in json.loads(frozen)["prompt"]
        current = get(db, "pages", item["id"])
    with transaction(cfg) as db:
        projects.patch_draft(
            db, current, {"page_prompt": "① 正面に変更。", "correction": "髪型を資料に合わせる。"}
        )
    with connect(cfg) as db:
        assert db.execute("SELECT snapshot_json FROM rounds").fetchone()[0] == frozen
        snap, _, _ = snapshot(cfg, db, get(db, "pages", item["id"]))
        assert "① 正面に変更。" in snap["prompt"]
        assert "髪型を資料に合わせる。" in snap["prompt"]


def test_image_reimport_preserves_page_prompt_when_omitted(cfg, tmp_path):
    pid, _ = make_project(cfg, tmp_path, 1)
    item = page_rows(cfg, pid)[0]
    with transaction(cfg) as db:
        projects.patch_draft(db, get(db, "pages", item["id"]), {"page_prompt": "① 保存した作画指示"})
    folder = tmp_path / "input-1"
    manifest = json.loads((folder / "manifest.json").read_text())
    token = projects.create_import(cfg)
    for path in folder.glob("*.png"):
        projects.upload(cfg, token, path.name, path.read_bytes())
    report = projects.validate_import(cfg, token, manifest, pid)
    projects.commit_import(cfg, token, report["expected_version"])
    with connect(cfg) as db:
        assert projects.page_detail(db, item["id"])["draft"]["page_prompt"] == "① 保存した作画指示"
