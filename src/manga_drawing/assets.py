from __future__ import annotations

import hashlib
import io
import warnings
from pathlib import PurePosixPath

from PIL import Image, ImageOps

from .common import DomainError, atomic_write

MAX_FILE = 20 * 1024**2


def safe_path(path):
    p = PurePosixPath(path)
    if (
        not path
        or p.is_absolute()
        or any(x in ("..", ".", "") for x in path.split("/"))
        or "\\" in path
        or ":" in path
    ):
        raise DomainError("unsafe_path", "相対パスに使用できない文字が含まれています。")
    return p.as_posix()


def decode(content):
    if len(content) > MAX_FILE:
        raise DomainError("file_size", "画像は20MiB以下にしてください。", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(content))
            if image.format not in ("PNG", "JPEG") or image.width * image.height > 40_000_000:
                raise ValueError()
            image.load()
            return image
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise DomainError("invalid_image", "PNG/JPEGの有効な画像を指定してください（最大40MP）。") from e


def png(image):
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def put(cfg, db, content):
    image = decode(content)
    h = hashlib.sha256(content).hexdigest()
    rel = f"assets/sha256/{h[:2]}/{h}"
    path = cfg.root / rel
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != h:
        atomic_write(path, content)
    db.execute(
        "INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?)",
        (h, rel, Image.MIME[image.format], len(content), image.width, image.height),
    )
    return h


def normalized(cfg, db, content):
    raw = put(cfg, db, content)
    im = decode(content)
    im = ImageOps.exif_transpose(im).convert("RGB")
    return raw, put(cfg, db, png(im))


def path_for(cfg, db, h):
    row = db.execute("SELECT relative_path FROM assets WHERE sha256=?", (h,)).fetchone()
    if row is None:
        raise DomainError("not_found", "画像が見つかりません。", 404)
    p = cfg.root / row[0]
    if not p.is_file():
        raise DomainError(
            "missing_asset", "画像ファイルが欠損しています。復元または結果の再取得が必要です。", 409
        )
    return p


def candidate_assets(cfg, db, raw):
    raw_hash = put(cfg, db, raw)
    image = decode(raw).convert("L")
    display = put(cfg, db, png(image))
    image.thumbnail((320, 320))
    thumb = put(cfg, db, png(image))
    return raw_hash, display, thumb
