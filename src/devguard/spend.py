"""spend: tokens an agent spent since you last typed, per running session.

A runaway agent loop shows up as one turn that keeps spending: many model calls
without a new prompt from you. This reads the agents' own session logs and
counts, per session, the tokens spent since the last prompt (the turn) and in
total. Counted: fresh input, cache writes and output (reasoning included).
Cache reads are not counted; they are cheap and grow with every call.

The method is curb's (misty-step/curb, MIT): per-turn spend from local logs,
without reading what was said. Only usage numbers and turn boundaries are kept.

  Claude Code  ~/.claude/projects/**/*.jsonl  assistant message usage; a turn
               starts at a user entry that is a prompt (not a tool result).
  Codex        ~/.codex/sessions/**/*.jsonl   token_count totals; a turn starts
               at task_started.

Logs are read incrementally: each pass reads only what was added since the last
one (state in <state_dir>/spend-state.json), and only for sessions written to in
the last spend.active_minutes. Warn only: watch logs a SPEND line and adds an
alert to status.json; nothing is stopped.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .fmt import home

STATE_FILE = "spend-state.json"
MAX_READ = 64 * 1024 * 1024  # per file per pass; the rest is read on the next pass


@dataclass
class Session:
    agent: str  # claude | codex
    path: str
    offset: int = 0
    cwd: str = ""
    turn: int = 0  # tokens since the last prompt
    total: int = 0  # tokens in this log
    calls: int = 0  # model calls since the last prompt
    turn_started: float = 0.0
    last_id: str = ""  # Claude: the last counted message id (one message spans several lines)
    codex_base: int = 0  # Codex: running total when the turn started
    codex_last: int = 0  # Codex: latest running total
    warned: int = 0  # turn tokens at the last warning, so each turn warns once per threshold step
    updated: float = 0.0


def roots() -> list[tuple[str, Path]]:
    # DEVGUARD_AGENT_HOME points elsewhere for tests; agents keep their logs under HOME.
    h = Path(os.environ.get("DEVGUARD_AGENT_HOME") or os.environ.get("HOME") or str(Path.home()))
    return [("claude", h / ".claude" / "projects"), ("codex", h / ".codex" / "sessions")]


def active_logs(active_seconds: float, now: float) -> list[tuple[str, Path, float]]:
    found = []
    for agent, root in roots():
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = Path(dirpath) / name
                with contextlib.suppress(OSError):
                    mtime = path.stat().st_mtime
                    if now - mtime <= active_seconds:
                        found.append((agent, path, mtime))
    return found


def _when(entry: dict, now: float) -> float:
    stamp = entry.get("timestamp")
    if isinstance(stamp, str):
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    return now


def _claude_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)


def _claude_line(s: Session, entry: dict, now: float) -> None:
    if entry.get("cwd"):
        s.cwd = entry["cwd"]
    if _claude_prompt(entry):
        s.turn, s.calls, s.warned, s.turn_started = 0, 0, 0, _when(entry, now)
        return
    if entry.get("type") != "assistant":
        return
    message = entry.get("message") or {}
    usage = message.get("usage")
    msg_id = message.get("id") or entry.get("uuid") or ""
    if not isinstance(usage, dict) or (msg_id and msg_id == s.last_id):
        return
    s.last_id = msg_id
    spent = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens"))
    s.turn += spent
    s.total += spent
    s.calls += 1


def _codex_line(s: Session, entry: dict, now: float) -> None:
    payload = entry.get("payload") or {}
    kind = payload.get("type")
    if entry.get("type") == "session_meta" and payload.get("cwd") or entry.get("type") == "turn_context" and payload.get("cwd"):
        s.cwd = payload["cwd"]
    if kind == "task_started":
        s.codex_base, s.turn, s.calls, s.warned, s.turn_started = s.codex_last, 0, 0, 0, _when(entry, now)
    elif kind == "token_count":
        usage = ((payload.get("info") or {}).get("total_token_usage")) or {}
        if not usage:
            return
        running = max(0, int(usage.get("input_tokens") or 0) - int(usage.get("cached_input_tokens") or 0)) + int(
            usage.get("output_tokens") or 0
        )
        if running < s.codex_last:  # a new log segment restarted the totals
            s.codex_base = 0
        if running != s.codex_last:
            s.calls += 1
        s.codex_last = running
        s.turn = max(0, running - s.codex_base)
        s.total = running


def read_new(s: Session, now: float) -> None:
    """Read what was added to the log since the last pass; only whole lines."""
    path = Path(s.path)
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < s.offset:  # rewritten (for example by a scrubber): start over
        fresh = Session(s.agent, s.path)
        s.__dict__.update(fresh.__dict__)
    if size == s.offset:
        return
    with path.open("rb") as handle:
        handle.seek(s.offset)
        data = handle.read(min(size - s.offset, MAX_READ))
    end = data.rfind(b"\n")
    if end < 0:
        return
    handle_line = _claude_line if s.agent == "claude" else _codex_line
    for raw in data[: end + 1].splitlines():
        if (
            b'"usage"' not in raw
            and b"token_count" not in raw
            and b'"user"' not in raw
            and b"task_started" not in raw
            and b"cwd" not in raw
        ):
            continue  # cheap skip: most lines are tool output
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict):
            handle_line(s, entry, now)
    s.offset += end + 1
    s.updated = now


def load_state(path: Path) -> dict[str, Session]:
    try:
        data = json.loads(path.read_text())
        return {k: Session(**v) for k, v in data.items()}
    except Exception:  # missing, corrupt or from another version: start fresh
        return {}


def save_state(path: Path, sessions: dict[str, Session]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({k: asdict(v) for k, v in sessions.items()}, handle, separators=(",", ":"))
        tmp.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def scan(state_dir: Path, active_minutes: float, now: float | None = None) -> list[Session]:
    """Update and return the sessions written to in the last active_minutes, biggest turn first.

    A session seen for the first time is read from the start, so its numbers are right
    from the first pass (that read is capped at MAX_READ per pass)."""
    now = time.time() if now is None else now
    state_path = state_dir / STATE_FILE
    state_dir.mkdir(parents=True, exist_ok=True)
    # One reader at a time (the watch timer and a manual devguard spend), so no line is counted twice.
    with open(state_dir / f".{STATE_FILE}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        known = load_state(state_path)
        active = {}
        for agent, path, _mtime in active_logs(active_minutes * 60, now):
            key = str(path)
            s = known.get(key) or Session(agent, key)
            read_new(s, now)
            active[key] = s
        save_state(state_path, active)  # forget sessions that went quiet
    return sorted(active.values(), key=lambda s: s.turn, reverse=True)


def remember_warned(state_dir: Path, sessions: list[Session]) -> None:
    """Store the warned marks set after a scan."""
    with open(state_dir / f".{STATE_FILE}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        known = load_state(state_dir / STATE_FILE)
        for s in sessions:
            if s.path in known:
                known[s.path].warned = s.warned
        save_state(state_dir / STATE_FILE, known)


def to_warn(sessions: list[Session], warn_tokens: int) -> list[Session]:
    """Sessions whose turn crossed the threshold, or another multiple of it, since the last warning."""
    return [s for s in sessions if warn_tokens and s.turn >= warn_tokens and s.turn // warn_tokens > s.warned // warn_tokens]


def over(sessions: list[Session], warn_tokens: int) -> list[Session]:
    return [s for s in sessions if warn_tokens and s.turn >= warn_tokens]


def tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def describe(s: Session) -> str:
    where = home(s.cwd) if s.cwd else Path(s.path).parent.name
    turn = f"{tokens(s.turn)} tokens in {s.calls} calls since the last prompt"
    return f"{s.agent} in {where}: {turn} ({tokens(s.total)} in the session)"


def alert(s: Session, now: float) -> dict:
    return {
        "kind": "spend",
        "agent": s.agent,
        "where": home(s.cwd) if s.cwd else "",
        "turn_tokens": s.turn,
        "turn_calls": s.calls,
        "session_tokens": s.total,
        "turn_minutes": round((now - s.turn_started) / 60) if s.turn_started else None,
    }
