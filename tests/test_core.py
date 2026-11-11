import random

from dualsubs.align import Style, Sync, estimate_sync, merge
from dualsubs.subtitles import Cue, parse, write_srt

SRT = """1
00:00:01,000 --> 00:00:03,000
<i>Hallo daar.</i>

2
00:00:04,000 --> 00:00:06,500
Hoe gaat het
met je?
"""

VTT = """WEBVTT

00:01.000 --> 00:03.000
Hello there.

00:04.000 --> 00:06.500 align:start
How are you?
"""

ASS = """[Script Info]
Title: test

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\i1}Hello, there.{\\i0}
Dialogue: 0,0:00:04.00,0:00:06.50,Default,,0,0,0,,How are\\Nyou?
"""


def test_parse_formats():
    srt = parse(SRT.encode("utf-8"))
    assert [(c.start, c.end, c.text) for c in srt] == [(1000, 3000, "Hallo daar."), (4000, 6500, "Hoe gaat het\nmet je?")]
    vtt = parse(VTT)
    assert [(c.start, c.end, c.text) for c in vtt] == [(1000, 3000, "Hello there."), (4000, 6500, "How are you?")]
    ass = parse(ASS, "ass")
    assert [(c.start, c.end, c.text) for c in ass] == [(1000, 3000, "Hello, there."), (4000, 6500, "How are\nyou?")]


def test_bom_and_cp1252():
    assert parse(b"\xef\xbb\xbf" + SRT.encode())[0].text == "Hallo daar."
    assert parse("1\n00:00:01,000 --> 00:00:02,000\ncaf\xe9\n".encode("cp1252"))[0].text == "café"


def test_merge_in_sync():
    out, stats = merge(parse(SRT), parse(VTT), Style(secondary_color=None))
    assert stats.paired == 2 and stats.primary_only == 0 and stats.secondary_only == 0
    assert out[0].text == "Hallo daar.\nHello there."
    assert out[1].text == "Hoe gaat het\nmet je?\nHow are you?"


def test_merge_styles():
    out, _ = merge(parse(SRT), parse(VTT), Style(compact=True, secondary_italic=True))
    assert out[1].text == 'Hoe gaat het met je?\n<font color="#FFD54F"><i>How are you?</i></font>'
    out, _ = merge(parse(SRT), parse(VTT), Style(secondary_color=None, secondary_on_top=True))
    assert out[0].text == "Hello there.\nHallo daar."


def test_many_to_one_split():
    primary = [Cue(1000, 2000, "A1"), Cue(2000, 3000, "A2")]
    secondary = [Cue(1000, 3000, "B")]
    out, stats = merge(primary, secondary, Style(secondary_color=None), sync=False)
    assert len(out) == 1 and out[0].text == "A1\nA2\nB" and (out[0].start, out[0].end) == (1000, 3000)


def test_unmatched_cues_kept():
    primary = [Cue(1000, 2000, "only primary")]
    secondary = [Cue(5000, 6000, "only secondary")]
    out, stats = merge(primary, secondary, Style(secondary_color=None), sync=False)
    assert [c.text for c in out] == ["only primary", "only secondary"]
    assert stats.primary_only == 1 and stats.secondary_only == 1


def _fake_track(n=400, seed=1):
    rnd = random.Random(seed)
    t, cues = 5000, []
    for k in range(n):
        t += rnd.randint(300, 4000)
        d = rnd.randint(900, 4500)
        cues.append(Cue(t, t + d, f"line {k}"))
        t += d
    return cues


def test_estimate_offset():
    primary = _fake_track()
    shifted = [Cue(c.start + 2730, c.end + 2730, c.text) for c in primary]
    sync = estimate_sync(primary, shifted)
    assert sync.scale == 1.0 and abs(sync.offset + 2730) <= 20
    assert sync.score_after > 0.95


def test_estimate_framerate_and_offset():
    primary = _fake_track()
    # Secondary made for a 25 fps PAL release: runs faster, plus a 1.5 s offset.
    k = 23.976 / 25
    pal = [Cue(round(c.start * k) - 1500, round(c.end * k) - 1500, c.text) for c in primary]
    sync = estimate_sync(primary, pal)
    assert abs(sync.scale - 25 / 23.976) < 1e-3
    fixed = sync.apply(pal)
    assert max(abs(a.start - b.start) for a, b in zip(primary, fixed)) < 150


def test_in_sync_not_touched():
    primary = _fake_track()
    assert estimate_sync(primary, list(primary)) == Sync(1.0, 0, 1.0, 1.0)


def test_write_srt_roundtrip():
    cues = parse(SRT)
    assert parse(write_srt(cues)) == cues
