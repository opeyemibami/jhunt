"""The shared page frame for every server-rendered page (ADR 0017): the
document head, the nav, the styles, and the live status script.

Every value a page puts in its body must already be escaped: this frame adds
no data of its own except the title, which it escapes.
"""

import html

from jhunt.api import live

NAV = [("/", "Digest"), ("/applications", "Applications")]


def page(title: str, body: str, *, waiting: bool = False) -> str:
    """A whole page. waiting: a run on it is still going, so the <noscript>
    fallback reloads it (live.NOSCRIPT_REFRESH)."""
    links = " ".join(f'<a href="{href}">{label}</a>' for href, label in NAV)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{live.NOSCRIPT_REFRESH if waiting else ""}
<style>{CSS}</style></head>
<body><main>
<nav>{links}</nav>
{body}
</main>
{live.SCRIPT}
</body></html>
"""


CSS = """
:root { --bg: #fff; --fg: #1c1c1c; --muted: #666; --line: #e3e3e3; --accent: #1d4ed8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #151515; --fg: #e8e8e8; --muted: #9a9a9a; --line: #2c2c2c; --accent: #8ab4ff; }
}
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
.meta { color: var(--muted); margin: .15rem 0; }
ol.postings { list-style: none; padding: 0; margin: 1rem 0; }
ol.postings li { border-top: 1px solid var(--line); padding: .7rem 0; }
.head { display: flex; gap: .6rem; align-items: baseline; }
.score { font-variant-numeric: tabular-nums; font-weight: 600; min-width: 2.6rem; }
a.title { color: var(--accent); text-decoration: none; font-weight: 600; }
a.title:hover { text-decoration: underline; }
span.title { font-weight: 600; }
li .meta, .matched, .action { margin-left: 3.2rem; }
.matched { font-size: .9rem; }
.action { margin-top: .3rem; font-size: .9rem; color: var(--muted); }
button { font: inherit; padding: .2rem .8rem; cursor: pointer; }
details { margin-top: 1.5rem; }
section.scout { border: 1px solid var(--line); border-radius: 6px; padding: .2rem 1rem;
  margin: 1rem 0; }
section.scout h2 { font-size: 1rem; margin: .6rem 0 .2rem; }
.live { white-space: pre-line; }
.problem { color: #b42318; }
form.inline { display: inline; }
summary { cursor: pointer; font-weight: 600; }
nav { display: flex; gap: 1rem; margin-bottom: 1rem; font-size: .95rem; }
nav a { color: var(--accent); text-decoration: none; }
table { border-collapse: collapse; width: 100%; font-size: .92rem; }
th, td { text-align: left; padding: .35rem .5rem; border-top: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
pre.job { white-space: pre-wrap; background: color-mix(in srgb, var(--line) 35%, transparent);
  padding: .8rem 1rem; border-radius: 6px; font: inherit; }
h2 { font-size: 1.1rem; margin: 1.6rem 0 .4rem; }
ul.plain { list-style: none; padding: 0; margin: .3rem 0; }
ul.plain li { padding: .25rem 0; border-top: 1px solid var(--line); }
"""
