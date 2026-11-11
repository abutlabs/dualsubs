"""Parse and write subtitle files (SRT, WebVTT, ASS/SSA) using only the stdlib.

Everything is normalised to a list of Cue objects with millisecond timings and
plain text (formatting tags stripped), which is what the aligner works on.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass


@dataclass
class Cue:
    start: int  # milliseconds
    end: int  # milliseconds
    text: str  # plain text, lines separated by "\n"

    @property
    def duration(self) -> int:
        return self.end - self.start


# --------------------------------------------------------------------------- decoding

def decode_bytes(data: bytes) -> str:
    """Decode subtitle bytes, coping with BOMs and legacy encodings."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Most non-UTF-8 European subtitle files are Windows-1252.
        return data.decode("cp1252", errors="replace")


# --------------------------------------------------------------------------- text cleanup

_HTML_TAG = re.compile(r"</?(?:i|b|u|s|font|span|c|v|ruby|rt|lang)(?:[ .][^>]*)?>", re.IGNORECASE)
_ASS_OVERRIDE = re.compile(r"\{[^}]*\}")
_WS = re.compile(r"[ \t]+")


def clean_text(text: str) -> str:
    text = _ASS_OVERRIDE.sub("", text)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    text = _HTML_TAG.sub("", text)
    text = html.unescape(text)
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# --------------------------------------------------------------------------- SRT / VTT

_TIME = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
)


def _ms(h, m, s, frac) -> int:
    frac = (frac or "0").ljust(3, "0")[:3]
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac)


def parse_srt_or_vtt(text: str) -> list[Cue]:
    """Parse SRT or WebVTT. Tolerant of missing indexes, blank-line quirks and CRLF."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines):
            m = _TIME.search(line)
            if m:
                g = m.groups()
                start, end = _ms(*g[0:4]), _ms(*g[4:8])
                body = clean_text("\n".join(lines[i + 1:]))
                if body and end > start:
                    cues.append(Cue(start, end, body))
                break
    cues.sort(key=lambda c: (c.start, c.end))
    return cues


# --------------------------------------------------------------------------- ASS / SSA

def _ass_time(t: str) -> int:
    h, m, s = t.strip().split(":")
    sec, _, cs = s.partition(".")
    return ((int(h) * 60 + int(m)) * 60 + int(sec)) * 1000 + int((cs or "0").ljust(2, "0")[:2]) * 10


def parse_ass(text: str) -> list[Cue]:
    fields: list[str] | None = None
    in_events = False
    cues: list[Cue] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("["):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue
        if line.lower().startswith("format:"):
            fields = [f.strip().lower() for f in line[7:].split(",")]
        elif line.lower().startswith("dialogue:") and fields:
            parts = line[9:].split(",", len(fields) - 1)
            row = dict(zip(fields, (p.strip() for p in parts)))
            body = clean_text(row.get("text", ""))
            # Skip drawing commands / signs that clean to nothing.
            if body:
                start, end = _ass_time(row["start"]), _ass_time(row["end"])
                if end > start:
                    cues.append(Cue(start, end, body))
    cues.sort(key=lambda c: (c.start, c.end))
    return _dedupe(cues)


def _dedupe(cues: list[Cue]) -> list[Cue]:
    out: list[Cue] = []
    for c in cues:
        if out and out[-1].start == c.start and out[-1].end == c.end and out[-1].text == c.text:
            continue
        out.append(c)
    return out


# --------------------------------------------------------------------------- entry points

def parse(data: bytes | str, fmt: str | None = None) -> list[Cue]:
    text = decode_bytes(data) if isinstance(data, bytes) else data
    fmt = (fmt or "").lower()
    if fmt in ("ass", "ssa") or (not fmt and "[Script Info]" in text[:2000]):
        return parse_ass(text)
    return parse_srt_or_vtt(text)


def _srt_time(ms: int) -> str:
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue]) -> str:
    """Serialise cues to SRT. Cue text may contain basic HTML (<i>, <font>)."""
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n")
    return "\n".join(out)
