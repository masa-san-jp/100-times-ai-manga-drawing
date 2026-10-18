from __future__ import annotations

from pathlib import Path
import re

from .db import get


def ensure_work_folder(cfg, db, work_id):
    key = f"work_folder:{work_id}"
    saved = db.execute("SELECT value FROM system WHERE key=?", (key,)).fetchone()
    if saved and saved[0]:
        return Path(saved[0])
    work = get(db, "works", work_id)
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", work["title"]).strip(" .")[:60] or "作品"
    root = cfg.root / "作品"
    root.mkdir(parents=True, exist_ok=True)
    sequence = 1
    while True:
        folder = root / (title if sequence == 1 else f"{title}（{sequence}）")
        try:
            folder.mkdir()
            break
        except FileExistsError:
            sequence += 1
    (folder / "生成結果").mkdir()
    (folder / "却下した候補").mkdir()
    db.execute("INSERT OR REPLACE INTO system VALUES(?,?)", (key, str(folder.resolve())))
    db.execute(
        "INSERT OR REPLACE INTO system VALUES(?,?)",
        (f"managed_work_folder:{work_id}", str(folder.relative_to(cfg.root))),
    )
    return folder.resolve()
