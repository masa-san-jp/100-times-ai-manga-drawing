import json
import sqlite3
from importlib.resources import files

import pytest

from conftest import make_project, start
from manga_drawing.common import DomainError
from manga_drawing.config import Config
from manga_drawing.db import connect, initialize
from manga_drawing import projects


def test_existing_database_migrates_without_changing_pages(tmp_path):
    cfg = Config(tmp_path / "old")
    cfg.prepare()
    with sqlite3.connect(cfg.root / "db.sqlite3") as db:
        db.executescript(files("manga_drawing").joinpath("migrations/001_initial.sql").read_text())
        db.execute("INSERT INTO projects(id,title,created_at) VALUES('p','既存作品','today')")
        db.execute("INSERT INTO pages(id,project_id,number,draft_json) VALUES('page','p',1,'{}')")
        db.execute("PRAGMA user_version=1")
    initialize(cfg)
    initialize(cfg)
    with connect(cfg) as db:
        assert dict(db.execute("SELECT * FROM works").fetchone())["title"] == "既存作品"
        assert db.execute("SELECT work_id FROM projects").fetchone()[0] == "p"
        assert db.execute("SELECT id FROM pages").fetchone()[0] == "page"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_group_episodes_and_preserve_import_history(cfg, tmp_path, client):
    first, _ = make_project(cfg, tmp_path, pages=1)
    second, token = make_project(cfg, tmp_path, pages=2)
    with connect(cfg) as db:
        version = db.execute("SELECT version FROM projects WHERE id=?", (second,)).fetchone()[0]
        record = db.execute("SELECT * FROM input_imports WHERE id=?", (token,)).fetchone()
        assert json.loads(record["files_json"])
        for item in json.loads(record["files_json"]).values():
            assert item["original"] and item["normalized"]
    response = client.patch(
        f"/api/projects/{second}",
        json={"expected_version": version, "patch": {"work_id": first, "lifecycle": "archived"}},
    )
    assert response.status_code == 200
    assert response.json()["data"]["work_id"] == first
    with pytest.raises(DomainError, match="制作中"):
        start(cfg, second)
    assert client.get("/").status_code == 200
    assert client.get(f"/projects/{second}/settings").status_code == 200
    version = response.json()["data"]["version"]
    assert (
        client.patch(
            f"/api/projects/{second}", json={"expected_version": version, "patch": {"lifecycle": "completed"}}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/projects/{second}", json={"expected_version": version, "patch": {"lifecycle": "active"}}
        ).status_code
        == 200
    )
    start(cfg, second)
    assert projects.commit_import(cfg, token)["project_id"] == second


def test_import_new_episode_into_existing_work(cfg, tmp_path):
    first, _ = make_project(cfg, tmp_path, pages=1)
    from manga_drawing.cli import example

    folder = tmp_path / "next-episode"
    manifest = example(folder, 2)
    manifest["title"] = "第2話"
    token = projects.create_import(cfg)
    for path in folder.glob("*.png"):
        projects.upload(cfg, token, path.name, path.read_bytes())
    assert not projects.validate_import(cfg, token, manifest)["errors"]
    second = projects.commit_import(cfg, token, work_id=first)["project_id"]
    with connect(cfg) as db:
        assert db.execute("SELECT work_id FROM projects WHERE id=?", (second,)).fetchone()[0] == first
        assert db.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM pages WHERE project_id=?", (first,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM pages WHERE project_id=?", (second,)).fetchone()[0] == 2
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert projects.commit_import(cfg, token, work_id=first)["project_id"] == second
