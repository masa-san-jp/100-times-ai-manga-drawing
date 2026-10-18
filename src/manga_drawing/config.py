from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import secrets
import sys
import threading
from dataclasses import dataclass

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_local = threading.local()


@dataclass(frozen=True)
class Config:
    root: Path
    provider: str = "mock"
    chunk_count: int = 1000
    chunk_bytes: int = 1024**3
    max_external: int = 8
    mock_delay: float = 2.0
    poll_interval: float = 1.0

    @classmethod
    def from_env(cls):
        base = Path.home() / ("Library/Application Support" if sys.platform == "darwin" else ".local/share")
        return cls(
            Path(os.environ.get("MANGA_DATA_DIR", base / "manga-drawing")).expanduser().resolve(),
            provider=os.environ.get("MANGA_PROVIDER", "mock"),
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
