"""One running engine per ledger.

Two processes trading the same ledger (a double-clicked launcher, a second `tradebot run` while `run_paper.bat` is
looping, an update script started next to a running bot) would read the same book, each place orders against it and
each book their own fills: duplicate orders and a double-counted position. An operating-system file lock next to the
ledger file prevents that. The lock is released automatically when the process exits, including on a crash, so a
stale lock never blocks a restart (unlike a PID file).

Windows uses msvcrt.locking, POSIX uses fcntl.flock. Both are non-blocking: the second process fails immediately.
"""
from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            holder = ""
            try:
                holder = self.path.read_text().strip().split()[0]
            except (OSError, IndexError):
                pass
            raise AlreadyRunningError(f"another process already holds {self.path}"
                                      + (f" (pid {holder})" if holder else "") + "; stop it first") from e
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".ljust(24).encode())
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def lock_for(ledger_path: str | Path) -> InstanceLock:
    p = Path(ledger_path)
    return InstanceLock(p.with_name(p.name + ".lock"))
