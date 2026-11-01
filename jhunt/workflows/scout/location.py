"""Is a posting in scope for a job search in Ireland? (ADR 0010)

In scope: located in Ireland, or remote across a region that includes Ireland
(EMEA, Europe, EU, worldwide, anywhere). A bare "Remote", with no region
stated, is also in scope but says so, because its region can't be told from
the text. Everything else is out of scope with the location as its reason.
Postings are kept either way; the digest shows the in-scope ones and counts
the rest. Northern Ireland is a setting in scope.toml.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

SCOPE_FILE = "scope.toml"

_IRISH_PLACES = ["Dublin", "Cork", "Galway", "Limerick"]
# Northern Ireland is in the UK: a different work permit. Removed before the
# Irish check, since it contains "Ireland"; scope.toml decides if it counts.
_NORTHERN_IRELAND = re.compile(r"\b(northern ireland|belfast|derry|londonderry)\b", re.I)
_IRELAND = re.compile(r"\bireland\b", re.I)
# "IE" only as a code standing alone ("Remote - IE", "Dublin, IE").
_IE_CODE = re.compile(r"(?<![A-Za-z])IE(?![A-Za-z])")
_REGIONS = [
    ("EMEA", re.compile(r"\bemea\b", re.I)),
    ("Europe", re.compile(r"\beurope(an)?\b", re.I)),
    ("EU", re.compile(r"\b(eu|european union)\b", re.I)),
    ("worldwide", re.compile(r"\b(worldwide|anywhere|global|international)\b", re.I)),
]
# Dublin, Cork, Galway and Limerick are also US towns. A location is in the
# US if it names the country, or a state after a comma ("Dublin, CA",
# "Dublin, Ohio", "Dublin, OH 43017"). "US" only in capitals, never "us".
_US_COUNTRY = re.compile(r"\b(united states|usa|u\.s\.a?\.?)(?![A-Za-z])", re.I)
_US_CODE = re.compile(r"(?<![A-Za-z])US(?![A-Za-z])")
_US_STATE_CODES = frozenset(
    [
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
        "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
        "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
        "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
    ]
)  # fmt: skip
_US_STATE_NAMES = frozenset(
    name.lower()
    for name in [
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
        "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
        "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
        "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
        "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
        "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
        "Washington", "West Virginia", "Wisconsin", "Wyoming",
        "District of Columbia",
    ]
)  # fmt: skip
_REMOTE = re.compile(r"\bremote\b", re.I)


class ScopeError(Exception):
    """scope.toml is missing or wrong."""


@dataclass(frozen=True)
class Scope:
    """The owner's settings from <JHUNT_CONFIG_DIR>/scope.toml, in git."""

    northern_ireland_in_scope: bool


def load_scope(config_dir: Path) -> Scope:
    path = config_dir / SCOPE_FILE
    if not path.is_file():
        raise ScopeError(f"scope file missing: {path} (resolved: {path.resolve()})")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ScopeError(f"{path}: invalid TOML: {exc}") from None
    unknown = set(data) - {"northern_ireland_in_scope"}
    if unknown:
        raise ScopeError(f"{path}: unknown setting {sorted(unknown)[0]!r}")
    if "northern_ireland_in_scope" not in data:
        raise ScopeError(f"{path}: northern_ireland_in_scope is missing")
    if not isinstance(data["northern_ireland_in_scope"], bool):
        raise ScopeError(f"{path}: northern_ireland_in_scope must be true or false")
    return Scope(northern_ireland_in_scope=data["northern_ireland_in_scope"])


def classify_location(location: str, remote: bool, scope: Scope) -> tuple[bool, str]:
    """(in_scope, reason). The reason names the place or region that decided."""
    text = location.strip()
    # A posting may list several locations ("London; Dublin"); each is read
    # on its own, so a US Dublin in one doesn't hide an Irish one in another.
    places = [p.strip() for p in re.split(r"[;|]", text) if p.strip()]

    in_us = False
    for place in places:
        irish = _irish_place(_NORTHERN_IRELAND.sub("", place))
        if irish and _in_us(place):
            in_us = True
        elif irish:
            return True, irish
    in_ni = bool(_NORTHERN_IRELAND.search(text))
    if in_ni and scope.northern_ireland_in_scope:
        return True, "Northern Ireland"

    is_remote = remote or bool(_REMOTE.search(text))
    for name, pattern in _REGIONS:
        if pattern.search(text):
            if is_remote:
                return True, f"region {name}"
            # An office somewhere in the region, not a job open across it.
            place = _without_regions(text)
            return False, f"out: on-site {place} ({name})" if place else f"out: on-site ({name})"
    if is_remote and not _REMOTE.sub("", text).strip(" -,;|()"):
        # "Remote" and nothing else: no country or region to rule it out.
        return True, "remote (region not stated)"
    if in_ni:
        return False, f"out: {text} (UK)"
    if in_us:
        return False, f"out: {text} (US)"
    return False, f"out: {text or 'no location'}"


def _irish_place(place: str) -> str | None:
    for name in _IRISH_PLACES:
        if re.search(rf"\b{name}\b", place, re.I):
            return name
    if _IRELAND.search(place) or _IE_CODE.search(place):
        return "Ireland"
    return None


def _in_us(place: str) -> bool:
    if _US_COUNTRY.search(place) or _US_CODE.search(place):
        return True
    for part in place.split(",")[1:]:
        part = re.sub(r"\s*\d[\d-]*$", "", part).strip()  # drop a ZIP code
        if part in _US_STATE_CODES or part.lower() in _US_STATE_NAMES:
            return True
    return False


def _without_regions(text: str) -> str:
    for _, pattern in _REGIONS:
        text = pattern.sub("", text)
    return re.sub(r"\(\s*\)", "", text).strip(" -,;|")
