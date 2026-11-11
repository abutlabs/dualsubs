"""`dualsubs ui HOST`: ssh tunnel + remote `dualsubs web` + browser, using a fake ssh."""
import os
import socket
import stat
import sys
import threading
import time

import pytest

from dualsubs import cli

FAKE_SSH = """#!/bin/sh
# fake ssh: ignore options and host, run the remote command locally
while [ $# -gt 1 ]; do shift; done
exec sh -c "$1"
"""


@pytest.fixture(autouse=True)
def watchdog():
    import signal

    def boom(*_):
        raise TimeoutError("dualsubs ui test hung")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(40)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("DUALSUBS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PYTHONPATH", str(__import__("pathlib").Path(__file__).parents[1]))
    return f"{sys.executable} -m dualsubs"


def test_ui_starts_remote_web_and_opens_browser(fake_ssh, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    # Take the default remote port so the first attempt fails and the next port is used.
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    port = busy.getsockname()[1]
    busy.listen()

    def interrupt_soon():
        for _ in range(100):
            if opened:
                break
            time.sleep(0.1)
        import signal
        os.kill(os.getpid(), signal.SIGINT)  # what Ctrl-C does

    threading.Thread(target=interrupt_soon, daemon=True).start()
    rc = cli.main(["ui", "plex-server", "--remote-cmd", fake_ssh, "--remote-port", str(port), "--local-port", "54321"])
    busy.close()
    assert rc == 0
    key = cli.load_config()["web_key"]
    assert opened == [f"http://localhost:54321/?key={key}"]


def test_ui_reports_missing_remote_install(fake_ssh, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["ui", "plex-server", "--remote-cmd", "/nonexistent/dualsubs", "--no-browser"])
    assert "Is dualsubs installed on the server?" in str(e.value)
