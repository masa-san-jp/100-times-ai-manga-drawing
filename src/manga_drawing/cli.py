from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .common import DomainError
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


def main():
    parser = argparse.ArgumentParser(description="マンガ下書きバッチ")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    web = sub.add_parser("web")
    web.add_argument("--port", type=int, default=8765)
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("doctor")
    sub.add_parser("live-check", help="実送信せずT14に必要な設定と残り3要求の枠を確認")
    back = sub.add_parser("backup")
    back.add_argument("--output", required=True)
    rest = sub.add_parser("restore")
    rest.add_argument("--source", required=True)
    sample = sub.add_parser("example")
    sample.add_argument("--output", required=True)
    sample.add_argument("--pages", type=int, default=3)
    args = parser.parse_args()
    try:
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
