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
            {"name": "seedhost", "provides": "server", "accessToken": "SRVTOKEN", "connections": [
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
    assert servers == [{"name": "seedhost", "token": "SRVTOKEN",
                        "urls": ["http://10.0.0.5:32400", "https://1-2-3-4.abc.plex.direct:32400"]}]
