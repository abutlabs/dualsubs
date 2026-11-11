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

## Install

dualsubs runs **on your Plex server**: a seedbox, NAS or home server. It has to
run there because subtitles are often embedded inside the video files, and
those only exist on that machine. You don't need root or Docker. The server
needs Python 3.9+, git, and ffmpeg (or Plex's own transcoder, which dualsubs
uses automatically when ffmpeg is missing).

You install and run it from your own computer over SSH, so you never have to
work on the server directly.

### 1. Set up an SSH shortcut to your Plex server (once)

If `ssh plex-server` already logs you in without a password, skip ahead.
Replace everything in `<angle brackets>` with your own values.

```bash
# On your computer: make a key (skip if you have ~/.ssh/id_ed25519)
ssh-keygen -t ed25519 -C "<a-label-for-this-key>"

# Put the public key on the server (asks for the server password once)
ssh-copy-id -i ~/.ssh/id_ed25519.pub <username>@<server-hostname>
```

Add a shortcut to `~/.ssh/config` on your computer, then `chmod 600 ~/.ssh/config`:

```
Host plex-server
    HostName <server-hostname>
    User <username>
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    AddKeysToAgent yes
    UseKeychain yes        # macOS only: remembers the key's passphrase
```

Check it with `ssh plex-server`. Some seedbox providers make you turn on SSH or
key login in their control panel first. If the login fails, `ssh -v plex-server`
shows why. Only the `.pub` file ever goes on the server; never copy the private
key anywhere.

The rest of this guide uses `plex-server`. If your shortcut has another name,
use that instead.

### 2. Install dualsubs on the server

```bash
ssh plex-server 'git clone https://github.com/abutlabs/dualsubs.git ~/dualsubs && ~/dualsubs/install.sh'
```

The installer puts dualsubs in your home folder (`~/.local/share/dualsubs`, with
the command at `~/.local/bin/dualsubs`). It also checks for ffmpeg and tells you
if anything is missing.

### 3. Connect it to Plex and choose your languages

```bash
ssh -t plex-server '~/.local/bin/dualsubs login'
```

This prints a link. Open it in the browser on your computer, sign in to Plex
and approve dualsubs. It then finds your server and saves the connection on the
server in `~/.config/dualsubs/config.json`, readable only by you. Check it worked:

```bash
ssh plex-server '~/.local/bin/dualsubs libraries'
```

Then set your language pairs (see [Language pairs](#language-pairs)) **before
the first real run**. Without this, the default is Dutch + English. Edit the
config, keep the `url`/`token` lines that `login` wrote, and add a `pairs` line:

```bash
ssh -t plex-server 'nano ~/.config/dualsubs/config.json'
#   "pairs": ["en+es"]
```

### 4. Try it on one title

```bash
ssh plex-server '~/.local/bin/dualsubs inspect "<a show or movie>"'      # what's there
ssh plex-server '~/.local/bin/dualsubs add "<a show or movie>" --dry-run'  # preview
ssh plex-server '~/.local/bin/dualsubs add "<a show or movie>"'            # do it
```

Play it on your TV and pick the dualsubs track.

### 5. Fill in your existing library

Do this **one library at a time**. To read subtitles that are embedded in a
video, ffmpeg has to read through the whole video file. dualsubs only reads
each file once, however many tracks and pairs it needs from it, but the first
pass over a large library is still a lot of disk reading. Run it in tmux so it
keeps going after you disconnect, and log the output:

```bash
ssh plex-server 'tmux new -d -s dualsubs "~/.local/bin/dualsubs add library:<Name> >> ~/dualsubs.log 2>&1"'
ssh plex-server 'tail -f ~/dualsubs.log'        # watch progress; Ctrl-C to stop watching
```

When it finishes, do the next library. Items that are already done are skipped
cheaply, so it's safe to re-run.

### 6. Handle new items automatically

Add a cron job that checks recently added items every 15 minutes. Unlike tmux,
cron survives server reboots. Open the crontab with `ssh -t plex-server 'crontab -e'`
and add:

```
*/15 * * * *  $HOME/.local/bin/dualsubs watch --once >> $HOME/dualsubs.log 2>&1
```

`watch --once` only looks at recently added items, so each run is quick. Use it
rather than `scan`, which checks every item every time. Only one dualsubs run
works at a time. If cron fires while a long run from step 5 is still going, it
logs "skipped" and tries again next time, so the two never overwrite each
other's progress.

(If cron isn't available, `dualsubs watch` keeps running and checks every 10
minutes, for example in tmux. That stops if the server reboots.)

### Update

```bash
ssh plex-server 'cd ~/dualsubs && git pull && ./install.sh'
```

Cron picks up the new version on its next run. If you have a `watch` running in
tmux, restart it.

### Uninstall

```bash
ssh plex-server '~/.local/bin/dualsubs remove all'   # take the tracks out of Plex first
ssh -t plex-server 'crontab -e'                       # delete the dualsubs line
ssh plex-server 'tmux kill-session -t dualsubs 2>/dev/null; rm -rf ~/dualsubs ~/.local/share/dualsubs ~/.local/bin/dualsubs ~/.config/dualsubs ~/dualsubs.log'
```

### Running it directly on the Plex machine

Clone the repo, run `./install.sh` and use `dualsubs ...` without the `ssh`
prefix. `dualsubs login` works the same way.

## Language pairs

A pair is written main language first, e.g. `en+es` means English on top and
Spanish underneath. The default is `nl+en`. You can have as many pairs as you
like; each one becomes its own subtitle track on every item, so you pick
"EN+ES" or "EN+FR" on the TV depending on who's watching.

Set them once in `~/.config/dualsubs/config.json` **on the server**
(`ssh plex-server 'nano ~/.config/dualsubs/config.json'`):

```json
{ "pairs": ["en+es", "en+fr", "nl+en"], "style": { "secondary_color": "#FFD54F" } }
```

or per command with `--pair en+es --pair en+fr` (also `--pair en+es,en+fr`), or
the `DUALSUBS_PAIRS=en+es,en+fr` environment variable. Language names work too:
`--pair English+Spanish`. An item only gets a pair's track if it has subtitles
in both languages; otherwise dualsubs reports which one is missing.

## Commands

Run these on the server, i.e. as `ssh plex-server '~/.local/bin/dualsubs ...'`.
If you'll use it a lot, an alias on your computer saves typing:
`alias dualsubs="ssh plex-server '~/.local/bin/dualsubs'"`. After that you can
run it from your own terminal, e.g. `dualsubs add '"The Bear"'`. Titles with
spaces need both quote types like that, because they pass through two shells.

```bash
dualsubs inspect "The Bear"          # what subtitle tracks does Plex have?
dualsubs add "The Bear" --dry-run    # what would happen, changing nothing
dualsubs add "The Bear"              # whole show (or a movie, season, ratingKey)
dualsubs add library:Movies          # a whole library
dualsubs scan                        # everything
dualsubs watch --once                # check recently added items once (for cron)
dualsubs watch                       # keep running; handle new items every 10 min
dualsubs remove "The Bear"           # take all bilingual tracks off again
dualsubs remove "The Bear" --pair en+fr   # ...or just one pair
dualsubs remove all                  # every track dualsubs ever added
dualsubs merge nl.srt en.srt -o both.srt   # offline, no Plex needed
```

On the TV, start playback, open subtitles and pick the track for your pair,
e.g. **"dualsubs EN+ES"**. The exact label depends on your Plex version; `add`
prints what Plex calls it.

## Style options

`--color '#80DEEA'` (or `none`), `--italic`, `--secondary-on-top`, and
`--compact` (one line per language, good for long sentences). Use `--force` to
redo items with the new style.

## If Plex and dualsubs see the media at different paths

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
