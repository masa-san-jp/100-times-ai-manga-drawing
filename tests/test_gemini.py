"""I15 offline contract tests. All remote behavior is in-memory; never calls Google."""

import base64
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from manga_drawing import planning, projects
from manga_drawing.assets import png
from manga_drawing.common import DomainError
from manga_drawing.db import connect, get, transaction
from manga_drawing.export import backup
from manga_drawing.providers.base import RateLimited, SubmissionUnknown
from manga_drawing.providers.gemini import GeminiProvider, MODEL, ProviderNotConfigured, ProviderRejected
from manga_drawing.web import create_app, health
from manga_drawing.worker import Worker
from manga_drawing.reservations import reclaim_rejected, release_unaccepted
from conftest import make_project, page_rows, start


class FakeGoogle:
    """Implements only public SDK calls exercised by the adapter."""

    def __init__(self):
        self.inputs = {}
        self.jobs = {}
        self.creates = []
        self.downloads = []
        self.files = SimpleNamespace(upload=self.upload, download=self.download)
        self.batches = SimpleNamespace(create=self.create, get=self.get, list=self.list)

    def upload(self, *, file, config):
        assert config["mime_type"] == "jsonl"
        name = "files/input-" + str(len(self.inputs))
        self.inputs[name] = Path(file).read_text()
        return SimpleNamespace(name=name)

    def create(self, *, model, src, config):
        self.creates.append((model, src, config))
        name = "batches/job-" + str(len(self.jobs))
        results = []
        for line in self.inputs[src].splitlines():
            raw = json.loads(line)
            image = base64.b64encode(png(Image.new("L", (48, 64), 240))).decode()
            results.append(
                {
                    "key": raw["key"],
                    "response": {
                        "candidates": [
                            {
                                "finishReason": "STOP",
                                "content": {
                                    "parts": [
                                        {
                                            "thought": True,
                                            "inlineData": {"mimeType": "image/png", "data": image},
                                        },
                                        {"inlineData": {"mimeType": "image/png", "data": image}},
                                    ]
                                },
                            }
                        ],
                        "usageMetadata": {"totalTokenCount": 50},
                    },
                }
            )
        job = SimpleNamespace(
            name=name,
            display_name=config["display_name"],
            model="models/" + model,
            state="JOB_STATE_SUCCEEDED",
            dest=SimpleNamespace(file_name="files/output-" + str(len(self.jobs))),
        )
        self.jobs[name] = (job, results)
        return job

    def get(self, *, name):
        return self.jobs[name][0]

    def list(self, *, config):
        assert config["page_size"] == 100
        return (job for job, _ in self.jobs.values())

    def download(self, *, file, destination):
        self.downloads.append((file, destination))
        results = next(rows for job, rows in self.jobs.values() if job.dest.file_name == file)
        with Path(destination).open("w") as out:
            for row in results:
                out.write(json.dumps(row) + "\n")


@pytest.fixture
def live(cfg, monkeypatch):
    config = replace(
        cfg,
        provider="gemini",
        enable_live=True,
        gemini_api_key="test-only-no-network",
        live_request_limit=3,
        live_unit_micro=100_000,
    )
    fake = FakeGoogle()
    provider = GeminiProvider(config, client=fake)
    monkeypatch.setattr("manga_drawing.worker.create_provider", lambda cfg, clock=None: provider)
    return config, fake, provider


@pytest.mark.parametrize(
    "field,value",
    [("enable_live", False), ("gemini_api_key", ""), ("live_request_limit", 0), ("live_unit_micro", 0)],
)
def test_live_gate_before_client_or_registration(cfg, tmp_path, field, value):
    config = replace(
        cfg,
        provider="gemini",
        enable_live=True,
        gemini_api_key="test-only",
        live_request_limit=3,
        live_unit_micro=100_000,
    )
    config = replace(config, **{field: value})
    pid, _ = make_project(config, tmp_path, 1)
    with pytest.raises(ProviderNotConfigured):
        with transaction(config) as db:
            planning.make_plan(config, db, pid, {})
    with connect(config) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    assert "test-only" not in repr(config)


def test_offline_sdk_protocol_and_full_pipeline(live, tmp_path, clock):
    cfg, fake, provider = live
    pid, _ = make_project(cfg, tmp_path, 2)
    with TestClient(create_app(cfg), base_url="http://127.0.0.1") as client:
        client.get("/")
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["data"]["csrf"]
        plan = client.post(f"/api/projects/{pid}/plans", json={"candidate_count": 1}).json()["data"]
        assert plan["unit_reserve_micro"] == 100_000
        result = client.post(
            f"/api/projects/{pid}/batches",
            json={"plan_token": plan["plan_token"]},
            headers={"Idempotency-Key": "native-start"},
        )
        assert result.status_code == 202
        worker = Worker(cfg, clock)
        worker.tick()
        worker.tick()
        assert len(fake.creates) == 1
        assert provider.find(fake.creates[0][2]["display_name"]) == ["batches/job-0"]
        assert provider.describe("batches/job-0")["model"] == MODEL
        request = json.loads(next(iter(fake.inputs.values())).splitlines()[0])
        assert "snapshot" not in request
        assert request["request"]["generation_config"]["imageConfig"] == {
            "aspectRatio": "3:4",
            "imageSize": "2K",
        }
        images = [p["inlineData"] for p in request["request"]["contents"][0]["parts"] if "inlineData" in p]
        assert len(images) == 3
        assert all(base64.b64decode(i["data"]) for i in images)
        assert fake.downloads and all(isinstance(destination, Path) for _, destination in fake.downloads)
        pages = page_rows(cfg, pid)
        with connect(cfg) as db:
            candidate = projects.page_detail(db, pages[0]["id"])["candidates"][0]
            assert candidate["provider"] == "gemini"
            assert get(db, "attempts", candidate["attempt_id"])["usage_json"] is not None
        planning.review(
            cfg,
            pages[0]["id"],
            {
                "expected_version": pages[0]["version"],
                "latest_round_id": pages[0]["latest_round_id"],
                "decision": "accept",
                "candidate_id": candidate["id"],
            },
        )
        response = client.post(
            f"/api/pages/{pages[1]['id']}/revise-and-generate",
            json={
                "expected_version": pages[1]["version"],
                "latest_round_id": pages[1]["latest_round_id"],
                "draft_patch": {"correction": "keep balloons"},
            },
            headers={"Idempotency-Key": "native-revise"},
        )
        assert response.status_code == 202, response.text
        clock.advance(6)
        worker.tick()
        worker.tick()
        assert len(fake.creates) == 2
        assert page_rows(cfg, pid)[0]["accepted_candidate_id"] == candidate["id"]
        with connect(cfg) as db:
            assert (
                db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()[0]
                == "3"
            )
            assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 3
        assert health(cfg)["live_remaining_requests"] == 0
        assert health(cfg)["worker_running"]
        with pytest.raises(DomainError):
            start(cfg, pid, kind="extra", page_ids=[pages[1]["id"]])


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"response": []},
        {"status": "bad"},
        {"response": {"candidates": "bad"}},
        {"response": {"candidates": [{"content": {"parts": [None]}}]}},
        {"response": {"candidates": [{"content": []}]}},
        {"response": {"candidates": [{"content": {"parts": [{"inlineData": "bad"}]}}]}},
    ],
)
def test_malformed_result_lines_raise_only_value_error(live, payload):
    _, _, provider = live
    # Empty response is a well-formed failed request, malformed nested structures must be isolated.
    try:
        result = provider.parse_line(json.dumps(payload))
        assert result.get("error") or not result.get("images")
    except ValueError:
        pass


@pytest.mark.parametrize(
    "code,expected",
    [
        (429, RateLimited),
        (400, ProviderRejected),
        (401, ProviderRejected),
        (403, ProviderRejected),
        (404, ProviderRejected),
        (500, SubmissionUnknown),
        (None, SubmissionUnknown),
    ],
)
def test_submit_error_classification_no_secret_leak(live, code, expected):
    _, fake, provider = live

    class SDKFailure(Exception):
        pass

    exc = SDKFailure("secret-url?key=do-not-log")
    exc.code = code

    def fail(**kwargs):
        raise exc

    fake.batches.create = fail
    with pytest.raises(expected) as caught:
        provider.submit("files/input", MODEL, "correlation")
    assert "do-not-log" not in str(caught.value)


def test_official_sdk_disables_hidden_submission_retries(live):
    genai = pytest.importorskip("google.genai")
    import httpx
    from google.genai import types

    cfg, _, _ = live
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            429, json={"error": {"code": 429, "message": "test rate limit", "status": "RESOURCE_EXHAUSTED"}}
        )

    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    client = genai.Client(
        api_key="test-only",
        vertexai=False,
        http_options=types.HttpOptions(
            httpx_client=http_client, retry_options=types.HttpRetryOptions(attempts=1)
        ),
    )
    try:
        provider = GeminiProvider(cfg, client=client)
        with pytest.raises(RateLimited):
            provider.submit("files/test-input", MODEL, "test-correlation")
        assert len(requests) == 1
        assert requests[0].url.host == "generativelanguage.googleapis.com"
    finally:
        client.close()
        http_client.close()


def test_provider_io_does_not_hold_application_lock(live, tmp_path, clock):
    cfg, _, provider = live
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    worker = Worker(cfg, clock)
    cid = worker.prepare()
    entered, released = threading.Event(), threading.Event()
    original = provider.upload

    def slow(path):
        entered.set()
        assert released.wait(5)
        return original(path)

    provider.upload = slow
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(worker.submit, cid)
        try:
            assert entered.wait(2)
            pool.submit(worker.heartbeat).result(timeout=1)
            pool.submit(backup, cfg, tmp_path / "during-upload").result(timeout=2)
        finally:
            released.set()
        pending.result(timeout=2)


def test_upload_failures_are_bounded_and_other_chunks_continue(live, tmp_path, clock):
    cfg, fake, provider = live
    pid, _ = make_project(cfg, tmp_path, 1)
    start(cfg, pid)
    count = []

    def fail(path):
        count.append(1)
        raise OSError("offline")

    provider.upload = fail
    worker = Worker(cfg, clock)
    worker.tick()
    clock.advance(31)
    worker.tick()
    clock.advance(61)
    worker.tick()
    assert len(count) == 3
    assert not fake.creates
    with connect(cfg) as db:
        assert {r[0] for r in db.execute("SELECT state FROM jobs")} == {"failed"}
        assert (
            int(db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()[0])
            == 0
        )
        assert db.execute("SELECT spent_reserved_micro FROM batches").fetchone()[0] == 0


def test_limit_reduction_blocks_existing_unsubmitted_chunk(live, tmp_path, clock, monkeypatch):
    cfg, fake, provider = live
    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)
    worker = Worker(cfg, clock)
    cid = worker.prepare()
    lower = replace(cfg, live_request_limit=1)
    blocked = Worker(lower, clock)
    blocked.submit(cid)
    assert not fake.creates
    with connect(cfg) as db:
        assert get(db, "chunks", cid)["last_error"] == "live_limit"


def test_cumulative_limit_wait_is_visible_and_heartbeat_is_independent(live, tmp_path, clock):
    cfg, _, _ = live
    pid, _ = make_project(cfg, tmp_path, 4)
    pages = page_rows(cfg, pid)
    start(cfg, pid, page_ids=[p["id"] for p in pages[:2]])
    start(cfg, pid, page_ids=[p["id"] for p in pages[2:]])
    worker = Worker(cfg, clock)
    worker.tick()
    h = health(cfg)
    assert h["worker_running"] and h["live_remaining_requests"] == 1
    assert h["live_limit_wait"] and h["live_queued_requests"] == 2


@pytest.mark.parametrize(
    "message,suffix",
    [
        ("Billing account required secret-token", "billing"),
        ("Unsupported MIME type secret-token", "mime_type"),
        ("Invalid JSONL generationConfig secret-token", "input_format"),
        ("Precondition check failed. secret-token", "precondition"),
        ("Unknown problem secret-token", ""),
    ],
)
def test_rejection_diagnosis_is_allowlisted(live, message, suffix):
    _, fake, provider = live

    class Failure(Exception):
        code = 400

    exc = Failure()
    exc.message = message

    def fail(**kwargs):
        raise exc

    fake.batches.create = fail
    with pytest.raises(ProviderRejected) as result:
        provider.submit("files/input", MODEL, "correlation")
    assert result.value.code == "gemini_rejected_400" + ("_" + suffix if suffix else "")
    assert "secret-token" not in str(result.value)


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429])
def test_known_non_acceptance_returns_count_and_budget_once(live, tmp_path, clock, code):
    cfg, fake, _ = live
    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)

    class Rejected(Exception):
        pass

    error = Rejected()
    error.code = code
    error.message = "Precondition check failed."
    original = fake.batches.create

    def fail(**kwargs):
        raise error

    fake.batches.create = fail
    Worker(cfg, clock).tick()
    with connect(cfg) as db:
        assert (
            int(db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()[0])
            == 0
        )
        assert db.execute("SELECT spent_reserved_micro FROM batches").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        assert (
            db.execute("SELECT COUNT(*) FROM audit_events WHERE action='release_unaccepted'").fetchone()[0]
            == 1
        )
    assert reclaim_rejected(cfg) == reclaim_rejected(cfg) == 0
    assert cfg.live_request_limit == 3
    if code != 429:
        # Restore the external service and retry two pages under the original cap.
        fake.batches.create = original
        start(cfg, pid, kind="extra", page_ids=[p["id"] for p in page_rows(cfg, pid)])
        clock.advance(6)
        worker = Worker(cfg, clock)
        worker.tick()
        worker.tick()
        with connect(cfg) as db:
            assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2
            assert (
                int(db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()[0])
                == 2
            )


@pytest.mark.parametrize("code", [500, 503, None])
def test_unknown_submission_never_returns_reservations(live, tmp_path, clock, code):
    cfg, fake, _ = live
    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)

    class Uncertain(Exception):
        pass

    error = Uncertain()
    error.code = code

    def fail(**kwargs):
        raise error

    fake.batches.create = fail
    Worker(cfg, clock).tick()
    assert reclaim_rejected(cfg) == 0
    with connect(cfg) as db:
        assert db.execute("SELECT state FROM chunks").fetchone()[0] == "unknown"
        assert (
            int(db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()[0])
            == 2
        )
        assert db.execute("SELECT spent_reserved_micro FROM batches").fetchone()[0] == 200000


def test_legacy_rejection_recovery_is_atomic_and_keeps_history(live, tmp_path, clock):
    cfg, _, _ = live
    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)
    cid = Worker(cfg, clock).prepare()
    # A pre-upgrade definitive rejection has reservations but no refund receipt.
    with transaction(cfg) as db:
        db.execute("UPDATE chunks SET state='failed',last_error='gemini_rejected_400' WHERE id=?", (cid,))
        db.execute(
            "UPDATE attempts SET state='terminal_failed',error_code='gemini_rejected_400' WHERE chunk_id=?",
            (cid,),
        )
        db.execute("UPDATE jobs SET state='failed'")
    with pytest.raises(RuntimeError):
        with transaction(cfg) as db:
            assert release_unaccepted(cfg, db, cid, "provider_rejected") == 2
            raise RuntimeError("simulate interrupted recovery")
    with connect(cfg) as db:
        assert (
            int(db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()[0])
            == 2
        )
    assert reclaim_rejected(cfg) == 2
    assert reclaim_rejected(cfg) == 0
    with connect(cfg) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        assert {r[0] for r in db.execute("SELECT state FROM jobs")} == {"failed"}


def test_accepted_batch_keeps_reservations_even_when_output_missing(live, tmp_path, clock):
    cfg, fake, _ = live
    pid, _ = make_project(cfg, tmp_path, 2)
    start(cfg, pid)
    worker = Worker(cfg, clock)
    worker.tick()
    for job, _ in fake.jobs.values():
        job.state = "JOB_STATE_FAILED"
        job.dest = None
    worker.tick()
    assert reclaim_rejected(cfg) == 0
    with connect(cfg) as db:
        assert db.execute("SELECT remote_name FROM chunks").fetchone()[0]
        assert (
            int(db.execute("SELECT value FROM system WHERE key='gemini_reserved_requests'").fetchone()[0])
            == 2
        )
