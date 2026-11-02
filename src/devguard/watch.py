"""watch: the dev server memory and cleanup policy.

Rules, applied in order on every pass:
  1. finished: stop any dev or preview server whose worktree was removed or whose
     branch is merged or deleted on origin (watch.stop_finished);
  2. per server (dev servers only, unless exempt): warn above soft_gb, stop above hard_gb;
  3. floor: when available memory is below floor_gb, stop the largest remaining dev
     server that uses more than min_victim_gb.
Servers under watch.protected_paths are never stopped. Without --apply nothing is
stopped; every decision is logged as WOULD-STOP or WARN.

Repeated lines are rate-limited: a server's WARN (or WOULD-STOP) is logged once, then
again only when it grew by 0.5 GiB or 15 minutes passed. When a dev server in a
finished worktree keeps coming back (stopped 3 times within an hour), watch logs one
LOOP line and leaves finished servers there alone for an hour instead of stopping
one every minute; the memory limits and the floor still apply. Each pass appends a
sample to metrics.jsonl (watch.metrics) for `devguard report` and rewrites
status.json (see status.py) for trays and widgets.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import metrics
from . import status as statusmod
from .config import Config
from .fmt import GIB, home, is_within, size
from .procs import MemInfo
from .servers import Server, Source, annotate, discover

LOG_MAX_BYTES = 1_000_000
STATE_FILE = "watch-state.json"
WARN_REPEAT_SECONDS = 15 * 60
WARN_GROWTH_BYTES = GIB // 2
LOOP_STOPS = 3
LOOP_WINDOW_SECONDS = 60 * 60
LOOP_HOLD_SECONDS = 60 * 60
FINISHED_RULES = ("merged", "gone", "removed")


@dataclass
class Action:
    verb: str  # stop | warn | hold (a finished server left running during a restart loop)
    server: Server
    reason: str
    rule: str = ""  # merged | gone | removed | hard | soft | floor
    outcome: str = ""  # set by run_once: logged | quiet (rate-limited) | hold | loop


def plan(servers: list[Server], mem: MemInfo, config: Config, held: set[str] | None = None) -> list[Action]:
    """The actions for one pass. Finished servers at a `held` location (a restart loop)
    get a "hold" action instead of a stop and stay subject to the memory rules."""
    w = config.watch
    held = held or set()
    protected = [str(Path(os.path.expanduser(p)).absolute()) for p in w.protected_paths]

    def is_protected(s: Server) -> bool:
        return bool(s.cwd) and any(is_within(s.cwd, p) for p in protected)

    actions: list[Action] = []
    stopped: set[int] = set()
    candidates = [s for s in servers if not is_protected(s)]
    if w.stop_finished:
        for s in candidates:
            if s.finished and metrics.location(s) in held:
                actions.append(Action("hold", s, f"its work is done ({s.reason})", s.state))
            elif s.finished:
                actions.append(Action("stop", s, f"its work is done ({s.reason})", s.state))
                stopped.add(s.pid)
    for s in candidates:
        if s.pid in stopped or s.kind != "dev" or s.exempt_memory:
            continue
        if s.rss > w.hard_gb * GIB:
            actions.append(Action("stop", s, f"over the {w.hard_gb:g} GiB limit", "hard"))
            stopped.add(s.pid)
        elif s.rss > w.soft_gb * GIB:
            actions.append(Action("warn", s, f"over {w.soft_gb:g} GiB, restart it soon", "soft"))
    if mem.total and mem.available < w.floor_gb * GIB:
        victims = [s for s in candidates if s.kind == "dev" and s.pid not in stopped and s.rss > w.min_victim_gb * GIB]
        if victims:
            victim = max(victims, key=lambda s: s.rss)
            actions.append(
                Action(
                    "stop",
                    victim,
                    f"only {mem.available / GIB:.1f} GiB available (floor {w.floor_gb:g} GiB); this is the largest dev server",
                    "floor",
                )
            )
    return actions


class Log:
    def __init__(self, path: Path, echo: Callable[[str], None] | None = print) -> None:
        self.path = path
        self.echo = echo

    def show(self, message: str) -> None:
        """Echo a line without writing it to the log file (rate-limited repeats)."""
        if self.echo:
            self.echo(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}")

    def write(self, message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.stat().st_size > LOG_MAX_BYTES:
                self.path.replace(self.path.with_suffix(".log.1"))
        except FileNotFoundError:
            pass
        with self.path.open("a") as handle:
            handle.write(line + "\n")
        if self.echo:
            self.echo(line)


class WatchState:
    """What earlier passes logged and stopped, kept in <state_dir>/watch-state.json.

    The timer starts a fresh process every minute, so rate limits live on disk. A
    missing or unreadable file just means a fresh start; saving never raises."""

    def __init__(self) -> None:
        self.warned: dict[str, list[float]] = {}  # "verb:pid:identity" -> [time, rss] last logged
        self.stops: dict[str, list[float]] = {}  # location -> times of recent finished stops
        self.held: dict[str, float] = {}  # location -> leave finished servers alone until then
        self.last_stop: dict | None = None  # the most recent stop, for status.json

    @classmethod
    def load(cls, path: Path) -> WatchState:
        state = cls()
        try:
            data = json.loads(path.read_text())
            state.warned = {str(k): [float(v[0]), float(v[1])] for k, v in data.get("warned", {}).items()}
            state.stops = {str(k): [float(t) for t in v] for k, v in data.get("stops", {}).items()}
            state.held = {str(k): float(v) for k, v in data.get("held", {}).items()}
            last = data.get("last_stop")
            state.last_stop = last if isinstance(last, dict) else None
        except Exception:  # missing, corrupt or from another version: start fresh
            return cls()
        return state

    def save(self, path: Path) -> None:
        data = json.dumps(
            {"warned": self.warned, "stops": self.stops, "held": self.held, "last_stop": self.last_stop}, separators=(",", ":")
        )
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(data)
            tmp.replace(path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def should_log(self, key: str, rss: int, now: float) -> bool:
        last = self.warned.get(key)
        if last is None or rss >= last[1] + WARN_GROWTH_BYTES or now - last[0] >= WARN_REPEAT_SECONDS:
            self.warned[key] = [now, rss]
            return True
        return False

    def record_stop(self, server: Server, reason: str, now: float, requested: bool = False) -> None:
        """`requested` marks a stop someone asked for (devguard stop), which trays need not announce."""
        self.last_stop = {
            "time": statusmod.iso(now),
            "server": f"{server.tool} {server.kind}",
            "port": server.port,
            "pid": server.pid,
            "worktree": home(server.worktree or server.cwd),
            "reason": reason,
            "requested": requested,
        }

    def holding(self, now: float) -> set[str]:
        return {where for where, until in self.held.items() if until > now}

    def finished_stop(self, where: str, now: float) -> str:
        """Record a finished stop at `where`: stop, or loop when it is the next one after
        LOOP_STOPS within the window (finished servers there are then held)."""
        recent = [t for t in self.stops.get(where, []) if now - t < LOOP_WINDOW_SECONDS]
        if len(recent) >= LOOP_STOPS:
            self.stops.pop(where, None)
            self.held[where] = now + LOOP_HOLD_SECONDS
            return "loop"
        self.stops[where] = [*recent, now]
        return "stop"

    def prune(self, servers: list[Server], now: float) -> None:
        alive = {f"{s.pid}:{s.identities.get(s.pid, '')}" for s in servers}
        self.warned = {k: v for k, v in self.warned.items() if k.split(":", 1)[1] in alive}
        self.stops = {k: kept for k, v in self.stops.items() if (kept := [t for t in v if now - t < LOOP_WINDOW_SECONDS])}
        self.held = {k: v for k, v in self.held.items() if v > now}


def rate_key(action: Action) -> str:
    s = action.server
    return f"{action.verb}:{s.pid}:{s.identities.get(s.pid, '')}"


Killer = Callable[[int, int], None]


def stop_server(
    server: Server, source: Source, grace: float, kill: Killer | None = None, sleep: Callable[[float], None] = time.sleep
) -> list[int]:
    """SIGTERM the server's process tree, then SIGKILL survivors after `grace` seconds.

    A pid is only signalled while its start time still matches the snapshot, so a
    reused pid is never hit. Returns the pids that were signalled."""

    # Looked up at call time (not as a default argument) so tests that patch os.kill
    # can never reach a real process.
    kill = kill or os.kill

    def alive(pids: list[int]) -> list[int]:
        return [p for p in pids if source.identity(p) == server.identities.get(p)]

    targets = alive(server.tree)
    for pid in targets:
        with contextlib.suppress(OSError):
            kill(pid, signal.SIGTERM)
    waited = 0.0
    while waited < grace and alive(targets):
        sleep(0.5)
        waited += 0.5
    for pid in alive(targets):
        with contextlib.suppress(OSError):
            kill(pid, signal.SIGKILL)
    return targets


def describe(s: Server) -> str:
    return f"port {s.port} pid {s.pid} {s.tool} {s.kind} {size(s.rss)} {home(s.cwd)}"


def devguard_command() -> list[str]:
    """argv that runs this same devguard from anywhere (for status.json readers)."""
    cmd, pythonpath = launcher()
    return ["env", f"PYTHONPATH={pythonpath}", *cmd] if pythonpath else cmd


def status_document(
    source: Source, config: Config, servers: list[Server], mem: MemInfo, snap, state: WatchState, now: float, mode: str
) -> dict:
    uid = getattr(source, "uid", os.getuid())
    return statusmod.build(servers, mem, snap, uid, now, mode, state.last_stop, state.held, LOOP_HOLD_SECONDS, devguard_command())


def run_once(
    source: Source,
    config: Config,
    apply: bool,
    log: Log,
    kill: Killer | None = None,
    sleep: Callable[[float], None] = time.sleep,
    self_pid: int | None = None,
    clock: Callable[[], float] = time.time,
) -> list[Action]:
    snap = source.snapshot()
    servers = discover(source, config, snap, self_pid=self_pid)
    if config.watch.stop_finished:
        annotate(servers, config.state_path() / "fetched", config.watch.fetch_interval_minutes * 60)
    elif config.watch.metrics:
        annotate(servers)
    mem = source.meminfo()
    now = clock()
    state_path = config.state_path() / STATE_FILE
    state = WatchState.load(state_path)
    actions = plan(servers, mem, config, state.holding(now) if apply else None)
    looping: set[int] = set()
    if apply:
        for action in actions:
            finished = action.verb == "stop" and action.rule in FINISHED_RULES
            if finished and state.finished_stop(metrics.location(action.server), now) == "loop":
                looping.add(action.server.pid)
        if looping:  # those places are held from now on; plan again so the memory rules still see them
            actions = plan(servers, mem, config, state.holding(now))
    for action in actions:
        s = action.server
        if action.verb == "hold":
            if s.pid in looping:
                action.outcome = "loop"
                log.write(
                    f"LOOP {describe(s)}: stopped {LOOP_STOPS} times in {LOOP_WINDOW_SECONDS // 60} minutes and restarted "
                    "each time, likely by an agent; leaving finished servers here running for "
                    f"{LOOP_HOLD_SECONDS // 60} minutes (memory limits still apply). "
                    "Remove the worktree or stop what restarts it."
                )
            else:
                action.outcome = "hold"
                log.show(f"HOLD {describe(s)}: {action.reason}; not stopped, it keeps being restarted (see the LOOP line)")
            continue
        if action.verb == "stop" and apply:
            action.outcome = "logged"
            log.write(f"STOP {describe(s)}: {action.reason}")
            stop_server(s, source, config.watch.grace_seconds, kill, sleep)
            state.record_stop(s, action.reason, now)
            continue
        if action.verb == "warn":
            message = f"WARN {describe(s)}: {action.reason}"
        else:
            message = f"WOULD-STOP {describe(s)}: {action.reason} (dry run; use --apply to stop)"
        if state.should_log(rate_key(action), s.rss, now):
            action.outcome = "logged"
            log.write(message)
        else:
            action.outcome = "quiet"
            log.show(message)
    state.prune(servers, now)
    state.save(state_path)
    if config.watch.metrics:
        metrics.append(config.state_path(), metrics.entry(servers, mem, actions, apply, config, now), now)
    stopped = {a.server.pid for a in actions if a.verb == "stop" and apply}
    running = [s for s in servers if s.pid not in stopped]
    try:
        document = status_document(source, config, running, mem, snap, state, now, "apply" if apply else "dry")
    except Exception as exc:  # the status file is optional; the pass already did its job
        print(f"devguard: could not build status: {type(exc).__name__}: {exc}", file=sys.stderr)
    else:
        statusmod.write(document, config)
    return actions


def matches(server: Server, target: str) -> bool:
    """True when `target` (a port number or a directory) names this server."""
    if target.isdigit():
        return target in server.port.split(",")
    where = str(Path(os.path.expanduser(target)).absolute())
    return (bool(server.cwd) and is_within(server.cwd, where)) or server.worktree == where


def stop_matching(
    source: Source,
    config: Config,
    target: str,
    log: Log,
    kill: Killer | None = None,
    sleep: Callable[[float], None] = time.sleep,
    self_pid: int | None = None,
    clock: Callable[[], float] = time.time,
) -> list[Server]:
    """Stop every dev or preview server on port `target`, or under directory `target`.

    What a tray's Stop button runs: same SIGTERM-then-SIGKILL as watch, logged as a
    STOP line, and remembered as the last stop in status.json."""
    servers = [s for s in discover(source, config, self_pid=self_pid) if matches(s, target)]
    if not servers:
        return []
    annotate(servers)
    now = clock()
    state_path = config.state_path() / STATE_FILE
    state = WatchState.load(state_path)
    reason = f"stopped on request (devguard stop {target})"
    for s in servers:
        log.write(f"STOP {describe(s)}: {reason}")
        stop_server(s, source, config.watch.grace_seconds, kill, sleep)
        state.record_stop(s, reason, now, requested=True)
    state.save(state_path)
    return servers


def loop(
    source: Source, config: Config, apply: bool, log: Log, passes: int | None = None, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Run passes forever (or `passes` times); one failed pass is logged, not fatal."""
    done = 0
    while passes is None or done < passes:
        try:
            run_once(source, config, apply, log)
        except Exception as exc:  # a long-running watcher must survive one bad pass
            log.write(f"ERROR pass failed: {type(exc).__name__}: {exc}")
        done += 1
        sleep(config.watch.interval_seconds)


# --- systemd timer -------------------------------------------------------

SERVICE = "devguard-watch.service"
TIMER = "devguard-watch.timer"


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def launcher() -> tuple[list[str], str | None]:
    """How to start this same devguard later: (argv prefix, PYTHONPATH or None).

    A zipapp runs as `python devguard.pyz`; an installed or cloned package runs as
    `python -m devguard` with PYTHONPATH pointing at the directory holding it."""
    package_parent = Path(__file__).absolute().parent.parent
    if package_parent.is_file():
        return [sys.executable, str(package_parent)], None
    return [sys.executable, "-m", "devguard"], str(package_parent)


def exec_command(apply: bool, config_path: Path | None) -> list[str]:
    cmd, _ = launcher()
    if config_path:
        cmd = [*cmd, "--config", str(config_path)]
    cmd = [*cmd, "watch", "--once"]
    if apply:
        cmd.append("--apply")
    return cmd


def timer_path(git: str | None = None) -> str:
    """A short PATH for the timer: system dirs plus wherever git lives. The login
    PATH on WSL holds Windows directories with spaces that the timer does not need."""
    dirs = ["/usr/local/bin", "/usr/bin", "/bin"]
    git = git or shutil.which("git")
    if git and os.path.dirname(git) not in dirs:
        dirs.insert(0, os.path.dirname(git))
    return ":".join(dirs)


def _env_line(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"\n'


def units(apply: bool, config_path: Path | None, path_env: str | None = None) -> tuple[str, str]:
    command = " ".join(shlex.quote(c) for c in exec_command(apply, config_path)).replace("%", "%%")
    _, pythonpath = launcher()
    mode = "stops servers" if apply else "dry run: logs what it would stop"
    service = (
        "[Unit]\n"
        f"Description=devguard watch ({mode})\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        + _env_line("PATH", path_env or timer_path())
        + (_env_line("PYTHONPATH", pythonpath) if pythonpath else "")
        + f"ExecStart={command}\n"
        "Nice=10\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run devguard watch every minute\n\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        "OnUnitActiveSec=1min\n"
        "AccuracySec=10s\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def systemd_available(run: Callable[[list[str]], int]) -> bool:
    return Path("/run/systemd/system").is_dir() and run(["systemctl", "--user", "show-environment"]) == 0


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


SYSTEMD_HELP_WSL = """\
systemd is not running, so the timer cannot be installed.
On WSL2, turn systemd on:
  1. Add these lines to /etc/wsl.conf (sudo nano /etc/wsl.conf):
       [boot]
       systemd=true
  2. From Windows (PowerShell or cmd): wsl.exe --shutdown
  3. Open a new WSL terminal and run: devguard watch --install
Without systemd, run `devguard watch` in a spare terminal instead; it checks every minute.
"""

SYSTEMD_HELP = """\
systemd user services are not available, so the timer cannot be installed.
Run `devguard watch` in a spare terminal instead (it checks every minute), or start
`devguard watch --once --apply` from cron or launchd every minute.
"""


def _run(argv: list[str]) -> int:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def install(
    apply: bool,
    config_path: Path | None,
    directory: Path | None = None,
    run: Callable[[list[str]], int] = _run,
    out: Callable[[str], None] = print,
) -> int:
    if not systemd_available(run):
        out(SYSTEMD_HELP_WSL if is_wsl() else SYSTEMD_HELP)
        return 1
    directory = directory or unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    service, timer = units(apply, config_path)
    (directory / SERVICE).write_text(service)
    (directory / TIMER).write_text(timer)
    for argv in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", TIMER]):
        if run(argv) != 0:
            out(f"failed: {' '.join(argv)}")
            return 1
    out(f"Installed {directory / SERVICE} and {TIMER}; it runs every minute.")
    if apply:
        out("Mode: apply. It stops servers according to your limits.")
    else:
        out("Mode: dry run. It only logs what it would stop. Check `devguard watch --log`,")
        out("then run `devguard watch --install --apply` when you agree with its decisions.")
    if shutil.which("loginctl"):
        out("To keep it running when no terminal is open: loginctl enable-linger $USER")
    return 0


def uninstall(directory: Path | None = None, run: Callable[[list[str]], int] = _run, out: Callable[[str], None] = print) -> int:
    directory = directory or unit_dir()
    run(["systemctl", "--user", "disable", "--now", TIMER])
    removed = False
    for name in (SERVICE, TIMER):
        try:
            (directory / name).unlink()
            removed = True
        except FileNotFoundError:
            pass
    run(["systemctl", "--user", "daemon-reload"])
    out("Removed the devguard watch timer." if removed else "The devguard watch timer was not installed.")
    return 0
