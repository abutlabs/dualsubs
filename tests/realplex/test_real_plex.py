"""End-to-end tests against a REAL Plex Media Server (see .github/workflows/ci.yml).

Skipped unless REAL_PLEX_URL is set. The server lets the runner in without a
token (allowedNetworks), so dualsubs uses `--token none`. There is no Plex
account, so subtitle downloads (`fetch`) are not exercised here: runs use
--no-fetch, and fetch-time behaviour stays covered by the fake-Plex tests.
"""
import json
import os
import subprocess
import sys

import pytest

URL = os.environ.get("REAL_PLEX_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs a real Plex server (REAL_PLEX_URL)")
ART = os.environ.get("ARTIFACTS", "/tmp/plex-artifacts")


def ds(*args, check=True):
    env = dict(os.environ, PLEX_URL=URL, PLEX_TOKEN="none", DUALSUBS_PATH_MAP=os.environ.get("REAL_PLEX_PATH_MAP", ""),
               DUALSUBS_HOME=os.environ.get("DUALSUBS_HOME", "/tmp/ds-home"), DUALSUBS_FETCH="0",
               DUALSUBS_YIELD_TO_STREAMS="0")
    r = subprocess.run([sys.executable, "-m", "dualsubs", *args], env=env, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    print(f"$ dualsubs {' '.join(args)}\n{out}")
    with open(os.path.join(ART, "dualsubs-output.txt"), "a") as f:
        f.write(f"$ dualsubs {' '.join(args)}\n{out}\n")
    if check:
        assert r.returncode == 0, out
    return out


PAIRS = ["--pair", "nl+en", "--pair", "en+es"]


def test_1_doctor_and_libraries():
    out = ds("doctor", check=False)
    assert "[ok  ] Plex" in out and "Movies" in out and "TV Shows" in out


def test_2_check_before():
    out = ds("check", "library:Movies", *PAIRS)
    line = [ln for ln in out.splitlines() if ln.startswith("Koffie")][0]
    assert "ready" in line and "no es" in line  # nl embedded + en next to it; no Spanish
    out = ds("check", "Test Show", *PAIRS)
    assert "NL+EN: " in out and "EN+ES: " in out


def test_3_add_uploads_real_tracks():
    out = ds("add", "library:Movies", *PAIRS)
    assert "[NL+EN]: added" in out and "embedded" in out
    out = ds("add", "Test Show", *PAIRS)
    assert out.count("]: added") == 2  # E01 EN+ES, E03 NL+EN; E02 has English only
    # What does real Plex call an uploaded track? Recorded for the README / UI.
    ins = ds("inspect", "Test Show", *PAIRS) + ds("inspect", "library:Movies", *PAIRS)
    ours = [ln for ln in ins.splitlines() if "dualsubs" in ln.lower()]
    with open(os.path.join(ART, "uploaded-track-labels.txt"), "w") as f:
        f.write("\n".join(ours))
    assert len(ours) == 3, ins


def test_4_rerun_changes_nothing():
    out = ds("add", "library:Movies", *PAIRS, "-v")
    assert "added" not in out.split("Summary")[0]
    out = ds("add", "Test Show", *PAIRS)
    assert "Summary:" in out and "added" not in out.split("Summary")[1]


def test_5_merged_track_content():
    st = json.load(open(os.path.join(os.environ.get("DUALSUBS_HOME", "/tmp/ds-home"), "state.json")))
    with_tracks = [k for k, v in st.items() if not k.startswith("_") and any(
        isinstance(x, dict) and x.get("stream_ids") for x in v.values())]
    assert len(with_tracks) == 3
    # Download one uploaded track back from Plex and check it's bilingual.
    import urllib.request
    rk = with_tracks[0]
    sid = next(x["stream_ids"][0] for x in st[rk].values() if isinstance(x, dict) and x.get("stream_ids"))
    body = urllib.request.urlopen(f"{URL}/library/streams/{sid}", timeout=30).read().decode()
    with open(os.path.join(ART, f"uploaded-track-{rk}.srt"), "w") as f:
        f.write(body)
    assert "Good morning" in body or "Buenos" in body
    assert "<font" in body


def test_6_web_status_counts():
    import threading
    import time as _t
    import urllib.request
    port = 18765
    env = dict(os.environ, PLEX_URL=URL, PLEX_TOKEN="none", DUALSUBS_HOME=os.environ.get("DUALSUBS_HOME", "/tmp/ds-home"))
    p = subprocess.Popen([sys.executable, "-m", "dualsubs", "web", "--port", str(port)], env=env,
                         stdout=subprocess.PIPE, text=True)
    try:
        line = ""
        while "key=" not in line:
            line = p.stdout.readline()
        key = line.strip().split("key=")[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status", headers={"Cookie": f"dualsubs_key={key}"})
        s = json.loads(urllib.request.urlopen(req, timeout=60).read())
        with open(os.path.join(ART, "web-status.json"), "w") as f:
            json.dump(s, f, indent=1)
        libs = {lb["title"]: lb for lb in s["libraries"]}
        assert libs["Movies"]["total"] == 1 and libs["TV Shows"]["total"] == 3
    finally:
        p.terminate()


def test_7_remove_all():
    ds("remove", "all")
    ins = ds("inspect", "Test Show") + ds("inspect", "library:Movies")
    assert "dualsubs" not in ins.lower()
