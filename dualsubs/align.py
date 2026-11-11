"""Align two subtitle tracks and merge them into one bilingual track.

Two independent subtitle files for the same video rarely share cue boundaries:
they may be offset (different release / intro length), run at a different
frame rate (23.976 vs 25 fps PAL speed-up), and split sentences differently.

1. ``estimate_sync`` finds the time scale + offset that maximises the total
   overlap between the two tracks.
2. ``merge`` builds an overlap graph between primary and secondary cues and
   merges each connected group into one bilingual cue, so 1:1, 1:many and
   many:1 splits all come out sensibly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from .subtitles import Cue

# Common frame-rate conversions between releases (PAL speed-up etc.).
_FPS = (23.976, 24.0, 25.0)
SCALES = sorted({round(a / b, 6) for a in _FPS for b in _FPS})


@dataclass
class Sync:
    scale: float = 1.0
    offset: int = 0  # ms, applied after scaling
    score_before: float = 0.0  # overlap ratio with no correction
    score_after: float = 0.0  # overlap ratio with correction

    def apply(self, cues: list[Cue]) -> list[Cue]:
        if self.scale == 1.0 and self.offset == 0:
            return list(cues)
        return [Cue(round(c.start * self.scale) + self.offset, round(c.end * self.scale) + self.offset, c.text) for c in cues]

    def describe(self) -> str:
        if self.scale == 1.0 and self.offset == 0:
            return "in sync (no correction)"
        parts = []
        if self.offset:
            parts.append(f"shifted {self.offset / 1000:+.2f}s")
        if self.scale != 1.0:
            parts.append(f"rescaled x{self.scale:.4f}")
        return ", ".join(parts) + f" (overlap {self.score_before:.0%} -> {self.score_after:.0%})"


def _overlap_total(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    """Total overlapping time between two sorted, mostly non-overlapping interval lists."""
    i = j = total = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if e > s:
            total += e - s
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def estimate_sync(primary: list[Cue], secondary: list[Cue], max_offset_ms: int = 60_000) -> Sync:
    """Find scale/offset for ``secondary`` that best lines it up with ``primary``."""
    if not primary or not secondary:
        return Sync()
    pa = [(c.start, c.end) for c in primary]
    sec = [(c.start, c.end) for c in secondary]
    denom = min(sum(e - s for s, e in pa), sum(e - s for s, e in sec)) or 1

    def score(scale: float, off: int) -> int:
        return _overlap_total(pa, [(round(s * scale) + off, round(e * scale) + off) for s, e in sec])

    base = score(1.0, 0)

    def best_for(scale: float) -> tuple[int, float, int]:
        best = (score(scale, 0), scale, 0)
        for off in range(-max_offset_ms, max_offset_ms + 1, 500):  # coarse
            sc = score(scale, off)
            if sc > best[0]:
                best = (sc, scale, off)
        off0 = best[2]
        for off in range(off0 - 500, off0 + 501, 20):  # fine
            sc = score(scale, off)
            if sc > best[0]:
                best = (sc, scale, off)
        return best

    plain = best_for(1.0)
    rescaled = max((best_for(s) for s in SCALES if s != 1.0), key=lambda b: b[0])
    # A frame-rate fix must clearly beat a plain shift: over short spans a
    # small rescale + different offset can fit almost as well by accident.
    sc, scale, off = rescaled if rescaled[0] > plain[0] * 1.03 + 0.01 * denom else plain
    # Only correct when it is a clear improvement; otherwise trust the files.
    if sc < base * 1.05 + 0.01 * denom:
        return Sync(1.0, 0, base / denom, base / denom)
    return Sync(scale, off, base / denom, sc / denom)


@dataclass
class Style:
    secondary_color: str | None = "#FFD54F"  # amber; None = no colour tag
    secondary_italic: bool = False
    secondary_on_top: bool = False
    compact: bool = False  # join each language onto a single line
    separator: str | None = None  # optional line between languages, e.g. "—"

    def _fmt(self, text: str, secondary: bool) -> str:
        if self.compact:
            text = " ".join(text.split("\n"))
        text = escape(text, quote=False)
        if secondary:
            if self.secondary_italic:
                text = "\n".join(f"<i>{ln}</i>" for ln in text.split("\n"))
            if self.secondary_color:
                text = "\n".join(f'<font color="{self.secondary_color}">{ln}</font>' for ln in text.split("\n"))
        return text

    def render(self, primary_text: str, secondary_text: str) -> str:
        blocks = []
        if primary_text:
            blocks.append(self._fmt(primary_text, False))
        if secondary_text:
            blocks.append(self._fmt(secondary_text, True))
        if self.secondary_on_top:
            blocks.reverse()
        if self.separator and len(blocks) == 2:
            blocks.insert(1, self.separator)
        return "\n".join(blocks)


@dataclass
class MergeStats:
    sync: Sync = field(default_factory=Sync)
    primary_cues: int = 0
    secondary_cues: int = 0
    paired: int = 0  # output cues containing both languages
    primary_only: int = 0
    secondary_only: int = 0

    def describe(self) -> str:
        total = self.paired + self.primary_only + self.secondary_only
        pct = self.paired / total if total else 0
        return (
            f"{total} cues: {self.paired} bilingual ({pct:.0%}), {self.primary_only} primary-only, "
            f"{self.secondary_only} secondary-only; secondary {self.sync.describe()}"
        )


def _overlap(a: Cue, b: Cue) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def merge(
    primary: list[Cue],
    secondary: list[Cue],
    style: Style | None = None,
    sync: Sync | bool = True,
    min_ratio: float = 0.3,
    max_group: int = 3,
) -> tuple[list[Cue], MergeStats]:
    """Merge two tracks into bilingual cues on the primary track's timeline.

    ``sync``: True to auto-estimate, False for none, or an explicit Sync.
    ``min_ratio``: two cues are linked when their overlap is at least this
    fraction of the shorter cue.
    """
    style = style or Style()
    if sync is True:
        sync = estimate_sync(primary, secondary)
    elif sync is False:
        sync = Sync()
    sec = sync.apply(secondary)
    stats = MergeStats(sync=sync, primary_cues=len(primary), secondary_cues=len(sec))

    # Build overlap edges (both lists are sorted by start).
    edges: dict[int, list[tuple[int, int]]] = {}  # primary idx -> [(secondary idx, overlap)]
    j0 = 0
    for i, p in enumerate(primary):
        while j0 < len(sec) and sec[j0].end <= p.start - 30_000:
            j0 += 1
        j = j0
        while j < len(sec) and sec[j].start < p.end:
            ov = _overlap(p, sec[j])
            if ov and ov >= min_ratio * min(p.duration, sec[j].duration):
                edges.setdefault(i, []).append((j, ov))
            j += 1

    # Union-find over primary (i) and secondary (n + j) nodes.
    n = len(primary)
    parent = list(range(n + len(sec)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, lst in edges.items():
        for j, _ in lst:
            parent[find(i)] = find(n + j)

    groups: dict[int, tuple[list[int], list[int]]] = {}
    for i in range(n):
        groups.setdefault(find(i), ([], []))[0].append(i)
    for j in range(len(sec)):
        groups.setdefault(find(n + j), ([], []))[1].append(j)

    out: list[Cue] = []
    for p_idx, s_idx in groups.values():
        if len(p_idx) > max_group or len(s_idx) > max_group:
            # Long chain (e.g. overlapping karaoke lines): fall back to giving each
            # secondary cue to the primary cue it overlaps most.
            best: dict[int, list[int]] = {i: [] for i in p_idx}
            orphans = []
            for j in s_idx:
                cands = [(ov, i) for i in p_idx for (jj, ov) in edges.get(i, []) if jj == j]
                if cands:
                    best[max(cands)[1]].append(j)
                else:
                    orphans.append(j)
            for i, js in best.items():
                out.append(_combine([primary[i]], [sec[j] for j in js], style, stats))
            for j in orphans:
                out.append(_combine([], [sec[j]], style, stats))
        else:
            out.append(_combine([primary[i] for i in p_idx], [sec[j] for j in s_idx], style, stats))

    out.sort(key=lambda c: (c.start, c.end))
    return _resolve_overlaps(out), stats


def _combine(ps: list[Cue], ss: list[Cue], style: Style, stats: MergeStats) -> Cue:
    ps = sorted(ps, key=lambda c: c.start)
    ss = sorted(ss, key=lambda c: c.start)
    if ps and ss:
        stats.paired += 1
    elif ps:
        stats.primary_only += 1
    else:
        stats.secondary_only += 1
    timing = ps or ss  # primary timeline wins when present
    start = min(c.start for c in timing)
    end = max(c.end for c in timing)
    p_text = "\n".join(c.text for c in ps)
    s_text = "\n".join(c.text for c in ss)
    return Cue(start, end, style.render(p_text, s_text))


def _resolve_overlaps(cues: list[Cue]) -> list[Cue]:
    """Trim cues so a later cue never shares the screen with an earlier one.

    Players handle simultaneous SRT cues inconsistently (some stack them, some
    drop one), so we trim the earlier cue's end to the next cue's start.
    """
    out: list[Cue] = []
    for c in cues:
        if out and out[-1].end > c.start:
            prev = out[-1]
            if c.start - prev.start >= 300:
                out[-1] = Cue(prev.start, c.start, prev.text)
            else:
                # Starts almost together: show both texts in one cue.
                out[-1] = Cue(prev.start, max(prev.end, c.end), prev.text + "\n" + c.text)
                continue
        out.append(c)
    return out
