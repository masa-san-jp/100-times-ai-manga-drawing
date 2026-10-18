import pytest

from conftest import make_project, page_rows, run, start
from manga_drawing import export, planning, projects
from manga_drawing.common import DomainError
from manga_drawing.db import connect, get


def adopt_all(cfg, pid):
    for row in page_rows(cfg, pid):
        with connect(cfg) as db:
            p = projects.page_detail(db, row["id"])
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


def test_workflow_complete_and_stale_export_without_new_generation(cfg, tmp_path, clock, client):
    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)
    run(cfg, clock)
    adopt_all(cfg, pid)
    with connect(cfg) as db:
        jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        version = get(db, "projects", pid)["version"]
    status = client.get(f"/api/projects/{pid}/progress").json()["data"]["workflow"]
    assert status["phase"] == "export"
    assert status["pages"] == {"accepted": 2, "total": 2, "unreviewed": 0, "rejected": 0, "not_requested": 0}
    assert status["characters"] == {"accepted": 1, "total": 1}
    assert "全ページ採用済み" in status["message"]
    exported = export.schedule(cfg, pid, {"mode": "complete", "expected_version": version})
    assert client.get(f"/api/projects/{pid}/progress").json()["data"]["workflow"]["phase"] == "exporting"
    export.process_export(cfg)
    status = client.get(f"/api/projects/{pid}/progress").json()["data"]["workflow"]
    assert status["phase"] == "complete"
    assert status["export"]["id"] == exported["export_id"]
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs
        assert get(db, "projects", pid)["version"] == version
        db.execute("UPDATE projects SET settings_json=? WHERE id=?", ('{"width":1000}', pid))
    status = client.get(f"/api/projects/{pid}/progress").json()["data"]["workflow"]
    assert status["phase"] == "export"
    assert not status["export"]["current"]


def test_accepted_pages_excluded_and_extra_requires_explicit_target(cfg, tmp_path, clock):
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    run(cfg, clock)
    adopt_all(cfg, pid)
    for kind in ["initial", "revision"]:
        with pytest.raises(DomainError, match="対象ページ"):
            start(cfg, pid, kind=kind)
    with pytest.raises(DomainError, match="明示的"):
        start(cfg, pid, kind="extra")
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    start(cfg, pid, kind="extra", page_ids=[page_rows(cfg, pid)[0]["id"]])
    assert page_rows(cfg, pid)[0]["accepted_candidate_id"] is not None


def test_workflow_counts_all_pages_and_reading_does_not_mutate(cfg, tmp_path, client):
    pid, _ = make_project(cfg, tmp_path, 81)
    before = page_rows(cfg, pid)
    status = client.get(f"/api/projects/{pid}/progress").json()["data"]["workflow"]
    assert status["pages"]["total"] == 81
    assert status["pages"]["not_requested"] == 81
    assert status["phase"] == "generation"
    assert len(client.get(f"/api/projects/{pid}/pages").json()["data"]["items"]) == 40
    assert page_rows(cfg, pid) == before
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_export_page_mapping_and_unused_character_do_not_mislead(cfg, tmp_path, clock, client):
    import json
    from manga_drawing.common import timestamp

    pid, _ = make_project(cfg, tmp_path, 2)
    with connect(cfg) as db:
        original = projects.references(db, pid)[0]
        db.execute(
            "INSERT INTO reference_sets VALUES(?,?,?,?,?)", ("unused-ref", pid, "character", "unused", None)
        )
        db.execute(
            "INSERT INTO reference_revisions VALUES(?,?,?,?,?)",
            (
                "unused-rev",
                "unused-ref",
                json.dumps(original["metadata"] | {"key": "unused", "name": "Unused"}),
                json.dumps(original["images"]),
                timestamp(),
            ),
        )
        db.execute("UPDATE reference_sets SET current_revision_id='unused-rev' WHERE id='unused-ref'")
    status = client.get(f"/api/projects/{pid}/progress").json()["data"]["workflow"]
    assert status["characters"] == {"accepted": 1, "total": 2}
    assert status["phase"] == "generation"
    start(cfg, pid)
    run(cfg, clock)
    adopt_all(cfg, pid)
    with connect(cfg) as db:
        version = get(db, "projects", pid)["version"]
    result = export.schedule(cfg, pid, {"mode": "complete", "expected_version": version})
    export.process_export(cfg)
    with connect(cfg) as db:
        manifest = json.loads(get(db, "exports", result["export_id"])["manifest_json"])
        manifest["pages"][0]["number"], manifest["pages"][1]["number"] = (
            manifest["pages"][1]["number"],
            manifest["pages"][0]["number"],
        )
        db.execute(
            "UPDATE exports SET manifest_json=? WHERE id=?", (json.dumps(manifest), result["export_id"])
        )
    status = client.get(f"/api/projects/{pid}/progress").json()["data"]["workflow"]
    assert status["phase"] == "export"
    assert not status["export"]["current"]
