"""The location filter: in scope means Ireland, or remote across a region that
includes Ireland. Every posting gets a reason; nothing is dropped (ADR 0010)."""

import pytest

from jhunt.workflows.scout.location import classify_location


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
        # Region-wide: can't be an office, so in scope with or without the remote flag
        ("EMEA", False, True, "region EMEA"),
        ("Remote - EMEA", True, True, "region EMEA"),
        ("Europe", True, True, "region Europe"),
        ("Remote, EU", True, True, "region EU"),
        ("Worldwide", True, True, "region worldwide"),
        ("Remote - US; Remote - EMEA", True, True, "region EMEA"),
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
    assert classify_location(location, remote) == (in_scope, reason)
