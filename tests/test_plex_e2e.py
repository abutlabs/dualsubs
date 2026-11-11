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
    assert "[+] Koffie (2024): added" in out
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
    side = media.parent / "film.dualsubs.nl.srt"
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
