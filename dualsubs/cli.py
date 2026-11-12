"""dualsubs command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, plex as plexmod
from .align import Style, merge
from .pipeline import Settings, State, compatibility, looks_like_key, pair_label, process_item, remove_item
from .plex import Plex, PlexError
from .progress import Progress
from .subtitles import parse, write_srt

def config_dir() -> Path:
    return Path(os.environ.get("DUALSUBS_HOME") or Path.home() / ".config" / "dualsubs")


def load_config() -> dict:
    p = config_dir() / "config.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_config(cfg: dict) -> Path:
    config_dir().mkdir(parents=True, exist_ok=True)
    p = config_dir() / "config.json"
    # Holds the Plex token: create it private (0600) so it is never readable by
    # other users, not even for a moment, and fix up an older file's mode.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(cfg, indent=2))
    return p


def _opt(args, name, env, cfg, default=None):
    val = getattr(args, name, None)
    if val not in (None, []):
        return val
    if os.environ.get(env):
        return os.environ[env]
    return cfg.get(name, default)


def build(args, lock: bool = False):
    cfg = load_config()
    url = _opt(args, "url", "PLEX_URL", cfg)
    token = _opt(args, "token", "PLEX_TOKEN", cfg)
    if not url or not token:
        sys.exit("No Plex server configured. Run `dualsubs login`, or pass --url and --token (or PLEX_URL / PLEX_TOKEN).")
    path_map = []
    raw = _opt(args, "path_map", "DUALSUBS_PATH_MAP", cfg, []) or []
    for pm in [raw] if isinstance(raw, str) else raw:
        for entry in pm.split(";"):
            if "=" in entry:
                src, _, dst = entry.partition("=")
                path_map.append((src.rstrip("/"), dst.rstrip("/")))
    ff = _opt(args, "ffmpeg", "DUALSUBS_FFMPEG", cfg)
    if ff:
        plexmod.FFMPEG = ff
    plexmod.PRIORITY = _opt(args, "priority", "DUALSUBS_PRIORITY", cfg, "low")
    if plexmod.PRIORITY not in ("low", "idle", "normal"):
        sys.exit(f"--priority must be low, idle or normal, not {plexmod.PRIORITY!r}")
    args._yield = not getattr(args, "ignore_streams", False) and cfg.get("yield_to_streams", True) \
        and os.environ.get("DUALSUBS_YIELD_TO_STREAMS", "1") != "0"
    if lock:
        # tmux kill-session / kill send SIGHUP / SIGTERM: exit through Python so
        # the latest progress is written to the state file.
        import signal
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, lambda *_: sys.exit(143))
    if lock and plexmod.PRIORITY != "normal":
        try:
            os.nice(19 - os.nice(0))  # background job: this process too, not just ffmpeg
        except OSError:
            pass
    settings = Settings(
        pairs=pairs_from(args, cfg),
        style=style_from(args, cfg),
        mode=_opt(args, "mode", "DUALSUBS_MODE", cfg, "upload"),
        path_map=path_map,
        fetch_missing=bool(getattr(args, "fetch_missing", False)) or (
            not getattr(args, "no_fetch", False) and bool(cfg.get("fetch_missing", True))
            and os.environ.get("DUALSUBS_FETCH", "1") != "0"),
        fetch_limit=int(_opt(args, "fetch_limit", "DUALSUBS_FETCH_LIMIT", cfg, 50)),
    )
    state = State(_opt(args, "state", "DUALSUBS_STATE", cfg) or config_dir() / "state.json")
    kind = "daemon" if lock == "daemon" else "run"
    if lock and not getattr(args, "dry_run", False) and not state.lock(kind):
        msg = ("`dualsubs watch` is already running" if kind == "daemon" else
               "another dualsubs run is already working on this library (e.g. a library run in tmux, or cron); "
               "not starting a second one")
        if getattr(args, "once", False):
            print(time.strftime("%Y-%m-%d %H:%M ") + "skipped: " + msg, flush=True)
            sys.exit(0)  # normal for cron while a long run is busy
        if kind == "daemon":
            if sys.stdout.isatty():
                print(msg)
            sys.exit(0)  # the cron keep-alive line: nothing to do, and nothing to log
        sys.exit(msg)
    return Plex(url, token), settings, state


def pairs_from(args, cfg) -> list:
    """--pair (repeatable) > -p/-s > DUALSUBS_PAIRS > config 'pairs' > config primary/secondary > nl+en."""
    if getattr(args, "pair", None):
        return [p for arg in args.pair for p in arg.split(",")]
    p, s = getattr(args, "primary", None), getattr(args, "secondary", None)
    if p or s:
        return [f"{p or cfg.get('primary', 'nl')}+{s or cfg.get('secondary', 'en')}"]
    if os.environ.get("DUALSUBS_PAIRS"):
        return os.environ["DUALSUBS_PAIRS"].split(",")
    if cfg.get("pairs"):
        return cfg["pairs"]
    return [f"{cfg.get('primary', 'nl')}+{cfg.get('secondary', 'en')}"]


def style_from(args, cfg) -> Style:
    st = cfg.get("style", {})
    color = getattr(args, "color", None) or st.get("secondary_color", "#FFD54F")
    return Style(
        secondary_color=None if str(color).lower() == "none" else color,
        secondary_italic=getattr(args, "italic", False) or st.get("secondary_italic", False),
        secondary_on_top=getattr(args, "secondary_on_top", False) or st.get("secondary_on_top", False),
        compact=getattr(args, "compact", False) or st.get("compact", False),
    )


def resolve_targets(plex: Plex, target: str) -> list[str]:
    if target == "all":
        return [k for lib in plex.libraries() for k in plex.library_items(lib["id"])]
    if target.startswith("library:"):
        name = target.split(":", 1)[1]
        libs = [lib for lib in plex.libraries() if name.lower() in (lib["title"].lower(), lib["id"])]
        if not libs:
            sys.exit(f"No library called {name!r}. Libraries: " + ", ".join(lib["title"] for lib in plex.libraries()))
        return plex.library_items(libs[0]["id"])
    if looks_like_key(target):
        return plex.expand(target)
    hits = plex.search(target)
    if not hits:
        sys.exit(f"Nothing in Plex matches {target!r}")
    exact = [h for h in hits if h["title"].lower() == target.lower()]
    if len(exact) == 1 or len(hits) == 1:
        h = (exact or hits)[0]
        print(f"Using {h['type']} '{h['title']}' (ratingKey {h['ratingKey']})")
        return plex.expand(h["ratingKey"])
    print(f"{target!r} matches several items; pass the ratingKey instead:")
    for h in hits:
        print(f"  {h['ratingKey']:>8}  {h['type']:<8} {h['title']} {h['year'] or ''}")
    sys.exit(2)


class StreamGate:
    """Holds dualsubs back while anyone is watching something on Plex."""

    POLL = 60  # seconds between checks while paused
    CACHE_S = 30  # re-ask Plex at most this often

    def __init__(self, plex: Plex, enabled: bool, progress: Progress | None = None):
        self.plex, self.enabled, self.progress = plex, enabled, progress
        self._checked, self._count = 0.0, 0

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

    def playing(self) -> int:
        if not self.enabled:
            return 0
        if time.time() - self._checked > self.CACHE_S:
            try:
                self._count = self.plex.playing_sessions()
            except PlexError as e:
                print(f"note: can't see Plex sessions ({e}); not pausing for streams", flush=True)
                self.enabled, self._count = False, 0
            self._checked = time.time()
        return self._count

    def wait(self) -> None:
        if not self.playing():
            return
        since = time.time()
        print(time.strftime("%Y-%m-%d %H:%M ") + f"paused: {self._count} Plex stream(s) playing; "
              "continuing when they stop", flush=True)
        while self.playing():
            if self.progress:
                self.progress.paused(self._count)
            self.sleep(self.POLL)
        if self.progress:
            self.progress.paused(0)
        print(time.strftime("%Y-%m-%d %H:%M ") + f"resumed after {int(time.time() - since) // 60} min", flush=True)


def settings_yield(args) -> bool:
    return getattr(args, "_yield", True)


def run_items(plex, settings, state, keys, args, label: str = "") -> int:
    counts: dict[str, int] = {}
    progress = None if args.dry_run else Progress(config_dir() / "progress.json", args.cmd, label)
    gate = StreamGate(plex, settings_yield(args) and not args.dry_run, progress)
    if progress:
        progress.start(len(keys))
    try:
        for k in keys:
            gate.wait()
            for res in process_item(plex, k, settings, state, dry_run=args.dry_run, force=args.force,
                                    save_to=getattr(args, "save", None)):
                counts[res.status] = counts.get(res.status, 0) + 1
                if progress:
                    progress.working_on(res.title)
                    progress.result(res)
                if res.status not in ("up-to-date", "unchanged") or args.verbose:
                    print(res.line(), flush=True)
            if progress:
                progress.item_done()
    except BaseException:
        if progress:
            progress.finish("stopped")
        raise
    if progress:
        progress.finish()
    state.flush()
    print("Summary: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) if counts else "Nothing to do.")
    return 1 if counts.get("error") else 0


# ---------------------------------------------------------------------- commands

def cmd_login(args):
    token = plexmod.login_pin()
    servers = plexmod.discover_servers(token)
    if not servers:
        sys.exit("Signed in, but no Plex servers were found on this account.")
    if args.server:
        servers = [s for s in servers if s["name"].lower() == args.server.lower()] or sys.exit(
            f"No server named {args.server!r} on this account")
    chosen = None
    for srv in servers:
        # When dualsubs runs on the Plex machine itself (the usual setup), localhost
        # on Plex's port is the most direct route; then whatever plex.tv reports.
        ports = list(dict.fromkeys([urlparse(u).port or 32400 for u in srv["urls"]] + [32400]))
        local = [f"http://127.0.0.1:{p}" for p in ports]
        for url in local + srv["urls"]:
            try:
                info = Plex(url, srv["token"], timeout=5).identity()
            except Exception:  # noqa: BLE001 - try the next connection
                continue
            # On shared hosts, localhost:32400 can be someone else's Plex.
            if srv.get("id") and info.get("id") and info["id"] != srv["id"]:
                continue
            chosen = (srv, url, info)
            break
        if chosen:
            break
    if not chosen:
        names = ", ".join(s["name"] for s in servers)
        sys.exit(f"Signed in, but couldn't reach a server from here (found: {names}). Pass --url manually.")
    srv, url, info = chosen
    if not srv.get("owned", True):
        print(f"warning: you don't own '{srv['name']}'; Plex only lets the owner's account add subtitles. "
              "Sign in with the server owner's Plex account instead.", flush=True)
    cfg = load_config()
    cfg.update({"url": url, "token": srv["token"]})
    path = save_config(cfg)
    print(f"Connected to '{info['name']}' (Plex {info['version']}) at {url}\nSaved to {path}")


def cmd_doctor(args):
    """Check everything dualsubs needs on this machine, and the Plex connection."""
    import platform
    import shutil

    ok = True

    def line(good, label, detail=""):
        nonlocal ok
        ok &= bool(good) or good is None
        mark = "ok  " if good else ("--  " if good is None else "FAIL")
        print(f"[{mark}] {label}" + (f": {detail}" if detail else ""))

    line(sys.version_info >= (3, 9), "Python", f"{platform.python_version()} ({sys.executable})")
    try:
        line(True, "ffmpeg", plexmod.find_ffmpeg() + " (for subtitles inside video files)")
    except PlexError as e:
        line(False, "ffmpeg", str(e))
    tools = [t for t in ("nice", "ionice") if shutil.which(t)]
    line(True if tools else None, "low-priority tools", ", ".join(tools) or "none; ffmpeg runs at normal priority")
    cfg = load_config()
    path = config_dir() / "config.json"
    if not (cfg.get("url") or os.environ.get("PLEX_URL")):
        line(False, "Plex connection", f"not set up; run `dualsubs login` (config: {path})")
    else:
        if path.exists():
            mode = path.stat().st_mode & 0o777
            line(mode == 0o600, "config file", f"{path} (mode {mode:o})")
        try:
            plex, settings, _ = build(args)
            info = plex.identity()
            line(True, "Plex", f"'{info['name']}' (Plex {info['version']}) at {plex.url}")
            libs = plex.libraries()
            line(bool(libs), "libraries", ", ".join(lib["title"] for lib in libs) or "none visible")
            mirrored = [p for p in settings.pairs if (p[1], p[0]) in settings.pairs and p[0] < p[1]]
            line(None if mirrored else True, "language pairs", ", ".join(pair_label(p) for p in settings.pairs)
                 + "".join(f"; {pair_label(p)} and {pair_label(p[::-1])} are the same two languages swapped, "
                           "you probably want only one" for p in mirrored))
        except (PlexError, OSError) as e:
            line(False, "Plex", str(e))
    print("All good." if ok else "Some checks failed; see above.")
    return 0 if ok else 1


def cmd_libraries(args):
    plex, _, _ = build(args)
    for lib in plex.libraries():
        print(f"{lib['id']:>4}  {lib['type']:<6} {lib['title']}")


def cmd_inspect(args):
    plex, settings, state = build(args)
    for k in resolve_targets(plex, args.target)[: args.limit]:
        item = plex.item(k)
        print(f"{item.title}  (ratingKey {k})  {item.parts[0].file if item.parts else ''}")
        if not item.subtitles:
            print("    no subtitle tracks")
        for s in item.subtitles:
            kind = "external" if s.external else f"embedded #{s.index}"
            flags = " ".join(f for f, on in (("forced", s.forced), ("SDH", s.hearing_impaired), ("image", not s.is_text)) if on)
            print(f"    [{s.id}] {s.raw_language or '?':<4} {s.codec:<8} {kind:<12} {flags:<14} {s.display or s.title}")


def cmd_check(args):
    """Show, without changing anything, what each pair can do for a title."""
    plex, settings, state = build(args)
    keys = resolve_targets(plex, args.target)
    labels = [pair_label(p) for p in settings.pairs]
    print(f"{'':<46}" + "".join(f"{lab:<16}" for lab in labels) + "subtitles")
    totals = {lab: {} for lab in labels}
    for k in keys:
        try:
            info = compatibility(plex.item(k), settings, state)
        except PlexError as e:
            print(f"{k}: {e}")
            continue
        cells = []
        for pair, lab in zip(settings.pairs, labels):
            x = info["pairs"]["+".join(pair)]
            totals[lab][x["status"]] = totals[lab].get(x["status"], 0) + 1
            cell = {"done": "done", "ready": "ready"}.get(x["status"], x["status"])
            if x["status"] == "missing":
                cell = "no " + "+".join(x["missing"])
                if any(v == "none" for v in x["searched"].values()):
                    cell += " (none found)"
            cells.append(f"{cell:<16}")
        langs = ",".join(info["languages"]) + (f" (image: {','.join(info['image_only'])})" if info["image_only"] else "")
        title = info["title"] if len(info["title"]) <= 44 else info["title"][:43] + "…"
        print(f"{title:<46}" + "".join(cells) + (langs or "none"))
    print()
    for lab, counts in totals.items():
        print(f"{lab}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print("\n'ready' items get their track on the next run. For 'no xx', "
          "`dualsubs add <title>` asks Plex's subtitle search for the missing language (on by default).")


def prioritize(plex: Plex, keys: list[str]) -> list[str]:
    """Order a big batch so what you're watching gets today's subtitle downloads first:
    shows on Continue Watching / watched lately, then recently added, then the rest."""
    try:
        first = plex.watched_first() + plex.recently_added(200)
    except PlexError:
        return keys
    wanted = set(keys)
    head = [k for k in dict.fromkeys(first) if k in wanted]
    seen = set(head)
    return head + [k for k in keys if k not in seen]


def cmd_add(args):
    plex, settings, state = build(args, lock=True)
    keys = resolve_targets(plex, args.target)
    if args.target == "all" or args.target.startswith("library:"):
        keys = prioritize(plex, keys)
    return run_items(plex, settings, state, keys, args, label=args.target)


def cmd_scan(args):
    args.target = "all"
    return cmd_add(args)


def cmd_remove(args):
    plex, settings, state = build(args, lock=True)
    pairs = settings.pairs if (args.pair or args.primary or args.secondary) else None
    # "all": only items we've touched (from the state file), not the whole library.
    keys = state.items() if args.target == "all" else resolve_targets(plex, args.target)
    for k in keys:
        try:
            print(remove_item(plex, k, state, pairs).line(), flush=True)
        except PlexError as e:
            print(f"[!] {k}: {e}", flush=True)


def cmd_watch(args):
    if args.once:
        return _watch_once(args)
    return _watch_daemon(args)


def _recent_pass(plex, settings, state, args, progress, gate, stop_for_streams: bool) -> bool:
    """One pass over recently added items. Returns False if it stopped early for viewers."""
    keys = plex.recently_added(args.batch)
    progress.start(len(keys))
    for k in keys:
        if stop_for_streams and gate.playing():
            progress.finish("waiting")
            return False
        for res in process_item(plex, k, settings, state):
            progress.working_on(res.title)
            progress.result(res)
            if res.status not in ("up-to-date", "unchanged"):
                print(time.strftime("%Y-%m-%d %H:%M ") + res.line(), flush=True)
        progress.item_done()
    state.flush()
    return True


def _watch_once(args):
    """From cron: check recently added items once."""
    plex, settings, state = build(args, lock=True)
    progress = Progress(config_dir() / "progress.json", "watch --once", "recently added")
    gate = StreamGate(plex, settings_yield(args), progress)
    if gate.playing():
        print(time.strftime("%Y-%m-%d %H:%M ") + f"skipped: {gate.playing()} Plex stream(s) playing", flush=True)
        return 0
    try:
        _recent_pass(plex, settings, state, args, progress, gate, stop_for_streams=True)
        progress.finish("finished")
    except (PlexError, OSError) as e:
        print(f"{time.strftime('%Y-%m-%d %H:%M')} [!] {e}", flush=True)
    return 0


def _watch_daemon(args):
    """Keep running: prepare what's being watched right away, and handle new items.

    - Every --tick seconds it looks at what's playing. An episode or movie that
      starts playing gets its bilingual tracks made immediately (fetching missing
      subtitles as needed), along with the next --ahead episodes of that show, so
      the following episodes are ready before they start.
    - Every --interval seconds it handles recently added items, stepping aside
      while anyone is watching.
    It doesn't take the library-run lock, so it runs next to a library run (they
    never work on the same item at once).
    """
    plex, settings, state = build(args, lock="daemon")
    labels = ", ".join(pair_label(p) for p in settings.pairs)
    print(f"Watching ({labels}): what's playing every {args.tick}s, new items every {args.interval}s. "
          "Ctrl-C to stop.", flush=True)
    if args.initial_scan:
        run_items(plex, settings, state, prioritize(plex, resolve_targets(plex, "all")), args, label="all")
    progress = Progress(config_dir() / "progress.json", "watch", "recently added")
    gate = StreamGate(plex, settings_yield(args), progress)
    prepared: dict[str, float] = {}  # ratingKey -> when we last made sure it's done
    next_recent = 0.0
    ticks = 0
    while not args.max_ticks or ticks < args.max_ticks:
        ticks += 1
        try:
            playing = plex.now_playing() if args.ahead >= 0 else []
            for s in playing:
                targets = [s["ratingKey"]]
                if s["type"] == "episode" and s["show"] and args.ahead:
                    targets += plex.next_episodes(s["show"], s["ratingKey"], args.ahead)
                for k in targets:
                    if time.time() - prepared.get(k, 0) < 600:
                        continue
                    for res in process_item(plex, k, settings, state):
                        if res.status not in ("up-to-date", "unchanged"):
                            tag = "now playing" if k == s["ratingKey"] else "coming up"
                            print(time.strftime("%Y-%m-%d %H:%M ") + f"({tag}) " + res.line(), flush=True)
                    prepared[k] = time.time()
            state.flush()
            if time.time() >= next_recent:
                if _recent_pass(plex, settings, state, args, progress, gate, stop_for_streams=True):
                    next_recent = time.time() + args.interval
                    progress.finish("waiting")
        except (PlexError, OSError) as e:
            print(f"{time.strftime('%Y-%m-%d %H:%M')} [!] {e}", flush=True)
            time.sleep(min(60, args.interval))
        if not args.max_ticks or ticks < args.max_ticks:
            time.sleep(args.tick)
    return 0


def cmd_web(args):
    from . import web

    cfg = load_config()
    key = web.ensure_key(cfg, save_config)

    def plex_factory():
        plex, settings, _ = build(args)
        return plex, settings

    portal = web.Portal(config_dir(), load_config, save_config, plex_factory)
    try:
        srv = web.serve(portal, key, args.host, args.port)
    except OSError as e:
        sys.exit(f"can't listen on {args.host}:{args.port} ({e}); pick another port with --port")
    print(f"dualsubs web is running. Open this link (it includes your access key):\n\n"
          f"  http://localhost:{args.port}/?key={key}\n\n"
          f"On another computer, tunnel the port over SSH first, e.g.\n"
          f"  ssh -L {args.port}:127.0.0.1:{args.port} plex-server '~/.local/bin/dualsubs web --port {args.port}'\n"
          f"Ctrl-C stops it.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def cmd_ui(args):
    """On your own computer: tunnel to the Plex server, start `dualsubs web` there, open the browser."""
    import re
    import signal
    import socket
    import subprocess
    import webbrowser

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    lport = args.local_port or free_port()
    rport = args.remote_port
    for _attempt in range(8):
        remote = f"{args.remote_cmd} web --port {rport}"
        cmd = ["ssh", "-tt", "-o", "ExitOnForwardFailure=yes", "-L", f"{lport}:127.0.0.1:{rport}", args.host, remote]
        print(f"Connecting to {args.host}...", flush=True)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, bufsize=1, start_new_session=True)
        seen, key, busy = [], None, False
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            seen.append(line)
            m = re.search(r"\?key=([\w-]+)", line)
            if m:
                key = m.group(1)
                break
            if "can't listen" in line:
                busy = True
                break
        if key:
            url = f"http://localhost:{lport}/?key={key}"
            print(f"dualsubs is open at {url}\n(Ctrl-C to close it)", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            try:
                for _ in proc.stdout:  # keep draining so the remote side never blocks
                    pass
                proc.wait()
                print("The connection to the server closed.")
                return 1
            except KeyboardInterrupt:
                print("\nClosing.")
                return 0
            finally:
                _stop(proc, signal)
        _stop(proc, signal)
        if busy:
            rport += 1  # someone else on the server uses that port: try the next one
            continue
        print("\n".join(seen[-15:]))
        hint = ("Is dualsubs installed on the server? Pass its path with --remote-cmd."
                if any("not found" in s or "No such file" in s for s in seen) else
                "Check that `ssh " + args.host + "` works on its own.")
        sys.exit(f"Couldn't start dualsubs web on {args.host}. {hint}")
    sys.exit(f"No free port on {args.host} between {args.remote_port} and {rport}; pass --remote-port.")


def _stop(proc, signal) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)  # ssh and anything it started locally
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


def cmd_merge(args):
    a = parse(Path(args.primary_file).read_bytes(), Path(args.primary_file).suffix[1:])
    b = parse(Path(args.secondary_file).read_bytes(), Path(args.secondary_file).suffix[1:])
    merged, stats = merge(a, b, style_from(args, {}))
    Path(args.output).write_text(write_srt(merged), encoding="utf-8")
    print(f"Wrote {args.output}: {stats.describe()}")


# ---------------------------------------------------------------------- parser

def main(argv=None):
    p = argparse.ArgumentParser(prog="dualsubs", description="Add bilingual subtitle tracks to your Plex library.")
    p.add_argument("--version", action="version", version=f"dualsubs {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    conn = argparse.ArgumentParser(add_help=False)
    conn.add_argument("--url", help="Plex server URL, e.g. http://127.0.0.1:32400 (env PLEX_URL)")
    conn.add_argument("--token", help="Plex token (env PLEX_TOKEN)")
    conn.add_argument("--state", help="state file (default ~/.config/dualsubs/state.json)")

    style = argparse.ArgumentParser(add_help=False)
    style.add_argument("--color", help="colour of the second language, e.g. '#FFD54F', or 'none'")
    style.add_argument("--italic", action="store_true", help="italicise the second language")
    style.add_argument("--secondary-on-top", action="store_true", help="put the second language above the first")
    style.add_argument("--compact", action="store_true", help="one line per language")

    work = argparse.ArgumentParser(add_help=False, parents=[conn, style])
    work.add_argument("--pair", action="append", metavar="EN+ES",
                      help="language pair, main language first; repeat (or comma-separate) for several, "
                           "each becomes its own track (default nl+en, or 'pairs' in the config)")
    work.add_argument("-p", "--primary", help="shorthand for a single pair: main language")
    work.add_argument("-s", "--secondary", help="shorthand for a single pair: second language")
    work.add_argument("--mode", choices=["upload", "sidecar"], help="upload into Plex (default) or write a .srt next to the video")
    work.add_argument("--path-map", action="append", metavar="PLEX_PATH=LOCAL_PATH", help="translate media paths if this machine mounts them elsewhere")
    work.add_argument("--ffmpeg", help="path to ffmpeg (or Plex's 'Plex Transcoder')")
    work.add_argument("--priority", choices=["low", "idle", "normal"],
                      help="CPU/disk priority for reading video files: low (default) yields to Plex and seeding, "
                           "idle only reads when the disk is otherwise unused, normal is full speed")
    work.add_argument("--no-fetch", action="store_true",
                      help="don't ask Plex's subtitle search for missing languages (it does by default, "
                           "within a daily limit; see --fetch-limit)")
    work.add_argument("--fetch-missing", action="store_true", help=argparse.SUPPRESS)  # default since 0.5
    work.add_argument("--fetch-limit", type=int, metavar="N",
                      help="max subtitle downloads per day (default 50)")
    work.add_argument("--ignore-streams", action="store_true",
                      help="don't pause while someone is watching on Plex (default: pause)")
    work.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, change nothing")
    work.add_argument("-f", "--force", action="store_true", help="redo items already done; add even when tracks barely line up")
    work.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("login", help="sign in to Plex in your browser and pick a server")
    s.add_argument("--server", help="server name, if your account has several")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("doctor", parents=[conn], help="check this machine and the Plex connection")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("libraries", parents=[conn], help="list movie and TV libraries")
    s.set_defaults(fn=cmd_libraries)

    s = sub.add_parser("inspect", parents=[work], help="show the subtitle tracks Plex knows about")
    s.add_argument("target", help="title, ratingKey, library:NAME or all")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_inspect)

    s = sub.add_parser("check", parents=[work], help="show what each pair can do for a title; changes nothing")
    s.add_argument("target", help="title, ratingKey, library:NAME or all")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("add", parents=[work], help="add bilingual tracks to a movie, show, season or library")
    s.add_argument("target", help="title, ratingKey, library:NAME or all")
    s.add_argument("--save", metavar="FILE.srt", help="also save the merged subtitles locally (single item)")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("scan", parents=[work], help="process every movie and episode")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("watch", parents=[work], help="keep running; process newly added items")
    s.add_argument("--interval", type=int, default=600, help="seconds between checks (default 600)")
    s.add_argument("--batch", type=int, default=100, help="how many recently added items to check")
    s.add_argument("--initial-scan", action="store_true", help="process the whole library first")
    s.add_argument("--once", action="store_true",
                   help="check recently added items once and exit (use this from cron)")
    s.add_argument("--max-ticks", type=int, default=0, help=argparse.SUPPRESS)  # tests
    s.add_argument("--tick", type=int, default=15, help="seconds between checks of what's playing (default 15)")
    s.add_argument("--ahead", type=int, default=3,
                   help="when an episode plays, also prepare this many next episodes (default 3)")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("remove", parents=[work], help="remove the bilingual tracks dualsubs added")
    s.add_argument("target")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("web", parents=[conn], help="status page: coverage, progress, language pairs")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this machine only)")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("ui", help="on your computer: open the server's status page through SSH",
                       description="Run this on your own computer. It connects to HOST over SSH, starts "
                                   "`dualsubs web` there, tunnels it and opens your browser.")
    s.add_argument("host", help="your SSH shortcut for the Plex server, e.g. plex-server")
    s.add_argument("--remote-cmd", default="~/.local/bin/dualsubs", help="dualsubs on the server (default %(default)s)")
    s.add_argument("--remote-port", type=int, default=8765, help="port on the server (tries the next ones if taken)")
    s.add_argument("--local-port", type=int, help="port on this computer (default: any free one)")
    s.add_argument("--no-browser", action="store_true", help="just print the link")
    s.set_defaults(fn=cmd_ui)

    s = sub.add_parser("merge", parents=[style], help="merge two subtitle files offline (no Plex)")
    s.add_argument("primary_file")
    s.add_argument("secondary_file")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(fn=cmd_merge)

    args = p.parse_args(argv)
    try:
        return args.fn(args) or 0
    except PlexError as e:
        sys.exit(f"error: {e}")
    except ValueError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        return 130


def run() -> None:
    """Entry point for the single-file build (zipapp ignores main()'s return value)."""
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
