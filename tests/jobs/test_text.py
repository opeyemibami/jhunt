"""Plain text from a job page's HTML (stdlib parser; ADR 0007)."""

from jhunt.jobs.text import html_to_text


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


def test_whitespace_in_the_source_is_kept_at_tag_boundaries() -> None:
    # The Adaption bug: "We <em>operate</em>" came out as "Weoperate".
    assert html_to_text(b"<p>We <em>operate</em> where <b>the</b> work is</p>") == (
        "We operate where the work is"
    )
    assert html_to_text(b"<p>Deep expertise<span> in</span> Python, and<a> aren't</a></p>") == (
        "Deep expertise in Python, and aren't"
    )


def test_no_space_is_invented_where_the_source_has_none() -> None:
    # Browsers render <p>We<span>operate</span></p> as "Weoperate"; so do we.
    assert html_to_text(b"<p>We<span>operate</span></p>") == "Weoperate"
    assert html_to_text(b"<p>un<b>likely</b></p>") == "unlikely"


def test_block_elements_still_start_new_lines() -> None:
    assert html_to_text(b"<p>one</p><p>two<br>three</p><li>four</li>") == "one\ntwo\nthree\nfour"
