"""Is a posting in scope for a job search in Ireland? (ADR 0010)

In scope: located in Ireland, or open to Ireland across a region (EMEA,
Europe, EU, worldwide, anywhere). A bare "Remote", with no region stated, is
also in scope but says so, because its region can't be told from the text.
Everything else is out of scope with the location as its reason. Postings are
kept either way; the digest shows the in-scope ones and counts the rest.
"""

import re

_IRISH_PLACES = ["Dublin", "Cork", "Galway", "Limerick"]
# Northern Ireland is in the UK: a different work permit. Checked first.
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
_REMOTE = re.compile(r"\bremote\b", re.I)


def classify_location(location: str, remote: bool) -> tuple[bool, str]:
    """(in_scope, reason). The reason names the place or region that decided."""
    text = location.strip()
    without_ni = _NORTHERN_IRELAND.sub("", text)
    for place in _IRISH_PLACES:
        if re.search(rf"\b{place}\b", without_ni, re.I):
            return True, place
    if _IRELAND.search(without_ni) or _IE_CODE.search(without_ni):
        return True, "Ireland"
    for name, pattern in _REGIONS:
        if pattern.search(text):
            return True, f"region {name}"
    is_remote = remote or bool(_REMOTE.search(text))
    if is_remote and not _REMOTE.sub("", text).strip(" -,;|()"):
        # "Remote" and nothing else: no country or region to rule it out.
        return True, "remote (region not stated)"
    if _NORTHERN_IRELAND.search(text):
        return False, f"out: {text} (UK)"
    return False, f"out: {text or 'no location'}"
