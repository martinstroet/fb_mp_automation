"""PID lockfile so cycle and digest runs never overlap."""

from __future__ import annotations

import os
import time
from pathlib import Path

# A worst-case legitimate cycle (pre-sleep + searches + two claude calls with
# retries + detail fetches) can run ~30 min, so age alone must not make a lock
# stealable while its PID is alive — that would put two Chromes on one profile.
# Dead PIDs are the real staleness signal; the age check is only a backstop for
# PID reuse / processes we can't signal.
STALE_SECONDS = 2 * 3600


class AlreadyRunning(Exception):
    pass


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def _read(self) -> tuple[int, float] | None:
        try:
            pid_s, ts_s = self.path.read_text().split()
            return int(pid_s), float(ts_s)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode())
                os.close(fd)
                self.acquired = True
                return
            except FileExistsError:
                info = self._read()
                stale = info is None or not self._alive(info[0]) or (
                    time.time() - info[1] > STALE_SECONDS
                )
                if stale:
                    self.path.unlink(missing_ok=True)
                    continue
                raise AlreadyRunning(f"another run holds {self.path} (pid {info[0]})")
        raise AlreadyRunning(f"could not acquire {self.path}")

    def release(self):
        if self.acquired:
            info = self._read()
            if info is None or info[0] == os.getpid():  # never unlink a stolen lock
                self.path.unlink(missing_ok=True)
            self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
