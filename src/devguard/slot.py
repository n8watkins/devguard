"""slot: run a heavy command only while one of N global slots is free.

Each slot is a lock file held with flock(2). Once a slot is taken, devguard
exec()s the command in place, keeping the locked file descriptor open. That means:
  - the command's exit code and signals are its own (nothing in between to forward);
  - the slot stays taken while the command or any child that inherited the
    descriptor is still running, and is freed by the kernel when they all exit,
    even after kill -9. There is no pid file to go stale and no pgrep polling.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config

EX_TEMPFAIL = 75


def slot_dir(config: Config) -> Path:
    if config.slot.dir:
        return Path(os.path.expanduser(config.slot.dir))
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return Path(runtime) / "devguard-slots"
    return Path("/tmp") / f"devguard-slots-{os.getuid()}"


def slot_count(config: Config, explicit: int | None) -> int:
    if explicit:
        return explicit
    env = os.environ.get("DEVGUARD_SLOTS")
    if env:
        try:
            value = int(env)
            if value >= 1:
                return value
        except ValueError:
            pass
        raise ValueError(f"DEVGUARD_SLOTS must be a positive whole number, got {env!r}")
    return config.slot.count


def ensure_dir(path: Path) -> None:
    """Create the lock directory privately and refuse one that someone else controls."""
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700, parents=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"{path} is not a plain directory")
    if info.st_uid != os.getuid():
        raise PermissionError(f"{path} belongs to another user")


def open_lock(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def try_acquire(directory: Path, count: int) -> tuple[int, int] | None:
    """(slot number, locked fd) for the first free slot, or None."""
    for number in range(1, count + 1):
        fd = open_lock(directory / f"slot-{number}.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                continue
            raise
        return number, fd
    return None


def record_holder(fd: int, command: list[str]) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()} {time.time():.0f} {' '.join(command)}\n".encode())


def status(directory: Path, count: int) -> list[tuple[int, str | None]]:
    """(slot, holder description or None when free) for slots 1..count."""
    rows = []
    for number in range(1, count + 1):
        path = directory / f"slot-{number}.lock"
        if not path.exists():
            rows.append((number, None))
            continue
        fd = open_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            rows.append((number, None))
        except OSError:
            holder = os.pread(fd, 4096, 0).decode(errors="replace").strip()
            rows.append((number, holder or "busy"))
        finally:
            os.close(fd)
    return rows


def acquire(
    directory: Path,
    count: int,
    timeout: float | None,
    poll: float = 0.5,
    notify: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int] | None:
    ensure_dir(directory)
    deadline = None if timeout is None else time.monotonic() + timeout
    told = False
    while True:
        got = try_acquire(directory, count)
        if got:
            return got
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if not told and notify:
            notify(f"devguard slot: all {count} slots are busy, waiting (see `devguard slot --status`)")
            told = True
        sleep(poll)


def run(command: list[str], config: Config, count: int | None = None, timeout: float | None = None) -> int:
    """Take a slot, then become the command. Only returns on failure."""
    program = shutil.which(command[0])
    if program is None:
        print(f"devguard slot: command not found: {command[0]}", file=sys.stderr)
        return 127
    n = slot_count(config, count)
    directory = slot_dir(config)
    got = acquire(directory, n, timeout, notify=lambda m: print(m, file=sys.stderr))
    if got is None:
        print(f"devguard slot: no slot free after {timeout:g}s", file=sys.stderr)
        return EX_TEMPFAIL
    _, fd = got
    record_holder(fd, command)
    os.set_inheritable(fd, True)
    os.environ["DEVGUARD_SLOT_FD"] = str(fd)
    try:
        os.execv(program, command)
    except OSError as exc:
        print(f"devguard slot: cannot run {command[0]}: {exc.strerror}", file=sys.stderr)
        return 126
    return 0  # pragma: no cover (execv does not return)
