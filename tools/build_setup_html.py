"""
Build SETUP.html from SETUP.md so the two never drift apart.

    pip install markdown          (one-time, only needed to rebuild the docs)
    python tools/build_setup_html.py

The output is a single self-contained file (the robot icon is embedded), so it
opens straight from disk, is served by the app at /setup, and prints cleanly.
Re-run this after every edit to SETUP.md and commit both files.
"""

import base64
import html
import os
import re

import markdown

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "SETUP.md")
OUT = os.path.join(ROOT, "SETUP.html")
ICON = os.path.join(ROOT, "frontend", "static", "favicon.png")   # 64 px keeps the page small


def slug(text):
    text = re.sub(r"<[^>]+>", "", text)
    return "s-" + re.sub(r"[^a-z0-9]+", "-", html.unescape(text).lower()).strip("-")


def github_style_lists(text):
    """GitHub lets a list start right after a line of text; Python-Markdown needs a
    blank line first. Insert one (outside code fences) so both render the same."""
    out, fence, prev = [], False, ""
    item = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and item.match(line) and prev.strip() and not item.match(prev) \
                and not prev.lstrip().startswith(("|", ">", "```")):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out) + "\n"


def main():
    md_text = github_style_lists(open(SRC, encoding="utf-8").read())
    title = md_text.splitlines()[0].lstrip("# ").strip()
    # tab_length=2: accept the 2-3 space nested lists GitHub renders (Python-Markdown defaults to 4)
    body = markdown.markdown(md_text.split("\n", 1)[1], extensions=["tables", "fenced_code", "sane_lists"], tab_length=2)

    # Anchors on section headings + a table of contents
    toc = []

    def add_id(m):
        inner = m.group(1)
        sid = slug(inner)
        toc.append((sid, re.sub(r"<[^>]+>", "", inner)))
        return f'<h2 id="{sid}">{inner}</h2>'
    body = re.sub(r"<h2>(.*?)</h2>", add_id, body)
    # Markdown blockquotes that start with a warning sign become callouts
    body = body.replace("<blockquote>\n<p>⚠️", '<blockquote class="warn">\n<p>⚠️')
    # Bare URLs in bold become links
    body = re.sub(r"<strong>(https?://[^<\s]+)</strong>", r'<strong><a href="\1" target="_blank" rel="noopener">\1</a></strong>', body)

    icon = base64.b64encode(open(ICON, "rb").read()).decode()
    toc_html = "".join(f'<li><a href="#{sid}">{html.escape(t)}</a></li>' for sid, t in toc)

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="icon" href="data:image/png;base64,{icon}">
<!-- Generated from SETUP.md by tools/build_setup_html.py. Edit SETUP.md, then rebuild. -->
<style>
  :root {{ --bg:#fff; --fg:#1a1d24; --muted:#667085; --line:#e4e7ec; --card:#f7f8fa; --accent:#d6283f;
          --link:#0b7fa5; --warn-bg:#fff4ed; --warn-line:#f79009; --code:#f2f4f7; }}
  @media (prefers-color-scheme:dark) {{ :root {{ --bg:#0d1016; --fg:#e2e8f0; --muted:#8b95a7; --line:#252a36; --card:#141821;
          --accent:#ff6b81; --link:#3cc8f0; --warn-bg:#2a1a12; --warn-line:#ff9a5c; --code:#1b202b; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif; }}
  main {{ max-width:860px; margin:0 auto; padding:28px 20px 80px; }}
  header {{ display:flex; align-items:center; gap:16px; margin-bottom:8px; }}
  header img {{ width:64px; height:64px; border-radius:12px; }}
  h1 {{ font-size:28px; margin:0; line-height:1.2; }} h1 span {{ color:var(--accent); }}
  .sub {{ color:var(--muted); font-size:14px; }}
  nav {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 18px; margin:20px 0 8px; }}
  nav b {{ font-size:13px; text-transform:uppercase; letter-spacing:1px; color:var(--muted); }}
  nav ol {{ margin:6px 0 0; padding-left:20px; columns:2; column-gap:28px; font-size:14px; }}
  h2 {{ font-size:21px; margin:38px 0 10px; padding-top:12px; border-top:1px solid var(--line); scroll-margin-top:12px; }}
  a {{ color:var(--link); }}
  table {{ border-collapse:collapse; width:100%; margin:12px 0; font-size:14px; }}
  th, td {{ border:1px solid var(--line); padding:8px 10px; text-align:left; vertical-align:top; }}
  th {{ background:var(--card); }}
  code {{ font:13px ui-monospace,Consolas,monospace; background:var(--code); padding:1px 6px; border-radius:4px; }}
  pre {{ background:var(--code); padding:12px 14px; border-radius:8px; overflow-x:auto; }} pre code {{ padding:0; background:none; }}
  blockquote {{ margin:14px 0; padding:10px 16px; border-left:4px solid var(--link); background:var(--card); border-radius:0 8px 8px 0; }}
  blockquote.warn {{ border-left-color:var(--warn-line); background:var(--warn-bg); }}
  blockquote p {{ margin:6px 0; }}
  hr {{ border:0; border-top:1px solid var(--line); margin:24px 0; }}
  li {{ margin:3px 0; }}
  footer {{ margin-top:48px; color:var(--muted); font-size:13px; }}
  @media (max-width:640px) {{ nav ol {{ columns:1; }} }}
  @media print {{ nav {{ display:none; }} body {{ font-size:12px; }} h2 {{ break-after:avoid; }} table, blockquote {{ break-inside:avoid; }} }}
</style></head>
<body><main>
<header><img src="data:image/png;base64,{icon}" alt="flinchbot robot">
<div><h1>UniFi <span>×</span> flinchbot</h1><div class="sub">Setup guide · AI-powered UniFi assistant (Claude or ChatGPT)</div></div></header>
<nav><b>Contents</b><ol>{toc_html}</ol></nav>
{body}
<footer>flinchbot · This page is generated from SETUP.md.</footer>
</main></body></html>
"""
    open(OUT, "w", encoding="utf-8", newline="\n").write(page)
    print(f"Wrote {OUT} ({len(page) // 1024} KB, {len(toc)} sections)")


if __name__ == "__main__":
    main()
