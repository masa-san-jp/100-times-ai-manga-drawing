from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from datetime import datetime, timezone


def uid():
    return str(uuid.uuid4())


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        d = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class DomainError(Exception):
    def __init__(self, code, message, status=422, fields=None):
        super().__init__(message)
        self.code, self.message, self.status, self.fields = code, message, status, fields or {}


def require(
    condition, code="conflict", message="別の操作で内容が変更されました。再読み込みしてください。", status=409
):
    if not condition:
        raise DomainError(code, message, status)
