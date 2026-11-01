"""port: a stable dev server port per directory.

port = base + int(first 8 hex digits of sha1(absolute path), 16) mod range
With the defaults (4000, 1000) this is 4000-4999 and gives the same numbers as the
older dev-port.sh script, so existing bookmarks keep working.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
from pathlib import Path

from .config import Config


def logical_cwd() -> str:
    """Like `pwd`: keep the symlinked path the user cd'd into when $PWD is accurate."""
    pwd = os.environ.get("PWD")
    cwd = os.getcwd()
    if pwd and os.path.isabs(pwd):
        try:
            if os.path.samefile(pwd, cwd):
                return os.path.normpath(pwd)
        except OSError:
            pass
    return cwd


def resolve_dir(path: str | None) -> str:
    """Absolute, normalized path the way `cd "$path" && pwd` would print it."""
    base = logical_cwd()
    if not path:
        return base
    joined = os.path.normpath(os.path.join(base, os.path.expanduser(path)))
    if not os.path.isdir(joined):
        raise NotADirectoryError(f"not a directory: {path}")
    return joined


def port_for(directory: str, base: int = 4000, span: int = 1000) -> int:
    digest = hashlib.sha1(directory.encode()).hexdigest()
    return base + int(digest[:8], 16) % span


def port_of(directory: str, config: Config) -> int:
    override = config.port.overrides.get(directory)
    if override:
        return override
    return port_for(directory, config.port.base, config.port.range)


def listening(port: int, timeout: float = 0.2) -> bool:
    for host in ("127.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                if sock.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def write_port_file(directory: str, port: int) -> None:
    with contextlib.suppress(OSError):
        Path(directory, ".dev-port").write_text(f"{port}\n")


def collision_chance(n: int, span: int = 1000) -> float:
    """Probability that at least two of n directories share a port."""
    p_unique = 1.0
    for i in range(n):
        p_unique *= max(0.0, (span - i) / span)
    return 1 - p_unique
