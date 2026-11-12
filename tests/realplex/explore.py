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


def session_subs():
    code, body = req("GET", "/status/sessions")
    root = ET.fromstring(body)
    out = []
    for v in root:
        player = v.find("Player")
        sel = [(s.get("id"), s.get("selected"), s.get("title"), s.get("languageCode"))
               for s in v.iter("Stream") if s.get("streamType") == "3"]
        out.append({"rk": v.get("ratingKey"), "player": player.attrib if player is not None else None,
                    "subtitle streams in session": sel,
                    "Session": (v.find("Session").attrib if v.find("Session") is not None else None)})
    return out


v = ET.fromstring(req("GET", f"/library/metadata/{rk}")[1]).find("Video")
part = v.find("Media/Part")
dur = v.get("duration", "40000")
# Upload a "placeholder" and select it, as a viewer would on the TV.
before = set(subs(rk))
req("POST", f"/library/metadata/{rk}/subtitles", data=SRT, headers={"Accept": "text/plain, */*"},
    title="dualsubs NL+EN (tap to prepare).nl.srt", format="srt")
ph = next(k for k in subs(rk) if k not in before)
print("placeholder stream", ph)
for sel in (ph, next(k for k in subs(rk) if k != ph)):
    code, _ = req("PUT", f"/library/parts/{part.get('id')}", subtitleStreamID=sel, allParts="1")
    print(f"select subtitle {sel} via PUT /library/parts -> {code}")
    for state in ("playing", "playing"):
        code, body = req("GET", "/:/timeline", ratingKey=rk, key=f"/library/metadata/{rk}", state=state, time="5000",
                         duration=dur, playbackTime="5000", identifier="com.plexapp.plugins.library")
        time.sleep(1)
    print("  sessions:", session_subs())
req("GET", "/:/timeline", ratingKey=rk, key=f"/library/metadata/{rk}", state="stopped", time="6000", duration=dur,
    identifier="com.plexapp.plugins.library")
time.sleep(1)
print("after stop, sessions:", session_subs())
req("DELETE", f"/library/streams/{ph}")
