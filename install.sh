#!/bin/sh
# Install or update dualsubs for the current user, on the machine where Plex runs.
# No root needed. Run from a clone of the repo:  ./install.sh
set -eu

REPO_DIR=$(cd "$(dirname "$0")" && pwd)
PREFIX=${DUALSUBS_PREFIX:-$HOME/.local/share/dualsubs}
BIN_DIR=${DUALSUBS_BIN:-$HOME/.local/bin}

say() { printf '%s\n' "$*"; }
fail() { say "error: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 is not installed"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || fail "Python 3.9 or newer is needed (found $(python3 -V 2>&1))"

mkdir -p "$BIN_DIR"
if python3 -m venv ${DUALSUBS_VENV_ARGS:-} "$PREFIX/venv" >/dev/null 2>&1 && [ -x "$PREFIX/venv/bin/pip" ]; then
  "$PREFIX/venv/bin/pip" install --quiet --upgrade "$REPO_DIR"
  ln -sf "$PREFIX/venv/bin/dualsubs" "$BIN_DIR/dualsubs"
  say "Installed dualsubs into $PREFIX/venv"
else
  # No venv module (common on minimal servers): fall back to a user install.
  rm -rf "$PREFIX/venv"
  python3 -m pip install --quiet --user --upgrade "$REPO_DIR" 2>/dev/null \
    || python3 -m pip install --quiet --user --upgrade --break-system-packages "$REPO_DIR" \
    || fail "pip could not install dualsubs (is pip available? try: python3 -m ensurepip --user)"
  say "Installed dualsubs with pip --user"
fi

"$BIN_DIR/dualsubs" --version >/dev/null || fail "install finished but $BIN_DIR/dualsubs does not run"
say "Command: $BIN_DIR/dualsubs"

# ffmpeg is only needed for subtitles embedded in video files.
if command -v ffmpeg >/dev/null 2>&1; then
  say "ffmpeg: $(command -v ffmpeg)"
else
  found=""
  for t in "/usr/lib/plexmediaserver/Plex Transcoder" "$HOME"/*/plexmediaserver/"Plex Transcoder" \
           "$HOME"/.local/*/plexmediaserver/"Plex Transcoder"; do
    [ -x "$t" ] && found=$t && break
  done
  if [ -n "$found" ]; then
    say "ffmpeg: not installed; will use Plex's own transcoder ($found)"
    say "  (if it complains, set: dualsubs ... --ffmpeg \"$found\")"
  else
    say "warning: no ffmpeg found. Subtitles inside video files can't be read until one is"
    say "  available; separate .srt files still work. Point to one with --ffmpeg PATH."
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "note: $BIN_DIR is not on your PATH; use the full path above, or add it to ~/.profile" ;;
esac
