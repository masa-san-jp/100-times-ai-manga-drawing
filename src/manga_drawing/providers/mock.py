from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

from PIL import Image, ImageDraw

from ..assets import png
from ..common import canonical, uid
from .base import RateLimited, SubmissionUnknown


class MockProvider:
    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.root = cfg.root / "mock-provider"
        with cfg.write_lock(), self.db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS remote(
                id TEXT PRIMARY KEY,display_key TEXT NOT NULL,model TEXT NOT NULL,input_name TEXT NOT NULL,
                ready_at REAL NOT NULL,output TEXT,state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,operation TEXT,display_key TEXT,created REAL);
            """)

    def db(self):
        db = sqlite3.connect(self.root / "state.sqlite3")
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def hash_file(path):
        with path.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    def upload(self, path):
        with self.cfg.write_lock():
            name = self.hash_file(path) + ".jsonl"
            target = self.root / "inputs" / name
            if not target.exists():
                shutil.copyfile(path, target)
            return name

    def submit(self, input_name, model, display_key):
        with self.cfg.write_lock(), self.db() as db:
            db.execute(
                "INSERT INTO calls(operation,display_key,created) VALUES('submit',?,?)",
                (display_key, self.clock()),
            )
            with (self.root / "inputs" / input_name).open() as f:
                first = json.loads(next(f))
            scenario = first["snapshot"]["settings"].get("scenario", "success")
            if scenario == "429" and first["attempt_number"] == 1:
                db.commit()
                raise RateLimited()
            remote = uid()
            delay = self.cfg.mock_delay * (5 if scenario == "delay" else 1)
            db.execute(
                "INSERT INTO remote VALUES(?,?,?,?,?,?,?)",
                (remote, display_key, model, input_name, self.clock() + delay, None, "running"),
            )
            db.commit()
            if scenario == "timeout":
                raise SubmissionUnknown()
            return remote

    def find(self, display_key):
        with self.db() as db:
            return [
                r[0]
                for r in db.execute("SELECT id FROM remote WHERE display_key=? ORDER BY id", (display_key,))
            ]

    def describe(self, remote_name):
        with self.db() as db:
            row = db.execute("SELECT * FROM remote WHERE id=?", (remote_name,)).fetchone()
            return dict(row) if row else None

    def poll(self, remote_name):
        with self.cfg.write_lock(), self.db() as db:
            row = db.execute("SELECT * FROM remote WHERE id=?", (remote_name,)).fetchone()
            if row is None:
                raise FileNotFoundError(remote_name)
            if row["state"] == "complete":
                return {"state": "complete", "result": row["output"]}
            if self.clock() < row["ready_at"]:
                return {"state": "running"}
            output = self.root / "outputs" / f"{remote_name}.jsonl"
            temp = output.with_suffix(".tmp")
            with temp.open("w") as out, (self.root / "inputs" / row["input_name"]).open() as source:
                for line in source:
                    request = json.loads(line)
                    result = self.make_result(request)
                    scenario = request["snapshot"]["settings"]["scenario"]
                    if scenario == "missing":
                        continue
                    out.write(canonical(result) + "\n")
                    if scenario == "duplicate":
                        out.write(canonical(result) + "\n")
            temp.replace(output)
            db.execute("UPDATE remote SET state='complete',output=? WHERE id=?", (output.name, remote_name))
            return {"state": "complete", "result": output.name}

    def make_result(self, request):
        scenario = request["snapshot"]["settings"]["scenario"]
        result = {"key": request["key"], "usage": {"provider": "mock", "billable": False}}
        if scenario == "unknown_key":
            result["key"] = "unmatched-" + request["key"]
        if scenario == "reject" or (scenario == "mixed" and request["page_number"] % 2 == 0):
            return result | {"error": "policy_rejected"}
        if scenario == "text_only":
            return result | {"text": "No image generated"}
        if scenario == "corrupt":
            return result | {"images": [base64.b64encode(b"corrupt-image").decode()]}
        # Small images keep mock load tests economical. Export exercises target dimensions separately.
        image = Image.new("L", (360, 480), "white")
        draw = ImageDraw.Draw(image)
        for y in (35, 185, 335):
            draw.rectangle((12, y, 347, y + 130), outline="black", width=2)
        draw.text(
            (20, 10), f"MOCK - PAGE {request['page_number']} / {request['candidate_index']}", fill="black"
        )
        draw.text((20, 60), request["key"][:18], fill="black")
        draw.text(
            (20, 210), hashlib.sha256(request["snapshot"]["prompt"].encode()).hexdigest()[:20], fill="black"
        )
        draw.text((20, 360), "NOT AI ARTWORK", fill="black")
        encoded = base64.b64encode(png(image)).decode()
        return result | {"images": [encoded] * (2 if scenario == "multiple" else 1)}

    def fetch(self, result_reference, local_temp_path):
        with self.cfg.write_lock():
            # Only provider-issued simple names; never accept arbitrary paths.
            if Path(result_reference).name != result_reference:
                raise ValueError("Invalid result reference")
            source = self.root / "outputs" / result_reference
            local_temp_path.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as inp, local_temp_path.open("wb") as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)

    def parse_line(self, line):
        return json.loads(line)

    def build_jsonl(self, path, requests):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with temp.open("w") as f:
            for request in requests:
                f.write(canonical(request) + "\n")
        temp.replace(path)
        return self.hash_file(path)

    def validate(self, profile, snapshots):
        return {"valid": profile == "mock-batch-v1", "count": len(snapshots)}

    def call_count(self):
        with self.db() as db:
            return db.execute("SELECT COUNT(*) FROM calls WHERE operation='submit'").fetchone()[0]
