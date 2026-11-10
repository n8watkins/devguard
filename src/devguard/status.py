"""status: a small, current summary of memory and dev servers for other tools.

Every watch pass writes <state_dir>/status.json. On WSL it also writes a copy on
the Windows side (by default %LOCALAPPDATA%\\devguard\\status.json, reached as
/mnt/c/Users/<user>/AppData/Local/devguard/status.json), so a Windows tray or
widget can show WSL memory without calling into WSL, which would boot a stopped
WSL VM. `devguard status` prints the same summary without writing anything.

Writing never raises: a full disk, a read-only directory or an unmounted /mnt/c
must not break the watch pass.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .fmt import GIB, home
from .procs import MemInfo, Snapshot
from .servers import Server, script_and_args

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

SCHEMA_VERSION = 1
FILE = "status.json"
WINDOWS_USER_CACHE = "windows-user"
ENV_WINDOWS_PATH = "DEVGUARD_WINDOWS_STATUS_PATH"
OFF = "off"
USERS_DIR = Path("/mnt/c/Users")
CMD_EXE = Path("/mnt/c/Windows/System32/cmd.exe")
# Folders under C:\Users that are not a person's profile.
NOT_PROFILES = {"public", "default", "default user", "all users", "defaultapppool", "wsiaccount"}

AGENTS = {"claude", "codex"}
# Paths of agents that run as `node <path>` instead of their own binary.
AGENT_PATH_PARTS = ("claude-code", "@openai/codex")
# Tools that are always a build, test or check, whatever their arguments.
BUILD_TOOLS = {
    "tsc",
    "vitest",
    "jest",
    "playwright",
    "pytest",
    "eslint",
    "lighthouse",
    "rollup",
    "tsup",
    "opennextjs-cloudflare",
}
# Tools that are a build only with one of these subcommands (`next dev` is a server).
BUILD_SUBCOMMANDS = {
    "next": {"build", "lint"},
    "vite": {"build"},
    "astro": {"build", "check"},
    "nuxt": {"build", "generate"},
    "nuxi": {"build", "generate"},
    "wrangler": {"deploy", "build", "check"},
    "turbo": {"run", "build", "test", "lint", "typecheck"},
    "cargo": {"build", "test", "check", "clippy"},
    "webpack": {"build"},
}
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
# npm-style script names that count as builds: build, build:worker, test:e2e, check:release-budgets...
SCRIPT_PREFIXES = ("build", "test", "lint", "typecheck", "check", "e2e", "ci")


def gb(n: float) -> float:
    return round(n / GIB, 2)


def _base(arg: str) -> str:
    return os.path.basename(arg)


def is_agent(argv: list[str]) -> bool:
    """A coding agent session: claude or codex, as a binary or as `node .../claude-code/...`."""
    if not argv:
        return False
    if _base(argv[0]) in AGENTS:
        return True
    parsed = script_and_args(argv)
    if parsed and parsed[0] in AGENTS:
        return True
    head = " ".join(argv[:2])
    return any(part in head for part in AGENT_PATH_PARTS)


def is_build(argv: list[str]) -> bool:
    """A build, test or check run: `next build`, `npm run build:worker`, `playwright test`..."""
    if not argv:
        return False
    prog = _base(argv[0])
    if prog in PACKAGE_MANAGERS:
        words = [a for a in argv[1:] if not a.startswith("-")]
        if words[:1] in (["run"], ["run-script"]) and len(words) > 1:
            return words[1].startswith(SCRIPT_PREFIXES)
        if words[:1] in (["exec"], ["x"]) and len(words) > 1:
            return is_build(words[1:])
        return bool(words) and words[0] in ("test", "build")
    parsed = script_and_args(argv)
    if not parsed:
        return False
    script, args = parsed
    script = script.removesuffix(".js").removesuffix(".mjs").removesuffix(".cjs")
    if script in BUILD_TOOLS:
        return True
    subcommands = BUILD_SUBCOMMANDS.get(script)
    if subcommands is None:
        return False
    words = [a for a in args if not a.startswith("-")]
    if script == "webpack" and not words:
        return True  # plain `webpack` builds; `webpack serve` is a server
    return bool(words) and words[0] in subcommands


def categories(snap: Snapshot, servers: list[Server], uid: int) -> dict[str, dict[str, float | int]]:
    """Resident memory of the user's processes by what they are for.

    Each process counts once, in this order: dev servers (their whole process tree),
    builds and tests (the outermost one and everything below it), agent sessions
    (the agent and its remaining children, such as MCP servers), then other.
    RSS double-counts shared pages, so the totals are an upper bound."""
    kids = snap.children()
    owner: dict[int, str] = {}
    for s in servers:
        for pid in s.tree:
            owner[pid] = "dev_servers"
    own = {pid: p for pid, p in snap.procs.items() if p.uid == uid}
    for category, test in (("builds_tests", is_build), ("agent_sessions", is_agent)):
        roots = {pid for pid, p in own.items() if pid not in owner and test(p.argv)}
        for pid in sorted(roots):
            if any(a in roots for a in snap.ancestors(pid)):
                continue  # counted with its outermost match
            for p in snap.tree(pid, kids):
                owner.setdefault(p, category)
    totals = {name: {"gb": 0.0, "processes": 0} for name in ("dev_servers", "agent_sessions", "builds_tests", "other")}
    rss: dict[str, int] = dict.fromkeys(totals, 0)
    for pid, p in own.items():
        name = owner.get(pid, "other")
        rss[name] += p.rss
        totals[name]["processes"] += 1
    for name in totals:
        totals[name]["gb"] = gb(rss[name])
    return totals


def projects(servers: list[Server]) -> list[dict[str, Any]]:
    """Running servers grouped by the worktree (or directory) they run in, largest first."""
    groups: dict[str, dict[str, Any]] = {}
    for s in servers:
        path = s.worktree or s.cwd or "?"
        group = groups.setdefault(
            path,
            {"name": s.project, "branch": s.branch, "worktree": home(path), "path": path, "servers": [], "total_rss_gb": 0.0},
        )
        entry: dict[str, Any] = {
            "pid": s.pid,
            "kind": s.kind,
            "tool": s.tool,
            "port": s.port,
            "rss_gb": gb(s.rss),
            "state": s.state,
        }
        if s.worktree and s.cwd and os.path.normpath(s.cwd) != os.path.normpath(s.worktree):
            entry["dir"] = os.path.relpath(s.cwd, s.worktree)
        group["servers"].append(entry)
        group["_rss"] = group.get("_rss", 0) + s.rss
    out = sorted(groups.values(), key=lambda g: -g["_rss"])
    for group in out:
        group["total_rss_gb"] = gb(group.pop("_rss"))
    return out


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(
    servers: list[Server],
    mem: MemInfo,
    snap: Snapshot,
    uid: int,
    now: float,
    mode: str,
    last_stop: dict[str, Any] | None,
    held: dict[str, float],
    hold_seconds: float,
    command: list[str],
) -> dict[str, Any]:
    """The status document. `mode` is apply or dry (a watch pass) or status (a read).
    `held` maps restart-loop locations to the time watch stops leaving them alone."""
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": iso(now),
        "updated_unix": int(now),
        "mode": mode,
        "distro": os.environ.get("WSL_DISTRO_NAME") or None,
        "command": command,
        "memory": {
            "total_gb": gb(mem.total),
            "available_gb": gb(mem.available),
            "used_gb": gb(max(0, mem.total - mem.available)),
            "cache_gb": gb(mem.cached),
            "swap_used_gb": gb(mem.swap_used),
            "swap_total_gb": gb(mem.swap_total),
        },
        "server_count": len(servers),
        "projects": projects(servers),
        "categories": categories(snap, servers, uid),
        "last_stop": last_stop,
        "alerts": [
            {"kind": "loop", "where": where, "since": iso(until - hold_seconds), "until": iso(until)}
            for where, until in sorted(held.items())
            if until > now
        ],
    }


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write next to the target, then rename over it, so readers never see half a file.

    On /mnt/c the rename fails while a Windows reader has the file open, so it is
    retried a few times before giving up (the caller reports the error)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


Runner = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _readable_dir(path: Path) -> bool:
    """A directory we may list. Other users' profiles raise PermissionError on stat."""
    try:
        return path.is_dir() and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        return False


def windows_user(state_dir: Path, users_dir: Path = USERS_DIR, run: Runner = _run, cmd_exe: Path = CMD_EXE) -> str | None:
    """The Windows user whose profile gets the status copy.

    Looked up once and cached in <state_dir>/windows-user: the only profile folder
    under C:\\Users with an AppData folder we can read, or else %USERNAME% from
    cmd.exe (which needs WSL interop, so it may fail under a systemd timer; the
    cache from an interactive run then covers it)."""
    cache = state_dir / WINDOWS_USER_CACHE
    with contextlib.suppress(OSError):
        cached = cache.read_text().strip()
        if cached and (users_dir / cached).is_dir():
            return cached
    try:
        candidates = sorted(
            d.name for d in users_dir.iterdir() if d.name.lower() not in NOT_PROFILES and _readable_dir(d / "AppData")
        )
    except OSError:
        return None
    user = candidates[0] if len(candidates) == 1 else None
    if user is None and cmd_exe.exists():
        answer = run([str(cmd_exe), "/d", "/c", "echo %USERNAME%"]).strip().splitlines()
        name = answer[-1].strip() if answer else ""
        if name and "%" not in name and (users_dir / name).is_dir():
            user = name
    if user:
        with contextlib.suppress(OSError):
            state_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(user + "\n")
    return user


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def windows_path(config: Config, users_dir: Path = USERS_DIR, wsl: bool | None = None, run: Runner = _run) -> Path | None:
    """Where the Windows-side copy goes, or None.

    $DEVGUARD_WINDOWS_STATUS_PATH, then watch.windows_status_path: a path, or "off".
    Empty means the default: on WSL with /mnt/c mounted,
    /mnt/c/Users/<windows user>/AppData/Local/devguard/status.json."""
    value = os.environ.get(ENV_WINDOWS_PATH, config.watch.windows_status_path).strip()
    if value.lower() == OFF:
        return None
    if value:
        return Path(os.path.expanduser(value))
    if not (is_wsl() if wsl is None else wsl) or not users_dir.is_dir():
        return None
    user = windows_user(config.state_path(), users_dir, run)
    if not user:
        return None
    return users_dir / user / "AppData" / "Local" / "devguard" / FILE


def write(
    document: dict[str, Any], config: Config, windows: Path | None = None, err: Callable[[str], None] | None = None
) -> bool:
    """Write the status to <state_dir>/status.json and to the Windows copy. Never raises."""
    report = err or (lambda message: print(message, file=sys.stderr))
    data = (json.dumps(document, separators=(",", ":")) + "\n").encode()
    ok = True
    try:
        write_atomic(config.state_path() / FILE, data, 0o600)
    except Exception as exc:
        ok = False
        with contextlib.suppress(Exception):
            report(f"devguard: could not write {config.state_path() / FILE}: {type(exc).__name__}: {exc}")
    try:
        target = windows if windows is not None else windows_path(config)
        if target is not None:
            write_atomic(target, data)
    except Exception as exc:
        ok = False
        with contextlib.suppress(Exception):
            report(f"devguard: could not write the Windows status copy: {type(exc).__name__}: {exc}")
    return ok


def render(document: dict[str, Any]) -> str:
    """The human-readable `devguard status`."""
    m = document["memory"]
    lines = [
        f"Memory: {m['used_gb']:.1f}G used of {m['total_gb']:.1f}G, {m['available_gb']:.1f}G available, "
        f"{m['cache_gb']:.1f}G cache, {m['swap_used_gb']:.1f}G of {m['swap_total_gb']:.1f}G swap in use"
    ]
    c = document["categories"]
    lines.append(
        "By use:  "
        + ", ".join(
            f"{label} {c[key]['gb']:.1f}G ({c[key]['processes']})"
            for key, label in (
                ("dev_servers", "dev servers"),
                ("agent_sessions", "agents"),
                ("builds_tests", "builds/tests"),
                ("other", "other"),
            )
        )
    )
    if document["projects"]:
        lines.append("")
        for p in document["projects"]:
            ports = ", ".join(f"{s['tool']} {s['kind']} :{s['port']} {s['rss_gb']:.1f}G" for s in p["servers"])
            lines.append(f"{p['total_rss_gb']:5.1f}G  {p['name']:<14} {p['worktree']}  ({ports})")
    else:
        lines.append("No dev servers running.")
    stop = document.get("last_stop")
    if stop:
        lines.append("")
        parts = [stop.get(k) for k in ("time", "server")] + [f"port {stop.get('port', '?')}", stop.get("worktree")]
        lines.append(f"Last stop: {' '.join(str(p) for p in parts if p)}: {stop.get('reason', '')}")
    for alert in document["alerts"]:
        if alert.get("kind") == "spend":
            where = f" in {alert['where']}" if alert.get("where") else ""
            lines.append(
                f"Spend: {alert['agent']}{where} spent {alert['turn_tokens']:,} tokens in {alert['turn_calls']} calls "
                "since the last prompt"
            )
        else:
            lines.append(f"Restart loop: {alert['where']} (finished servers left running until {alert['until']})")
    return "\n".join(lines)
