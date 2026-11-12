"""`dualsubs lab`: experiments to run on a real server, with timings.

  lab time TITLE          build one item's tracks from scratch, timing every step;
                          optionally again without reading the video file
  lab placeholder TITLE   put a "tap to prepare" track on one item, wait for someone
                          to pick it on a TV, build the real track, and record what
                          the player does afterwards

Both write a JSON report next to the config so results can be shared.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from . import pipeline
from .pipeline import Settings, State, is_ours, pair_label, process_item, remove_item
from .plex import Plex, PlexError
from .subtitles import Cue, write_srt

PLACEHOLDER_TAG = "tap to prepare"


def _timed_build(plex: Plex, rk: str, settings: Settings, state: State, label: str) -> dict:
    pipeline.PHASES = []
    t0 = time.perf_counter()
    try:
        results = process_item(plex, rk, settings, state, force=True)
    finally:
        phases, pipeline.PHASES = pipeline.PHASES, None
    total = time.perf_counter() - t0
    state.flush()
    print(f"\n{label}: {total:.1f} s")
    for name, sec in phases:
        print(f"  {sec:7.1f} s  {name}")
    for r in results:
        print("  " + r.line())
    return {"label": label, "total_s": round(total, 2), "phases": [[n, round(s, 2)] for n, s in phases],
            "results": [r.line() for r in results]}


def lab_time(plex: Plex, rk: str, settings: Settings, state: State, compare: bool) -> dict:
    item = plex.item(rk)
    print(f"Timing a from-scratch build for {item.title} ({', '.join(pair_label(p) for p in settings.pairs)})")
    print("Existing dualsubs tracks on this item are replaced; nothing else changes.")
    runs = [_timed_build(plex, rk, settings, state, "As configured (reads the video file when needed)")]
    if compare:
        runs.append(_timed_build(plex, rk, replace(settings, no_file_read=True), state,
                                 "Without reading the video file (downloads every language instead)"))
    return {"experiment": "time", "item": item.title, "ratingKey": rk, "runs": runs}


def _placeholder_srt(pair) -> bytes:
    text = (f"Bilingual subtitles ({pair_label(pair)}) are being prepared.\n"
            "Open the subtitle menu again in about a minute.")
    return write_srt([Cue(0, 15 * 60 * 1000, text)]).encode()


def lab_placeholder(plex: Plex, rk: str, settings: Settings, state: State, *, wait_s: int, poll_s: float,
                    observe_s: int, compare: bool = False, out=print) -> dict:
    pair = settings.pairs[0]
    single = replace(settings, pairs=[pair], no_file_read=compare)
    report: dict = {"experiment": "placeholder", "ratingKey": rk, "pair": pair_label(pair), "events": []}

    def log(msg, **kw):
        out(time.strftime("%H:%M:%S ") + msg)
        report["events"].append({"t": round(time.time(), 2), "msg": msg, **kw})

    item = plex.item(rk)
    report["item"] = item.title
    # Start clean: no real track for this pair, so the build has real work to do.
    remove_item(plex, rk, state, [pair])
    name = f"{pipeline.MARKER} {pair_label(pair)} ({PLACEHOLDER_TAG}).{pair[0]}.srt"
    before = {s.id for s in plex.item(rk).subtitles}
    plex.upload_subtitle(rk, _placeholder_srt(pair), name)
    new = [s for s in plex.item(rk).subtitles if s.id not in before]
    if not new:
        log("Plex did not list the placeholder track after upload; stopping")
        return report
    ph = new[0]
    report["placeholder"] = {"id": ph.id, "plex_label": ph.display or ph.title, "title": ph.title}
    log(f"Placeholder added to {item.title}. Plex lists it as '{ph.display or ph.title}' (stream {ph.id}).")
    log(f"Now start {item.title} on the TV and pick that subtitle track. Waiting up to {wait_s // 60} min...")

    t_end = time.time() + wait_s
    last = object()
    selected_at = None
    while time.time() < t_end:
        try:
            sess = plex.session_subtitle(rk)
        except PlexError as e:
            log(f"couldn't read sessions: {e}")
            sess = None
        cur = (sess or {}).get("subtitle") if sess else None
        if sess and cur != last:
            log(f"player '{sess['player']}' {sess['state']}, selected subtitle stream: {cur}")
            last = cur
        if sess and cur == ph.id:
            selected_at = time.time()
            break
        time.sleep(poll_s)
    if not selected_at:
        log("Nobody picked the placeholder in time.")
        _cleanup(plex, rk, ph, log)
        return report

    log("Placeholder picked: building the real track now")
    build = _timed_build(plex, rk, single, state, "On-demand build")
    report["build"] = build
    real = [s for s in plex.item(rk).subtitles if is_ours(s, state.all_stream_ids(rk)) and s.id != ph.id
            and PLACEHOLDER_TAG not in (s.title or "")]
    report["real_track"] = [{"id": s.id, "plex_label": s.display or s.title} for s in real]
    log(f"Real track ready {time.time() - selected_at:.1f} s after the pick: "
        + ", ".join(f"'{s.display or s.title}' (stream {s.id})" for s in real))
    _cleanup(plex, rk, ph, log)
    log(f"Watching the player for {observe_s} s: does it switch to the real track by itself? "
        "(Re-open the subtitle menu at some point and pick the real track, so we see that too.)")
    t_end = time.time() + observe_s
    last = object()
    while time.time() < t_end:
        try:
            sess = plex.session_subtitle(rk)
        except PlexError:
            sess = None
        cur = sess.get("subtitle") if sess else None
        if cur != last:
            where = ("the real track" if any(cur == s.id for s in real) else
                     "the removed placeholder" if cur == ph.id else "no/other subtitles")
            log(f"selected subtitle stream now: {cur} ({where})" if sess else "playback stopped")
            last = cur
        time.sleep(poll_s)
    return report


def _cleanup(plex: Plex, rk: str, ph, log) -> None:
    try:
        plex.delete_stream(ph)
        log("Placeholder removed.")
    except PlexError as e:
        log(f"couldn't remove the placeholder: {e}")


def save_report(config_dir: Path, report: dict) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"lab-{report['experiment']}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2))
    return path
