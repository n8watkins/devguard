"""metrics: one compact JSON line per watch pass in <state_dir>/metrics.jsonl.

Each line holds the time, memory, the limits in effect, every dev server seen
(kind, port, project, worktree, branch state, memory, uptime, exempt flag) and the
actions taken. `devguard report` reads them back.

The active file is rotated into `metrics.jsonl.<first timestamp>` once it holds a
day of passes or 2 MB; archives older than 30 days are deleted, and the oldest
are deleted while everything together passes 20 MB. Writing never raises: a full
disk or a read-only state dir must not break the watcher.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .fmt import GIB, home
from .procs import MemInfo
from .servers import Server

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .watch import Action

FILE = "metrics.jsonl"
SEGMENT_MAX_BYTES = 2_000_000
SEGMENT_MAX_SECONDS = 86400
KEEP_SECONDS = 30 * 86400
KEEP_BYTES = 20_000_000


def gb(n: float) -> float:
    return round(n / GIB, 2)


def location(s: Server) -> str:
    """Where a server runs, shortened to ~/...: its working directory."""
    return home(s.cwd) if s.cwd else "?"


def server_entry(s: Server) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "pid": s.pid,
        "kind": s.kind,
        "tool": s.tool,
        "port": s.port,
        "proj": s.project,
        "wt": home(s.worktree or s.cwd),
        "state": s.state,
        "gb": gb(s.rss),
        "up": round(s.age),
    }
    if s.worktree and s.cwd and os.path.normpath(s.cwd) != os.path.normpath(s.worktree):
        entry["dir"] = os.path.relpath(s.cwd, s.worktree)
    if s.branch:
        entry["branch"] = s.branch
    if s.exempt_memory:
        entry["exempt"] = True
    return entry


def action_verb(action: Action, apply: bool) -> str:
    """warn | stop | would-stop | hold | loop."""
    if action.verb == "hold":
        return "loop" if action.outcome == "loop" else "hold"
    if action.verb == "stop" and not apply:
        return "would-stop"
    return action.verb


def entry(servers: list[Server], mem: MemInfo, actions: list[Action], apply: bool, config: Config, now: float) -> dict:
    w = config.watch
    record: dict[str, Any] = {
        "t": int(now),
        "mode": "apply" if apply else "dry",
        "mem": {"total": gb(mem.total), "avail": gb(mem.available), "swap": gb(mem.swap_used)},
        "lim": {"soft": w.soft_gb, "hard": w.hard_gb, "floor": w.floor_gb},
        "srv": [server_entry(s) for s in servers],
    }
    acts = []
    for a in actions:
        act: dict[str, Any] = {"verb": action_verb(a, apply), "pid": a.server.pid, "rule": a.rule, "why": a.reason}
        if a.outcome == "quiet":
            act["quiet"] = True
        acts.append(act)
    if acts:
        record["act"] = acts
    return record


def _first_time(path: Path) -> float | None:
    try:
        with path.open() as handle:
            return float(json.loads(handle.readline())["t"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def archives(state_dir: Path) -> list[Path]:
    """Rotated files, oldest first."""
    found = []
    for p in state_dir.glob(FILE + ".*"):
        suffix = p.name[len(FILE) + 1 :]
        if suffix.isdigit():
            found.append((int(suffix), p))
    return [p for _, p in sorted(found)]


def rotate(state_dir: Path, now: float) -> None:
    path = state_dir / FILE
    try:
        st = path.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        return  # not ours to move; the write below fails and is reported
    first = _first_time(path)
    if st.st_size < SEGMENT_MAX_BYTES and first is not None and now - first < SEGMENT_MAX_SECONDS:
        return
    start = int(first if first is not None else st.st_mtime)
    path.replace(state_dir / f"{FILE}.{start}")
    prune(state_dir, now)


def prune(state_dir: Path, now: float) -> None:
    kept = []
    for p in archives(state_dir):
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        if now - st.st_mtime > KEEP_SECONDS:
            p.unlink(missing_ok=True)
        else:
            kept.append((p, st.st_size))
    active = state_dir / FILE
    total = sum(n for _, n in kept) + (active.stat().st_size if active.exists() else 0)
    for p, n in kept:
        if total <= KEEP_BYTES:
            break
        p.unlink(missing_ok=True)
        total -= n


def append(state_dir: Path, record: dict, now: float) -> bool:
    """Append one record; True on success. Never raises."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        rotate(state_dir, now)
        data = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        fd = os.open(state_dir / FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
        finally:
            os.close(fd)
        return True
    except Exception as exc:  # metrics are optional; the watcher must keep running
        with contextlib.suppress(Exception):
            print(f"devguard: could not write metrics: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def read(state_dir: Path, since: float = 0) -> Iterator[dict]:
    """Records with t >= since, oldest first; unreadable lines are skipped."""
    files = archives(state_dir)
    starts = [int(p.name[len(FILE) + 1 :]) for p in files]
    # An archive can be skipped when the next file starts before `since`.
    paths = [p for i, p in enumerate(files) if i + 1 >= len(starts) or starts[i + 1] > since]
    paths.append(state_dir / FILE)
    for path in paths:
        try:
            handle = path.open()
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if isinstance(record, dict) and float(record["t"]) >= since:
                        yield record
                except (ValueError, KeyError, TypeError):
                    continue
