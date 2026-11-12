"""Minimal Plex Media Server client: just what dualsubs needs.

Endpoints used (all against the local server, authenticated with X-Plex-Token):
  GET    /library/sections                         libraries
  GET    /library/sections/{id}/all                items in a library
  GET    /library/metadata/{key}                   item with Media/Part/Stream
  GET    /library/metadata/{key}/allLeaves         every episode of a show/season
  GET    /library/recentlyAdded                    newest items (watch mode)
  GET    /library/streams/{id}                     an external subtitle's text
  POST   /library/metadata/{key}/subtitles         upload a subtitle (stored in Plex metadata)
  DELETE /library/streams/{id}                     remove an uploaded subtitle
Plus the plex.tv PIN flow for `dualsubs login`.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import quote


from . import __version__
from .http import Response, Session
from .languages import normalize

TEXT_CODECS = {"srt", "subrip", "ass", "ssa", "mov_text", "webvtt", "vtt", "text", "smi"}
IMAGE_CODECS = {"pgs", "hdmv_pgs_subtitle", "vobsub", "dvd_subtitle", "dvb_subtitle", "dvbsub"}


class PlexError(RuntimeError):
    pass


@dataclass
class SubStream:
    id: int
    codec: str
    language: str | None  # ISO 639-1 where known
    raw_language: str
    title: str
    display: str
    forced: bool
    hearing_impaired: bool
    key: str | None  # set for external/uploaded subtitles
    index: int | None  # container stream index for embedded subtitles

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_CODECS

    @property
    def external(self) -> bool:
        return bool(self.key)


@dataclass
class Part:
    id: int
    file: str
    subtitles: list[SubStream] = field(default_factory=list)


@dataclass
class Item:
    rating_key: str
    type: str  # movie | episode
    title: str
    parts: list[Part] = field(default_factory=list)
    library_id: str = ""
    library: str = ""
    show: str = ""  # series title for episodes

    @property
    def subtitles(self) -> list[SubStream]:
        # Subtitles live on the first part for multi-part items; uploaded ones too.
        return self.parts[0].subtitles if self.parts else []


def _b(v: str | None) -> bool:
    return v in ("1", "true", "True")


class Plex:
    def __init__(self, url: str, token: str, session: Session | None = None, timeout: int = 30):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.s = session or Session()
        self.s.headers.update({
            "X-Plex-Token": token,
            "X-Plex-Product": "dualsubs",
            "X-Plex-Version": __version__,
            "X-Plex-Client-Identifier": "dualsubs",
            "Accept": "application/xml",
        })

    # ------------------------------------------------------------------ http
    def _req(self, method: str, path: str, **kw) -> Response:
        try:
            r = self.s.request(method, self.url + path, timeout=self.timeout, **kw)
        except OSError as e:  # connection refused, DNS, timeout
            raise PlexError(f"can't reach Plex at {self.url}: {e}") from e
        if r.status_code == 401:
            raise PlexError("Plex rejected the token (401). Run `dualsubs login` or check PLEX_TOKEN.")
        if r.status_code == 403 and method != "GET":
            raise PlexError(
                f"Plex refused {method} {path} (403). Adding subtitles needs the server OWNER's account; "
                "a shared or managed user can watch but not change items. Run `dualsubs login` and sign in as the owner.")
        if r.status_code >= 400:
            raise PlexError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r

    def _xml(self, path: str, **params) -> ET.Element:
        return ET.fromstring(self._req("GET", path, params=params or None).content)

    # ------------------------------------------------------------------ browse
    def identity(self) -> dict:
        root = self._xml("/")
        return {"name": root.get("friendlyName"), "version": root.get("version"), "id": root.get("machineIdentifier")}

    def playing_sessions(self) -> int:
        """How many people are actively watching or listening right now.

        Paused sessions don't count: someone leaving a film paused overnight
        shouldn't stall dualsubs.
        """
        root = self._xml("/status/sessions")
        n = 0
        for el in root:
            player = el.find("Player")
            state = player.get("state") if player is not None else "playing"
            if state in ("playing", "buffering"):
                n += 1
        return n

    def watched_first(self) -> list[str]:
        """Items to give the daily download budget to first: everything in shows on
        Continue Watching or watched in the last weeks, and those movies."""
        keys: list[str] = []
        shows: list[str] = []
        sources = [("/library/onDeck", {}),
                   ("/status/sessions/history/all", {"sort": "viewedAt:desc",
                                                     "viewedAt>": str(int(time.time()) - 30 * 86400),
                                                     "X-Plex-Container-Size": "300"})]
        for path, params in sources:
            try:
                root = self._xml(path, **params)
            except PlexError:
                continue
            for v in root:
                if v.get("type") == "episode" and v.get("grandparentRatingKey"):
                    if v.get("grandparentRatingKey") not in shows:
                        shows.append(v.get("grandparentRatingKey"))
                elif v.get("type") == "movie" and v.get("ratingKey"):
                    keys.append(v.get("ratingKey"))
        for show in shows:
            try:
                keys.extend(self.leaves(show))
            except PlexError:
                continue
        return list(dict.fromkeys(keys))

    def libraries(self) -> list[dict]:
        return [
            {"id": d.get("key"), "title": d.get("title"), "type": d.get("type")}
            for d in self._xml("/library/sections").findall("Directory")
            if d.get("type") in ("movie", "show")
        ]

    def library_items(self, section_id: str) -> list[str]:
        """Rating keys of every movie, or every episode, in a library."""
        root = self._xml(f"/library/sections/{section_id}/all")
        keys = []
        for el in root:
            if el.get("type") == "movie":
                keys.append(el.get("ratingKey"))
            elif el.get("type") in ("show", "season"):
                keys.extend(self.leaves(el.get("ratingKey")))
        return keys

    def library_size(self, section_id: str, kind: str) -> int:
        """Number of movies (kind 'movie') or episodes (kind 'show') in a library, cheaply."""
        root = self._xml(f"/library/sections/{section_id}/all", type="1" if kind == "movie" else "4",
                         **{"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "0"})
        return int(root.get("totalSize") or root.get("size") or 0)

    def leaves(self, rating_key: str) -> list[str]:
        return [v.get("ratingKey") for v in self._xml(f"/library/metadata/{rating_key}/allLeaves").findall("Video")]

    def expand(self, rating_key: str) -> list[str]:
        """Movie/episode -> itself; show/season -> all episodes."""
        root = self._xml(f"/library/metadata/{rating_key}")
        el = root[0] if len(root) else None
        if el is None:
            raise PlexError(f"No item with ratingKey {rating_key}")
        if el.get("type") in ("show", "season"):
            return self.leaves(rating_key)
        return [rating_key]

    def search(self, query: str) -> list[dict]:
        root = self._xml("/hubs/search", query=query, limit="10")
        out = []
        for el in root.iter():
            if el.get("ratingKey") and el.get("type") in ("movie", "show", "season", "episode"):
                title = el.get("title")
                if el.get("type") == "episode":
                    title = f"{el.get('grandparentTitle')} S{el.get('parentIndex', '?')}E{el.get('index', '?')} {title}"
                out.append({"ratingKey": el.get("ratingKey"), "type": el.get("type"), "title": title, "year": el.get("year")})
        return out

    def recently_added(self, size: int = 100) -> list[str]:
        root = self._xml("/library/recentlyAdded", **{"X-Plex-Container-Start": "0", "X-Plex-Container-Size": str(size)})
        keys: list[str] = []
        for el in root:
            if el.get("type") in ("movie", "episode"):
                keys.append(el.get("ratingKey"))
            elif el.get("type") in ("season", "show"):
                keys.extend(self.leaves(el.get("ratingKey")))
        return keys

    def item(self, rating_key: str) -> Item:
        root = self._xml(f"/library/metadata/{rating_key}")
        v = root.find("Video")
        if v is None:
            raise PlexError(f"{rating_key} is not a movie or episode")
        title = v.get("title", "")
        if v.get("type") == "episode":
            title = f"{v.get('grandparentTitle')} S{int(v.get('parentIndex', 0)):02d}E{int(v.get('index', 0)):02d} {title}"
        elif v.get("year"):
            title = f"{title} ({v.get('year')})"
        item = Item(rating_key, v.get("type", ""), title,
                    library_id=v.get("librarySectionID") or root.get("librarySectionID") or "",
                    library=v.get("librarySectionTitle") or root.get("librarySectionTitle") or "",
                    show=v.get("grandparentTitle", ""))
        for p in v.iter("Part"):
            part = Part(int(p.get("id")), p.get("file", ""))
            for s in p.findall("Stream"):
                if s.get("streamType") != "3":
                    continue
                raw = s.get("languageCode") or s.get("languageTag") or s.get("language") or ""
                part.subtitles.append(SubStream(
                    id=int(s.get("id")),
                    codec=(s.get("codec") or s.get("format") or "").lower(),
                    language=normalize(raw) or normalize(s.get("language")),
                    raw_language=raw,
                    title=s.get("title", ""),
                    display=s.get("extendedDisplayTitle") or s.get("displayTitle") or "",
                    forced=_b(s.get("forced")),
                    hearing_impaired=_b(s.get("hearingImpaired")),
                    key=s.get("key"),
                    index=int(s.get("index")) if s.get("index") not in (None, "-1") else None,
                ))
            item.parts.append(part)
        return item

    # ------------------------------------------------------------------ subtitle IO
    def download_subtitle(self, stream: SubStream, part: Part, path_map: list[tuple[str, str]] | None = None) -> tuple[bytes, str]:
        """Return (bytes, format) for a text subtitle stream, external or embedded."""
        if stream.key:
            r = self._req("GET", stream.key)
            return r.content, stream.codec
        if stream.index is None:
            raise PlexError(f"Subtitle stream {stream.id} has neither a key nor a container index")
        return extract_embedded(map_path(part.file, path_map), stream.index, stream.codec)

    def search_subtitles(self, rating_key: str, lang: str, allow_sdh: bool = False) -> list[dict]:
        """Plex's own on-demand subtitle search (OpenSubtitles), best match first.

        This is what "Search subtitles" in a Plex app does. Only text formats are
        returned, preferring exact release matches and non-SDH.
        """
        # hearingImpaired 3 = only non-SDH, 0 = prefer non-SDH; forced 3 = only full subtitles
        root = self._xml(f"/library/metadata/{rating_key}/subtitles",
                         language=lang, hearingImpaired="0" if allow_sdh else "3", forced="3")
        found = []
        for s in root.iter("Stream"):
            codec = (s.get("codec") or s.get("format") or "").lower()
            if not s.get("key") or codec not in TEXT_CODECS:
                continue
            found.append({
                "key": s.get("key"), "codec": codec, "provider": s.get("providerTitle", ""),
                "perfect": _b(s.get("perfectMatch")), "score": float(s.get("score") or 0),
                "sdh": _b(s.get("hearingImpaired")), "title": s.get("title") or s.get("displayTitle") or "",
            })
        found.sort(key=lambda f: (f["sdh"], not f["perfect"], -f["score"]))
        return found

    def download_found_subtitle(self, rating_key: str, found: dict) -> None:
        """Have Plex download a search result. Plex fetches it in the background."""
        self._req("PUT", f"/library/metadata/{rating_key}/subtitles", params={"key": found["key"]})

    def upload_subtitle(self, rating_key: str, data: bytes, filename: str) -> None:
        fmt = filename.rsplit(".", 1)[-1]
        self._req(
            "POST", f"/library/metadata/{rating_key}/subtitles",
            params={"title": filename, "format": fmt},
            data=data, headers={"Accept": "text/plain, */*"},
        )

    def delete_stream(self, stream: SubStream) -> None:
        if stream.key:
            self._req("DELETE", stream.key)


def map_path(path: str, path_map: list[tuple[str, str]] | None) -> str:
    """Translate a path as Plex sees it to a path this process can open."""
    for src, dst in path_map or []:
        if path.startswith(src):
            return dst + path[len(src):]
    return path


FFMPEG_CANDIDATES = [
    "ffmpeg",
    # Plex ships its own ffmpeg fork; usable when the system has no ffmpeg.
    "/usr/lib/plexmediaserver/Plex Transcoder",
    "/Applications/Plex Media Server.app/Contents/MacOS/Plex Transcoder",
]
FFMPEG: str | None = None  # set from --ffmpeg / DUALSUBS_FFMPEG
PRIORITY = "low"  # low | idle | normal: CPU and disk priority for ffmpeg


def priority_prefix(level: str | None = None) -> list[str]:
    """Command prefix that runs ffmpeg behind Plex streaming and seeding.

    low:  nice 19 + best-effort lowest disk priority (still makes progress on a busy box)
    idle: nice 19 + idle disk class (only reads when nothing else wants the disk)
    """
    import shutil
    level = level or PRIORITY
    if level == "normal":
        return []
    prefix = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    if shutil.which("ionice"):  # Linux only
        prefix += ["ionice", "-c", "3"] if level == "idle" else ["ionice", "-c", "2", "-n", "7"]
    return prefix


def find_ffmpeg() -> str:
    import shutil
    for cand in ([FFMPEG] if FFMPEG else []) + FFMPEG_CANDIDATES:
        path = shutil.which(cand) or (cand if os.path.isfile(cand) and os.access(cand, os.X_OK) else None)
        if path:
            return path
    raise PlexError("ffmpeg is needed to read embedded subtitles but was not found (set --ffmpeg or DUALSUBS_FFMPEG)")


def extract_embedded(file: str, index: int, codec: str) -> tuple[bytes, str]:
    return extract_embedded_many(file, [(index, codec)])[index]


def extract_embedded_many(file: str, streams: list[tuple[int, str]]) -> dict[int, tuple[bytes, str]]:
    """Extract several embedded subtitle streams in ONE pass over the video file.

    ffmpeg has to read through the whole container to collect a subtitle
    track, so pulling every track we need in a single run matters on big files.
    Returns {container index: (bytes, format)}.
    """
    import tempfile

    if not os.path.exists(file):
        raise PlexError(f"can't open {file} (is the media folder mounted here? see --path-map)")
    exe = find_ffmpeg()
    env = dict(os.environ)
    if "Plex Transcoder" in exe:
        # Plex's transcoder finds its codec libraries relative to its own folder.
        env["LD_LIBRARY_PATH"] = os.path.dirname(exe) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    with tempfile.TemporaryDirectory(prefix="dualsubs-") as tmp:
        cmd = priority_prefix() + [exe, "-nostdin", "-v", "error", "-y", "-i", file]
        outs = {}
        for index, codec in dict(streams).items():
            fmt = "ass" if codec in ("ass", "ssa") else "srt"
            path = os.path.join(tmp, f"{index}.{fmt}")
            cmd += ["-map", f"0:{index}", "-f", fmt, path]
            outs[index] = (path, fmt)
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=3600, env=env)
        except OSError as e:
            raise PlexError(f"could not run {exe}: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise PlexError(f"ffmpeg took over an hour reading {file}") from e
        result = {}
        for index, (path, fmt) in outs.items():
            data = open(path, "rb").read() if os.path.exists(path) else b""
            if data:
                result[index] = (data, fmt)
        if not result:
            msg = res.stderr.decode(errors="replace").strip()[-300:]
            raise PlexError(f"ffmpeg could not extract subtitle streams {sorted(outs)} from {file}: {msg}")
        missing = set(outs) - set(result)
        for index in missing:
            result[index] = (b"", outs[index][1])  # empty track; caller falls back to another candidate
        return result


# ---------------------------------------------------------------------- plex.tv sign-in

PLEX_TV = "https://plex.tv"


def _client_id() -> str:
    return "dualsubs-" + uuid.uuid5(uuid.NAMESPACE_DNS, uuid.getnode().__str__()).hex[:12]


def login_pin(session: Session | None = None, poll: float = 2.0, timeout: float = 300, out=None) -> str:
    """Sign in through app.plex.tv in a browser and return an auth token."""
    s = session or Session()
    out = out or (lambda msg: print(msg, flush=True))  # flush: often run over ssh
    cid = _client_id()
    headers = {"Accept": "application/json", "X-Plex-Product": "dualsubs", "X-Plex-Client-Identifier": cid}
    pin = s.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"}, headers=headers, timeout=30).json()
    url = (
        "https://app.plex.tv/auth#?" f"clientID={quote(cid)}&code={quote(pin['code'])}"
        "&context%5Bdevice%5D%5Bproduct%5D=dualsubs"
    )
    out(f"Open this link, sign in to Plex and approve dualsubs:\n\n  {url}\n")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = s.get(f"{PLEX_TV}/api/v2/pins/{pin['id']}", headers=headers, timeout=30).json()
        if r.get("authToken"):
            return r["authToken"]
        time.sleep(poll)
    raise PlexError("Timed out waiting for Plex sign-in")


def discover_servers(token: str, session: Session | None = None) -> list[dict]:
    """Servers on the account, with their connection URLs (local ones first)."""
    s = session or Session()
    r = s.get(
        f"{PLEX_TV}/api/v2/resources",
        params={"includeHttps": "1", "includeRelay": "0"},
        headers={"Accept": "application/json", "X-Plex-Token": token, "X-Plex-Client-Identifier": _client_id()},
        timeout=30,
    )
    r.raise_for_status()
    servers = []
    for res in r.json():
        if "server" not in (res.get("provides") or ""):
            continue
        conns = sorted(res.get("connections", []), key=lambda c: (not c.get("local"), c.get("relay", False)))
        servers.append({
            "name": res.get("name"),
            "id": res.get("clientIdentifier"),
            "owned": bool(res.get("owned")),
            "token": res.get("accessToken") or token,
            "urls": [c["uri"] for c in conns],
        })
    servers.sort(key=lambda s: not s["owned"])  # your own server first; shared ones can't take uploads
    return servers
