"""Gemini Developer Batch API. Network is opt-in; tests inject an offline client."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re

from .. import assets
from ..common import DomainError, canonical, require
from ..db import connect
from .base import RateLimited, SubmissionUnknown

MODEL = "gemini-nano-banana-2.1"
SUPPORTED_MODELS = {MODEL, "gemini-3.1-flash-image", "gemini-3-pro-image"}
TERMINAL = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}


class ProviderNotConfigured(DomainError):
    def __init__(self):
        super().__init__(
            "provider_not_configured", "Geminiには明示live設定、APIキー、正の送信上限と予算単位が必要です。"
        )


class ProviderRejected(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def rejection_code(exc):
    """Retain an allowlisted diagnosis, never arbitrary SDK messages or credentials."""
    text = str(getattr(exc, "message", "") or "").lower()
    reason = ""
    for label, fragments in (
        ("billing", ("billing", "paid tier", "free tier")),
        ("mime_type", ("mime type", "mime_type", "mimetype")),
        ("input_format", ("jsonl", "json line", "generation_config", "generationconfig")),
        ("quota", ("quota", "rate limit")),
        ("api_key", ("api key", "api_key")),
        ("precondition", ("precondition",)),
    ):
        if any(fragment in text for fragment in fragments):
            reason = "_" + label
            break
    return "gemini_rejected_" + str(exc.code) + reason


def ensure_configured(cfg):
    if not (
        cfg.enable_live
        and cfg.gemini_api_key
        and (cfg.live_request_limit is None or cfg.live_request_limit > 0)
        and cfg.live_unit_micro > 0
    ):
        raise ProviderNotConfigured()


def safe_resource(name, prefix):
    if not isinstance(name, str) or not re.fullmatch(re.escape(prefix) + r"/[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid provider resource")
    return name


class GeminiProvider:
    def __init__(self, cfg, client=None):
        ensure_configured(cfg)
        self.cfg = cfg
        if client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError:
                raise DomainError(
                    "sdk_missing", "uv sync --extra gemini --lockedで公式SDKを導入してください。"
                ) from None
            # attempts includes the initial request. Never let SDK retries duplicate a submission.
            self.client = genai.Client(
                api_key=cfg.gemini_api_key,
                vertexai=False,
                http_options=types.HttpOptions(
                    api_version="v1beta",
                    base_url="https://generativelanguage.googleapis.com",
                    timeout=60_000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        else:
            self.client = client

    def validate(self, profile, snapshots):
        return {
            "valid": profile in SUPPORTED_MODELS
            and all(
                s["settings"]["provider"] == "gemini" and s["settings"]["model"] == profile for s in snapshots
            ),
            "count": len(snapshots),
        }

    def build_jsonl(self, path, requests):
        """One REST GenerateContentRequest per line, carrying real immutable image bytes."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        try:
            with temp.open("w") as out, connect(self.cfg) as db:
                for item in requests:
                    snap = item["snapshot"]
                    require(
                        snap["settings"]["provider"] == "gemini"
                        and snap["settings"]["model"] in SUPPORTED_MODELS,
                        "provider_mismatch",
                        "保存済み入力のAPIとモデルが一致しません。",
                        422,
                    )
                    parts = [{"text": snap["prompt"]}]
                    for image in snap["attachments"]:
                        image_path = assets.path_for(self.cfg, db, image["hash"])
                        content = image_path.read_bytes()
                        require(
                            hashlib.sha256(content).hexdigest() == image["hash"],
                            "asset_corrupt",
                            "送信画像の内容が保存時と異なります。",
                            422,
                        )
                        mime = db.execute(
                            "SELECT mime FROM assets WHERE sha256=?", (image["hash"],)
                        ).fetchone()[0]
                        parts.extend(
                            [
                                {"text": image["role"] + (" " + image["key"] if image.get("key") else "")},
                                {
                                    "inlineData": {
                                        "mimeType": mime,
                                        "data": base64.b64encode(content).decode("ascii"),
                                    }
                                },
                            ]
                        )
                    request = {
                        "contents": [{"role": "user", "parts": parts}],
                        "generation_config": {
                            "responseModalities": ["TEXT", "IMAGE"],
                            "candidateCount": 1,
                            "maxOutputTokens": 8192,
                            "imageConfig": {"aspectRatio": "3:4", "imageSize": "2K"},
                        },
                    }
                    line = canonical({"key": item["key"], "request": request}) + "\n"
                    require(
                        len(line.encode()) <= 20 * 1024**2,
                        "request_limit",
                        "JSONLの1要求が20MiBを超えます。",
                        422,
                    )
                    out.write(line)
                out.flush()
                os.fsync(out.fileno())
            require(
                temp.stat().st_size <= self.cfg.chunk_bytes,
                "chunk_size",
                "送信ファイルが上限を超えます。",
                422,
            )
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()

    def upload(self, path):
        try:
            uploaded = self.client.files.upload(
                file=path, config={"mime_type": "jsonl", "display_name": path.stem}
            )
            return safe_resource(uploaded.name, "files")
        except Exception:
            # Uploading a file is not submitting a generation. Do not expose SDK URLs or credentials.
            raise OSError("gemini_upload_failed") from None

    def submit(self, input_name, model, display_key):
        ensure_configured(self.cfg)
        safe_resource(input_name, "files")
        require(model in SUPPORTED_MODELS, "model", "対応確認対象のモデルを指定してください。", 422)
        try:
            job = self.client.batches.create(
                model=model, src=input_name, config={"display_name": display_key}
            )
            return safe_resource(job.name, "batches")
        except DomainError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == 429:
                raise RateLimited() from None
            if code in (400, 401, 403, 404):
                raise ProviderRejected(rejection_code(exc)) from None
            raise SubmissionUnknown() from None

    def find(self, display_key):
        try:
            # SDK pager automatically follows all pages. A zero-match result never proves non-submission.
            return [
                safe_resource(job.name, "batches")
                for job in self.client.batches.list(config={"page_size": 100})
                if job.display_name == display_key and self._model(job.model) in SUPPORTED_MODELS
            ]
        except Exception:
            raise OSError("gemini_list_failed") from None

    @staticmethod
    def _model(value):
        return str(value or "").removeprefix("models/")

    def describe(self, remote_name):
        safe_resource(remote_name, "batches")
        try:
            job = self.client.batches.get(name=remote_name)
            return {"id": job.name, "display_key": job.display_name, "model": self._model(job.model)}
        except Exception:
            raise OSError("gemini_get_failed") from None

    def poll(self, remote_name):
        safe_resource(remote_name, "batches")
        try:
            job = self.client.batches.get(name=remote_name)
            state = getattr(job.state, "value", job.state)
            if state not in TERMINAL:
                return {"state": "running"}
            destination = getattr(getattr(job, "dest", None), "file_name", None)
            if destination:
                return {"state": "complete", "result": safe_resource(destination, "files")}
            return {"state": "terminal_unknown", "error": "gemini_batch_" + str(state).lower()}
        except Exception:
            raise OSError("gemini_poll_failed") from None

    def fetch(self, result_reference, local_temp_path):
        safe_resource(result_reference, "files")
        local_temp_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Public SDK destination streams chunks to disk instead of returning an enormous bytes object.
            self.client.files.download(file=result_reference, destination=local_temp_path)
        except Exception:
            local_temp_path.unlink(missing_ok=True)
            raise OSError("gemini_download_failed") from None

    def parse_line(self, line):
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("invalid_result")
        key = raw.get("key")
        response = raw.get("response") or {}
        if not isinstance(response, dict) or (raw.get("status") and not isinstance(raw["status"], dict)):
            raise ValueError("invalid_response")
        result = {"key": key, "usage": response.get("usageMetadata", response.get("usage_metadata"))}
        if raw.get("error") or (raw.get("status") and raw["status"].get("code", 0)):
            return result | {"error": "gemini_request_failed"}
        candidates = response.get("candidates") or []
        if not isinstance(candidates, list):
            raise ValueError("invalid_candidates")
        if len(candidates) > 1:
            return result | {"error": "multiple_candidates"}
        if not candidates:
            return result | {"error": "gemini_blocked_or_empty"}
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ValueError("invalid_candidate")
        finish = candidate.get("finishReason", candidate.get("finish_reason"))
        if finish not in (None, "STOP"):
            return result | {"error": "gemini_finish_" + str(finish).lower()}
        images = []
        content = candidate.get("content") or {}
        if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
            raise ValueError("invalid_content")
        for part in content.get("parts", []):
            if not isinstance(part, dict):
                raise ValueError("invalid_part")
            if part.get("thought"):
                continue
            blob = part.get("inlineData", part.get("inline_data"))
            if blob and not isinstance(blob, dict):
                raise ValueError("invalid_blob")
            if blob and blob.get("mimeType", blob.get("mime_type")) in ("image/png", "image/jpeg"):
                images.append(blob.get("data", ""))
        return result | {"images": images}


def create(cfg, client=None):
    return GeminiProvider(cfg, client=client)
