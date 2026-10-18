import json

import pytest
from fastapi.testclient import TestClient

from manga_drawing.cli import example
from manga_drawing.config import Config
from manga_drawing.db import connect, initialize
from manga_drawing import projects, planning
from manga_drawing.worker import Worker
from manga_drawing.web import create_app


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds=3):
        self.value += seconds


@pytest.fixture
def cfg(tmp_path):
    config = Config(tmp_path / "data", mock_delay=0)
    initialize(config)
    return config


@pytest.fixture
def clock():
    return Clock()


def make_project(cfg, tmp_path, pages=3, settings=None, ready_references=True):
    folder = tmp_path / f"input-{pages}"
    manifest = example(folder, pages)
    token = projects.create_import(cfg)
    for path in folder.glob("*.png"):
        projects.upload(cfg, token, path.name, path.read_bytes())
    report = projects.validate_import(cfg, token, manifest)
    assert not report["errors"]
    pid = projects.commit_import(cfg, token)["project_id"]
    if ready_references:
        seed_character_references(cfg, pid)
    if settings:
        with connect(cfg) as db:
            db.execute("UPDATE projects SET settings_json=? WHERE id=?", (json.dumps(settings), pid))
    return pid, token


def start(cfg, pid, **kwargs):
    from manga_drawing.db import transaction
    from manga_drawing.common import uid

    with transaction(cfg) as db:
        plan = planning.make_plan(cfg, db, pid, kwargs)
    return planning.start(cfg, pid, plan["plan_token"], uid()), plan


def run(cfg, clock, ticks=4):
    worker = Worker(cfg, clock)
    for _ in range(ticks):
        worker.tick()
        clock.advance()
    return worker


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg), base_url="http://127.0.0.1") as c:
        c.get("/")
        csrf = c.get("/api/session").json()["data"]["csrf"]
        c.headers["X-CSRF-Token"] = csrf
        yield c


def page_rows(cfg, pid):
    with connect(cfg) as db:
        return [dict(r) for r in db.execute("SELECT * FROM pages WHERE project_id=? ORDER BY number", (pid,))]


def seed_character_references(cfg, pid):
    """Unit fixtures treat their synthetic image as an already approved sheet.

    Full reference generation and review are tested separately; no production bypass.
    """
    from manga_drawing.common import timestamp

    with connect(cfg) as db:
        for ref in projects.references(db, pid):
            if ref["kind"] == "character":
                db.execute(
                    "INSERT OR REPLACE INTO character_reference_selections VALUES(?,?,?,?,?)",
                    (ref["id"], ref["current_revision_id"], None, ref["images"][0], timestamp()),
                )
