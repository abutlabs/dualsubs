"""Per-item processing: for each language pair, pick two subtitle tracks,
merge them, upload the result and remember what we did."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
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


class State:
    """Remembers what we produced per item and pair, so re-runs are cheap and idempotent.

    Layout: {ratingKey: {"en+es": {"signature": ..., "stream_ids": [...] | "sidecar": path}}}
    """

    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        self.data: dict = {}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text() or "{}")

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
        self.save()

    def all_stream_ids(self, key: str) -> set[int]:
        return {i for v in self.data.get(key, {}).values() for i in v.get("stream_ids", [])}

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


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

def process_item(plex: Plex, rating_key: str, cfg: Settings, state: State, *, dry_run=False, force=False,
                 save_to: str | None = None) -> list[Result]:
    """Process every configured language pair for one movie/episode."""
    try:
        item = plex.item(rating_key)
    except PlexError as e:
        return [Result(rating_key, rating_key, "error", str(e))]
    if not item.parts:
        return [Result(rating_key, item.title, "error", "item has no media file")]

    cache: dict[int, list] = {}  # stream id -> parsed cues, shared between pairs
    _prefetch_embedded(plex, item, cfg, state, cache, force)
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


def _context(item: Item, pair: Pair, cfg: Settings, state: State) -> dict:
    """What we know about one pair on one item, and whether it needs (re)doing."""
    prev = state.get(item.rating_key, pair)
    filename = cfg.track_filename(pair)
    all_ours = [s for s in item.subtitles if is_ours(s, state.all_stream_ids(item.rating_key))]
    mine = [s for s in all_ours if belongs_to(s, pair, prev, filename)]
    sources = [s for s in item.subtitles if s not in all_ours]
    signature = hashlib.sha1(json.dumps(
        [cfg.fingerprint(pair), sorted((s.id, s.language, s.codec, s.forced) for s in sources)]
    ).encode()).hexdigest()[:16]
    done = mine if cfg.mode == "upload" else (prev.get("sidecar") and Path(prev["sidecar"]).exists())
    return {"prev": prev, "filename": filename, "mine": mine, "sources": sources, "signature": signature,
            "up_to_date": bool(prev.get("signature") == signature and done)}


def _can_read(item: Item, cfg: Settings) -> bool:
    part = item.parts[0]
    return bool(part.file) and os.path.exists(map_path(part.file, cfg.path_map))


def _prefetch_embedded(plex: Plex, item: Item, cfg: Settings, state: State, cache: dict, force: bool) -> None:
    """Pull every embedded track the pending pairs will want in one ffmpeg pass.

    ffmpeg reads the whole video file per run, so on large libraries one run per
    file (instead of one per subtitle) is the difference that matters.
    """
    if not _can_read(item, cfg):
        return
    wanted: dict[int, SubStream] = {}
    for pair in cfg.pairs:
        ctx = _context(item, pair, cfg, state)
        if ctx["up_to_date"] and not force:
            continue
        for lang in pair:
            cands = candidates(ctx["sources"], lang, True)
            if cands and not cands[0].external and cands[0].index is not None and cands[0].id not in cache:
                wanted[cands[0].id] = cands[0]
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


def _process_pair(plex: Plex, item: Item, pair: Pair, cfg: Settings, state: State, cache: dict, *,
                  dry_run, force, save_to) -> Result:
    def result(status, detail="", stats=None):
        return Result(item.rating_key, item.title, status, detail, stats, pair)

    part = item.parts[0]
    ctx = _context(item, pair, cfg, state)
    prev, filename, mine, sources, signature = (ctx[k] for k in ("prev", "filename", "mine", "sources", "signature"))
    if not force and ctx["up_to_date"]:
        return result("up-to-date")

    can_read = _can_read(item, cfg)
    picked = {}
    for lang in pair:
        cands = candidates(sources, lang, can_read)
        if not cands:
            why = ("only image-based (PGS/VobSub) subtitles, which can't be merged as text"
                   if _image_only(sources, lang) else "no subtitles")
            return result("missing", f"{lang_name(lang)}: {why}")
        errors = []
        for s in cands:
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
        state.set(item.rating_key, pair, {"signature": signature, "sidecar": str(target)})
        return result("added", detail + f"; wrote {target.name} (Plex shows it after its next scan)", stats)

    for s in mine:
        plex.delete_stream(s)
    before = {s.id for s in plex.item(item.rating_key).subtitles}
    plex.upload_subtitle(item.rating_key, srt, filename)
    new = [s for s in plex.item(item.rating_key).subtitles if s.id not in before]
    state.set(item.rating_key, pair, {"signature": signature, "stream_ids": [s.id for s in new]})
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
        pairs = [tuple(k.split("+")) for k in entry]
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
