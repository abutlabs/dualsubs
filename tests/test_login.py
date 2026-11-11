from dualsubs.plex import discover_servers, login_pin


class Resp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self):
        self.polls = 0

    def post(self, url, **kw):
        assert url.endswith("/api/v2/pins") and kw["params"]["strong"] == "true"
        return Resp({"id": 42, "code": "abc123"})

    def get(self, url, **kw):
        if url.endswith("/pins/42"):
            self.polls += 1
            return Resp({"authToken": "TOKEN" if self.polls >= 2 else None})
        assert url.endswith("/api/v2/resources")
        return Resp([
            {"name": "Phone", "provides": "client,player", "connections": []},
            {"name": "seedhost", "clientIdentifier": "abc", "owned": True, "provides": "server", "accessToken": "SRVTOKEN", "connections": [
                {"uri": "https://1-2-3-4.abc.plex.direct:32400", "local": False, "relay": False},
                {"uri": "http://10.0.0.5:32400", "local": True, "relay": False},
            ]},
        ])


def test_login_and_discover():
    s = FakeSession()
    shown = []
    assert login_pin(session=s, poll=0, out=shown.append) == "TOKEN"
    assert "app.plex.tv/auth#?" in shown[0] and "code=abc123" in shown[0]
    servers = discover_servers("TOKEN", session=s)
    assert servers == [{"name": "seedhost", "id": "abc", "owned": True, "token": "SRVTOKEN",
                        "urls": ["http://10.0.0.5:32400", "https://1-2-3-4.abc.plex.direct:32400"]}]


def test_login_command_prefers_localhost_and_checks_identity(monkeypatch, tmp_path):
    """`dualsubs login` on the Plex machine: localhost wins, but only if it is *our* server."""
    import json
    from dualsubs import cli, plex as plexmod
    from .mock_plex import FakePlex, serve

    srv, url = serve(FakePlex())
    port = int(url.rsplit(":", 1)[1])
    monkeypatch.setenv("DUALSUBS_HOME", str(tmp_path))
    monkeypatch.setattr(plexmod, "login_pin", lambda: "acct")
    tried = []
    real_identity = plexmod.Plex.identity

    def identity(self):
        tried.append(self.url)
        return real_identity(self)

    monkeypatch.setattr(plexmod.Plex, "identity", identity)
    for server_id, expect_ok in (("fake-machine", True), ("someone-else", False)):
        tried.clear()
        monkeypatch.setattr(plexmod, "discover_servers", lambda tok, sid=server_id: [
            {"name": "box", "id": sid, "token": "test-token", "urls": [f"https://1-2-3-4.x.plex.direct:{port}"]}])
        if expect_ok:
            cli.main(["login"])
            assert json.loads((tmp_path / "config.json").read_text())["url"] == f"http://127.0.0.1:{port}"
            assert tried[0] == f"http://127.0.0.1:{port}"
        else:
            import pytest
            with pytest.raises(SystemExit):
                cli.main(["login"])
    srv.shutdown()
