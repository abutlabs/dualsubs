"""dualsubs web: access key, status data and managing pairs."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from dualsubs import cli, web
from dualsubs.cli import main

from .test_plex_e2e import needs_ffmpeg, plex, media, ES  # noqa: F401  (fixtures)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _get(url, cookie=None):
    req = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(req, timeout=5)
        return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _post(url, body, cookie):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Cookie": cookie, "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def portal(plex, tmp_path):  # noqa: F811
    cfg = cli.load_config()
    key = web.ensure_key(cfg, cli.save_config)

    def factory():
        p, s, _ = cli.build(type("A", (), {})())
        return p, s

    srv = web.serve(web.Portal(cli.config_dir(), cli.load_config, cli.save_config, factory), key, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", key
    srv.shutdown()


def test_key_required_and_exchanged_for_cookie(portal):
    base, key = portal
    assert _get(base + "/")[0] == 403
    assert _get(base + "/api/status")[0] == 403
    assert _get(base + "/?key=wrong")[0] == 403
    code, headers, _ = _get(base + f"/?key={key}")
    assert code == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    code, _, body = _get(base + "/", cookie)
    assert code == 200 and b"dualsubs" in body
    assert _post(base + "/api/pairs", {"pairs": ["en+es"]}, "dualsubs_key=nope")[0] == 403


@needs_ffmpeg
def test_status_shows_coverage_and_progress(plex, portal):  # noqa: F811
    base, key = portal
    cookie = f"dualsubs_key={key}"
    plex.items["101"]["streams"] = [s for s in plex.items["101"]["streams"] if s.get("languageCode") != "fre"]
    main(["add", "101", "--pair", "nl+en", "--pair", "en+es", "--pair", "en+fr"])
    code, _, body = _get(base + "/api/status", cookie)
    s = json.loads(body)
    assert code == 200 and s["server"]["ok"] and s["server"]["name"] == "FakePlex"
    lib = s["libraries"][0]
    assert lib["title"] == "Movies" and lib["total"] == 1 and lib["checked"] == 1
    # The page shows the configured pairs (default nl+en), counted from the state file.
    assert lib["pairs"]["nl+en"]["done"] == 1
    assert s["progress"]["state"] == "finished" and s["progress"]["done"] == 1 and s["progress"]["total"] == 1
    assert any("added" in r["line"] for r in s["progress"]["recent"])

    # Manage pairs from the page: now all three are counted.
    code, out = _post(base + "/api/pairs", {"pairs": ["nl+en", "en+es", "en+fr"]}, cookie)
    assert code == 200 and out["pairs"] == ["nl+en", "en+es", "en+fr"]
    s = json.loads(_get(base + "/api/status", cookie)[2])
    p = s["libraries"][0]["pairs"]
    assert p["en+es"]["done"] == 1 and p["en+fr"]["missing"] == 1
    assert s["libraries"][0]["missing_langs"] == {"fr": 1}
    assert _post(base + "/api/pairs", {"pairs": ["en+en"]}, cookie)[0] == 400
    assert _post(base + "/api/pairs", {"pairs": []}, cookie)[0] == 400
    assert _post(base + "/api/fetch", {"enabled": True}, cookie)[0] == 200
    assert cli.load_config()["fetch_missing"] is True


def test_progress_reports_dead_runs_as_stopped(tmp_path):
    from dualsubs import progress
    path = tmp_path / "progress.json"
    p = progress.Progress(path, "add", "library:Movies")
    p.start(10)
    assert progress.read(path)["state"] == "running"
    data = json.loads(path.read_text())
    data["pid"] = 999999999
    path.write_text(json.dumps(data))
    assert progress.read(path)["state"] == "stopped"
