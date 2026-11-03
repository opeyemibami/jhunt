"""The shared page frame for every server-rendered page (ADR 0017): the
document head, the top navigation, the styles and the live status script.

The styles are hand-written, with no build step and nothing from a CDN.
They follow the system's light or dark setting and have a phone layout below
40rem. Every value a page puts in its body must already be escaped: this
frame adds no data of its own except the title, which it escapes.
"""

import html

from jhunt.api import live

NAV = [("/", "Digest"), ("/applications", "Applications"), ("/facts", "Facts")]


def page(title: str, body: str, *, current: str = "/", waiting: bool = False) -> str:
    """A whole page. current: the nav entry to mark. waiting: a run on the page
    is still going, so the <noscript> fallback reloads it."""
    links = "".join(
        f'<a href="{href}"{' aria-current="page"' if href == current else ""}>{label}</a>'
        for href, label in NAV
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · jhunt</title>
{live.NOSCRIPT_REFRESH if waiting else ""}
<style>{CSS}</style></head>
<body>
<header class="top"><div class="top-inner"><span class="brand">jhunt</span>
<nav>{links}</nav></div></header>
<main>
{body}
</main>
{live.SCRIPT}
</body></html>
"""


CSS = """
:root {
  --bg: #f7f7f5; --surface: #ffffff; --fg: #1d1d1b; --muted: #6b6b66;
  --line: #e4e3df; --accent: #2457d6; --accent-soft: #e8eefc;
  --good: #1f7a3f; --good-soft: #e3f3e8; --warn: #9a5b00; --warn-soft: #fbf0dc;
  --bad: #b42318; --bad-soft: #fbe6e4; --tag: #f0efeb;
  --radius: 10px; --gap: 1rem;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121211; --surface: #1b1b1a; --fg: #ececea; --muted: #a2a29c;
    --line: #2d2d2b; --accent: #8fb0ff; --accent-soft: #1e2a47;
    --good: #7fd19b; --good-soft: #17301f; --warn: #f1bd6c; --warn-soft: #3a2c14;
    --bad: #ff9b8f; --bad-soft: #3d1a17; --tag: #262624;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: var(--accent); }
header.top { background: var(--surface); border-bottom: 1px solid var(--line); }
.top-inner { max-width: 60rem; margin: 0 auto; padding: .7rem 1rem; display: flex;
  align-items: center; gap: 1.5rem; }
.brand { font-weight: 700; letter-spacing: .01em; }
nav { display: flex; gap: .25rem; }
nav a { color: var(--muted); text-decoration: none; padding: .3rem .7rem; border-radius: 6px; }
nav a:hover { background: var(--tag); color: var(--fg); }
nav a[aria-current="page"] { color: var(--fg); background: var(--tag); font-weight: 600; }
main { max-width: 60rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
h1 { font-size: 1.5rem; margin: 0 0 .3rem; }
h2 { font-size: 1.1rem; margin: 2rem 0 .6rem; }
.meta, .muted { color: var(--muted); }
p.meta { margin: .2rem 0; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: .8rem 1rem; margin: 1rem 0; }
.panel h2 { margin: 0 0 .3rem; font-size: 1rem; }
.problem { color: var(--bad); }
button { font: inherit; padding: .3rem .9rem; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--line); background: var(--surface); color: var(--fg); }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--surface); }
form.inline { display: inline; }
.nowrap { white-space: nowrap; }
ol.cards { list-style: none; padding: 0; margin: 1rem 0; display: grid; gap: .6rem; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: .8rem 1rem; display: flex; gap: var(--gap); align-items: flex-start; }
.card-main { flex: 1; min-width: 0; }
.card-title { font-size: 1.05rem; margin: 0; line-height: 1.35; }
.card-title a.title { color: var(--fg); text-decoration: none; }
.card-title a.title:hover { color: var(--accent); text-decoration: underline; }
.card-meta { margin: .15rem 0 .45rem; color: var(--muted); font-size: .92rem; }
.card-side { display: flex; flex-direction: column; align-items: flex-end; gap: .45rem;
  flex-shrink: 0; }
.card-actions { display: flex; gap: .4rem; flex-wrap: wrap; justify-content: flex-end;
  font-size: .9rem; color: var(--muted); text-align: right; }
.score { font-variant-numeric: tabular-nums; font-weight: 600; font-size: .85rem;
  background: var(--accent-soft); color: var(--accent); border-radius: 999px;
  padding: .1rem .55rem; }
.tags { display: flex; flex-wrap: wrap; gap: .3rem; }
.tag { font-size: .78rem; background: var(--tag); color: var(--muted); border-radius: 999px;
  padding: .05rem .5rem; }
.tag-title { background: var(--accent-soft); color: var(--accent); }
.tag-penalty { background: var(--warn-soft); color: var(--warn); }
.tag-exclude { background: var(--bad-soft); color: var(--bad); }
.badge { font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  border-radius: 4px; padding: .05rem .4rem; margin-left: .4rem; vertical-align: middle; }
.badge-new { background: var(--good-soft); color: var(--good); }
.badge-gone { background: var(--tag); color: var(--muted); }
details { margin-top: 1.5rem; }
summary { cursor: pointer; font-weight: 600; padding: .3rem 0; }
.live { white-space: pre-line; }
table { border-collapse: collapse; width: 100%; font-size: .93rem; background: var(--surface);
  border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
th, td { text-align: left; padding: .55rem .75rem; border-top: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: .85rem; border-top: none; }
pre.job { white-space: pre-wrap; background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 1rem 1.2rem; font: inherit; line-height: 1.6;
  max-height: 32rem; overflow: auto; }
ul.plain { list-style: none; padding: 0; margin: .3rem 0; }
ul.plain li { padding: .45rem 0; border-top: 1px solid var(--line); }
ul.plain ul.plain li { border-top: none; padding: .1rem 0 .1rem 1rem; color: var(--muted); }
@media (max-width: 40rem) {
  body { font-size: 14px; }
  .top-inner { gap: .8rem; padding: .6rem .8rem; }
  main { padding: 1rem .8rem 3rem; }
  .card { flex-direction: column; gap: .5rem; }
  .card-side { flex-direction: row-reverse; align-items: center; justify-content: space-between;
    width: 100%; }
  .card-actions { justify-content: flex-start; text-align: left; }
  table, thead, tbody, tr, th, td { display: block; }
  table { border: none; background: none; }
  tr { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    margin-bottom: .5rem; padding: .3rem 0; }
  tr:first-child { display: none; }
  td { border-top: none; padding: .2rem .8rem; max-width: 100%; overflow-wrap: anywhere; }
  td:empty { display: none; }
  tr, table { width: 100%; max-width: 100%; }
}
"""
