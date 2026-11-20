from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from . import __version__
from .common import DomainError, atomic_write
from .config import Config
from .db import initialize


def example(output, count=3):
    from PIL import Image, ImageDraw
    from .assets import png

    path = Path(output)
    path.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (360, 480), "white")
    d = ImageDraw.Draw(im)
    for y in (20, 180, 340):
        d.rectangle((12, y, 347, y + 125), outline="black", width=2)
    (path / "name.png").write_bytes(png(im))
    d.text((20, 40), "1 Character - read right to left", fill="red")
    (path / "instructions.png").write_bytes(png(im))
    (path / "character.png").write_bytes(png(Image.new("L", (160, 240), "white")))
    manifest = {
        "schema_version": 1,
        "title": "サンプル作品",
        "characters": [{"key": "a", "name": "A", "height_cm": 170, "images": ["character.png"]}],
        "pages": [
            {
                "number": n + 1,
                "name": "name.png",
                "instructions": "instructions.png",
                "characters": ["a"],
                "directions": [{"marker": "①", "text": "人物Aを中央に配置。"}],
            }
            for n in range(count)
        ],
    }
    (path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def create_manifest(root, title=None, force=False):
    from .models import Manifest

    errors = []

    def images(primary, fallback):
        folder = root / primary
        if not folder.is_dir():
            folder = root / fallback
        if not folder.is_dir():
            errors.append(f"{primary}/ または {fallback}/ が見つかりません。")
            return []
        files = sorted(
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
        )
        if not files:
            errors.append(f"{folder.name}/ に対象画像がありません。")
        return files

    def numbered(files):
        result = {}
        for path in files:
            match = re.match(r"[0-9]+", path.name)
            if not match or int(match[0]) < 1:
                errors.append(f"{path.relative_to(root)} の先頭に正のページ番号を付けてください。")
                continue
            number = int(match[0])
            if number in result:
                errors.append(f"{path.relative_to(root)} と {result[number]} のページ番号が重複しています。")
            else:
                result[number] = path.relative_to(root).as_posix()
        return result

    characters = images("キャラクタースケッチ", "characters")
    names = numbered(images("ネーム", "name"))
    instructions = numbered(images("作画指示書", "instructions"))
    for number in sorted(names.keys() - instructions.keys()):
        errors.append(f"{names[number]} に対応する作画指示書がありません。")
    for number in sorted(instructions.keys() - names.keys()):
        errors.append(f"{instructions[number]} に対応するネームがありません。")
    target = root / "manifest.json"
    if not force and (target.exists() or target.is_symlink()):
        errors.append("manifest.json が既にあります。上書きする場合は --force を指定してください。")
    if errors:
        raise ValueError("\n".join(errors))
    manifest = {
        "schema_version": 1,
        "title": title if title is not None else root.name,
        "characters": [
            {"key": path.stem, "name": path.stem, "images": [path.relative_to(root).as_posix()]}
            for path in characters
        ],
        "pages": [
            {"number": number, "name": names[number], "instructions": instructions[number], "characters": []}
            for number in sorted(names)
        ],
    }
    Manifest.model_validate(manifest)
    content = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    if force:
        atomic_write(target, content)
    else:
        try:
            with target.open("xb") as file:
                file.write(content)
        except FileExistsError:
            raise ValueError(
                "manifest.json が既にあります。上書きする場合は --force を指定してください。"
            ) from None
    return target


def main():
    parser = argparse.ArgumentParser(description="100 TIMES AI MANGA DRAWING")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    web = sub.add_parser("web")
    web.add_argument("--port", type=int, default=8765)
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("doctor")
    sub.add_parser("live-check", help="実送信せずにGeminiの設定を確認")
    back = sub.add_parser("backup")
    back.add_argument("--output", required=True)
    rest = sub.add_parser("restore")
    rest.add_argument("--source", required=True)
    sample = sub.add_parser("example")
    sample.add_argument("--output", required=True)
    sample.add_argument("--pages", type=int, default=3)
    manifest = sub.add_parser("manifest", help="画像フォルダーから manifest.json を作成")
    manifest.add_argument("--input", type=Path, required=True)
    manifest.add_argument("--title")
    manifest.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "manifest":
            target = create_manifest(args.input.expanduser().resolve(), args.title, args.force)
            print(
                f"{target} を作成しました。各ページの登場人物を指定し、人物の身長（cm）を入力してください。"
            )
            return
        cfg = Config.from_env()
        if args.command == "example":
            example(args.output, args.pages)
            return
        if args.command == "live-check":
            from .maintenance import live_check

            report = live_check(cfg)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not report["ready"]:
                sys.exit(1)
            return
        if args.command == "restore":
            from .export import restore

            restore(cfg, args.source)
        if args.command in {"init", "web", "worker"}:
            env_info = str(cfg.env_path) if cfg.env_path else "未読込"
            storage = "（既定）" if cfg.uses_default_root else ""
            print(f"保存先={cfg.root}{storage} API={cfg.provider} .env={env_info}", file=sys.stderr)
        initialize(cfg)
        if args.command == "web":
            import uvicorn
            from .web import create_app

            uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port, access_log=False)
        elif args.command == "worker":
            from .worker import Worker

            Worker(cfg).run(once=args.once)
        elif args.command == "backup":
            from .export import backup

            print(backup(cfg, args.output))
        elif args.command == "doctor":
            from .maintenance import doctor

            report = doctor(cfg)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not report["ok"]:
                sys.exit(1)
        else:
            mode = "Gemini・実送信設定はlive-checkで確認" if cfg.provider == "gemini" else "模擬API・課金なし"
            print(f"Ready: {cfg.root} ({mode})")
    except (DomainError, ValueError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
