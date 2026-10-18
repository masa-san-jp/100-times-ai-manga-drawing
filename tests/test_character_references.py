import json

import pytest

from conftest import make_project, page_rows, run, start
from manga_drawing import character_references as characters, planning, projects
from manga_drawing.common import DomainError, timestamp
from manga_drawing.db import connect, get, transaction
from manga_drawing.prompts import snapshot


def refs(cfg, pid):
    with connect(cfg) as db:
        return characters.list_references(db, pid)


def adopt(cfg, ref, candidate=None):
    p = ref["page"]
    return planning.review(
        cfg,
        p["id"],
        {
            "expected_version": p["version"],
            "latest_round_id": p["latest_round_id"],
            "decision": "accept",
            "candidate_id": candidate or p["candidates"][0]["id"],
            "restore": True,
        },
    )


def test_reference_pipeline_gates_pages_and_uses_approved_generated_image(cfg, tmp_path, clock, client):
    pid, _ = make_project(cfg, tmp_path, 1, ready_references=False)
    with pytest.raises(DomainError, match="リファレンス"):
        start(cfg, pid)
    assert refs(cfg, pid)[0]["page"] is None
    result = characters.generate(cfg, pid, {}, "initial-reference")
    assert characters.generate(cfg, pid, {}, "initial-reference") == result
    run(cfg, clock)
    ref = refs(cfg, pid)[0]
    assert ref["page"]["status"] == "review"
    with pytest.raises(DomainError, match="リファレンス"):
        start(cfg, pid)
    adopt(cfg, ref)
    with connect(cfg) as db:
        page = get(db, "pages", page_rows(cfg, pid)[0]["id"])
        payload, _, _ = snapshot(cfg, db, page)
        generated = ref["page"]["candidates"][0]["asset_hash"]
        assert generated != ref["sketch"]
        assert next(a["hash"] for a in payload["attachments"] if a["role"] == "character") == generated
        assert payload["references"][0]["candidate_id"] == ref["page"]["candidates"][0]["id"]
        reference_snapshot = ref["page"]["latest_round"]["snapshot"]
        assert reference_snapshot["attachments"] == [{"role": "キャラクタースケッチ", "hash": ref["sketch"]}]
        assert reference_snapshot["template_version"] == "character-reference-author-original-v1"
        assert len(projects.page_list(db, pid)["items"]) == 1
    listed = client.get("/api/projects").json()["data"]
    assert [x["id"] for x in listed] == [pid]
    assert client.get("/").text.count("episode-item") == 1
    run(cfg, clock)
    with connect(cfg) as db:
        folder = db.execute("SELECT value FROM system WHERE key=?", (f"work_folder:{pid}",)).fetchone()[0]
    from pathlib import Path

    output = Path(folder) / "キャラクターリファレンス" / ref["key"]
    assert (output / "テイク01.png").is_file()
    assert json.loads((output / "候補一覧.json").read_text())[0]["status"] == "採用"
    assert (output / "テイク01_生成指示.txt").is_file()
    start(cfg, pid)
    run(cfg, clock)
    page = page_rows(cfg, pid)[0]
    detail = projects.page_detail(connect(cfg), page["id"])
    planning.review(
        cfg,
        page["id"],
        {
            "expected_version": detail["version"],
            "latest_round_id": detail["latest_round_id"],
            "decision": "accept",
            "candidate_id": detail["candidates"][0]["id"],
        },
    )
    from manga_drawing.export import schedule, process_export, output_folder

    exported = schedule(
        cfg, pid, {"mode": "complete", "expected_version": get(connect(cfg), "projects", pid)["version"]}
    )
    process_export(cfg)
    with connect(cfg) as db:
        assert output_folder(cfg, get(db, "exports", exported["export_id"])) == Path(folder) / "生成結果"


def test_reference_regeneration_only_target_and_keeps_approved_until_replaced(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1, ready_references=False)
    # Add a second character without altering the comic page.
    with transaction(cfg) as db:
        first = projects.references(db, pid)[0]
        db.execute(
            "INSERT INTO reference_sets VALUES(?,?,?,?,?)", ("ref-second", pid, "character", "second", None)
        )
        metadata = first["metadata"] | {"key": "second", "name": "Second"}
        db.execute(
            "INSERT INTO reference_revisions VALUES(?,?,?,?,?)",
            ("revision-second", "ref-second", json.dumps(metadata), json.dumps(first["images"]), timestamp()),
        )
        db.execute("UPDATE reference_sets SET current_revision_id='revision-second' WHERE id='ref-second'")
    characters.generate(cfg, pid, {}, "all")
    run(cfg, clock)
    for r in refs(cfg, pid):
        adopt(cfg, r)
    before = refs(cfg, pid)
    target = before[0]
    with transaction(cfg) as db:
        projects.patch_draft(
            db, get(db, "pages", target["page"]["id"]), {"page_prompt": "髪型をスケッチに忠実に。"}
        )
    characters.generate(cfg, pid, {"reference_ids": [target["id"]]}, "one")
    run(cfg, clock)
    after = refs(cfg, pid)
    assert after[0]["selection"]["candidate_id"] == before[0]["selection"]["candidate_id"]
    assert after[0]["page"]["latest_round"]["sequence"] == 2
    assert after[1]["page"]["latest_round_id"] == before[1]["page"]["latest_round_id"]
    assert "髪型をスケッチに忠実に。" in after[0]["page"]["latest_round"]["prompt_text"]
    adopt(cfg, after[0])
    assert refs(cfg, pid)[0]["selection"]["candidate_id"] != before[0]["selection"]["candidate_id"]
    r = refs(cfg, pid)[0]
    p = r["page"]
    planning.review(
        cfg,
        p["id"],
        {
            "expected_version": p["version"],
            "latest_round_id": p["latest_round_id"],
            "decision": "reject",
            "clear_accepted": True,
            "reason": "顔を直す",
        },
    )
    with pytest.raises(DomainError, match="リファレンス"):
        start(cfg, pid)
    characters.generate(cfg, pid, {}, "rejected-only")
    run(cfg, clock)
    assert refs(cfg, pid)[0]["page"]["latest_round"]["sequence"] == 3
    assert refs(cfg, pid)[1]["page"]["latest_round_id"] == before[1]["page"]["latest_round_id"]


def test_source_update_invalidates_reference_and_pending_page_plan(cfg, tmp_path, clock, client):
    pid, _ = make_project(cfg, tmp_path, 1, ready_references=False)
    characters.generate(cfg, pid, {}, "first")
    run(cfg, clock)
    r = refs(cfg, pid)[0]
    adopt(cfg, r)
    with transaction(cfg) as db:
        plan = planning.make_plan(cfg, db, pid, {})
    response = client.patch(
        f"/api/references/{r['id']}",
        json={"expected_revision": r["revision"], "metadata": {"height_cm": 171}},
    )
    assert response.status_code == 200
    assert refs(cfg, pid)[0]["selection"] is None
    with pytest.raises(DomainError):
        planning.start(cfg, pid, plan["plan_token"], "stale-page-plan")
    with pytest.raises(DomainError, match="元資料"):
        adopt(cfg, refs(cfg, pid)[0])
    with pytest.raises(DomainError, match="リファレンス"):
        start(cfg, pid)
    characters.generate(cfg, pid, {}, "new-source")
    run(cfg, clock)
    adopt(cfg, refs(cfg, pid)[0])
    start(cfg, pid)


def test_reference_active_request_is_not_duplicated_and_prompt_saved(cfg, tmp_path, client):
    pid, _ = make_project(cfg, tmp_path, 1, ready_references=False)
    r = refs(cfg, pid)[0]
    response = client.patch(
        f"/api/references/{r['id']}/generation-prompt",
        json={"expected_revision": r["revision"], "expected_version": None, "prompt": "全身と顔を描く"},
    )
    assert response.status_code == 200
    assert refs(cfg, pid)[0]["page"]["draft"]["page_prompt"] == "全身と顔を描く"
    response = client.post(
        f"/api/projects/{pid}/characters/generate", json={}, headers={"Idempotency-Key": "ui-first"}
    )
    assert response.status_code == 202
    duplicate = client.post(
        f"/api/projects/{pid}/characters/generate", json={}, headers={"Idempotency-Key": "ui-first"}
    )
    assert duplicate.json() == response.json()
    response = client.post(
        f"/api/projects/{pid}/characters/generate",
        json={"reference_ids": [r["id"]]},
        headers={"Idempotency-Key": "ui-again"},
    )
    assert response.status_code == 409
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_reimport_unchanged_sketch_keeps_approved_reference(cfg, tmp_path, clock):
    pid, token = make_project(cfg, tmp_path, 1, ready_references=False)
    characters.generate(cfg, pid, {}, "first")
    run(cfg, clock)
    adopt(cfg, refs(cfg, pid)[0])
    before = refs(cfg, pid)[0]
    # Re-register the same source manifest as an update of the comic.
    folder, meta = projects.import_meta(cfg, token)
    manifest = json.loads((folder / "plan.json").read_text())["manifest"]
    projects.validate_import(cfg, token, manifest, pid)
    with connect(cfg) as db:
        version = get(db, "projects", pid)["version"]
    projects.commit_import(cfg, token, expected_version=version)
    after = refs(cfg, pid)[0]
    assert after["revision"] == before["revision"]
    assert after["selection"] == before["selection"]
    start(cfg, pid)


def test_reference_failure_recovers_without_losing_prior_takes(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1, ready_references=False)
    with transaction(cfg) as db:
        internal = characters.ensure_tasks(db, pid)
        db.execute(
            "UPDATE projects SET settings_json=? WHERE id=?",
            (json.dumps({"scenario": "text_only"}), internal),
        )
    characters.generate(cfg, pid, {}, "broken")
    run(cfg, clock)
    r = refs(cfg, pid)[0]
    assert r["page"]["status"] == "error"
    assert r["selection"] is None
    with transaction(cfg) as db:
        db.execute("UPDATE projects SET settings_json='{}' WHERE id=?", (internal,))
    characters.generate(cfg, pid, {"reference_ids": [r["id"]]}, "recovery")
    run(cfg, clock)
    r = refs(cfg, pid)[0]
    assert r["page"]["latest_round"]["sequence"] == 2
    adopt(cfg, r)
    with connect(cfg) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM rounds WHERE page_id=?", (r["page"]["id"],)).fetchone()[0] == 2
        )
    start(cfg, pid)


def test_old_source_plan_and_unavailable_reference_are_rejected(cfg, tmp_path, clock, client):
    pid, _ = make_project(cfg, tmp_path, 1, ready_references=False)
    with transaction(cfg) as db:
        internal = characters.ensure_tasks(db, pid)
        plan = planning.make_plan(cfg, db, internal, {})
    ref = refs(cfg, pid)[0]
    client.patch(
        f"/api/references/{ref['id']}",
        json={"expected_revision": ref["revision"], "metadata": {"height_cm": 172}},
    )
    with pytest.raises(DomainError, match="資料が更新"):
        planning.start(cfg, internal, plan["plan_token"], "old-source")
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    characters.generate(cfg, pid, {}, "current-source")
    run(cfg, clock)
    ref = refs(cfg, pid)[0]
    adopt(cfg, ref)
    with transaction(cfg) as db:
        db.execute(
            "UPDATE candidates SET availability='unavailable' WHERE id=?",
            (ref["page"]["candidates"][0]["id"],),
        )
    assert refs(cfg, pid)[0]["selection"] is None
    assert refs(cfg, pid)[0]["selection_problem"] == "unavailable"
    with pytest.raises(DomainError, match="リファレンス"):
        start(cfg, pid)


def test_internal_tasks_cannot_export_comic_and_casefold_keys_are_distinct(cfg, tmp_path, clock, client):
    from manga_drawing.export import schedule

    pid, _ = make_project(cfg, tmp_path, 1, ready_references=False)
    characters.generate(cfg, pid, {}, "reference")
    run(cfg, clock)
    r = refs(cfg, pid)[0]
    adopt(cfg, r)
    internal = r["page"]["project_id"]
    with connect(cfg) as db:
        version = get(db, "projects", internal)["version"]
    with pytest.raises(DomainError, match="漫画ページ"):
        schedule(cfg, internal, {"mode": "complete", "expected_version": version})
    assert client.get(f"/projects/{internal}").status_code == 422
    assert client.get(f"/projects/{internal}/settings").status_code == 422
    with transaction(cfg) as db:
        first = projects.references(db, pid)[0]
        for i, key in enumerate(["X", "x", "é", "e\u0301"]):
            rid = f"case-ref-{i}"
            rev = f"case-rev-{i}"
            db.execute("INSERT INTO reference_sets VALUES(?,?,?,?,?)", (rid, pid, "character", key, None))
            db.execute(
                "INSERT INTO reference_revisions VALUES(?,?,?,?,?)",
                (
                    rev,
                    rid,
                    json.dumps(first["metadata"] | {"key": key}),
                    json.dumps(first["images"]),
                    timestamp(),
                ),
            )
            db.execute("UPDATE reference_sets SET current_revision_id=? WHERE id=?", (rev, rid))
        import unicodedata

        folders = [characters.character_folder_key(db, pid, k) for k in ["X", "x", "é", "e\u0301"]]
        assert len({unicodedata.normalize("NFC", f).casefold() for f in folders}) == 4


def test_schema2_upgrade_preserves_existing_adopted_comic_data(cfg, tmp_path, clock):
    from manga_drawing.db import initialize

    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    run(cfg, clock)
    p = projects.page_detail(connect(cfg), page_rows(cfg, pid)[0]["id"])
    planning.review(
        cfg,
        p["id"],
        {
            "expected_version": p["version"],
            "latest_round_id": p["latest_round_id"],
            "decision": "accept",
            "candidate_id": p["candidates"][0]["id"],
        },
    )
    with connect(cfg) as db:
        before = {
            table: [dict(r) for r in db.execute(f"SELECT * FROM {table}")]
            for table in ["pages", "rounds", "candidates", "assets", "review_events"]
        }
        db.executescript("""DROP TABLE character_reference_selections;
          DROP TABLE character_reference_tasks;
          DROP INDEX one_reference_project;
          ALTER TABLE projects DROP COLUMN parent_project_id;
          ALTER TABLE projects DROP COLUMN purpose;
          PRAGMA user_version=2;""")
    initialize(cfg)
    initialize(cfg)
    with connect(cfg) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert get(db, "projects", pid)["purpose"] == "comic"
        for table, records in before.items():
            assert [dict(r) for r in db.execute(f"SELECT * FROM {table}")] == records
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
