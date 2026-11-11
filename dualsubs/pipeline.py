"""Per-item processing: pick two subtitle tracks, merge, upload, remember."""
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
from .plex import Item, Plex, PlexError, SubStream, map_path
from .subtitles import parse, write_srt

MARKER = "dualsubs"


@dataclass
class Settings:
    primary: str = "nl"
    secondary: str = "en"
    style: Style = field(default_factory=Style)
    mode: str = "upload"  # upload | sidecar
    path_map: list[tuple[str, str]] = field(default_factory=list)
    min_paired: float = 0.3  # refuse merges where fewer cues line up than this
    filename: str = "dualsubs {P}+{S}.{p}.srt"

    def __post_init__(self):
        for attr in ("primary", "secondary"):
            code = normalize(getattr(self, attr))
            if not code:
                raise ValueError(f"Unknown language: {getattr(self, attr)!r}")
            setattr(self, attr, code)
        if self.primary == self.secondary:
            raise ValueError("Primary and secondary language must differ")

    def track_filename(self) -> str:
        p, s = self.primary, self.secondary
        return self.filename.format(p=p, s=s, P=p.upper(), S=s.upper(), primary=lang_name(p), secondary=lang_name(s))

    def fingerprint(self) -> str:
        blob = json.dumps([self.primary, self.secondary, asdict(self.style), self.mode, self.filename], sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]


@dataclass
class Result:
    rating_key: str
    title: str
    status: str  # added | up-to-date | dry-run | missing | low-match | error
    detail: str = ""
    stats: MergeStats | None = None

    def line(self) -> str:
        icon = {"added": "+", "up-to-date": "=", "dry-run": "~", "removed": "-"}.get(self.status, "!")
        return f"[{icon}] {self.title}: {self.status}" + (f" - {self.detail}" if self.detail else "")


class State:
    """Remembers what we produced per item, so re-runs are cheap and idempotent."""

    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        self.data: dict = {}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text() or "{}")

    def get(self, key: str) -> dict:
        return self.data.get(key, {})

    def set(self, key: str, value: dict | None) -> None:
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value
        self.save()

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


def candidates(subs: list[SubStream], lang: str, can_read_files: bool) -> list[SubStream]:
    """Text subtitles in ``lang``, best first: full (not forced) > not SDH > readable."""
    pool = [s for s in subs if s.language == lang and s.is_text]

    def rank(s: SubStream):
        return (s.forced, s.hearing_impaired, not (s.external or can_read_files), not s.external)

    return sorted(pool, key=rank)


def _image_only(subs: list[SubStream], lang: str) -> bool:
    return any(s.language == lang and not s.is_text for s in subs)


# ---------------------------------------------------------------------- processing

def process_item(plex: Plex, rating_key: str, cfg: Settings, state: State, *, dry_run=False, force=False, save_to: str | None = None) -> Result:
    try:
        item = plex.item(rating_key)
    except PlexError as e:
        return Result(rating_key, rating_key, "error", str(e))
    try:
        return _process(plex, item, cfg, state, dry_run=dry_run, force=force, save_to=save_to)
    except PlexError as e:
        return Result(rating_key, item.title, "error", str(e))


def _process(plex: Plex, item: Item, cfg: Settings, state: State, *, dry_run, force, save_to) -> Result:
    if not item.parts:
        return Result(item.rating_key, item.title, "error", "item has no media file")
    part = item.parts[0]
    prev = state.get(item.rating_key)
    ours = [s for s in item.subtitles if is_ours(s, set(prev.get("stream_ids", [])))]
    sources = [s for s in item.subtitles if s not in ours]

    signature = hashlib.sha1(json.dumps(
        [cfg.fingerprint(), sorted((s.id, s.language, s.codec, s.forced) for s in sources)]
    ).encode()).hexdigest()[:16]
    already = ours if cfg.mode == "upload" else ([1] if prev.get("sidecar") and Path(prev["sidecar"]).exists() else [])
    if not force and prev.get("signature") == signature and already:
        return Result(item.rating_key, item.title, "up-to-date")

    can_read = bool(part.file) and os.path.exists(map_path(part.file, cfg.path_map))
    picked = {}
    for role, lang in (("primary", cfg.primary), ("secondary", cfg.secondary)):
        cands = candidates(sources, lang, can_read)
        if not cands:
            why = "only image-based (PGS/VobSub) subtitles, which can't be merged as text" if _image_only(sources, lang) else "no subtitles"
            return Result(item.rating_key, item.title, "missing", f"{lang_name(lang)}: {why}")
        errors = []
        for s in cands:
            try:
                data, fmt = plex.download_subtitle(s, part, cfg.path_map)
                cues = parse(data, fmt)
                if cues:
                    picked[role] = (s, cues)
                    break
                errors.append(f"stream {s.id} is empty")
            except PlexError as e:
                errors.append(str(e))
        if role not in picked:
            return Result(item.rating_key, item.title, "error", f"{lang_name(lang)}: " + "; ".join(errors))

    (ps, pcues), (ss, scues) = picked["primary"], picked["secondary"]
    merged, stats = merge(pcues, scues, cfg.style)
    total = stats.paired + stats.primary_only + stats.secondary_only
    srt = write_srt(merged).encode("utf-8")
    if save_to:
        Path(save_to).write_bytes(srt)
    detail = f"{_src(ps)} + {_src(ss)}; {stats.describe()}"
    if total and stats.paired / total < cfg.min_paired and not force:
        return Result(item.rating_key, item.title, "low-match",
                      detail + " - the two tracks barely line up (different cut?); use --force to add anyway", stats)
    if dry_run:
        return Result(item.rating_key, item.title, "dry-run", detail, stats)

    filename = cfg.track_filename()
    if cfg.mode == "sidecar":
        target = _sidecar_path(map_path(part.file, cfg.path_map), filename)
        target.write_bytes(srt)
        state.set(item.rating_key, {"signature": signature, "sidecar": str(target), "title": item.title})
        return Result(item.rating_key, item.title, "added", detail + f"; wrote {target.name} (Plex shows it after its next scan)", stats)

    for s in ours:
        plex.delete_stream(s)
    before = {s.id for s in plex.item(item.rating_key).subtitles}
    plex.upload_subtitle(item.rating_key, srt, filename)
    after = plex.item(item.rating_key).subtitles
    new = [s for s in after if s.id not in before]
    state.set(item.rating_key, {"signature": signature, "stream_ids": [s.id for s in new], "title": item.title})
    shown = ", ".join(f"'{s.display or s.title}'" for s in new) or "(not listed yet; Plex may still be processing)"
    return Result(item.rating_key, item.title, "added", detail + f"; Plex lists it as {shown}", stats)


def _src(s: SubStream) -> str:
    where = "external" if s.external else f"embedded #{s.index}"
    extra = "".join([" forced" if s.forced else "", " SDH" if s.hearing_impaired else ""])
    return f"{s.language} {s.codec} {where}{extra}"


def _sidecar_path(video: str, filename: str) -> Path:
    v = Path(video)
    # "dualsubs NL+EN.nl.srt" -> "<video stem>.dualsubs.nl.srt"
    lang_ext = ".".join(filename.split(".")[-2:])
    return v.with_name(f"{v.stem}.{MARKER}.{lang_ext}")


def remove_item(plex: Plex, rating_key: str, state: State) -> Result:
    item = plex.item(rating_key)
    prev = state.get(rating_key)
    ours = [s for s in item.subtitles if is_ours(s, set(prev.get("stream_ids", [])))]
    for s in ours:
        plex.delete_stream(s)
    if prev.get("sidecar") and Path(prev["sidecar"]).exists():
        Path(prev["sidecar"]).unlink()
        ours.append(None)
    state.set(rating_key, None)
    return Result(rating_key, item.title, "removed" if ours else "up-to-date", f"{len(ours)} track(s)")


_RK = re.compile(r"^\d+$")


def looks_like_key(s: str) -> bool:
    return bool(_RK.match(s))
