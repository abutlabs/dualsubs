"""Language code helpers. Plex reports ISO 639-2 codes (often the /B variants
such as ``dut``, ``ger``, ``fre``); users think in ISO 639-1 (``nl``, ``de``)."""
from __future__ import annotations

_GROUPS = [
    ("en", "eng", "English"), ("nl", "nld dut", "Dutch Nederlands Flemish Vlaams"),
    ("de", "deu ger", "German Deutsch"), ("fr", "fra fre", "French Français"),
    ("es", "spa", "Spanish Español"), ("it", "ita", "Italian Italiano"),
    ("pt", "por", "Portuguese Português"), ("sv", "swe", "Swedish Svenska"),
    ("da", "dan", "Danish Dansk"), ("no", "nor nob nno", "Norwegian Norsk"),
    ("fi", "fin", "Finnish Suomi"), ("pl", "pol", "Polish Polski"),
    ("ru", "rus", "Russian"), ("uk", "ukr", "Ukrainian"), ("tr", "tur", "Turkish"),
    ("el", "ell gre", "Greek"), ("cs", "ces cze", "Czech"), ("hu", "hun", "Hungarian"),
    ("ro", "ron rum", "Romanian"), ("ar", "ara", "Arabic"), ("he", "heb", "Hebrew"),
    ("hi", "hin", "Hindi"), ("ja", "jpn", "Japanese"), ("ko", "kor", "Korean"),
    ("zh", "zho chi", "Chinese"), ("id", "ind", "Indonesian"), ("vi", "vie", "Vietnamese"),
    ("th", "tha", "Thai"),
]

_LOOKUP: dict[str, str] = {}
NAMES: dict[str, str] = {}
for two, threes, names in _GROUPS:
    NAMES[two] = names.split()[0]
    for alias in [two, *threes.split(), *(n.lower() for n in names.split())]:
        _LOOKUP[alias.lower()] = two


def normalize(code: str | None) -> str | None:
    """Map any code or English/native name to ISO 639-1, or None if unknown."""
    if not code:
        return None
    code = code.strip().lower().replace("_", "-")
    return _LOOKUP.get(code) or _LOOKUP.get(code.split("-")[0])


def name(code: str) -> str:
    return NAMES.get(normalize(code) or code, code)
