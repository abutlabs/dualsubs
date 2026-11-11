"""Live progress of the current run, for `dualsubs web` (and anyone curious).

Written to ~/.config/dualsubs/progress.json, at most every couple of seconds.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class Progress:
    EVERY_S = 2.0
    KEEP = 15  # recent results to show

    def __init__(self, path: Path | None, command: str, target: str = ""):
        self.path = path
        self.data = {
            "pid": os.getpid(), "command": command, "target": target, "state": "running",
            "started": time.time(), "updated": time.time(), "total": 0, "done": 0,
            "current": "", "paused_streams": 0, "counts": {}, "recent": [],
        }
        self._written = 0.0
        prev = read(path) if path else None
        if prev:
            self.data["recent"] = prev.get("recent", [])  # keep history across runs

    def start(self, total: int, target: str | None = None) -> None:
        self.data.update(total=total, done=0, state="running", counts={})
        if target is not None:
            self.data["target"] = target
        self.write(force=True)

    def working_on(self, title: str) -> None:
        self.data["current"] = title
        self.write()

    def result(self, res) -> None:
        c = self.data["counts"]
        c[res.status] = c.get(res.status, 0) + 1
        if res.status not in ("up-to-date", "unchanged"):
            self.data["recent"] = ([{"at": time.time(), "line": res.line()}] + self.data["recent"])[: self.KEEP]
        self.write()

    def item_done(self) -> None:
        self.data["done"] += 1
        self.write()

    def paused(self, streams: int) -> None:
        self.data.update(state="paused" if streams else "running", paused_streams=streams)
        self.write(force=True)

    def finish(self, state: str = "finished") -> None:
        self.data.update(state=state, current="")
        self.write(force=True)

    def write(self, force: bool = False) -> None:
        if not self.path or (not force and time.time() - self._written < self.EVERY_S):
            return
        self.data["updated"] = self._written = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".progress")
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass  # progress is a nicety; never let it stop the real work


def read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if data.get("state") in ("running", "paused") and not _alive(data.get("pid")):
        data["state"] = "stopped"  # killed, crashed or the server rebooted mid-run
    return data


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False
