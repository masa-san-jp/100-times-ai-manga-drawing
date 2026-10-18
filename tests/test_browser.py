"""Opt-in local Chromium tests. MANGA_BROWSER_TESTS=1; never contacts a generation API."""

import os
from pathlib import Path
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from manga_drawing.cli import example
from manga_drawing.db import connect
from manga_drawing.web import create_app
from conftest import make_project, page_rows, run, start

pytestmark = pytest.mark.skipif(
    os.environ.get("MANGA_BROWSER_TESTS") != "1",
    reason="Run with MANGA_BROWSER_TESTS=1 and installed Chromium",
)


@pytest.fixture
def browser_app(cfg):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        yield page, context, f"http://127.0.0.1:{port}"
        browser.close()
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()


def test_browser_import_review_regenerate_and_themes(browser_app, cfg, tmp_path, clock):
    page, context, base = browser_app
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    folder = tmp_path / "sample"
    example(folder, 3)
    page.goto(base)
    for mode in ("light", "dark"):
        page.locator("#theme").select_option(mode)
        Path("artifacts/screenshots").mkdir(parents=True, exist_ok=True)
        page.screenshot(path=f"artifacts/screenshots/settings-{mode}-1280.png", full_page=True)
    page.locator("#folder").set_input_files(str(folder))
    expect(page.locator("#commit-import")).to_be_visible()
    page.locator("#commit-import").click()
    page.wait_for_url("**/projects/*")
    expect(page.locator(".page-card")).to_have_count(3)
    for mode in ("light", "dark"):
        page.locator("#theme").select_option(mode)
        page.screenshot(path=f"artifacts/screenshots/pages-{mode}-1280.png", full_page=True)
    page.locator("#generate").click()
    expect(page.locator("#plan-summary")).to_contain_text("対象3ページ")
    run(cfg, clock)
    page.reload()
    expect(page.locator(".card-caption span").first).to_have_text("確認待ち")
    page.locator(".page-card a").first.click()
    expect(page.locator("#candidate-select option")).to_have_count(1)
    page.locator("#accept").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#page-status")).to_have_text("採用済み")
    artifacts = Path("artifacts/screenshots")
    artifacts.mkdir(parents=True, exist_ok=True)
    for mode in ("light", "dark"):
        page.locator("#theme").select_option(mode)
        expect(page.locator("html")).to_have_attribute("data-theme", mode)
        colors = page.evaluate(
            """() => {const body=getComputedStyle(document.body),button=getComputedStyle(document.getElementById('accept'));return [body.color,body.backgroundColor,button.color,button.backgroundColor];}"""
        )
        assert contrast(colors[0], colors[1]) >= 4.5
        assert contrast(colors[2], colors[3]) >= 4.5

        page.screenshot(path=str(artifacts / f"detail-{mode}-1280.png"), full_page=True)
        page.set_viewport_size({"width": 360, "height": 900})
        page.screenshot(path=str(artifacts / f"detail-{mode}-360.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        page.set_viewport_size({"width": 1280, "height": 900})
    pid = page_rows(cfg, next(iter(_project_ids(cfg))))[0]["project_id"]
    page.goto(f"{base}/projects/{pid}")
    page.locator(".page-card a").nth(1).click()
    page.locator("#correction").fill("Move face to the right; preserve balloon.")
    page.locator("#reject-save").click()
    expect(page.locator("#page-status")).to_have_text("要修正")
    page.goto(f"{base}/projects/{pid}")
    page.locator("#regenerate").click()
    expect(page.locator("#plan-summary")).to_contain_text("対象1ページ")
    run(cfg, clock)
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4
    assert not errors


def _project_ids(cfg):
    with connect(cfg) as db:
        return [r[0] for r in db.execute("SELECT id FROM projects")]


def test_dirty_poll_keeps_base_version_and_fields(browser_app, cfg, tmp_path, clock):
    page, context, base = browser_app
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    run(cfg, clock)
    p = page_rows(cfg, pid)[0]
    url = f"{base}/pages/{p['id']}"
    page.goto(url)
    expect(page.locator("#candidate-select option")).to_have_count(1)
    other = context.new_page()
    other.goto(url)
    expect(other.locator("#candidate-select option")).to_have_count(1)
    page.locator("#correction").fill("Tab A unsaved")
    other.locator("#correction").fill("Tab B saved")
    other.locator("#save-draft").click()
    expect(other.locator("#dirty")).to_have_text("保存済み")
    expect(page.locator("#dirty")).to_contain_text("別の画面", timeout=8000)
    expect(page.locator("#correction")).to_have_value("Tab A unsaved")
    page.locator("#save-draft").click()
    expect(page.locator("#notice")).to_contain_text("変更されました")
    with connect(cfg) as db:
        import json

        saved = json.loads(db.execute("SELECT draft_json FROM pages WHERE id=?", (p["id"],)).fetchone()[0])
        assert saved["correction"] == "Tab B saved"
    other.close()


def test_response_loss_replays_operation_after_reload(browser_app, cfg, tmp_path, clock):
    page, _, base = browser_app
    pid, _ = make_project(cfg, tmp_path, 1)
    page.goto(f"{base}/projects/{pid}")
    count = {"value": 0}

    def lose_response(route):
        count["value"] += 1
        route.fetch()  # server commits, browser receives a network failure
        route.abort("failed")

    page.route("**/api/projects/*/batches", lose_response)
    page.locator("#generate").click()
    expect(page.locator("#notice")).to_be_visible()
    run(cfg, clock)
    page.unroute("**/api/projects/*/batches", lose_response)
    page.reload()
    expect(page.locator(".page-card")).to_have_count(1)
    page.locator("#generate").click()
    expect(page.locator("#generate")).to_be_enabled()
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert count["value"] == 1


def test_diagnostic_inputs_survive_poll(browser_app, cfg, tmp_path, clock):
    page, _, base = browser_app
    pid, _ = make_project(cfg, tmp_path, 1, {"scenario": "missing"})
    start(cfg, pid)
    run(cfg, clock)
    page.goto(f"{base}/projects/{pid}")
    page.get_by_text("処理の詳細と復旧", exact=True).click()
    external = page.locator("#chunk-details input")
    expect(external).to_have_count(1)
    external.fill("external-id-being-entered")
    page.wait_for_timeout(5500)
    expect(external).to_have_value("external-id-being-entered")
    budget = page.locator("#batch-details input")
    budget.fill("17")
    page.wait_for_timeout(5500)
    expect(budget).to_have_value("17")


def test_revision_response_loss_does_not_discard_new_edit(browser_app, cfg, tmp_path, clock):
    page, _, base = browser_app
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    run(cfg, clock)
    p = page_rows(cfg, pid)[0]
    page.goto(f"{base}/pages/{p['id']}")
    expect(page.locator("#candidate-select option")).to_have_count(1)
    page.locator("#correction").fill("First correction sent")

    def lose_response(route):
        route.fetch()
        route.abort("failed")

    page.route("**/api/pages/*/revise-and-generate", lose_response)
    page.locator("#revise").click()
    expect(page.locator("#notice")).to_be_visible()
    page.locator("#correction").fill("NEW correction must not be discarded")
    page.unroute("**/api/pages/*/revise-and-generate", lose_response)
    page.locator("#revise").click()
    expect(page.locator("#notice")).to_contain_text("新しい編集内容は未保存")
    expect(page.locator("#correction")).to_have_value("NEW correction must not be discarded")
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 2
        import json

        saved = json.loads(db.execute("SELECT draft_json FROM pages WHERE id=?", (p["id"],)).fetchone()[0])
        assert saved["correction"] == "First correction sent"
    page.locator("#rebase-edit").click()
    page.locator("#save-draft").click()
    expect(page.locator("#dirty")).to_have_text("保存済み")
    with connect(cfg) as db:
        saved = json.loads(db.execute("SELECT draft_json FROM pages WHERE id=?", (p["id"],)).fetchone()[0])
        assert saved["correction"] == "NEW correction must not be discarded"


def contrast(first, second):
    import re

    def luminance(value):
        channels = [int(c) / 255 for c in re.findall(r"\d+", value)[:3]]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * w for c, w in zip(linear, (0.2126, 0.7152, 0.0722)))

    a, b = sorted((luminance(first), luminance(second)))
    return (b + 0.05) / (a + 0.05)
