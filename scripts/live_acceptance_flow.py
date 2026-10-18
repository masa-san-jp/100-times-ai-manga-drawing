"""Finish the synthetic T14 state-transition test after its initial batch is submitted.

No provider calls here: the regular worker handles submit/poll/recovery under its
configured limit. Acceptance below is a test transition, never an author's quality verdict.
Run from the repository root with the same .env as the live worker.
"""

import fcntl
import hashlib
import json
from pathlib import Path
import time

from PIL import Image

from manga_drawing import assets, planning
from manga_drawing.common import atomic_write, canonical, require, timestamp
from manga_drawing.config import Config
from manga_drawing.db import connect, get
from manga_drawing.export import schedule, output_folder

RECORD = Path("artifacts/live-acceptance.json")


def save(path, record):
    atomic_write(path, canonical(record).encode())


def batch_candidates(db, bid, pid, numbers):
    require(get(db, "batches", bid)["project_id"] == pid, "test_scope", "別作品のバッチです。")
    jobs = db.execute(
        "SELECT j.*,p.project_id,p.number,r.snapshot_json FROM jobs j JOIN rounds r ON r.id=j.round_id "
        "JOIN pages p ON p.id=r.page_id WHERE j.batch_id=? ORDER BY p.number",
        (bid,),
    ).fetchall()
    expected = len(numbers)
    require(
        [j["number"] for j in jobs] == numbers
        and all(
            j["project_id"] == pid and json.loads(j["snapshot_json"])["settings"]["provider"] == "gemini"
            for j in jobs
        ),
        "test_scope",
        "試験対象ページまたはAPIが一致しません。",
    )
    require(len(jobs) == expected, "test_batch", "試験バッチの要求数が一致しません。")
    require(
        not any(j["state"] in ("failed", "unknown") for j in jobs),
        "test_blocked",
        "試験要求が失敗または成否不明です。自動再送しません。",
    )
    if any(j["state"] != "succeeded" for j in jobs):
        return None
    candidates = db.execute(
        "SELECT c.*,r.page_id,r.id round_id,p.number FROM candidates c JOIN jobs j ON j.id=c.job_id "
        "JOIN rounds r ON r.id=j.round_id JOIN pages p ON p.id=r.page_id "
        "WHERE j.batch_id=? AND c.availability='available' ORDER BY p.number",
        (bid,),
    ).fetchall()
    require(len(candidates) == expected, "test_candidates", "試験画像数が一致しません。")
    return [dict(c) for c in candidates]


def matching_manifest(db, pid, manifest):
    project = get(db, "projects", pid)
    settings = json.loads(project["settings_json"])
    expected = [
        dict(r)
        for r in db.execute(
            "SELECT p.number,c.id candidate_id,c.asset_hash,c.raw_asset_hash,j.round_id,'gemini' provider "
            "FROM pages p JOIN candidates c ON c.id=p.accepted_candidate_id "
            "JOIN jobs j ON j.id=c.job_id WHERE p.project_id=? ORDER BY p.number",
            (pid,),
        )
    ]
    if manifest.get("pages") and "take" in manifest["pages"][0]:
        for item in expected:
            item["take"] = get(db, "rounds", item["round_id"])["sequence"]
            item["candidate_index"] = db.execute(
                "SELECT j.candidate_index FROM candidates c JOIN jobs j ON j.id=c.job_id WHERE c.id=?",
                (item["candidate_id"],),
            ).fetchone()[0]
    return (
        len(expected) == 2
        and [p["number"] for p in expected] == [1, 2]
        and {
            key: value
            for key, value in manifest.items()
            if key not in {"work_id", "work_title", "output_relative", "publication_relative", "work_folder"}
        }
        == {
            "provider": "gemini",
            "project_id": pid,
            "title": project["title"],
            "pages": expected,
            "missing_pages": [],
            "width": settings.get("width", 1410),
            "height": settings.get("height", 1920),
            "conversion": "grayscale-contain-white-center-lanczos",
        }
    )


def verify_directory(cfg, output):
    manifest = json.loads(output["manifest_json"])
    from manga_drawing.export import export_target, public_manifest

    folder = export_target(cfg, output) / "pages"
    require(
        json.loads((folder.parent / "manifest.json").read_text()) == public_manifest(manifest),
        "test_export",
        "出力のmanifestが一致しません。",
    )
    require(
        sorted(p.name for p in folder.iterdir()) == ["0001.png", "0002.png"],
        "test_export",
        "出力ページの構成が一致しません。",
    )
    for number in (1, 2):
        with Image.open(folder / f"{number:04}.png") as image:
            require(
                image.mode == "L" and image.size == (manifest["width"], manifest["height"]),
                "test_export",
                "書き出しの色または寸法が一致しません。",
            )


def accept_fixture(cfg, candidate):
    with connect(cfg) as db:
        page = get(db, "pages", candidate["page_id"])
    if page["accepted_candidate_id"]:
        require(page["accepted_candidate_id"] == candidate["id"], "test_changed", "採用が変更されています。")
        return
    planning.review(
        cfg,
        page["id"],
        {
            "expected_version": page["version"],
            "latest_round_id": page["latest_round_id"],
            "decision": "accept",
            "candidate_id": candidate["id"],
            "reason": "合成資料による受入試験の状態遷移。作者の画質判定ではありません。",
        },
    )


def advance_once(cfg, path=RECORD):
    record = json.loads(path.read_text())
    require(
        cfg.provider == "gemini" and cfg.live_request_limit == 3,
        "test_config",
        "試験はGemini・元の上限3で実行してください。",
    )
    with connect(cfg) as db:
        project = get(db, "projects", record["project_id"])
        require(
            project["title"] == "実API受け入れ試験（画質採否とは別）",
            "test_project",
            "合成試験作品ではありません。",
        )
        require(
            [
                p[0]
                for p in db.execute(
                    "SELECT number FROM pages WHERE project_id=? ORDER BY number", (record["project_id"],)
                )
            ]
            == [1, 2],
            "test_scope",
            "試験は2ページです。",
        )
        initial = batch_candidates(db, record["after_billing_batch_id"], record["project_id"], [1, 2])
    if initial is None:
        return "waiting_initial"
    first, second = initial
    expected_baseline = {
        "page_id": first["page_id"],
        "candidate_id": first["id"],
        "asset_hash": first["asset_hash"],
    }
    require(
        "baseline" not in record or record["baseline"] == expected_baseline,
        "test_scope",
        "採用基準が試験作品と一致しません。",
    )
    require(
        "revision_page_id" not in record or record["revision_page_id"] == second["page_id"],
        "test_scope",
        "修正先が試験作品と一致しません。",
    )
    require(
        "revision_request" not in record
        or record["revision_request"]["latest_round_id"] == second["round_id"],
        "test_scope",
        "修正元の版が試験対象と一致しません。",
    )
    if "revision_batch_id" in record:
        with connect(cfg) as db:
            batch_candidates(db, record["revision_batch_id"], record["project_id"], [2])
    if "baseline" not in record:
        accept_fixture(cfg, first)
        record["baseline"] = {
            "page_id": first["page_id"],
            "candidate_id": first["id"],
            "asset_hash": first["asset_hash"],
        }
        save(path, record)
    if "revision_request" not in record:
        with connect(cfg) as db:
            page = get(db, "pages", second["page_id"])
        record["revision_page_id"] = second["page_id"]
        record["revision_request"] = {
            "expected_version": page["version"],
            "latest_round_id": page["latest_round_id"],
            "candidate_count": 1,
            "reason": "受入試験用にページ2だけ却下。ページ1の採用を維持します。",
            "draft_patch": {
                "correction": "下のコマのロボットAも右手を大きく振るポーズに修正してください。三角帽子、四角い頭、丸い胸章と2コマの枠・空の吹き出しを保持してください。モノクロ。"
            },
        }
        save(path, record)
    if "revision_batch_id" not in record:
        result = planning.revise_and_generate(
            cfg, record["revision_page_id"], record["revision_request"], "live-t14-after-billing-revision"
        )
        record["revision_batch_id"] = result["batch_id"]
        record["phase"] = "revision_queued"
        save(path, record)
    with connect(cfg) as db:
        revised = batch_candidates(db, record["revision_batch_id"], record["project_id"], [2])
        baseline = record["baseline"]
        page = get(db, "pages", baseline["page_id"])
        candidate = get(db, "candidates", baseline["candidate_id"])
        require(
            page["accepted_candidate_id"] == baseline["candidate_id"]
            and candidate["asset_hash"] == baseline["asset_hash"],
            "test_changed",
            "ページ1の採用が変更されています。",
        )
        with assets.path_for(cfg, db, candidate["asset_hash"]).open("rb") as source:
            require(
                hashlib.file_digest(source, "sha256").hexdigest() == baseline["asset_hash"],
                "test_asset_changed",
                "ページ1の画像が変更されています。",
            )
    if revised is None:
        return "waiting_revision"
    accept_fixture(cfg, revised[0])
    with connect(cfg) as db:
        # Reuse only a snapshot of these exact final adopted pages, never an older output.
        prior = next(
            (
                r
                for r in db.execute(
                    "SELECT * FROM exports WHERE project_id=? AND state!='failed' AND (archive_path IS NULL OR archive_path NOT LIKE '%.zip') ORDER BY created_at DESC",
                    (record["project_id"],),
                )
                if matching_manifest(db, record["project_id"], json.loads(r["manifest_json"]))
            ),
            None,
        )
        version = get(db, "projects", record["project_id"])["version"]
    if not prior or record.get("export_id") != prior["id"]:
        record["export_id"] = (
            prior["id"]
            if prior
            else schedule(cfg, record["project_id"], {"mode": "complete", "expected_version": version})[
                "export_id"
            ]
        )
        save(path, record)
    with connect(cfg) as db:
        output = get(db, "exports", record["export_id"])
        require(output["state"] != "failed", "test_export", "書き出しが失敗しています。")
        if output["state"] != "complete":
            return "waiting_export"
        require(
            matching_manifest(db, record["project_id"], json.loads(output["manifest_json"])),
            "test_export",
            "最終採用と書き出しが一致しません。",
        )
        verify_directory(cfg, output)
        accepted_attempts = [
            dict(r)
            for r in db.execute(
                "SELECT a.request_key,a.usage_json,c.remote_name FROM attempts a JOIN chunks c ON c.id=a.chunk_id "
                "JOIN jobs j ON j.id=a.job_id WHERE j.batch_id IN (?,?) AND a.state='succeeded' ORDER BY a.created_at",
                (record["after_billing_batch_id"], record["revision_batch_id"]),
            )
        ]
        reserved = int(
            db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()[0]
        )
    require(reserved == len(accepted_attempts) == 3, "test_count", "受理要求数が3と一致しません。")
    history = record.setdefault("before_billing_failure_history", {})
    for field in (
        "initial_batch_id",
        "initial_jobs",
        "diagnostic_batch_id",
        "diagnostic_jobs",
        "attempted_generation_requests",
        "successful_images",
        "remote_batch_count",
        "last_http_code",
        "last_server_message",
        "returned_unaccepted_requests",
    ):
        if field in record:
            history[field] = record.pop(field)
    record.update(
        {
            "phase": "state_transition_pass",
            "completed_at": record.get("completed_at") or timestamp(),
            "reserved_requests": reserved,
            "successful_test_images": 3,
            "accepted_generation_requests": 3,
            "accepted_native_batches": len({a["remote_name"] for a in accepted_attempts}),
            "baseline_unchanged": True,
            "author_quality_review": "pending",
            "accepted_attempts": accepted_attempts,
            "export_path": str(output_folder(cfg, output)),
        }
    )
    save(path, record)
    return "complete"


def main():
    cfg = Config.from_env()
    with RECORD.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + 172800
        last = None
        while time.monotonic() < deadline:
            state = advance_once(cfg)
            if state != last:
                print(json.dumps({"test_state": state}, ensure_ascii=False), flush=True)
                last = state
            if state == "complete":
                return
            time.sleep(5)
        raise TimeoutError("Live test waiting deadline reached; generation was not resubmitted.")


if __name__ == "__main__":
    main()
