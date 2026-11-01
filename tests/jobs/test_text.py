"""Plain text from a job page's HTML (stdlib parser; ADR 0007)."""

import pytest

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


# --- regression coverage: real Ashby HTML -----------------------------------
# Paragraphs copied byte for byte from the Adaption job's descriptionHtml
# (Ashby posting API, jobs.ashbyhq.com/adaption/5635dd7e-a0ca-414d-b738-71500502dba9,
# fetched 2026-11-01). The owner saw these words joined; it was a terminal-wrap
# artifact when copying, not an extractor bug. These cases make sure the
# extractor keeps getting them right. They are regression coverage, not a fix.

# Kept exactly as served, so the lines are long (hence the noqa).
ADAPTION_PARAGRAPHS = {
    "p1": b'<p style="min-height:1.5em"><strong>Sweat the details. </strong>Technical excellence requires obsessing over every detail. We co-design serving, algorithms, and interface as one system to maximize efficiency and enable real-time adaptation.</p>',  # noqa: E501
    "p2": b'<p style="min-height:1.5em"><strong>Move with conviction. </strong>Extraordinary results require extraordinary effort. We operate with urgency as a unified team, concentrating resources on a few high-conviction bets where research and impact intersect.</p>',  # noqa: E501
    "p3": b'<p style="min-height:1.5em">This is a research role which is focused on real world impact. We care about building our frontier innovation around efficiency, gradient free exploration, real time learning and interface design. A revolution is underway where the cost of generating synthetic data is now low enough that we can treat the data space as malleable and something which can be optimized. We can steer synthetic data towards desirable properties and make previously invisible worlds with limited data coverage more visible. The most intelligent system will increasingly be defined by building an algorithm that can interact with the world. This means for the first time researchers who care about intelligence need also be obsessed with how a model interacts. If any of these statements resonate with you, we would like to hear from you.</p>',  # noqa: E501
    "p4": b'<p style="min-height:1.5em"><strong>Measure what matters</strong>: we believe strongly that the ultimate signal of value is whether we have real world impact. Our current algorithms are capable of interacting with the world which is why product matters so much.</p>',  # noqa: E501
    "p5": b"<p style=\"min-height:1.5em\"><strong>Metrics that matter. </strong>The most intelligent system will increasingly be defined by building an algorithm that can interact with the world. Research ideas are tested through working products. If it doesn't improve what users can do, we question whether it matters. We share what moves the field; we don't optimize for paper count.</p>",  # noqa: E501
    "p6": b'<p style="min-height:1.5em"><strong>Deep expertise</strong> in at least one area: model efficiency, real time alignment or algorithmic optimization</p>',  # noqa: E501
}


@pytest.mark.parametrize(
    ("paragraph", "phrase", "joined"),
    [
        ("p1", "maximize efficiency", "maximizeefficiency"),
        ("p2", "bets where research", "betswhere"),
        ("p2", "We operate with", "Weoperate"),
        ("p3", "underway where the", "wherethe"),
        ("p4", "value is whether", "valueis"),
        ("p4", "capable of interacting", "ofinteracting"),
        ("p5", "interact with the world", "withthe"),
        ("p5", "Research ideas are", "ideasare"),
        ("p6", "Deep expertise in", "expertisein"),
    ],
)
def test_real_ashby_paragraphs_keep_their_spaces(paragraph: str, phrase: str, joined: str) -> None:
    text = html_to_text(ADAPTION_PARAGRAPHS[paragraph])
    assert phrase in text
    assert joined not in text
