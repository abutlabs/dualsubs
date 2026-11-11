"""dualsubs command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, plex as plexmod
from .align import Style, merge
from .pipeline import Settings, State, looks_like_key, pair_label, process_item, remove_item
from .plex import Plex, PlexError
from .subtitles import parse, write_srt

def config_dir() -> Path:
    return Path(os.environ.get("DUALSUBS_HOME") or Path.home() / ".config" / "dualsubs")


def load_config() -> dict:
    p = config_dir() / "config.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_config(cfg: dict) -> Path:
    config_dir().mkdir(parents=True, exist_ok=True)
    p = config_dir() / "config.json"
    p.write_text(json.dumps(cfg, indent=2))
    p.chmod(0o600)  # holds the Plex token
    return p


def _opt(args, name, env, cfg, default=None):
    val = getattr(args, name, None)
    if val not in (None, []):
        return val
    if os.environ.get(env):
        return os.environ[env]
    return cfg.get(name, default)


def build(args):
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
    settings = Settings(
        pairs=pairs_from(args, cfg),
        style=style_from(args, cfg),
        mode=_opt(args, "mode", "DUALSUBS_MODE", cfg, "upload"),
        path_map=path_map,
    )
    state = State(_opt(args, "state", "DUALSUBS_STATE", cfg) or config_dir() / "state.json")
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


def run_items(plex, settings, state, keys, args) -> int:
    counts: dict[str, int] = {}
    for k in keys:
        for res in process_item(plex, k, settings, state, dry_run=args.dry_run, force=args.force,
                                save_to=getattr(args, "save", None)):
            counts[res.status] = counts.get(res.status, 0) + 1
            if res.status != "up-to-date" or args.verbose:
                print(res.line(), flush=True)
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
        for url in srv["urls"]:
            try:
                chosen = (srv, url, Plex(url, srv["token"], timeout=5).identity())
                break
            except Exception:  # noqa: BLE001 - try the next connection
                continue
        if chosen:
            break
    if not chosen:
        names = ", ".join(s["name"] for s in servers)
        sys.exit(f"Signed in, but couldn't reach a server from here (found: {names}). Pass --url manually.")
    srv, url, info = chosen
    cfg = load_config()
    cfg.update({"url": url, "token": srv["token"]})
    path = save_config(cfg)
    print(f"Connected to '{info['name']}' (Plex {info['version']}) at {url}\nSaved to {path}")


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


def cmd_add(args):
    plex, settings, state = build(args)
    keys = resolve_targets(plex, args.target)
    return run_items(plex, settings, state, keys, args)


def cmd_scan(args):
    args.target = "all"
    return cmd_add(args)


def cmd_remove(args):
    plex, settings, state = build(args)
    pairs = settings.pairs if (args.pair or args.primary or args.secondary) else None
    for k in resolve_targets(plex, args.target):
        print(remove_item(plex, k, state, pairs).line())


def cmd_watch(args):
    plex, settings, state = build(args)
    labels = ", ".join(pair_label(p) for p in settings.pairs)
    print(f"Watching for new items every {args.interval}s ({labels}). Ctrl-C to stop.", flush=True)
    if args.initial_scan:
        run_items(plex, settings, state, resolve_targets(plex, "all"), args)
    while True:
        try:
            keys = plex.recently_added(args.batch)
            for k in keys:
                for res in process_item(plex, k, settings, state):
                    if res.status != "up-to-date":
                        print(time.strftime("%Y-%m-%d %H:%M ") + res.line(), flush=True)
        except (PlexError, OSError) as e:
            print(f"{time.strftime('%Y-%m-%d %H:%M')} [!] {e}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


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
    work.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, change nothing")
    work.add_argument("-f", "--force", action="store_true", help="redo items already done; add even when tracks barely line up")
    work.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("login", help="sign in to Plex in your browser and pick a server")
    s.add_argument("--server", help="server name, if your account has several")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("libraries", parents=[conn], help="list movie and TV libraries")
    s.set_defaults(fn=cmd_libraries)

    s = sub.add_parser("inspect", parents=[work], help="show the subtitle tracks Plex knows about")
    s.add_argument("target", help="title, ratingKey, library:NAME or all")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_inspect)

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
    s.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("remove", parents=[work], help="remove the bilingual tracks dualsubs added")
    s.add_argument("target")
    s.set_defaults(fn=cmd_remove)

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


if __name__ == "__main__":
    sys.exit(main())
