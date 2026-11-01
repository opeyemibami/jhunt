"""The location filter: in scope means Ireland, or remote across a region that
includes Ireland. Every posting gets a reason; nothing is dropped (ADR 0010)."""

from pathlib import Path

import pytest

from jhunt.workflows.scout.location import Scope, ScopeError, classify_location, load_scope

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
NI_OUT = Scope(northern_ireland_in_scope=False)
NI_IN = Scope(northern_ireland_in_scope=True)


@pytest.mark.parametrize(
    ("location", "remote", "in_scope", "reason"),
    [
        # Ireland, however it's written
        ("Dublin", False, True, "Dublin"),
        ("Dublin, Ireland", False, True, "Dublin"),
        ("Cork, Ireland", False, True, "Cork"),
        ("Galway", False, True, "Galway"),
        ("Republic of Ireland", False, True, "Ireland"),
        ("Ireland", True, True, "Ireland"),
        ("Remote - IE", True, True, "Ireland"),
        ("Remote - Ireland", True, True, "Ireland"),
        ("London | Dublin", False, True, "Dublin"),  # any Irish location counts
        ("UK & Ireland", True, True, "Ireland"),
        # Irish city names in the US are out
        ("Dublin, CA", False, False, "out: Dublin, CA (US)"),
        ("Dublin, California", False, False, "out: Dublin, California (US)"),
        ("Dublin, OH", False, False, "out: Dublin, OH (US)"),
        ("Dublin, Ohio, United States", False, False, "out: Dublin, Ohio, United States (US)"),
        ("Dublin, OH 43017", False, False, "out: Dublin, OH 43017 (US)"),
        ("Galway, NY", False, False, "out: Galway, NY (US)"),
        ("Limerick, Pennsylvania", False, False, "out: Limerick, Pennsylvania (US)"),
        ("Cork, USA", False, False, "out: Cork, USA (US)"),
        ("Dublin, Ireland; Dublin, CA", False, True, "Dublin"),  # each location on its own
        # A region counts only for a remote posting (the flag or the word)
        ("Remote - EMEA", True, True, "region EMEA"),
        ("Remote - EMEA", False, True, "region EMEA"),  # the word, without the flag
        ("Remote, Europe", False, True, "region Europe"),
        ("Europe", True, True, "region Europe"),
        ("Remote, EU", True, True, "region EU"),
        ("Worldwide", True, True, "region worldwide"),
        ("Remote - US; Remote - EMEA", True, True, "region EMEA"),
        ("London, UK (EMEA)", False, False, "out: on-site London, UK (EMEA)"),
        ("Berlin, Germany; Europe", False, False, "out: on-site Berlin, Germany (Europe)"),
        ("EMEA", False, False, "out: on-site (EMEA)"),
        # Bare "Remote": the region isn't stated, so it's shown, flagged
        ("Remote", True, True, "remote (region not stated)"),
        ("", True, True, "remote (region not stated)"),
        # Out, with the reason
        ("San Francisco, CA", False, False, "out: San Francisco, CA"),
        ("London", False, False, "out: London"),
        ("Remote - US", True, False, "out: Remote - US"),
        ("Remote - Germany", True, False, "out: Remote - Germany"),
        ("United States (Remote)", True, False, "out: United States (Remote)"),
        ("Belfast, Northern Ireland", False, False, "out: Belfast, Northern Ireland (UK)"),
        ("", False, False, "out: no location"),
        # "IE" only as a code, never inside a word
        ("Paris, France", False, False, "out: Paris, France"),
        ("Remote - Chile", True, False, "out: Remote - Chile"),
    ],
)
def test_classify_location(location: str, remote: bool, in_scope: bool, reason: str) -> None:
    assert classify_location(location, remote, NI_OUT) == (in_scope, reason)


@pytest.mark.parametrize(
    ("location", "with_ni_out", "with_ni_in"),
    [
        (
            "Belfast, Northern Ireland",
            (False, "out: Belfast, Northern Ireland (UK)"),
            (True, "Northern Ireland"),
        ),
        ("Derry", (False, "out: Derry (UK)"), (True, "Northern Ireland")),
        ("London; Belfast", (False, "out: London; Belfast (UK)"), (True, "Northern Ireland")),
        ("Dublin", (True, "Dublin"), (True, "Dublin")),
    ],
)
def test_northern_ireland_is_a_setting(
    location: str, with_ni_out: tuple[bool, str], with_ni_in: tuple[bool, str]
) -> None:
    assert classify_location(location, False, NI_OUT) == with_ni_out
    assert classify_location(location, False, NI_IN) == with_ni_in


def test_repo_scope_keeps_northern_ireland_out() -> None:
    assert load_scope(REPO_CONFIG) == NI_OUT


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("", "northern_ireland_in_scope"),
        ('northern_ireland_in_scope = "yes"\n', "true or false"),
        ("northern_ireland_in_scope = false\nother = 1\n", "unknown setting"),
    ],
)
def test_bad_scope_fails(tmp_path: Path, body: str, error: str) -> None:
    (tmp_path / "scope.toml").write_text(body)
    with pytest.raises(ScopeError, match=error):
        load_scope(tmp_path)


def test_missing_scope_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ScopeError, match="scope file missing"):
        load_scope(tmp_path)
