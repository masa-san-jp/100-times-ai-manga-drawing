from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import files
import sqlite3

from .common import DomainError


def connect(cfg):
    db = sqlite3.connect(cfg.root / "db.sqlite3", timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def initialize(cfg):
    cfg.prepare()
    with cfg.write_lock(), connect(cfg) as db:
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 3:
            raise DomainError("migration", "このアプリより新しいDBです。更新版を使用してください。")
        if version == 0:
            script = files("manga_drawing").joinpath("migrations/001_initial.sql").read_text()
            db.executescript("BEGIN IMMEDIATE;\n" + script + "\nPRAGMA user_version=1;COMMIT;")

        if version < 2:
            script = files("manga_drawing").joinpath("migrations/002_works.sql").read_text()
            db.executescript("BEGIN IMMEDIATE;\n" + script + "\nPRAGMA user_version=2;COMMIT;")
        if version < 3:
            script = files("manga_drawing").joinpath("migrations/003_character_references.sql").read_text()
            db.executescript("BEGIN IMMEDIATE;\n" + script + "\nPRAGMA user_version=3;COMMIT;")
        from .workspaces import ensure_work_folder

        for work in db.execute("SELECT id FROM works").fetchall():
            ensure_work_folder(cfg, db, work["id"])


@contextmanager
def transaction(cfg):
    with cfg.write_lock():
        db = connect(cfg)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def get(db, table, ident):
    # table is always a code constant, never a request field.
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise DomainError("not_found", "対象が見つかりません。", 404)
    return dict(row)
