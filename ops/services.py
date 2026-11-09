"""Minimal background-process manager for Colab (no systemd, no supervisor).

Each service writes a pidfile under /content/runtime/run and logs to
$DRIVE_ROOT/logs/<name>.log so logs survive a runtime reset.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .env import DRIVE_ROOT, RUNTIME, child_env


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # zombie check (Linux)
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def http_ok(url: str, headers: dict | None = None, timeout: float = 3) -> bool:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


@dataclass
class Service:
    name: str
    cmd: list[str]
    health: Callable[[], bool] | None = None
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    user: str | None = None          # run as this user (e.g. postgres) via runuser

    @property
    def pidfile(self) -> Path:
        return RUNTIME / "run" / f"{self.name}.pid"

    @property
    def logfile(self) -> Path:
        return DRIVE_ROOT / "logs" / f"{self.name}.log"

    def pid(self) -> int | None:
        try:
            pid = int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if _alive(pid) else None

    def running(self) -> bool:
        return self.pid() is not None

    def start(self, wait_s: float = 120) -> None:
        if self.running():
            print(f"[{self.name}] already running (pid {self.pid()})")
        else:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            log = open(self.logfile, "ab", buffering=0)
            log.write(f"\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
            cmd = (["runuser", "-u", self.user, "--"] + self.cmd) if self.user else self.cmd
            p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=child_env(self.env), cwd=self.cwd, start_new_session=True)
            self.pidfile.write_text(str(p.pid))
            print(f"[{self.name}] started pid {p.pid}, log {self.logfile}")
        if self.health is None:
            return
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.health():
                print(f"[{self.name}] healthy")
                return
            if not self.running():
                raise RuntimeError(f"[{self.name}] exited during startup; see {self.logfile}:\n" + self.tail(30))
            time.sleep(1)
        raise TimeoutError(f"[{self.name}] not healthy after {wait_s}s; see {self.logfile}:\n" + self.tail(30))

    def stop(self, timeout_s: float = 20) -> None:
        pid = self.pid()
        if pid is None:
            print(f"[{self.name}] not running")
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + timeout_s
        while time.time() < deadline and _alive(pid):
            time.sleep(0.5)
        if _alive(pid):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        self.pidfile.unlink(missing_ok=True)
        print(f"[{self.name}] stopped")

    def tail(self, n: int = 40) -> str:
        try:
            return "\n".join(self.logfile.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return "(no log)"
