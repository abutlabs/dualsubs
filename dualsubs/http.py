"""A tiny HTTP client on the standard library, so dualsubs needs nothing but Python.

It mirrors the small part of the `requests` API that dualsubs uses.
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.request
from urllib.parse import urlencode


class Response:
    def __init__(self, status_code: int, content: bytes, url: str):
        self.status_code = status_code
        self.content = content
        self.url = url

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return _json.loads(self.content or b"null")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code} from {self.url}: {self.text[:200]}")


class Session:
    def __init__(self):
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, params: dict | None = None, data: bytes | None = None,
                headers: dict | None = None, timeout: float = 30) -> Response:
        if params:
            q = urlencode({k: v for k, v in params.items() if v is not None})
            url += ("&" if "?" in url else "?") + q
        if isinstance(data, str):
            data = data.encode()
        if data is None and method in ("POST", "PUT"):
            data = b""
        req = urllib.request.Request(url, data=data, method=method, headers={**self.headers, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Response(r.status, r.read(), url)
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read() or b"", url)
        # URLError / timeouts are OSError subclasses and propagate as such.

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Response:
        return self.request("POST", url, **kw)
