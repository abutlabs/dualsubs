"""Per-item processing: for each language pair, pick two subtitle tracks,
merge them, upload the result and remember what we did."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import tempfile
import time
import weakref
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .align import MergeStats, Style, merge
from .languages import name as lang_name
from .languages import normalize
from .plex import Item, Plex, PlexError, SubStream, extract_embedded_many, map_path
from .subtitles import parse, write_srt

MARKER = "dualsubs"
Pair = tuple  # (primary, secondary) ISO 639-1 codes


def parse_pair(text: str) -> Pair:
    """'en+es', 'en/es', 'en-es', 'English+Spanish' -> ('en', 'es')."""
    bits = [b for b in re.split(r"[+/,:]", text.strip()) if b.strip()]
    if len(bits) == 1:
        bits = [b for b in bits[0].split("-") if b.strip()]
    if len(bits) != 2:
        raise ValueError(f"A language pair looks like 'en+es', got {text!r}")
    codes = tuple(normalize(b) for b in bits)
    if None in codes:
        raise ValueError(f"Unknown language in {text!r}")
    if codes[0] == codes[1]:
        raise ValueError(f"The two languages in {text!r} must differ")
    return codes


def pair_label(pair: Pair) -> str:
    return f"{pair[0].upper()}+{pair[1].upper()}"


@dataclass
class Settings:
    pairs: list = field(default_factory=lambda: [("nl", "en")])
    style: Style = field(default_factory=Style)
    mode: str = "upload"  # upload | sidecar
    path_map: list = field(default_factory=list)
    min_paired: float = 0.3  # refuse merges where fewer cues line up than this
    filename: str = "dualsubs {P}+{S}.{p}.srt"
    fetch_missing: bool = True  # ask Plex's subtitle search for a missing language
    fetch_limit: int = 50  # max subtitle downloads per day
    fetch_retry_days: int = 30  # don't search again for a language Plex didn't find

    def __post_init__(self):
        pairs = []
        for p in self.pairs:
            pair = parse_pair(p) if isinstance(p, str) else parse_pair("+".join(p))
            if pair not in pairs:
                pairs.append(pair)
        if not pairs:
            raise ValueError("No language pairs configured")
        self.pairs = pairs

    def track_filename(self, pair: Pair) -> str:
        p, s = pair
        return self.filename.format(p=p, s=s, P=p.upper(), S=s.upper(), primary=lang_name(p), secondary=lang_name(s))

    def fingerprint(self, pair: Pair) -> str:
        blob = json.dumps([list(pair), asdict(self.style), self.mode, self.filename], sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]


@dataclass
class Result:
    rating_key: str
    title: str
    status: str  # added | up-to-date | dry-run | missing | low-match | error | removed
    detail: str = ""
    stats: MergeStats | None = None
    pair: Pair | None = None

    def line(self) -> str:
        icon = {"added": "+", "up-to-date": "=", "dry-run": "~", "removed": "-"}.get(self.status, "!")
        tag = f" [{pair_label(self.pair)}]" if self.pair else ""
        return f"[{icon}] {self.title}{tag}: {self.status}" + (f" - {self.detail}" if self.detail else "")


_OPEN_STATES: "weakref.WeakSet[State]" = weakref.WeakSet()
atexit.register(lambda: [s.flush() for s in list(_OPEN_STATES)])


class _write_lock:
    """Short exclusive lock around reading-merging-writing the state file."""

    def __init__(self, path: Path):
        self.path = path.with_name(path.name + ".write")

    def __enter__(self):
        try:
            import fcntl
            self.f = open(self.path, "w")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except ImportError:
            self.f = None
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()


class State:
    """Remembers what we produced per item and pair, so re-runs are cheap and idempotent.

    Layout: {ratingKey: {"en+es": {"signature": ..., "stream_ids": [...] | "sidecar": path}}}
    """

    SAVE_EVERY_S = 15  # batch writes: a big library means a big state file

    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        self.data: dict = {}
        self._dirty: set = set()  # top-level keys we changed since the last save
        self._fetched = 0  # downloads we made since the last save
        self._saved_at = time.time()
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text() or "{}")
        if self.path:
            _OPEN_STATES.add(self)  # flushed at exit; weak, so a dropped State releases its lock

    def lock(self) -> bool:
        """Take an exclusive lock for this state file; False if another run holds it.

        Two runs writing the same state (a `watch` in tmux plus a cron job, say)
        would lose each other's updates. The lock lives as long as this process.
        """
        if not self.path:
            return True
        try:
            import fcntl
        except ImportError:  # Windows: no locking
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.path.with_name(self.path.name + ".lock"), "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            return False
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()
        return True

    def get(self, key: str, pair: Pair | None = None) -> dict:
        entry = self.data.get(key, {})
        return entry.get("+".join(pair), {}) if pair else entry

    def set(self, key: str, pair: Pair, value: dict | None) -> None:
        entry = self.data.setdefault(key, {})
        if value is None:
            entry.pop("+".join(pair), None)
        else:
            entry["+".join(pair)] = value
        if not entry:
            self.data.pop(key, None)
        self._dirty.add(key)
        if time.time() - self._saved_at >= self.SAVE_EVERY_S:
            self.save()

    def flush(self) -> None:
        if self._dirty:
            self.save()

    def all_stream_ids(self, key: str) -> set[int]:
        return {i for k, v in self.data.get(key, {}).items() if not k.startswith("_") for i in v.get("stream_ids", [])}

    def items(self) -> list[str]:
        return [k for k in self.data if not k.startswith("_")]

    # -- per-item notes about subtitle searches, and the daily download budget
    def fetch_note(self, key: str, lang: str) -> dict:
        return self.data.get(key, {}).get("_fetch", {}).get(lang, {})

    def set_fetch_note(self, key: str, lang: str, note: dict) -> None:
        self.data.setdefault(key, {}).setdefault("_fetch", {})[lang] = note
        self._dirty.add(key)

    def set_info(self, item) -> None:
        """Remember where an item lives, for the overview in `dualsubs web`."""
        self.data.setdefault(item.rating_key, {})["_info"] = {
            "lib": item.library_id, "library": item.library, "title": item.title,
            "show": item.show, "type": item.type}
        self._dirty.add(item.rating_key)

    PAUSE_AFTER = 3  # downloads in a row that never arrive: the provider's daily limit, most likely

    def provider_paused(self) -> bool:
        p = self.data.get("_provider", {})
        return p.get("date") == time.strftime("%Y-%m-%d") and p.get("paused", False)

    def note_delivery(self, ok: bool) -> None:
        today = time.strftime("%Y-%m-%d")
        p = self.data.get("_provider", {})
        fails = 0 if ok or p.get("date") != today else p.get("fails", 0)
        fails = 0 if ok else fails + 1
        self.data["_provider"] = {"date": today, "fails": fails, "paused": fails >= self.PAUSE_AFTER}
        self._dirty.add("_provider")

    @staticmethod
    def _count_today(data: dict) -> int:
        b = data.get("_budget", {})
        return b.get("count", 0) if b.get("date") == time.strftime("%Y-%m-%d") else 0

    def fetches_today(self) -> int:
        return self._count_today(self.data) + self._fetched

    def count_fetch(self) -> None:
        self._fetched += 1
        self._dirty.add("_budget")

    def save(self) -> None:
        """Write our changes, merged into whatever is on disk now.

        Another dualsubs process (a repair from `dualsubs web` next to a long
        library run) may have saved its own items in the meantime; we only
        replace the items *we* changed, under a short write lock.
        """
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock(self.path):
            try:
                disk = json.loads(self.path.read_text() or "{}") if self.path.exists() else {}
            except ValueError:
                disk = {}
            for key in self._dirty - {"_budget"}:
                if key in self.data:
                    disk[key] = self.data[key]
                else:
                    disk.pop(key, None)
            if self._fetched:
                # Several processes may download on the same day: add ours to theirs.
                disk["_budget"] = {"date": time.strftime("%Y-%m-%d"), "count": self._count_today(disk) + self._fetched}
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state")
            with os.fdopen(fd, "w") as f:
                json.dump(disk, f, separators=(",", ":"))  # compact: thousands of items
            os.replace(tmp, self.path)
        self.data = disk  # pick up what other processes wrote
        self._dirty = set()
        self._fetched = 0
        self._saved_at = time.time()


# ---------------------------------------------------------------------- selection

def is_ours(s: SubStream, remembered_ids: set[int]) -> bool:
    return s.id in remembered_ids or MARKER in (s.title or "").lower() or MARKER in (s.display or "").lower()


def belongs_to(s: SubStream, pair: Pair, prev: dict, filename: str) -> bool:
    """Is this one of *our* tracks for this particular pair?"""
    return s.id in set(prev.get("stream_ids", [])) or (s.title or "") == filename


def candidates(subs: list[SubStream], lang: str, can_read_files: bool) -> list[SubStream]:
    """Text subtitles in ``lang``, best first: full (not forced) > not SDH > readable."""
    pool = [s for s in subs if s.language == lang and s.is_text]

    def rank(s: SubStream):
        return (s.forced, s.hearing_impaired, not (s.external or can_read_files), not s.external)

    return sorted(pool, key=rank)


def _image_only(subs: list[SubStream], lang: str) -> bool:
    return any(s.language == lang and not s.is_text for s in subs)


# ---------------------------------------------------------------------- processing

class item_lock:
    """Per-item lock across processes, so a repair from `dualsubs web` and a
    library run never work on the same item at once. One file, one byte-range
    lock per ratingKey (no file clutter)."""

    def __init__(self, state: State, rating_key: str):
        self.path = state.path.with_name(state.path.name + ".items") if state.path else None
        self.offset = int(rating_key) if str(rating_key).isdigit() else abs(hash(rating_key)) % (1 << 40)
        self.f = None

    def __enter__(self) -> bool:
        if not self.path:
            return True
        try:
            import fcntl
        except ImportError:
            return True
        self.f = open(self.path, "a+b")
        try:
            fcntl.lockf(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, self.offset)
            return True
        except OSError:
            self.f.close()
            self.f = None
            return False

    def __exit__(self, *exc):
        if self.f:
            self.f.close()  # releases the lock


def process_item(plex: Plex, rating_key: str, cfg: Settings, state: State, *, dry_run=False, force=False,
                 save_to: str | None = None) -> list[Result]:
    if dry_run:
        return _process_item(plex, rating_key, cfg, state, dry_run=dry_run, force=force, save_to=save_to)
    with item_lock(state, rating_key) as got:
        if not got:
            return [Result(rating_key, rating_key, "busy", "another dualsubs process is working on this item")]
        return _process_item(plex, rating_key, cfg, state, dry_run=dry_run, force=force, save_to=save_to)


def _process_item(plex: Plex, rating_key: str, cfg: Settings, state: State, *, dry_run=False, force=False,
                  save_to: str | None = None) -> list[Result]:
    """Process every configured language pair for one movie/episode.

    Order matters for disk use: decide from Plex's metadata alone which pairs can
    be made, fetch missing languages if allowed, and only then read the video file
    (once) for the embedded tracks those pairs actually need.
    """
    try:
        item = plex.item(rating_key)
    except PlexError as e:
        return [Result(rating_key, rating_key, "error", str(e))]
    if not item.parts:
        return [Result(rating_key, item.title, "error", "item has no media file")]

    if rating_key in state.data and "_info" not in state.data[rating_key]:
        state.set_info(item)  # entries from older versions: fill in the library for the overview
    pending = [pair for pair in cfg.pairs if force or not _context(item, pair, cfg, state)["up_to_date"]]
    if cfg.fetch_missing and pending and not dry_run:
        item = _fetch_missing(plex, item, pending, cfg, state)
    cache: dict[int, list] = {}  # stream id -> parsed cues, shared between pairs
    _prefetch_embedded(plex, item, cfg, state, cache, pending)
    results = []
    for pair in cfg.pairs:
        out = save_to if (save_to and len(cfg.pairs) == 1) else (
            f"{Path(save_to).with_suffix('')}.{pair[0]}-{pair[1]}.srt" if save_to else None)
        try:
            res = _process_pair(plex, item, pair, cfg, state, cache, dry_run=dry_run, force=force, save_to=out)
        except PlexError as e:
            res = Result(item.rating_key, item.title, "error", str(e), pair=pair)
        results.append(res)
        if res.status == "added" and cfg.mode == "upload":
            item = plex.item(rating_key)  # pick up the new stream before the next pair
    return results


# Outcomes worth remembering, so re-runs skip the item until its subtitles change.
SETTLED = ("missing", "error", "low-match")


def _context(item: Item, pair: Pair, cfg: Settings, state: State) -> dict:
    """What we know about one pair on one item, and whether it needs (re)doing."""
    prev = state.get(item.rating_key, pair)
    filename = cfg.track_filename(pair)
    all_ours = [s for s in item.subtitles if is_ours(s, state.all_stream_ids(item.rating_key))]
    mine = [s for s in all_ours if belongs_to(s, pair, prev, filename)]
    sources = [s for s in item.subtitles if s not in all_ours]
    def sig(subs):
        return hashlib.sha1(json.dumps(
            [cfg.fingerprint(pair), sorted((s.id, s.language, s.codec, s.forced) for s in subs)]
        ).encode()).hexdigest()[:16]

    # Only this pair's languages count: a French track turning up shouldn't redo EN+ES.
    signature = sig([s for s in sources if s.language in pair])
    if prev.get("signature") and prev["signature"] != signature and prev["signature"] == sig(sources):
        # Recorded by dualsubs <= 0.3 (signature over all languages): still valid, upgrade in place.
        prev = {**prev, "signature": signature}
        state.set(item.rating_key, pair, prev)
    done = mine if cfg.mode == "upload" else (prev.get("sidecar") and Path(prev["sidecar"]).exists())
    settled = prev.get("status") in SETTLED
    if settled and prev.get("status") == "missing" and cfg.fetch_missing:
        # Still worth another go unless every missing language was searched recently without luck
        # (no budget yesterday, a failed download, or fetching just switched on all reopen it).
        settled = all(_recently_unavailable(state.fetch_note(item.rating_key, lang), cfg)
                      for lang in prev.get("missing", []))
    return {"prev": prev, "filename": filename, "mine": mine, "sources": sources, "signature": signature,
            "up_to_date": bool(prev.get("signature") == signature and (done or settled))}


def _can_read(item: Item, cfg: Settings) -> bool:
    part = item.parts[0]
    return bool(part.file) and os.path.exists(map_path(part.file, cfg.path_map))


def _missing_langs(sources: list[SubStream], pair: Pair, can_read: bool) -> list[str]:
    return [lang for lang in pair if not candidates(sources, lang, can_read)]


def _prefetch_embedded(plex: Plex, item: Item, cfg: Settings, state: State, cache: dict, pending: list) -> None:
    """Pull every embedded track the pending pairs need in ONE ffmpeg pass.

    ffmpeg reads the whole video file per run. Pairs missing a language are left
    out entirely: reading a file for half a pair would be wasted.
    """
    if not pending or not _can_read(item, cfg):
        return
    wanted: dict[int, SubStream] = {}
    for pair in pending:
        sources = _context(item, pair, cfg, state)["sources"]
        if _missing_langs(sources, pair, True):
            continue
        for lang in pair:
            best = candidates(sources, lang, True)[0]
            if not best.external and best.index is not None and best.id not in cache:
                wanted[best.id] = best
    if not wanted:
        return
    part = item.parts[0]
    try:
        got = extract_embedded_many(map_path(part.file, cfg.path_map), [(s.index, s.codec) for s in wanted.values()])
    except PlexError:
        return  # each pair retries on its own and reports the error
    for s in wanted.values():
        data, fmt = got.get(s.index, (b"", "srt"))
        cache[s.id] = parse(data, fmt) if data else []


def _fetch_missing(plex: Plex, item: Item, pending: list, cfg: Settings, state: State) -> Item:
    """Ask Plex's subtitle search for every language the pending pairs lack, so
    the pairs can be made. Returns the item, refreshed when anything arrived.

    Pairs missing one language come first (one download completes them); a pair
    missing both is only started when today's budget covers both downloads.
    """
    can_read = _can_read(item, cfg)
    sources = [s for s in item.subtitles if not is_ours(s, state.all_stream_ids(item.rating_key))]
    needs = sorted((_missing_langs(sources, pair, can_read) for pair in pending), key=len)
    if state.provider_paused():
        return item
    downloaded: list[str] = []
    tried: set = set()
    for missing in needs:
        todo = [lang for lang in missing if lang not in tried and lang not in downloaded]
        if not todo:
            continue
        if any(_recently_unavailable(state.fetch_note(item.rating_key, lang), cfg) for lang in missing):
            continue  # this pair can't be completed for now; don't spend downloads on half of it
        if state.fetches_today() + len(todo) > cfg.fetch_limit:
            continue  # not enough budget left today to complete this pair
        for lang in todo:
            tried.add(lang)
            try:
                found = plex.search_subtitles(item.rating_key, lang) \
                    or plex.search_subtitles(item.rating_key, lang, allow_sdh=True)  # better than nothing
            except PlexError:
                continue
            if not found:
                state.set_fetch_note(item.rating_key, lang, {"result": "none", "at": time.time()})
                break  # the pair can't be completed: skip its other language
            try:
                plex.download_found_subtitle(item.rating_key, found[0])
            except PlexError as e:
                state.set_fetch_note(item.rating_key, lang, {"result": "error", "at": time.time(), "detail": str(e)[:200]})
                break
            state.count_fetch()
            state.set_fetch_note(item.rating_key, lang, {"result": "downloaded", "at": time.time(),
                                                         "from": found[0].get("provider", ""),
                                                         "exact": found[0].get("perfect", False)})
            downloaded.append(lang)
    if not downloaded:
        return item
    # Plex downloads in the background; wait for the new track(s) to show up.
    fresh = item
    for _ in range(FETCH_WAIT_TRIES):
        fresh = plex.item(item.rating_key)
        have = {s.language for s in fresh.subtitles if s.is_text}
        if all(lang in have for lang in downloaded):
            break
        time.sleep(FETCH_WAIT_S)
    have = {s.language for s in fresh.subtitles if s.is_text}
    for lang in downloaded:
        arrived = lang in have
        if not arrived:
            state.set_fetch_note(item.rating_key, lang, {"result": "not-delivered", "at": time.time()})
        state.note_delivery(arrived)
    return fresh


def _recently_unavailable(note: dict, cfg: Settings) -> bool:
    """A search that found nothing isn't repeated for a while; failures are retried the next day."""
    age = time.time() - note.get("at", 0)
    if note.get("result") == "none":
        return age < cfg.fetch_retry_days * 86400
    if note.get("result") in ("error", "not-delivered"):
        return age < 20 * 3600
    return False


FETCH_WAIT_TRIES, FETCH_WAIT_S = 15, 2


def compatibility(item: Item, cfg: Settings, state: State) -> dict:
    """What each language pair can do for this item, from Plex's metadata only (no file reads).

    Per pair, status is one of:
      done       a bilingual track is in Plex
      ready      both languages are there; a run will make the track
      missing    one or both languages are missing (see "missing"); --fetch-missing can try
                 when only one is missing
      low-match / error   tried before and couldn't make a good track
    """
    can_read = _can_read(item, cfg) if item.parts else False
    sources = [s for s in item.subtitles if not is_ours(s, state.all_stream_ids(item.rating_key))]
    text = sorted({s.language for s in sources if s.is_text and s.language})
    image = sorted({s.language for s in sources if not s.is_text and s.language} - set(text))
    pairs = {}
    for pair in cfg.pairs:
        ctx = _context(item, pair, cfg, state)
        prev = ctx["prev"]
        missing = _missing_langs(ctx["sources"], pair, can_read)
        notes = {lang: state.fetch_note(item.rating_key, lang).get("result") for lang in missing}
        if ctx["mine"] or (cfg.mode == "sidecar" and prev.get("sidecar")):
            status = "done"
        elif missing:
            status = "missing"
        elif prev.get("status") in ("low-match", "error") and ctx["up_to_date"]:
            status = prev["status"]
        else:
            status = "ready"
        pairs["+".join(pair)] = {"status": status, "missing": missing,
                                 "searched": {k: v for k, v in notes.items() if v}}
    return {"ratingKey": item.rating_key, "title": item.title, "show": item.show, "type": item.type,
            "languages": text, "image_only": image, "pairs": pairs}


def _process_pair(plex: Plex, item: Item, pair: Pair, cfg: Settings, state: State, cache: dict, *,
                  dry_run, force, save_to) -> Result:
    part = item.parts[0]
    ctx = _context(item, pair, cfg, state)
    prev, filename, mine, sources, signature = (ctx[k] for k in ("prev", "filename", "mine", "sources", "signature"))

    def result(status, detail="", stats=None, **remember):
        if status in SETTLED and not dry_run:
            state.set(item.rating_key, pair, {"signature": signature, "status": status, **remember})
            state.set_info(item)
        return Result(item.rating_key, item.title, status, detail, stats, pair)

    if not force and ctx["up_to_date"]:
        # "unchanged": we already know this pair can't be made here, and nothing changed since.
        status = "unchanged" if prev.get("status") in SETTLED else "up-to-date"
        return Result(item.rating_key, item.title, status, prev.get("status", ""), None, pair)

    # Fail fast, from metadata only: both languages must be there before anything is read.
    can_read = _can_read(item, cfg)
    missing = _missing_langs(sources, pair, can_read)
    if missing:
        whys = []
        for lang in missing:
            why = ("only image-based (PGS/VobSub) subtitles, which can't be merged as text"
                   if _image_only(sources, lang) else "no subtitles")
            note = state.fetch_note(item.rating_key, lang)
            if not cfg.fetch_missing:
                pass
            elif dry_run:
                why += " (a real run would search Plex's subtitle provider)"
            elif note.get("result") == "none":
                why += "; Plex's subtitle search found none"
            elif note.get("result") == "not-delivered":
                why += "; a download was requested but never arrived (retrying tomorrow)"
            elif state.provider_paused():
                why += "; Plex's subtitle provider stopped delivering today, retrying tomorrow"
            elif not note and state.fetches_today() >= cfg.fetch_limit:
                why += "; today's download limit is used up, retrying tomorrow"
            whys.append(f"{lang_name(lang)}: {why}")
        return result("missing", "; ".join(whys), missing=missing)

    picked = {}
    for lang in pair:
        errors = []
        for s in candidates(sources, lang, can_read):
            try:
                if s.id not in cache:
                    data, fmt = plex.download_subtitle(s, part, cfg.path_map)
                    cache[s.id] = parse(data, fmt)
                if cache[s.id]:
                    picked[lang] = (s, cache[s.id])
                    break
                errors.append(f"stream {s.id} is empty")
            except PlexError as e:
                errors.append(str(e))
        if lang not in picked:
            return result("error", f"{lang_name(lang)}: " + "; ".join(errors))

    (ps, pcues), (ss, scues) = picked[pair[0]], picked[pair[1]]
    merged, stats = merge(pcues, scues, cfg.style)
    total = stats.paired + stats.primary_only + stats.secondary_only
    srt = write_srt(merged).encode("utf-8")
    if save_to:
        Path(save_to).write_bytes(srt)
    detail = f"{_src(ps)} + {_src(ss)}; {stats.describe()}"
    if total and stats.paired / total < cfg.min_paired and not force:
        return result("low-match", detail + " - the two tracks barely line up (different cut?); use --force to add anyway", stats)
    if dry_run:
        return result("dry-run", detail, stats)

    if cfg.mode == "sidecar":
        target = _sidecar_path(map_path(part.file, cfg.path_map), pair)
        target.write_bytes(srt)
        state.set(item.rating_key, pair, {"signature": signature, "sidecar": str(target), "at": time.time()})
        state.set_info(item)
        return result("added", detail + f"; wrote {target.name} (Plex shows it after its next scan)", stats)

    for s in mine:
        plex.delete_stream(s)
    before = {s.id for s in plex.item(item.rating_key).subtitles}
    plex.upload_subtitle(item.rating_key, srt, filename)
    new = [s for s in plex.item(item.rating_key).subtitles if s.id not in before]
    state.set(item.rating_key, pair, {"signature": signature, "stream_ids": [s.id for s in new], "at": time.time()})
    state.set_info(item)
    shown = ", ".join(f"'{s.display or s.title}'" for s in new) or "(not listed yet; Plex may still be processing)"
    return result("added", detail + f"; Plex lists it as {shown}", stats)


def _src(s: SubStream) -> str:
    where = "external" if s.external else f"embedded #{s.index}"
    extra = "".join([" forced" if s.forced else "", " SDH" if s.hearing_impaired else ""])
    return f"{s.language} {s.codec} {where}{extra}"


def _sidecar_path(video: str, pair: Pair) -> Path:
    # "<video stem>.dualsubs-es.en.srt": Plex reads the language from the last part.
    v = Path(video)
    return v.with_name(f"{v.stem}.{MARKER}-{pair[1]}.{pair[0]}.srt")


def remove_item(plex: Plex, rating_key: str, state: State, pairs: list | None = None) -> Result:
    """Remove our tracks for the given pairs (default: every pair we ever added)."""
    item = plex.item(rating_key)
    entry = state.get(rating_key)
    removed = 0
    if pairs is None:
        targets = [s for s in item.subtitles if is_ours(s, state.all_stream_ids(rating_key))]
        pairs = [tuple(k.split("+")) for k in entry if not k.startswith("_")]
    else:
        targets = []
        for pair in pairs:
            prev = state.get(rating_key, pair)
            fname = Settings(pairs=[pair]).track_filename(pair)
            targets += [s for s in item.subtitles if belongs_to(s, pair, prev, fname)]
    for s in targets:
        plex.delete_stream(s)
        removed += 1
    for pair in pairs:
        side = state.get(rating_key, pair).get("sidecar")
        if side and Path(side).exists():
            Path(side).unlink()
            removed += 1
        state.set(rating_key, pair, None)
    return Result(rating_key, item.title, "removed" if removed else "up-to-date", f"{removed} track(s)")


_RK = re.compile(r"^\d+$")


def looks_like_key(s: str) -> bool:
    return bool(_RK.match(s))
