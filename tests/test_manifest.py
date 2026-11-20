import json
from pathlib import Path
import shutil

import pytest

from manga_drawing.cli import create_manifest


def test_create_manifest_from_example_inputs(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples/example-001"
    for name in ("キャラクタースケッチ", "ネーム", "作画指示書"):
        shutil.copytree(source / name, tmp_path / name)
    path = create_manifest(tmp_path)
    actual = json.loads(path.read_text())
    example = json.loads((source / "manifest.json").read_text())
    assert {x["key"]: x["images"] for x in actual["characters"]} == {
        x["key"]: x["images"] for x in example["characters"]
    }
    assert actual["pages"] == [
        {key: item[key] for key in ("number", "name", "instructions")} | {"characters": []}
        for item in example["pages"]
    ]
    with pytest.raises(ValueError, match="--force"):
        create_manifest(tmp_path)
    (tmp_path / "ネーム/01-duplicate.png").touch()
    (tmp_path / "ネーム/unmatched.png").touch()
    (tmp_path / "ネーム/06-name.png").unlink()
    with pytest.raises(ValueError) as failed:
        create_manifest(tmp_path, force=True)
    assert (
        "重複" in str(failed.value)
        and "unmatched.png" in str(failed.value)
        and "06-WI.png" in str(failed.value)
    )
    assert json.loads(path.read_text()) == actual


def test_manifest_prompt_defaults_preserve_page_overrides(cfg, tmp_path):
    from manga_drawing import projects
    from manga_drawing.cli import example
    from manga_drawing.db import connect

    folder = tmp_path / "input"
    manifest = example(folder, 3)
    manifest["base_prompt"] = "作品本文"
    manifest["pages"][1]["base_prompt"] = "ページ本文"
    manifest["pages"][2]["base_prompt"] = None

    def register(project_id=None):
        token = projects.create_import(cfg)
        for path in folder.glob("*.png"):
            projects.upload(cfg, token, path.name, path.read_bytes())
        report = projects.validate_import(cfg, token, manifest, project_id)
        assert not report["errors"]
        return projects.commit_import(cfg, token, report["expected_version"])["project_id"]

    def prompts(project_id):
        with connect(cfg) as db:
            return [
                json.loads(row[0]).get("base_prompt")
                for row in db.execute(
                    "SELECT draft_json FROM pages WHERE project_id=? ORDER BY number", (project_id,)
                )
            ]

    pid = register()
    assert prompts(pid) == ["作品本文", "ページ本文", None]
    del manifest["base_prompt"]
    del manifest["pages"][1]["base_prompt"]
    register(pid)
    assert prompts(pid) == ["作品本文", "ページ本文", None]
    manifest["base_prompt"] = None
    register(pid)
    assert prompts(pid) == [None, None, None]


def test_manifest_documentation_tracks_input_limits():
    from manga_drawing.assets import MAX_FILE
    from manga_drawing.models import Manifest, PageInput

    docs = (Path(__file__).resolve().parents[1] / "docs/manifest.md").read_text()
    pages = next(
        item.max_length for item in Manifest.model_fields["pages"].metadata if hasattr(item, "max_length")
    )
    characters = next(
        item.max_length
        for item in PageInput.model_fields["characters"].metadata
        if hasattr(item, "max_length")
    )
    assert f"1話{pages}ページ" in docs
    assert f"1ページ{characters}人" in docs
    assert f"1ファイル{MAX_FILE // 1024**2}MiB" in docs
