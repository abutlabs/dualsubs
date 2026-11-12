"""`dualsubs lab`: timings and the placeholder experiment, against the fake Plex."""
import json
import threading
import time

from dualsubs.cli import main

from .test_plex_e2e import ES, media, needs_ffmpeg, plex  # noqa: F401  (fixtures)


@needs_ffmpeg
def test_lab_time_reports_phases(plex, capsys, tmp_path):  # noqa: F811
    plex.items["101"]["streams"] = [s for s in plex.items["101"]["streams"] if s["id"] in (301, 304)]
    plex.catalog[("101", "nl")] = [{"key": "/os/nl", "text": (b"1\n00:00:01,000 --> 00:00:03,000\nGoedemorgen\n")}]
    plex.catalog[("101", "en")] = [{"key": "/os/en", "text": ES.encode()}]
    assert main(["lab", "time", "101", "--pair", "nl+en", "--compare"]) in (0, None)
    out = capsys.readouterr().out
    assert "read video file for 1 embedded track(s) (ffmpeg)" in out  # Dutch is inside the .mkv
    assert "Without reading the video file" in out and "search nl subtitles" in out  # ...so it's downloaded instead
    report = json.loads(sorted((tmp_path / "home").glob("lab-time-*.json"))[-1].read_text())
    assert len(report["runs"]) == 2 and report["runs"][0]["total_s"] >= 0


@needs_ffmpeg
def test_lab_placeholder_flow(plex, capsys, tmp_path):  # noqa: F811
    """Placeholder is uploaded, the 'TV' picks it, the real track is built and the placeholder removed."""
    def tv():
        for _ in range(200):
            ph = [s for s in plex.items["101"]["streams"] if "tap to prepare" in s.get("title", "")]
            if ph:
                plex.sessions = [{"rk": "101", "type": "movie", "sub": ph[0]["id"]}]
                return
            time.sleep(0.05)

    threading.Thread(target=tv, daemon=True).start()
    main(["lab", "placeholder", "101", "--pair", "nl+en", "--wait", "1", "--observe", "1", "--poll", "0.1"])
    out = capsys.readouterr().out
    assert "Placeholder added" in out and "Placeholder picked" in out and "Real track ready" in out
    titles = [s.get("title", "") for s in plex.items["101"]["streams"]]
    assert "dualsubs NL+EN.nl.srt" in titles and not any("tap to prepare" in x for x in titles)
    assert "the removed placeholder" in out  # the fake 'TV' stays on the old track, as a real one might
    report = json.loads(sorted((tmp_path / "home").glob("lab-placeholder-*.json"))[-1].read_text())
    assert report["placeholder"]["plex_label"] and report["build"]["total_s"] >= 0
