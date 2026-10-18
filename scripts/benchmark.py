"""Reproducible offline 300-page / 900-candidate load check."""

import json
from pathlib import Path
import resource
import sys
import tempfile
import time

from manga_drawing.cli import example
from manga_drawing.config import Config
from manga_drawing.db import connect, initialize, transaction
from manga_drawing import planning, projects
from manga_drawing.worker import Worker


def main():
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        cfg = Config(root / "data", mock_delay=0)
        initialize(cfg)
        manifest = example(root / "input", 300)
        token = projects.create_import(cfg)
        for file in (root / "input").glob("*.png"):
            projects.upload(cfg, token, file.name, file.read_bytes())
        projects.validate_import(cfg, token, manifest)
        pid = projects.commit_import(cfg, token)["project_id"]
        with transaction(cfg) as db:
            plan = planning.make_plan(cfg, db, pid, {"candidate_count": 3})
        planning.start(cfg, pid, plan["plan_token"], "benchmark")
        worker = Worker(cfg)
        worker.tick()
        worker.tick()
        with connect(cfg) as db:
            succeeded = db.execute("SELECT COUNT(*) FROM jobs WHERE state='succeeded'").fetchone()[0]
            candidates = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            first = projects.page_list(db, pid)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_bytes = rss if sys.platform == "darwin" else rss * 1024
        result = {
            "pages": 300,
            "jobs": 900,
            "succeeded": succeeded,
            "candidates": candidates,
            "provider_submit_calls": worker.provider.call_count(),
            "first_page_items": len(first["items"]),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "peak_rss_mib": round(rss_bytes / 1024**2, 2),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "provider": "mock",
        }
        assert succeeded == candidates == 900
        assert result["first_page_items"] == 40
        assert rss_bytes <= 512 * 1024**2
        print(json.dumps(result, indent=2))
        Path("artifacts").mkdir(exist_ok=True)
        Path("artifacts/benchmark.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
