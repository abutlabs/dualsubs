"""Prepare a fresh Plex Media Server (Docker) for the end-to-end tests.

Waits for the server, creates a Movies and a TV library on /data, waits until
the scan has found every file and the subtitle streams are analysed, and saves
the raw XML Plex returns into $ARTIFACTS for inspection.
Environment: PLEX_URL (default http://127.0.0.1:32400), ARTIFACTS (default /tmp/plex-artifacts).
"""
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
ART = os.environ.get("ARTIFACTS", "/tmp/plex-artifacts")
os.makedirs(ART, exist_ok=True)
HDR = {"Accept": "application/xml", "X-Plex-Client-Identifier": "dualsubs-ci", "X-Plex-Product": "dualsubs-ci"}


def req(method, path, **params):
    url = URL + path + ("?" + urlencode(params) if params else "")
    r = urllib.request.Request(url, method=method, headers=HDR, data=b"" if method in ("POST", "PUT") else None)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        return 0, str(e).encode()


def xml(path, **params):
    code, body = req("GET", path, **params)
    if code != 200:
        raise RuntimeError(f"GET {path} -> {code}: {body[:300]!r}")
    return ET.fromstring(body)


def save(name, path):
    code, body = req("GET", path)
    with open(os.path.join(ART, name), "wb") as f:
        f.write(body)


def wait(what, fn, timeout=300, every=3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = fn()
            if v:
                print(f"ok: {what} ({time.time() - t0:.0f}s)", flush=True)
                return v
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(every)
    sys.exit(f"timed out waiting for {what}")


wait("server up", lambda: req("GET", "/identity")[0] == 200, timeout=240)
save("identity.xml", "/identity")
code, body = req("GET", "/library/sections")
print(f"GET /library/sections without a token -> {code}", flush=True)
if code != 200:
    print(body[:500])
    sys.exit("The server doesn't allow this network in without a token (check ALLOWED_NETWORKS)")

LIBS = [
    ("Movies", "movie", "/data/Movies", [("tv.plex.agents.none", "Plex Movie"),
                                         ("com.plexapp.agents.none", "Plex Movie Scanner"),
                                         ("tv.plex.agents.movie", "Plex Movie")]),
    ("TV Shows", "show", "/data/TV", [("tv.plex.agents.none", "Plex TV Series"),
                                       ("com.plexapp.agents.none", "Plex Series Scanner"),
                                       ("tv.plex.agents.series", "Plex TV Series")]),
]
def create(name, kind, loc, combos):
    for agent, scanner in combos:
        for _ in range(60):  # a fresh server answers "still starting up" for a while
            code, body = req("POST", "/library/sections", name=name, type=kind, agent=agent, scanner=scanner,
                             language="xn" if "none" in agent else "en-US", location=loc)
            if b"starting up" not in body:
                break
            time.sleep(3)
        print(f"create {name} with {agent} / {scanner} -> {code} {body[:160]!r}", flush=True)
        if code in (200, 201):
            return
    sys.exit(f"could not create library {name}")


for name, kind, loc, combos in LIBS:
    create(name, kind, loc, combos)
save("sections.xml", "/library/sections")
sections = {d.get("title"): d.get("key") for d in xml("/library/sections").findall("Directory")}
print("sections:", sections, flush=True)
for key in sections.values():
    req("GET", f"/library/sections/{key}/refresh")


def items(section, type_):
    return [v.get("ratingKey") for v in xml(f"/library/sections/{section}/all", type=type_).findall("Video")]


movies = wait("movie scanned", lambda: items(sections["Movies"], "1") or None)
episodes = wait("3 episodes scanned", lambda: (lambda e: e if len(e) >= 3 else None)(items(sections["TV Shows"], "4")))


def subs_ready(rk, expected):
    v = xml(f"/library/metadata/{rk}").find("Video")
    n = sum(1 for s in v.iter("Stream") if s.get("streamType") == "3")
    if n < expected:
        req("PUT", f"/library/metadata/{rk}/analyze")
    return n >= expected


# Movie: 1 embedded + 1 sidecar; episodes: 2, 1, 1 + 1 sidecar.
expect = dict(zip(movies, [2]))
eps = sorted(episodes, key=lambda rk: xml(f"/library/metadata/{rk}").find("Video").get("index"))
expect.update(zip(eps, [2, 1, 2]))
for rk, n in expect.items():
    wait(f"{n} subtitle stream(s) on {rk}", lambda rk=rk, n=n: subs_ready(rk, n), timeout=240)
    save(f"item-{rk}.xml", f"/library/metadata/{rk}")
print("Plex is ready", flush=True)
