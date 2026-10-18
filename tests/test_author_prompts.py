"""Author source text must survive import, snapshot and wire serialization unchanged."""

from dataclasses import replace
import hashlib
import json

import pytest
from pathlib import Path

from manga_drawing import projects
from manga_drawing.character_references import REFERENCE_RULES
from manga_drawing.cli import example
from manga_drawing.db import connect
from manga_drawing.prompts import RULES, snapshot
from manga_drawing.providers.gemini import GeminiProvider
from conftest import seed_character_references

SOURCE = Path(__file__).resolve().parents[1] / "docs/source-prompts/article-originals.json"


def test_author_originals_are_anchored():
    source = json.loads(SOURCE.read_text())
    assert RULES == source["pages"][0]["text"]
    assert REFERENCE_RULES == source["character_reference"]
    assert hashlib.sha256(RULES.encode()).hexdigest() == "e035e5a26f0691e7c5eed8a0b1f97cefd426a31ef47b1d1d53c69fa2241b22b5"
    assert hashlib.sha256(REFERENCE_RULES.encode()).hexdigest() == "fb0eb02b50b7c7af944c9aef9998f634f622aba2790dc8bf3f815152f942898a"


@pytest.mark.parametrize("field", ["prompt_override", "base_prompt"])
def test_all_author_page_prompts_survive_import_and_wire(cfg, tmp_path, field):
    source = json.loads(SOURCE.read_text())
    originals = {p["page"]: p["text"] for p in source["pages"]}
    folder = tmp_path / "author-input"
    manifest = example(folder, 8)
    for page in manifest["pages"]:
        page[field] = originals.get(page["number"], RULES)
        if field == "base_prompt":
            page["directions"] = []
    token = projects.create_import(cfg)
    for path in folder.glob("*.png"):
        projects.upload(cfg, token, path.name, path.read_bytes())
    assert not projects.validate_import(cfg, token, manifest)["errors"]
    pid = projects.commit_import(cfg, token)["project_id"]
    seed_character_references(cfg, pid)
    live = replace(cfg, provider="gemini", enable_live=True, gemini_api_key="offline", live_unit_micro=1)
    requests = []
    with connect(cfg) as db:
        for row in db.execute("SELECT * FROM pages WHERE project_id=? ORDER BY number", (pid,)):
            snap, _, _ = snapshot(live, db, dict(row))
            assert snap["prompt"] == originals.get(row["number"], RULES)
            assert "system_prompt" not in snap
            requests.append({"key": str(row["number"]), "snapshot": snap})
    path = tmp_path / "author.jsonl"
    GeminiProvider(live, client=object()).build_jsonl(path, requests)
    wire = [json.loads(line)["request"] for line in path.read_text().splitlines()]
    for number, request in enumerate(wire, 1):
        assert "systemInstruction" not in request
        assert request["contents"][0]["parts"][0]["text"] == originals.get(number, RULES)


def test_reimport_preserves_original_unless_explicitly_cleared(cfg, tmp_path):
    folder = tmp_path / "reimport"
    manifest = example(folder, 1)
    manifest["pages"][0]["prompt_override"] = RULES

    def commit(manifest, pid=None):
        token = projects.create_import(cfg)
        for path in folder.glob("*.png"):
            projects.upload(cfg, token, path.name, path.read_bytes())
        report = projects.validate_import(cfg, token, manifest, project_id=pid)
        assert not report["errors"]
        return projects.commit_import(cfg, token, expected_version=report["expected_version"])["project_id"]

    pid = commit(manifest)
    manifest["pages"][0].pop("prompt_override")
    assert commit(manifest, pid) == pid
    with connect(cfg) as db:
        draft = json.loads(db.execute("SELECT draft_json FROM pages WHERE project_id=?", (pid,)).fetchone()[0])
        assert draft["prompt_override"] == RULES
    manifest["pages"][0]["prompt_override"] = None
    commit(manifest, pid)
    with connect(cfg) as db:
        draft = json.loads(db.execute("SELECT draft_json FROM pages WHERE project_id=?", (pid,)).fetchone()[0])
        assert draft["prompt_override"] is None


def test_explicit_base_import_replaces_legacy_override(cfg, tmp_path):
    folder = tmp_path / "base-migration"
    manifest = example(folder, 1)
    manifest["pages"][0]["prompt_override"] = "OLD"

    def commit(pid=None):
        token = projects.create_import(cfg)
        for path in folder.glob("*.png"):
            projects.upload(cfg, token, path.name, path.read_bytes())
        report = projects.validate_import(cfg, token, manifest, project_id=pid)
        assert not report["errors"]
        return projects.commit_import(cfg, token, expected_version=report["expected_version"])["project_id"]

    pid = commit()
    manifest["pages"][0].pop("prompt_override")
    manifest["pages"][0]["base_prompt"] = "NEW"
    commit(pid)
    seed_character_references(cfg, pid)
    with connect(cfg) as db:
        row = dict(db.execute("SELECT * FROM pages WHERE project_id=?", (pid,)).fetchone())
        draft = json.loads(row["draft_json"])
        assert draft["prompt_override"] is None
        assert draft["base_prompt"] == "NEW"
        assert snapshot(cfg, db, row)[0]["prompt"].startswith("NEW\n\n")
        projects.patch_draft(db, row, {"base_prompt": draft["base_prompt"], "prompt_override": None, "page_prompt": "USER NOTE"})
        row = dict(db.execute("SELECT * FROM pages WHERE project_id=?", (pid,)).fetchone())
        sent = snapshot(cfg, db, row)[0]["prompt"]
        assert sent.startswith("NEW\n\n") and "USER NOTE" in sent and "OLD" not in sent
