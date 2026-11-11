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

### 3. Connect it to Plex

```bash
ssh -t plex-server '~/.local/bin/dualsubs login'
```

This prints a link. Open it in the browser on your computer, sign in to Plex
and approve dualsubs. It then finds your server and saves the connection on the
server in `~/.config/dualsubs/config.json`, readable only by you. Check it worked:

```bash
ssh plex-server '~/.local/bin/dualsubs libraries'
```

### 4. Try it on one show, then turn on automatic mode

```bash
ssh plex-server '~/.local/bin/dualsubs add "<a show or movie>" --dry-run'   # preview
ssh plex-server '~/.local/bin/dualsubs add "<a show or movie>"'             # do it
```

Play it on your TV and pick the dualsubs track. If you're happy, have the server
handle everything, including new episodes as they arrive:

```bash
ssh plex-server 'tmux new -d -s dualsubs "~/.local/bin/dualsubs watch --initial-scan"'
```

That keeps running after you disconnect. To look in on it, run
`ssh -t plex-server 'tmux attach -t dualsubs'` and press Ctrl-B then D to leave.
If your server has no tmux, use cron instead (`ssh -t plex-server 'crontab -e'`):

```
*/15 * * * *  $HOME/.local/bin/dualsubs scan >> $HOME/dualsubs.log 2>&1
```

### Update

```bash
ssh plex-server 'cd ~/dualsubs && git pull && ./install.sh'
```

If dualsubs is in `watch` mode, restart it afterwards:
`ssh plex-server 'tmux kill-session -t dualsubs'`, then start it again as in step 4.

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
dualsubs watch                       # keep running; handle new items every 10 min
dualsubs remove "The Bear"           # take all bilingual tracks off again
dualsubs remove "The Bear" --pair en+fr   # ...or just one pair
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
