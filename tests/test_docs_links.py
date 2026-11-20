import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def test_relative_document_links():
    root = Path(__file__).resolve().parents[1]
    documents = [root / "README.md", *(root / "docs").rglob("*.md"), *(root / "examples").rglob("*.md")]
    links = re.compile(r'\]\(<?([^\s)>]+)>?|(?:href|src)="([^"]+)"')
    broken = []
    for document in documents:
        for markdown, html in links.findall(document.read_text()):
            target = urlsplit(markdown or html)
            if target.scheme or target.netloc or not target.path:
                continue
            if not (document.parent / unquote(target.path)).exists():
                broken.append(f"{document.relative_to(root)} → {target.path}")
    assert not broken, "\n".join(broken)
