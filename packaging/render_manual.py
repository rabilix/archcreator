"""Render docs/MANUAL.md to a self-contained docs/MANUAL.html (one file, inline style, no scripts, no network).

    python packaging/render_manual.py

Needs the `docs` extra (pip install -e .[docs]). The HTML is generated, not edited: change MANUAL.md and render again.
"""

import re
import sys
from html import escape
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "MANUAL.md"
TARGET = ROOT / "docs" / "MANUAL.html"

STYLE = """
:root{--bg:#ffffff;--fg:#1c2330;--muted:#5b6678;--line:#d9dee7;--accent:#0b5cad;--code:#f3f5f9;--head:#eef2f8}
@media (prefers-color-scheme:dark){:root{--bg:#10151d;--fg:#e4e8ef;--muted:#9aa5b8;--line:#2a3342;--accent:#6fb0ff;--code:#1a212d;--head:#18202c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:62rem;margin:0 auto;padding:1.5rem 1rem 4rem;display:grid;grid-template-columns:15rem minmax(0,1fr);gap:2rem}
nav{position:sticky;top:1rem;align-self:start;max-height:calc(100vh - 2rem);overflow:auto;font-size:.9rem}
nav ol{list-style:none;margin:0;padding:0}nav li{margin:.2rem 0}
nav a{color:var(--muted);text-decoration:none}nav a:hover{color:var(--accent)}
main{min-width:0}
h1{font-size:2rem;margin:.2rem 0 1rem}h2{font-size:1.4rem;margin:2.4rem 0 .6rem;padding-top:.6rem;border-top:1px solid var(--line)}
h3{font-size:1.1rem;margin:1.6rem 0 .4rem}
a{color:var(--accent)}hr{display:none}
code,pre{font:.88rem/1.5 ui-monospace,SFMono-Regular,Consolas,Menlo,monospace}
code{background:var(--code);padding:.1rem .3rem;border-radius:4px}
pre{background:var(--code);padding:.8rem 1rem;border-radius:8px;overflow:auto;border:1px solid var(--line)}
pre code{background:none;padding:0}
table{border-collapse:collapse;width:100%;display:block;overflow-x:auto;margin:1rem 0}
th,td{border:1px solid var(--line);padding:.4rem .6rem;text-align:left;vertical-align:top}
th{background:var(--head)}
blockquote{margin:1rem 0;padding:.2rem 1rem;border-left:4px solid var(--accent);color:var(--muted)}
@media (max-width:52rem){.wrap{grid-template-columns:1fr}nav{position:static;max-height:none}}
"""


def render() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"], extension_configs={"toc": {"toc_depth": "2"}})
    body = md.convert(text)
    title = re.match(r"#\s+(.+)", text).group(1).strip()
    toc = "".join(f'<li><a href="#{t["id"]}">{escape(t["name"])}</a></li>'
                  for t in md.toc_tokens[0]["children"]) if md.toc_tokens else ""
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f'<title>{escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n<div class="wrap">\n'
            f'<nav aria-label="Contents"><ol>{toc}</ol></nav>\n<main>\n{body}\n</main>\n</div>\n</body>\n</html>\n')


def main() -> int:
    TARGET.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
