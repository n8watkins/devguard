"""watch: the dev server memory and cleanup policy.

Rules, applied in order on every pass:
  1. finished: stop any dev or preview server whose worktree was removed or whose
     branch is merged or deleted on origin (watch.stop_finished);
  2. per server (dev servers only, unless exempt): warn above soft_gb, stop above hard_gb;
  3. floor: when available memory is below floor_gb, stop the largest remaining dev
     server that uses more than min_victim_gb.
Servers under watch.protected_paths are never stopped. Without --apply nothing is
stopped; every decision is logged as WOULD-STOP or WARN.
"""

from __future__ import annotations

import contextlib
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

from .config import Config
from .fmt import GIB, home, is_within, size
from .procs import MemInfo
from .servers import Server, Source, annotate, discover

LOG_MAX_BYTES = 1_000_000


@dataclass
class Action:
    verb: str  # stop | warn
    server: Server
    reason: str


def plan(servers: list[Server], mem: MemInfo, config: Config) -> list[Action]:
    w = config.watch
    protected = [str(Path(os.path.expanduser(p)).absolute()) for p in w.protected_paths]

    def is_protected(s: Server) -> bool:
        return bool(s.cwd) and any(is_within(s.cwd, p) for p in protected)

    actions: list[Action] = []
    stopped: set[int] = set()
    candidates = [s for s in servers if not is_protected(s)]
    if w.stop_finished:
        for s in candidates:
            if s.finished:
                actions.append(Action("stop", s, f"its work is done ({s.reason})"))
                stopped.add(s.pid)
    for s in candidates:
        if s.pid in stopped or s.kind != "dev" or s.exempt_memory:
            continue
        if s.rss > w.hard_gb * GIB:
            actions.append(Action("stop", s, f"over the {w.hard_gb:g} GiB limit"))
            stopped.add(s.pid)
        elif s.rss > w.soft_gb * GIB:
            actions.append(Action("warn", s, f"over {w.soft_gb:g} GiB, restart it soon"))
    if mem.total and mem.available < w.floor_gb * GIB:
        victims = [s for s in candidates if s.kind == "dev" and s.pid not in stopped and s.rss > w.min_victim_gb * GIB]
        if victims:
            victim = max(victims, key=lambda s: s.rss)
            actions.append(
                Action(
                    "stop",
                    victim,
                    f"only {mem.available / GIB:.1f} GiB available (floor {w.floor_gb:g} GiB); this is the largest dev server",
                )
            )
    return actions


class Log:
    def __init__(self, path: Path, echo: Callable[[str], None] | None = print) -> None:
        self.path = path
        self.echo = echo

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


Killer = Callable[[int, int], None]


def stop_server(
    server: Server, source: Source, grace: float, kill: Killer = os.kill, sleep: Callable[[float], None] = time.sleep
) -> list[int]:
    """SIGTERM the server's process tree, then SIGKILL survivors after `grace` seconds.

    A pid is only signalled while its start time still matches the snapshot, so a
    reused pid is never hit. Returns the pids that were signalled."""

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


def run_once(
    source: Source,
    config: Config,
    apply: bool,
    log: Log,
    kill: Killer = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    self_pid: int | None = None,
) -> list[Action]:
    servers = discover(source, config, self_pid=self_pid)
    if config.watch.stop_finished:
        annotate(servers, config.state_path() / "fetched", config.watch.fetch_interval_minutes * 60)
    actions = plan(servers, source.meminfo(), config)
    for action in actions:
        if action.verb == "warn":
            log.write(f"WARN {describe(action.server)}: {action.reason}")
        elif apply:
            log.write(f"STOP {describe(action.server)}: {action.reason}")
            stop_server(action.server, source, config.watch.grace_seconds, kill, sleep)
        else:
            log.write(f"WOULD-STOP {describe(action.server)}: {action.reason} (dry run; use --apply to stop)")
    return actions


def loop(source: Source, config: Config, apply: bool, log: Log) -> None:
    while True:
        run_once(source, config, apply, log)
        time.sleep(config.watch.interval_seconds)


# --- systemd timer -------------------------------------------------------

SERVICE = "devguard-watch.service"
TIMER = "devguard-watch.timer"


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def exec_command(apply: bool, config_path: Path | None) -> list[str]:
    """How the timer runs devguard: the same interpreter and package as this process."""
    cmd = [sys.executable, "-m", "devguard"]
    if config_path:
        cmd += ["--config", str(config_path)]
    cmd += ["watch", "--once"]
    if apply:
        cmd.append("--apply")
    return cmd


def units(apply: bool, config_path: Path | None, path_env: str) -> tuple[str, str]:
    command = " ".join(shlex.quote(c) for c in exec_command(apply, config_path))
    mode = "stops servers" if apply else "dry run: logs what it would stop"
    service = (
        "[Unit]\n"
        f"Description=devguard watch ({mode})\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=PATH={path_env}\n"
        f"ExecStart={command}\n"
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
    service, timer = units(apply, config_path, os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
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
