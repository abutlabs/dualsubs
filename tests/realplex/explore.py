"""Exploratory probes against the real Plex in CI. Prints what Plex does; asserts nothing.

1. Upload naming: which upload parameters give the nicest label in a player's menu.
2. Simulated player: can a fake client make a "playing" session (with a selected
   subtitle) that shows up in /status/sessions, the way a TV would?
"""
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

URL = os.environ.get("REAL_PLEX_URL", "http://127.0.0.1:32400")
CLIENT = {"X-Plex-Client-Identifier": "dualsubs-fake-tv", "X-Plex-Product": "Plex for Android (TV)",
          "X-Plex-Device-Name": "FAKE SHIELD", "X-Plex-Platform": "Android", "X-Plex-Device": "SHIELD Android TV",
          "X-Plex-Version": "10.0", "Accept": "application/xml"}
SRT = b"1\n00:00:01,000 --> 00:00:03,000\nHallo\n<font color=\"#FFD54F\">Hello</font>\n"


def req(method, path, data=None, headers=None, **params):
    url = URL + path + ("?" + urlencode(params) if params else "")
    r = urllib.request.Request(url, method=method, data=data if data is not None else (b"" if method in ("POST", "PUT") else None),
                               headers={**CLIENT, **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def subs(rk):
    v = ET.fromstring(req("GET", f"/library/metadata/{rk}")[1]).find("Video")
    return {s.get("id"): s.attrib for s in v.iter("Stream") if s.get("streamType") == "3"}


print("=== 1. upload naming ===")
rk = "5"  # Test Show S01E02 (English only)
variants = [
    {"title": "dualsubs NL+EN.nl.srt", "format": "srt"},
    {"title": "dualsubs NL+EN", "format": "srt", "language": "nl"},
    {"title": "NL+EN", "format": "srt", "language": "nld"},
    {"title": "Nederlands + English.nl.srt", "format": "srt"},
    {"title": "x.nl.srt", "format": "srt", "displayTitle": "NL+EN"},
]
for params in variants:
    before = set(subs(rk))
    code, body = req("POST", f"/library/metadata/{rk}/subtitles", data=SRT, headers={"Accept": "text/plain, */*"}, **params)
    time.sleep(1)
    new = {k: v for k, v in subs(rk).items() if k not in before}
    for sid, a in new.items():
        print(f"{params} -> HTTP {code}; stream {sid}: language={a.get('language')!r} languageCode={a.get('languageCode')!r} "
              f"title={a.get('title')!r} displayTitle={a.get('displayTitle')!r} extended={a.get('extendedDisplayTitle')!r}")
        req("DELETE", f"/library/streams/{sid}")
    if not new:
        print(f"{params} -> HTTP {code}; no new stream ({body[:120]!r})")

print("\n=== 2. simulated player ===")
v = ET.fromstring(req("GET", f"/library/metadata/{rk}")[1]).find("Video")
part = v.find("Media/Part")
sid = next(iter(subs(rk)))
print("part", part.get("id"), "subtitle to select", sid)
code, _ = req("PUT", f"/library/parts/{part.get('id')}", subtitleStreamID=sid, allParts="1")
print("select subtitle via PUT /library/parts ->", code)
for state in ("playing", "playing", "paused"):
    code, body = req("GET", "/:/timeline", ratingKey=rk, key=f"/library/metadata/{rk}", state=state, time="5000",
                     duration=v.get("duration", "40000"), playbackTime="5000", identifier="com.plexapp.plugins.library")
    print(f"timeline state={state} -> {code} {body[:100]!r}")
    time.sleep(1)
    code, body = req("GET", "/status/sessions")
    print("/status/sessions ->", code, body[:1500].decode(errors="replace"))
code, body = req("GET", "/video/:/transcode/universal/decision", path=f"/library/metadata/{rk}", directPlay="1",
                 directStream="1", subtitleStreamID=sid, subtitles="sidecar", protocol="http", mediaIndex="0", partIndex="0")
print("transcode decision ->", code, body[:600].decode(errors="replace"))
