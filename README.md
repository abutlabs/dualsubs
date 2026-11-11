# dualsubs

Bilingual subtitles for Plex. Each movie or episode gets an extra subtitle track
with **both languages on screen at once**. The first language is on top and the
second is underneath in a different colour:

```
Waar is de koffie?
Ik heb hem nergens gezien.
Where's the coffee?                ← amber
I haven't seen it anywhere.        ← amber
```

Nothing needs installing on your TV or phone. The track is an ordinary Plex
subtitle track, so it shows up in every Plex app (Android TV / NVIDIA Shield,
phone, web), and playback, resume and remote control keep working exactly as
before. Plex remembers your subtitle choice per show.

## How it works

1. dualsubs asks your Plex server which subtitle tracks an item has. They can be
   embedded in the `.mkv`, `.srt` files next to the video, or subtitles Plex
   downloaded itself.
2. It picks the best text track for each language. It prefers full subtitles
   over forced ones and regular ones over SDH. Image-based PGS/VobSub tracks
   can't be merged as text.
3. It **re-syncs** the second track to the first. Two subtitle files are often
   from different releases: offset by seconds, or timed for 25 fps PAL versus
   23.976 fps. dualsubs detects and corrects both.
4. It pairs lines by overlap, so it copes when one language splits a sentence in
   two and the other doesn't. It then uploads the merged track back into Plex.
   Your media folders aren't touched.
5. It remembers what it did, so re-runs skip finished items, and it refreshes an
   item when its source subtitles change.

## Install (on the machine where Plex's media lives, e.g. a seedbox)

dualsubs needs Python 3.9+ and `ffmpeg`. ffmpeg is used to read subtitles that
are embedded in video files. If the machine has no ffmpeg, dualsubs falls back
to Plex's own "Plex Transcoder", or you can point `--ffmpeg` at any ffmpeg
binary. No root or Docker needed.

```bash
git clone https://github.com/abutlabs/dualsubs.git
cd dualsubs
python3 -m pip install --user .        # puts `dualsubs` in ~/.local/bin
```

## Set up

```bash
dualsubs login            # prints a link: sign in to Plex, approve, done
dualsubs libraries        # check it can see your libraries
```

`login` saves your server address and token in `~/.config/dualsubs/config.json`
(readable only by you). You can also use `--url/--token` or
`PLEX_URL`/`PLEX_TOKEN` instead.

### Language pairs

A pair is written main language first, e.g. `en+es` means English on top and
Spanish underneath. The default is `nl+en`. You can have as many pairs as you
like; each one becomes its own subtitle track on every item, so you pick
"EN+ES" or "EN+FR" on the TV depending on who's watching.

Set them once in `~/.config/dualsubs/config.json`:

```json
{ "pairs": ["en+es", "en+fr", "nl+en"], "style": { "secondary_color": "#FFD54F" } }
```

or per command with `--pair en+es --pair en+fr` (also `--pair en+es,en+fr`), or
the `DUALSUBS_PAIRS=en+es,en+fr` environment variable. Language names work too:
`--pair English+Spanish`. An item only gets a pair's track if it has subtitles
in both languages; otherwise dualsubs reports which one is missing.

## Use

```bash
dualsubs inspect "The Bear"          # what subtitle tracks does Plex have?
dualsubs add "The Bear" --dry-run    # what would happen, changing nothing
dualsubs add "The Bear"              # whole show (or a movie, season, ratingKey)
dualsubs add library:Movies          # a whole library
dualsubs scan                        # everything
dualsubs watch                       # keep running; handle new items every 10 min
dualsubs remove "The Bear"           # take all bilingual tracks off again
dualsubs remove "The Bear" --pair en+fr   # ...or just one pair
dualsubs merge nl.srt en.srt -o both.srt   # offline, no Plex needed
```

On the TV, start playback, open subtitles and pick the track for your pair,
e.g. **"dualsubs EN+ES"**. The exact label depends on your Plex version; `add`
prints what Plex calls it.

### Keep it running on a seedbox

```bash
tmux new -s dualsubs 'dualsubs watch --initial-scan'
# or from cron, every 15 minutes:
# */15 * * * *  $HOME/.local/bin/dualsubs scan >> $HOME/dualsubs.log 2>&1
```

### Style options

`--color '#80DEEA'` (or `none`), `--italic`, `--secondary-on-top`, and
`--compact` (one line per language, good for long sentences). Use `--force` to
redo items with the new style.

### If Plex and dualsubs see the media at different paths

For example, when Plex runs in Docker: `--path-map /data/media=/home/me/media`
(or `DUALSUBS_PATH_MAP`).

## Status

This is a prototype, built and tested against a simulated Plex server and real
ffmpeg-made `.mkv` files. Still to confirm on a real server:

- how Plex labels the uploaded track in its subtitle menu. With several pairs
  that share a main language (EN+ES, EN+FR), the label must show the track's
  title, not just "English", to tell them apart. If it doesn't, change the
  `filename` template in the config;
- that the Plex app on Android TV renders the coloured second line. If it
  doesn't, use `--color none --italic`;
- `--mode sidecar` (writes a `.srt` next to the video instead of uploading).

## Development

```bash
python3 -m pytest -q
```
