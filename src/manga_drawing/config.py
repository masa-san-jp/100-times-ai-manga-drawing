from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from dataclasses import dataclass, field

from dotenv import dotenv_values

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_local = threading.local()


def budget_from_env(env):
    """Accept dollars at the configuration boundary; retain integer accounting internally."""
    dollar_key = "MANGA_LIVE_BUDGET_PER_IMAGE_USD"
    legacy_key = "MANGA_LIVE_UNIT_RESERVE_MICRO_USD"
    # An explicitly exported legacy value still takes precedence over a local file.
    if dollar_key not in os.environ and legacy_key in os.environ:
        return int(os.environ[legacy_key] or "0")
    value = (env.get(dollar_key) or "").strip()
    if not value:
        return int(env.get(legacy_key) or "0")
    message = "MANGA_LIVE_BUDGET_PER_IMAGE_USDは米ドル額で入力してください（例：0.10）。"
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", value):
        raise ValueError(message)
    whole, _, fraction = value.partition(".")
    amount = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
    if amount <= 0:
        raise ValueError(message)
    return amount


@dataclass(frozen=True)
class Config:
    root: Path
    provider: str = "mock"
    chunk_count: int = 1000
    chunk_bytes: int = 1024**3
    max_external: int = 8
    mock_delay: float = 2.0
    poll_interval: float = 1.0
    enable_live: bool = False
    gemini_api_key: str = field(default="", repr=False, compare=False)
    live_request_limit: int | None = None
    live_unit_micro: int = 0

    @classmethod
    def from_env(cls):
        # Read only the explicit working directory, never search parents or mutate process env.
        env = dotenv_values(Path.cwd() / ".env", interpolate=False) | dict(os.environ)
        selected_provider = env.get("MANGA_PROVIDER") or "gemini"
        base = Path.home() / ("Library/Application Support" if sys.platform == "darwin" else ".local/share")
        return cls(
            Path(env.get("MANGA_DATA_DIR") or base / "manga-drawing").expanduser().resolve(),
            provider=selected_provider,
            enable_live=env.get("MANGA_ENABLE_LIVE") == "1",
            gemini_api_key=env.get("GEMINI_API_KEY") or "",
            live_request_limit=int(env["MANGA_LIVE_MAX_REQUESTS"])
            if env.get("MANGA_LIVE_MAX_REQUESTS")
            else None,
            live_unit_micro=budget_from_env(env),
            poll_interval=60.0 if selected_provider == "gemini" else 1.0,
        )

    def prepare(self):
        if self.chunk_count < 1 or self.chunk_bytes < 1 or self.max_external < 1:
            raise ValueError("Invalid queue settings")
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in (
            "assets/sha256",
            "chunks",
            "exports",
            "staging",
            "logs",
            "mock-provider/inputs",
            "mock-provider/outputs",
        ):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        with self.write_lock():
            if not (self.root / "secret").exists():
                fd = os.open(self.root / "secret", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(secrets.token_hex(32))

    @property
    def secret(self):
        return (self.root / "secret").read_text().strip()

    @contextlib.contextmanager
    def write_lock(self):
        """Process + thread lock; reentrant so services can call asset/provider helpers."""
        key = str(self.root)
        with _guard:
            lock = _locks.setdefault(key, threading.RLock())
        with lock:
            depths = getattr(_local, "depths", {})
            _local.depths = depths
            depth = depths.get(key, 0)
            depths[key] = depth + 1
            try:
                if depth:
                    yield
                else:
                    with (self.root / "write.lock").open("a+") as f:
                        fcntl.flock(f, fcntl.LOCK_EX)
                        try:
                            yield
                        finally:
                            fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                depths[key] -= 1
