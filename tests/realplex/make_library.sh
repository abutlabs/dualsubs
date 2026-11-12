#!/bin/sh
# Build a tiny Plex library for end-to-end tests: real video files with subtitles
# embedded and alongside, in a few languages. Usage: make_library.sh DIR
set -eu
D=${1:?media dir}
SRC=$(cd "$(dirname "$0")" && pwd)/subs
rm -rf "$D" && mkdir -p "$D/Movies/Koffie (2024)" "$D/TV/Test Show (2024)/Season 01"

video() {  # video OUT [srt lang]...
  out=$1; shift
  set -- "$@"
  inputs="" maps="" meta="" i=2 s=0
  while [ $# -gt 0 ]; do
    inputs="$inputs -i $SRC/$1.srt"; maps="$maps -map $i"
    meta="$meta -metadata:s:s:$s language=$2"; i=$((i+1)); s=$((s+1)); shift 2
  done
  # shellcheck disable=SC2086
  ffmpeg -v error -y -f lavfi -i "color=c=black:s=320x180:d=40,format=yuv420p" -f lavfi -i "anullsrc=r=48000:cl=stereo" \
    $inputs -map 0:v -map 1:a $maps -t 40 -c:v libx264 -preset ultrafast -c:a aac -c:s srt $meta -shortest "$out"
}

# Movie: Dutch embedded, English next to it.
video "$D/Movies/Koffie (2024)/Koffie (2024).mkv" nl dut
cp "$SRC/en.srt" "$D/Movies/Koffie (2024)/Koffie (2024).en.srt"
# Show: E01 English + Spanish embedded; E02 English only; E03 English embedded, Dutch alongside.
S="$D/TV/Test Show (2024)/Season 01"
video "$S/Test Show (2024) - S01E01.mkv" en eng es spa
video "$S/Test Show (2024) - S01E02.mkv" en eng
video "$S/Test Show (2024) - S01E03.mkv" en eng
cp "$SRC/nl.srt" "$S/Test Show (2024) - S01E03.nl.srt"
find "$D" -type f | sort
