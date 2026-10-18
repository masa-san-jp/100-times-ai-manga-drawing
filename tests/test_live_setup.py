"""Local live setup must expose no secrets, make no requests, and create no data."""

from dataclasses import replace
import json

from manga_drawing.config import Config
from manga_drawing.db import connect
from manga_drawing.maintenance import live_check
from conftest import make_project, start


def test_dotenv_explicit_directory_no_interpolation_or_environment_mutation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "MANGA_PROVIDER",
        "MANGA_ENABLE_LIVE",
        "GEMINI_API_KEY",
        "MANGA_DATA_DIR",
        "MANGA_LIVE_MAX_REQUESTS",
        "MANGA_LIVE_UNIT_RESERVE_MICRO_USD",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "MANGA_PROVIDER=gemini\nMANGA_ENABLE_LIVE=1\nGEMINI_API_KEY='secret-${HOME}'\n"
        "MANGA_LIVE_MAX_REQUESTS=3\nMANGA_LIVE_UNIT_RESERVE_MICRO_USD=123456\n"
        "MANGA_DATA_DIR=.data/live\n"
    )
    cfg = Config.from_env()
    assert cfg.root == tmp_path / ".data/live"
    assert cfg.gemini_api_key == "secret-${HOME}"
    assert cfg.live_request_limit == 3 and cfg.live_unit_micro == 123456
    assert cfg.provider == "gemini" and cfg.enable_live
    assert "secret-" not in repr(cfg)
    import os

    assert "GEMINI_API_KEY" not in os.environ
    monkeypatch.setenv("GEMINI_API_KEY", "environment-takes-priority")
    assert Config.from_env().gemini_api_key == "environment-takes-priority"
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert Config.from_env().provider == "mock"  # Never search parent directories.


def test_live_check_no_client_no_storage_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr("manga_drawing.maintenance.version", lambda name: "2.31.0")
    cfg = Config(tmp_path / "new-data")
    report = live_check(cfg)
    assert not report["ready"] and report["api_requests_made"] == 0
    assert "GEMINI_API_KEY" in report["missing"]
    assert not cfg.root.exists()
    ready = replace(
        cfg,
        provider="gemini",
        enable_live=True,
        gemini_api_key="secret-test",
        live_request_limit=3,
        live_unit_micro=123456,
    )
    report = live_check(ready)
    assert report["ready"] and report["required_requests"] == 3
    assert "secret-test" not in json.dumps(report)
    assert not cfg.root.exists()


def test_live_check_counts_existing_reservations_and_rejects_bad_database(cfg, monkeypatch):
    monkeypatch.setattr("manga_drawing.maintenance.version", lambda name: "2.31.0")
    cfg = replace(
        cfg,
        provider="gemini",
        enable_live=True,
        gemini_api_key="secret-test",
        live_request_limit=3,
        live_unit_micro=123456,
    )
    with connect(cfg) as db:
        db.execute("INSERT INTO system(key,value) VALUES('gemini_reserved_requests','1')")
    report = live_check(cfg)
    assert report["reserved_requests"] == 1
    assert not report["ready"] and "remaining_requests_at_least_3" in report["missing"]
    with connect(cfg) as db:
        db.execute("UPDATE system SET value='invalid' WHERE key='gemini_reserved_requests'")
    assert "database_readable" in live_check(cfg)["missing"]


def test_live_check_rejects_unreserved_live_queue(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("manga_drawing.maintenance.version", lambda name: "2.31.0")
    cfg = replace(
        cfg,
        provider="gemini",
        enable_live=True,
        gemini_api_key="secret-test",
        live_request_limit=3,
        live_unit_micro=123456,
    )
    pid, _ = make_project(cfg, tmp_path, pages=2)
    start(cfg, pid)
    report = live_check(cfg)
    assert report["reserved_requests"] == 0 and report["existing_live_jobs"] == 2
    assert not report["ready"] and "no_existing_live_jobs" in report["missing"]
