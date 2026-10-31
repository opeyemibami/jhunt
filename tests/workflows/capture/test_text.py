"""Plain text from a job page's HTML (stdlib parser; ADR 0007)."""

from jhunt.workflows.capture.text import html_to_text


def test_keeps_the_words_and_drops_scripts_and_styles() -> None:
    html = b"""<html><head><title>Senior Engineer</title>
      <style>body { color: red }</style><script>track()</script></head>
      <body><h1>Senior Engineer</h1><p>Dublin &amp; remote&nbsp;EU.</p>
      <ul><li>Python</li><li>Postgres</li></ul><noscript>enable js</noscript></body></html>"""
    assert (
        html_to_text(html)
        == "Senior Engineer\nSenior Engineer\nDublin & remote EU.\nPython\nPostgres"
    )


def test_collapses_whitespace_and_blank_lines() -> None:
    html = b"<div>  a\n\n   b  </div><br><br><p>   </p><p>c</p>"
    assert html_to_text(html) == "a b\nc"


def test_bad_bytes_do_not_fail() -> None:
    assert html_to_text(b"<p>caf\xe9</p>") == "caf�"
