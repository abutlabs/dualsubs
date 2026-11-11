"""A tiny fake Plex Media Server for end-to-end tests.

It mimics the XML shapes of the real endpoints dualsubs uses, keeps uploaded
subtitles in memory and lists them as new external streams, like Plex does.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import quoteattr

TOKEN = "test-token"


class FakePlex:
    def __init__(self):
        self.items: dict[str, dict] = {}  # ratingKey -> {title, type, file, part_id, streams: [dict]}
        self.stream_text: dict[int, bytes] = {}
        self.next_id = 9000
        self.uploads: list[dict] = []
        self.requests: list[str] = []
        self.sessions: list[str] = []
        self.catalog: dict[tuple, list[dict]] = {}  # (ratingKey, lang) -> search results
        self.searches: list[tuple] = []  # player states, e.g. ["playing", "paused"]
        self.deny_writes = False  # behave like a shared (non-owner) user

    def add_movie(self, rk, title, file, streams, year="2024"):
        self.items[rk] = {"title": title, "type": "movie", "file": file, "part_id": int(rk) + 1000,
                          "streams": streams, "year": year}

    def add_external(self, rk, sid, lang, text: bytes, codec="srt", **attrs):
        s = {"id": sid, "codec": codec, "languageCode": lang, "key": f"/library/streams/{sid}", **attrs}
        self.items[rk]["streams"].append(s)
        self.stream_text[sid] = text

    # ------------------------------------------------------------------ xml
    def _video(self, rk):
        it = self.items[rk]
        streams = "".join(
            "<Stream streamType=\"3\" " + " ".join(f"{k}={quoteattr(str(v))}" for k, v in s.items()) + "/>"
            for s in it["streams"]
        )
        return (
            f'<Video ratingKey="{rk}" type="{it["type"]}" title={quoteattr(it["title"])} year="{it["year"]}">'
            f'<Media id="{it["part_id"]}"><Part id="{it["part_id"]}" file={quoteattr(it["file"])}>'
            f'<Stream id="1" streamType="1" codec="h264" index="0"/>{streams}</Part></Media></Video>'
        )

    def handle(self, method, path, query, body):
        self.requests.append(f"{method} {path}")
        if path == "/":
            return 200, '<MediaContainer friendlyName="FakePlex" version="1.41.0" machineIdentifier="fake-machine"/>'
        if path == "/status/sessions":
            vids = "".join(f'<Video type="episode"><Player state="{s}"/></Video>' for s in self.sessions)
            return 200, f'<MediaContainer size="{len(self.sessions)}">{vids}</MediaContainer>'
        if self.deny_writes and method != "GET":
            return 403, "Forbidden"
        if path == "/library/sections":
            return 200, '<MediaContainer><Directory key="1" type="movie" title="Movies"/></MediaContainer>'
        if path == "/library/sections/1/all" or path == "/library/recentlyAdded":
            vids = "".join(f'<Video ratingKey="{rk}" type="movie" title={quoteattr(i["title"])}/>' for rk, i in self.items.items())
            return 200, f"<MediaContainer>{vids}</MediaContainer>"
        if path == "/hubs/search":
            q = query.get("query", [""])[0].lower()
            hits = "".join(
                f'<Video ratingKey="{rk}" type="movie" title={quoteattr(i["title"])} year="{i["year"]}"/>'
                for rk, i in self.items.items() if q in i["title"].lower()
            )
            return 200, f'<MediaContainer><Hub type="movie">{hits}</Hub></MediaContainer>'
        parts = path.strip("/").split("/")
        if parts[:2] == ["library", "metadata"] and len(parts) == 3 and method == "GET":
            rk = parts[2]
            return (200, f"<MediaContainer>{self._video(rk)}</MediaContainer>") if rk in self.items else (404, "")
        if parts[:2] == ["library", "metadata"] and parts[3:] == ["subtitles"] and method == "GET":
            rk, lang = parts[2], query["language"][0]
            self.searches.append((rk, lang))
            res = "".join(
                f'<Stream id="{80000 + i}" streamType="3" key="{r["key"]}" codec="srt" languageCode="{lang}" '
                f'providerTitle="OpenSubtitles" score="{r.get("score", 1)}" perfectMatch="{r.get("perfect", 0)}"/>'
                for i, r in enumerate(self.catalog.get((rk, lang), [])))
            return 200, f"<MediaContainer>{res}</MediaContainer>"
        if parts[:2] == ["library", "metadata"] and parts[3:] == ["subtitles"] and method == "PUT":
            rk, key = parts[2], query["key"][0]
            for (crk, lang), results in self.catalog.items():
                for r in results:
                    if crk == rk and r["key"] == key:
                        sid = self.next_id
                        self.next_id += 1
                        self.add_external(rk, sid, lang, r["text"], providerTitle="OpenSubtitles")
                        return 200, ""
            return 404, ""
        if parts[:2] == ["library", "metadata"] and parts[3:] == ["subtitles"] and method == "POST":
            rk = parts[2]
            title = query["title"][0]
            sid = self.next_id
            self.next_id += 1
            lang = title.rsplit(".", 2)[-2] if title.count(".") >= 2 else ""
            self.items[rk]["streams"].append({
                "id": sid, "codec": query["format"][0], "languageCode": lang, "key": f"/library/streams/{sid}",
                "title": title, "displayTitle": f"{lang} ({query['format'][0].upper()} External)",
            })
            self.stream_text[sid] = body
            self.uploads.append({"rk": rk, "title": title, "body": body})
            return 200, ""
        if parts[:2] == ["library", "streams"]:
            sid = int(parts[2])
            if method == "GET":
                return (200, self.stream_text[sid]) if sid in self.stream_text else (404, "")
            if method == "DELETE":
                for it in self.items.values():
                    it["streams"] = [s for s in it["streams"] if s["id"] != sid]
                self.stream_text.pop(sid, None)
                return 200, ""
        return 404, "not found"


def serve(fake: FakePlex):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if self.headers.get("X-Plex-Token") != TOKEN:
                self.send_response(401)
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, out = fake.handle(self.command, u.path, q, body)
            out = out.encode() if isinstance(out, str) else out
            self.send_response(status)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = do_DELETE = do_PUT = _do

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
