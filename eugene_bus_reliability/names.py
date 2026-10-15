"""Stop names as people read them: LTD writes "W/S of Coburg S of Frontier"; the site shows
"Coburg south of Frontier (W side)". Shared by the site (app/common.py) and the plain report
pages (static_site.py)."""

from __future__ import annotations

import math
import re

_COMPASS = {
    "N": "north",
    "S": "south",
    "E": "east",
    "W": "west",
    "NE": "northeast",
    "NW": "northwest",
    "SE": "southeast",
    "SW": "southwest",
}
_STOP_WORDS = {
    "SPFLD": "Springfield",
    "STA": "Station",
    "STN": "Station",
    "CTR": "Center",
    "MKTPL": "Marketplace",
    "HWY": "Hwy",
    "EUG": "Eugene",
}
_SIDE = r"(?P<side>[NSEW])/\s?S\.?\s+of\s+"
_STOP_RULES = [
    (
        "side, street, direction of cross street",
        re.compile(
            _SIDE + r"(?P<street>.+?)\s+(?P<dir>NE|NW|SE|SW|N|S|E|W)\.?\s+of\s+(?P<cross>.+)$", re.I
        ),
        "{street} {dir} of {cross} ({side} side)",
    ),
    (
        "side, street at cross street",
        re.compile(_SIDE + r"(?P<street>.+?)\s+(?:at|@|&|and)\s+(?P<cross>.+)$", re.I),
        "{street} at {cross} ({side} side)",
    ),
    ("side, street only", re.compile(_SIDE + r"(?P<street>.+)$", re.I), "{street} ({side} side)"),
]
STOP_NAME_COLUMNS = ("stop_name", "next_stop", "stop2", "name1", "name2")


def _tidy_words(s: str) -> str:
    out = []
    for w in s.split():
        key = w.strip(".,").upper()
        if key in _STOP_WORDS:
            out.append(_STOP_WORDS[key])
        elif w.isupper() and len(w) > 3 and w.isalpha():  # "HILYARD" -> "Hilyard"; keep "LCC", "UO"
            out.append(w.capitalize())
        else:
            out.append(w)
    return " ".join(out)


def stop_name_rule(name) -> tuple[str, str | None]:
    """(readable name, the rule that produced it or None if no rule matched)."""
    if name is None or (isinstance(name, float) and math.isnan(name)):
        return "", None
    raw = " ".join(str(name).split())
    for label, rx, template in _STOP_RULES:
        m = rx.match(raw)
        if m:
            parts = {k: _tidy_words(v) for k, v in m.groupdict().items()}
            parts["side"] = m.group("side").upper()  # "(S side)": short, it's on every stop
            if "dir" in parts:
                parts["dir"] = _COMPASS[m.group("dir").upper()]
            return template.format(**parts), label
    return _tidy_words(raw), None


def clean_stop_name(name) -> str:
    return stop_name_rule(name)[0]
