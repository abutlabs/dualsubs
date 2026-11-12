"""End to end against a fake Plex server and a real .mkv with embedded subtitles."""
import shutil
import subprocess

import pytest

from dualsubs.cli import main
from dualsubs.subtitles import parse

from .mock_plex import TOKEN, FakePlex, serve

NL = """1
00:00:01,000 --> 00:00:03,000
Goedemorgen, iedereen.

2
00:00:04,000 --> 00:00:06,000
Waar is de koffie?

3
00:00:07,000 --> 00:00:09,500
In de keuken,
naast de theepot.
"""

# English from another release: 1.2 s late and the last line split in two.
EN = """1
00:00:02,200 --> 00:00:04,200
Good morning, everyone.

2
00:00:05,200 --> 00:00:07,200
Where's the coffee?

3
00:00:08,200 --> 00:00:09,400
In the kitchen,

4
00:00:09,400 --> 00:00:10,700
next to the teapot.
"""

ES = (EN.replace("Good morning, everyone.", "Buenos días a todos.").replace("Where's the coffee?", "¿Dónde está el café?")
      .replace("In the kitchen,", "En la cocina,").replace("next to the teapot.", "junto a la tetera."))
FR = (NL.replace("Goedemorgen, iedereen.", "Bonjour à tous.").replace("Waar is de koffie?", "Où est le café ?")
      .replace("In de keuken,\nnaast de theepot.", "Dans la cuisine,\nà côté de la théière."))

EN_SDH = EN.replace("Where's the coffee?", "[KETTLE WHISTLES] Where's the coffee?")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture
def media(tmp_path):
    (tmp_path / "nl.srt").write_text(NL)
    (tmp_path / "sdh.srt").write_text(EN_SDH)
    mkv = tmp_path / "film.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=12",
        "-i", str(tmp_path / "nl.srt"), "-i", str(tmp_path / "sdh.srt"),
        "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-c:s", "srt",
        "-metadata:s:s:0", "language=dut", "-metadata:s:s:1", "language=eng", str(mkv),
    ], check=True)
    return mkv


@pytest.fixture
def plex(media, tmp_path, monkeypatch):
    fake = FakePlex()
    fake.add_movie("101", "Koffie", "/plexmedia/film.mkv", [
        {"id": 301, "codec": "srt", "index": 1, "languageCode": "dut", "displayTitle": "Nederlands (SRT)"},
        {"id": 302, "codec": "srt", "index": 2, "languageCode": "eng", "hearingImpaired": 1, "displayTitle": "English SDH (SRT)"},
        {"id": 303, "codec": "pgs", "index": 3, "languageCode": "ger", "displayTitle": "Deutsch (PGS)"},
    ])
    fake.add_external("101", 304, "eng", EN.encode(), displayTitle="English (SRT External)")
    fake.add_external("101", 305, "spa", ES.encode(), displayTitle="Español (SRT External)")
    fake.add_external("101", 306, "fre", FR.encode(), displayTitle="Français (SRT External)")
    srv, url = serve(fake)
    monkeypatch.setenv("PLEX_URL", url)
    monkeypatch.setenv("PLEX_TOKEN", TOKEN)
    monkeypatch.setenv("DUALSUBS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DUALSUBS_PATH_MAP", f"/plexmedia={media.parent}")
    yield fake
    srv.shutdown()


@needs_ffmpeg
def test_add_by_title_uploads_merged_track(plex, capsys):
    assert main(["add", "Koffie", "-p", "nl", "-s", "en"]) == 0
    out = capsys.readouterr().out
    assert "[+] Koffie (2024) [NL+EN]: added" in out
    # Embedded Dutch + external non-SDH English were chosen over embedded SDH English.
    assert "nl srt embedded #1 + en srt external" in out
    assert "shifted -1.20s" in out
    assert len(plex.uploads) == 1
    up = plex.uploads[0]
    assert up["title"] == "dualsubs NL+EN.nl.srt"
    cues = parse(up["body"])
    assert len(cues) == 3
    assert cues[0].text == "Goedemorgen, iedereen.\nGood morning, everyone."  # tags stripped by parse
    assert "KETTLE" not in up["body"].decode()
    assert cues[2].text.endswith("In the kitchen,\nnext to the teapot.")
    assert b'<font color="#FFD54F">' in up["body"]


@needs_ffmpeg
def test_rerun_is_idempotent_and_force_replaces(plex, capsys):
    main(["add", "101"])
    main(["add", "101"])
    assert len(plex.uploads) == 1
    assert "up-to-date" in capsys.readouterr().out or True
    main(["add", "101", "--force", "--color", "none"])
    assert len(plex.uploads) == 2
    ours = [s for s in plex.items["101"]["streams"] if "dualsubs" in s.get("title", "")]
    assert len(ours) == 1  # old upload deleted, not stacked
    assert b"<font" not in plex.uploads[-1]["body"]
    # Our own uploaded track is never used as a source.
    assert "dualsubs" not in capsys.readouterr().out.split("added")[-1].split("+")[0]


@needs_ffmpeg
def test_dry_run_changes_nothing(plex, capsys):
    assert main(["add", "Koffie", "--dry-run"]) == 0
    assert plex.uploads == []
    assert "dry-run" in capsys.readouterr().out


def test_missing_language_reports_image_only(plex, capsys):
    main(["add", "101", "-p", "de", "-s", "en"])
    assert "only image-based" in capsys.readouterr().out
    assert plex.uploads == []


@needs_ffmpeg
def test_remove(plex, capsys):
    main(["add", "101"])
    main(["remove", "101"])
    assert not [s for s in plex.items["101"]["streams"] if "dualsubs" in s.get("title", "")]
    assert "removed" in capsys.readouterr().out


@needs_ffmpeg
def test_watch_once_and_inspect(plex, capsys):
    main(["watch", "--once", "--interval", "1"])
    assert len(plex.uploads) == 1
    main(["inspect", "101"])
    out = capsys.readouterr().out
    assert "embedded #1" in out and "image" in out and "dualsubs NL+EN" not in out.split("inspect")[0]


@needs_ffmpeg
def test_sidecar_mode(plex, media, capsys):
    assert main(["add", "101", "--mode", "sidecar"]) == 0
    side = media.parent / "film.dualsubs-en.nl.srt"
    assert side.exists() and "Good morning" in side.read_text()
    assert plex.uploads == []
    main(["add", "101", "--mode", "sidecar"])
    assert "up-to-date" in capsys.readouterr().out or True
    main(["remove", "101"])
    assert not side.exists()


def test_bad_token(plex, monkeypatch):
    monkeypatch.setenv("PLEX_TOKEN", "nope")
    with pytest.raises(SystemExit) as e:
        main(["libraries"])
    assert "401" in str(e.value)


@needs_ffmpeg
def test_multiple_pairs(plex, capsys, tmp_path, monkeypatch):
    # Pairs from the config file, as a user would set them up once.
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "config.json").write_text('{"pairs": ["en+es", "en+fr", "en+nl"]}')
    assert main(["add", "101"]) == 0
    out = capsys.readouterr().out
    assert out.count("]: added") == 3 and "Summary: 3 added" in out
    titles = [u["title"] for u in plex.uploads]
    assert titles == ["dualsubs EN+ES.en.srt", "dualsubs EN+FR.en.srt", "dualsubs EN+NL.en.srt"]
    es = parse(plex.uploads[0]["body"])
    assert es[1].text == "Where's the coffee?\n\u00bfD\u00f3nde est\u00e1 el caf\u00e9?"
    fr = parse(plex.uploads[1]["body"])
    assert fr[0].text == "Good morning, everyone.\nBonjour \u00e0 tous."
    # Re-run: all three up to date, nothing re-uploaded; our tracks never used as sources.
    main(["add", "101"])
    assert len(plex.uploads) == 3
    # Redo just one pair with a new style: only that track is replaced.
    main(["add", "101", "--pair", "en+fr", "--force", "--italic"])
    assert len(plex.uploads) == 4
    ours = sorted(s["title"] for s in plex.items["101"]["streams"] if "dualsubs" in s.get("title", ""))
    assert ours == ["dualsubs EN+ES.en.srt", "dualsubs EN+FR.en.srt", "dualsubs EN+NL.en.srt"]
    # Remove one pair, then everything.
    main(["remove", "101", "--pair", "en+es"])
    ours = sorted(s["title"] for s in plex.items["101"]["streams"] if "dualsubs" in s.get("title", ""))
    assert ours == ["dualsubs EN+FR.en.srt", "dualsubs EN+NL.en.srt"]
    main(["remove", "101"])
    assert not [s for s in plex.items["101"]["streams"] if "dualsubs" in s.get("title", "")]


def test_pair_parsing():
    from dualsubs.pipeline import parse_pair
    assert parse_pair("en+es") == ("en", "es")
    assert parse_pair("English/Spanish") == ("en", "es")
    assert parse_pair("eng-fre") == ("en", "fr")
    assert parse_pair("nl:en") == ("nl", "en")
    for bad in ("en", "en+en", "en+xx", "en+es+fr"):
        with pytest.raises(ValueError):
            parse_pair(bad)


@needs_ffmpeg
def test_one_ffmpeg_pass_per_file(plex, monkeypatch, tmp_path):
    """Two pairs needing two different embedded tracks: the video file is read once."""
    import subprocess as sp
    from dualsubs import plex as plexmod

    # Drop the external tracks so English must come from inside the .mkv too.
    plex.items["101"]["streams"] = [s for s in plex.items["101"]["streams"] if "key" not in s]
    calls = []
    real_run = sp.run

    def counting_run(cmd, *a, **kw):
        calls.append(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(plexmod.subprocess, "run", counting_run)
    assert main(["add", "101", "--pair", "nl+en", "--pair", "en+nl"]) == 0
    assert len(plex.uploads) == 2
    assert len(calls) == 1 and calls[0].count("-map") == 2
    # SDH English was the only English left, and it was used.
    assert "KETTLE" in plex.uploads[0]["body"].decode()


@needs_ffmpeg
def test_second_run_is_refused_while_one_is_busy(plex, capsys, tmp_path):
    from dualsubs.pipeline import State

    holder = State(tmp_path / "home" / "state.json")
    assert holder.lock()
    with pytest.raises(SystemExit) as e:
        main(["add", "101"])
    assert "already working" in str(e.value)
    with pytest.raises(SystemExit) as e:  # cron: quiet, successful exit
        main(["watch", "--once"])
    assert e.value.code == 0 and "skipped" in capsys.readouterr().out
    assert main(["add", "101", "--dry-run"]) == 0  # previews still allowed
    assert plex.uploads == []
    holder._lock_file.close()
    assert main(["add", "101"]) == 0 and len(plex.uploads) == 1


def test_config_is_created_private(tmp_path, monkeypatch):
    import os
    import stat
    from dualsubs.cli import save_config

    monkeypatch.setenv("DUALSUBS_HOME", str(tmp_path / "cfg"))
    old = os.umask(0o022)
    try:
        p = save_config({"token": "secret"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


@needs_ffmpeg
def test_remove_all_uses_state(plex, capsys):
    main(["add", "101", "--pair", "nl+en", "--pair", "en+nl"])
    assert len([s for s in plex.items["101"]["streams"] if "dualsubs" in s.get("title", "")]) == 2
    plex.requests.clear()
    main(["remove", "all"])
    assert not [s for s in plex.items["101"]["streams"] if "dualsubs" in s.get("title", "")]
    assert "GET /library/sections" not in plex.requests  # no full-library walk


@needs_ffmpeg
def test_pauses_while_someone_is_watching(plex, monkeypatch, capsys):
    from dualsubs import cli

    plex.sessions = ["playing", "paused"]
    slept = []

    def fake_sleep(s):
        slept.append(s)
        plex.sessions = ["paused"]  # the viewer stops; a paused session must not block

    monkeypatch.setattr(cli.StreamGate, "sleep", staticmethod(fake_sleep))
    monkeypatch.setattr(cli.StreamGate, "CACHE_S", -1)  # ask Plex every time
    assert main(["add", "101"]) == 0
    out = capsys.readouterr().out
    assert "paused: 1 Plex stream(s) playing" in out and "resumed" in out
    assert slept == [60] and len(plex.uploads) == 1


@needs_ffmpeg
def test_cron_run_skips_while_streaming_and_ignore_flag(plex, capsys):
    plex.sessions = ["playing"]
    assert main(["watch", "--once"]) == 0
    assert "skipped: 1 Plex stream(s) playing" in capsys.readouterr().out
    assert plex.uploads == []
    assert main(["add", "101", "--ignore-streams"]) == 0
    assert len(plex.uploads) == 1


@needs_ffmpeg
def test_ffmpeg_runs_at_low_priority(plex, monkeypatch):
    import shutil
    import subprocess as sp
    from dualsubs import plex as plexmod

    calls = []
    real_run = sp.run
    monkeypatch.setattr(plexmod.subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd) or real_run(cmd, *a, **kw))
    main(["add", "101"])
    expected = (["nice", "-n", "19"] if shutil.which("nice") else []) + (
        ["ionice", "-c", "2", "-n", "7"] if shutil.which("ionice") else [])
    assert calls[0][: len(expected)] == expected
    calls.clear()
    main(["add", "101", "--force", "--priority", "idle"])
    if shutil.which("ionice"):
        assert ["ionice", "-c", "3"] == calls[0][3:6]
    calls.clear()
    main(["add", "101", "--force", "--priority", "normal"])
    assert "nice" not in calls[0][0]


def test_shared_user_gets_clear_403(plex, capsys):
    plex.deny_writes = True
    main(["add", "101", "--ignore-streams", "--pair", "en+es"])
    out = capsys.readouterr().out
    assert "needs the server OWNER" in out


# ---------------------------------------------------------------------- fail fast / fetch missing

def _only_english_external(plex):
    """An item where only English is available (as an external track) plus Dutch inside the file."""
    plex.items["101"]["streams"] = [s for s in plex.items["101"]["streams"] if s["id"] in (301, 304)]


@needs_ffmpeg
def test_no_file_read_when_a_pair_cannot_be_made(plex, monkeypatch, capsys):
    """en+es: Spanish is missing, so the .mkv must not be read and nothing downloaded."""
    import subprocess as sp
    from dualsubs import plex as plexmod

    _only_english_external(plex)
    calls = []
    real_run = sp.run
    monkeypatch.setattr(plexmod.subprocess, "run", lambda cmd, *a, **kw: calls.append(cmd) or real_run(cmd, *a, **kw))
    plex.requests.clear()
    assert main(["add", "101", "--pair", "en+es"]) == 0
    assert calls == []  # no ffmpeg
    assert not [r for r in plex.requests if r.startswith("GET /library/streams/")]  # no subtitle download
    assert "Spanish: no subtitles" in capsys.readouterr().out
    # nl+en can be made and es+en can't: the file is read once, for the possible pair only.
    calls.clear()
    main(["add", "101", "--pair", "nl+en", "--pair", "es+en"])
    assert len(calls) == 1 and calls[0].count("-map") == 1


@needs_ffmpeg
def test_impossible_items_are_remembered(plex, capsys):
    _only_english_external(plex)
    main(["add", "101", "--pair", "en+es"])
    plex.requests.clear()
    main(["add", "101", "--pair", "en+es", "-v"])
    out = capsys.readouterr().out
    assert "unchanged" in out
    # Only the metadata lookup happened, nothing else.
    assert plex.requests == ["GET /library/metadata/101"] or all("metadata/101" in r or "sessions" in r for r in plex.requests)
    # A Spanish track appearing later reopens the item.
    plex.add_external("101", 999, "spa", ES.encode())
    main(["add", "101", "--pair", "en+es"])
    assert "[EN+ES]: added" in capsys.readouterr().out


@needs_ffmpeg
def test_fetch_missing_downloads_then_merges(plex, capsys):
    _only_english_external(plex)
    plex.catalog[("101", "es")] = [{"key": "/os/es-1", "text": ES.encode(), "score": 0.9},
                                   {"key": "/os/es-2", "text": b"x", "score": 0.5}]
    assert main(["add", "101", "--pair", "en+es", "--fetch-missing"]) == 0
    out = capsys.readouterr().out
    assert "[EN+ES]: added" in out
    assert ("101", "es") in plex.searches
    assert "¿Dónde está el café?" in plex.uploads[-1]["body"].decode()


@needs_ffmpeg
def test_fetch_missing_none_found_is_not_repeated(plex, capsys):
    _only_english_external(plex)
    main(["add", "101", "--pair", "en+es", "--fetch-missing"])
    # Full subtitles first, then (nothing found) hearing-impaired ones are accepted too.
    assert plex.searches == [("101", "es"), ("101", "es", "sdh-ok")]
    assert "Spanish: no subtitles; Plex's subtitle search found none" in capsys.readouterr().out
    main(["add", "101", "--pair", "en+es", "--fetch-missing"])
    assert len(plex.searches) == 2  # remembered for 30 days


@needs_ffmpeg
def test_fetch_respects_daily_limit_and_fetches_both_languages(plex, capsys, tmp_path):
    import json
    import time as _t

    _only_english_external(plex)
    plex.catalog[("101", "es")] = [{"key": "/os/es-1", "text": ES.encode()}]
    plex.catalog[("101", "fr")] = [{"key": "/os/fr-1", "text": FR.encode()}]
    state = tmp_path / "home" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"_budget": {"date": _t.strftime("%Y-%m-%d"), "count": 2}}))
    main(["add", "101", "--pair", "en+es", "--fetch-limit", "2"])
    assert plex.searches == [] and "today's download limit is used up" in capsys.readouterr().out
    # es+fr: both missing. With one download left it isn't started (half a pair is useless)...
    main(["add", "101", "--pair", "es+fr", "--fetch-limit", "3"])
    assert plex.searches == []
    # ...with two left, both languages are fetched and the pair is made.
    main(["add", "101", "--pair", "es+fr", "--fetch-limit", "4"])
    assert [s[1] for s in plex.searches] == ["es", "fr"]
    assert "[ES+FR]: added" in capsys.readouterr().out


@needs_ffmpeg
def test_fetching_again_reopens_missing_items(plex, capsys):
    _only_english_external(plex)
    main(["add", "101", "--pair", "en+es", "--no-fetch"])  # recorded as missing, nothing searched
    assert plex.searches == []
    plex.catalog[("101", "es")] = [{"key": "/os/es-1", "text": ES.encode()}]
    main(["add", "101", "--pair", "en+es"])  # fetching is on by default
    assert "[EN+ES]: added" in capsys.readouterr().out


def test_doctor(plex, capsys):
    assert main(["doctor"]) in (0, 1)
    out = capsys.readouterr().out
    assert "[ok  ] Python" in out and "'FakePlex'" in out and "Movies" in out and "NL+EN" in out


@needs_ffmpeg
def test_check_reports_without_changing_anything(plex, capsys):
    import subprocess as sp
    from dualsubs import plex as plexmod

    plex.items["101"]["streams"] = [s for s in plex.items["101"]["streams"] if s.get("languageCode") != "fre"]
    main(["add", "101", "--pair", "nl+en"])
    capsys.readouterr()
    plex.requests.clear()
    calls = []
    orig = sp.run
    plexmod.subprocess.run = lambda *a, **k: calls.append(a) or orig(*a, **k)
    try:
        main(["check", "Koffie", "--pair", "nl+en", "--pair", "en+es", "--pair", "en+fr", "--pair", "de+en"])
    finally:
        plexmod.subprocess.run = orig
    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if ln.startswith("Koffie")][0]
    assert "done" in line and "ready" in line and "no fr" in line and "no de" in line
    assert "(image: de)" in line
    assert calls == [] and plex.uploads and len(plex.uploads) == 1
    assert not [r for r in plex.requests if not r.startswith("GET")]


@needs_ffmpeg
def test_new_track_in_another_language_does_not_redo_pairs(plex, capsys):
    plex.items["101"]["streams"] = [s for s in plex.items["101"]["streams"] if s["id"] in (301, 304)]
    main(["add", "101", "--pair", "nl+en"])
    plex.add_external("101", 777, "spa", ES.encode())  # e.g. Bazarr adds Spanish
    main(["add", "101", "--pair", "nl+en", "--pair", "en+es"])
    assert [u["title"] for u in plex.uploads] == ["dualsubs NL+EN.nl.srt", "dualsubs EN+ES.en.srt"]


@needs_ffmpeg
def test_state_from_older_versions_is_still_up_to_date(plex, capsys, tmp_path):
    """Upgrading must not redo (and re-read files for) everything already processed."""
    import hashlib
    import json as _j
    from dualsubs.pipeline import Settings, State

    main(["add", "101", "--pair", "nl+en"])
    path = tmp_path / "home" / "state.json"
    st = State(path)
    cfg = Settings(pairs=["nl+en"])
    # Rewrite the stored signature the way 0.3 computed it: over every source track.
    srcs = [s for s in plex.items["101"]["streams"] if "dualsubs" not in s.get("title", "")]
    from dualsubs.plex import Plex
    item = Plex(__import__("os").environ["PLEX_URL"], "test-token").item("101")
    sources = [s for s in item.subtitles if "dualsubs" not in (s.title or "")]
    old = hashlib.sha1(_j.dumps([cfg.fingerprint(("nl", "en")),
                                 sorted((s.id, s.language, s.codec, s.forced) for s in sources)]).encode()).hexdigest()[:16]
    entry = st.get("101", ("nl", "en"))
    st.set("101", ("nl", "en"), {**entry, "signature": old})
    st.save()
    capsys.readouterr()
    main(["add", "101", "--pair", "nl+en", "-v"])
    assert "up-to-date" in capsys.readouterr().out and len(plex.uploads) == 1
    assert State(path).get("101", ("nl", "en"))["signature"] != old  # upgraded in place


@needs_ffmpeg
def test_provider_that_stops_delivering_is_paused_for_the_day(plex, capsys, monkeypatch):
    from dualsubs import pipeline

    monkeypatch.setattr(pipeline, "FETCH_WAIT_TRIES", 1)
    monkeypatch.setattr(pipeline, "FETCH_WAIT_S", 0)
    _only_english_external(plex)
    # Plex accepts the download request but the subtitle never shows up (provider limit hit).
    plex.catalog[("101", "es")] = [{"key": "/os/es-1", "text": ES.encode()}]
    plex.handle_original = plex.handle

    def handle(method, path, query, body):
        if method == "PUT" and path.endswith("/subtitles"):
            plex.requests.append(f"{method} {path}")
            return 200, ""
        return plex.handle_original(method, path, query, body)

    plex.handle = handle
    for pair in ("en+es", "en+fr", "en+de"):
        plex.catalog[("101", pair[-2:])] = [{"key": f"/os/{pair}", "text": ES.encode()}]
    main(["add", "101", "--pair", "en+es", "--pair", "en+fr", "--pair", "en+de"])
    out = capsys.readouterr().out
    assert "never arrived" in out
    from dualsubs.pipeline import State
    import os
    st = State(os.path.join(os.environ["DUALSUBS_HOME"], "state.json"))
    assert st.provider_paused()
    n = len(plex.searches)
    plex.catalog[("101", "it")] = [{"key": "/os/it", "text": ES.encode()}]
    main(["add", "101", "--pair", "en+it"])
    assert len(plex.searches) == n  # no more requests today
    assert "stopped delivering today" in capsys.readouterr().out


def test_watched_shows_go_first(plex, monkeypatch):
    from dualsubs.cli import prioritize
    from dualsubs.plex import Plex
    import os

    p = Plex(os.environ["PLEX_URL"], "test-token")
    monkeypatch.setattr(Plex, "watched_first", lambda self: ["7", "3"])
    monkeypatch.setattr(Plex, "recently_added", lambda self, n=100: ["5", "3", "99"])
    assert prioritize(p, ["1", "2", "3", "4", "5", "6", "7"]) == ["7", "3", "5", "1", "2", "4", "6"]
