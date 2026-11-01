"""Configuration: ~/.config/devguard/config.toml with documented defaults.

Every key is optional. Unknown keys are an error, so a typo never silently
falls back to a default. `devguard config --default` prints DEFAULT_TOML.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ARTIFACTS = [
    "node_modules",
    ".next",
    "dist",
    "build",
    "out",
    ".turbo",
    "target",
    ".svelte-kit",
    ".nuxt",
    ".output",
    ".parcel-cache",
    ".vite",
    "coverage",
]

DEFAULT_TOML = """\
# devguard configuration. Every key is optional; the values below are the defaults.
# Location: ~/.config/devguard/config.toml (or $XDG_CONFIG_HOME/devguard/config.toml,
# or the path in $DEVGUARD_CONFIG, or --config PATH).

# Where watch logs and sweep receipts go. Empty means $XDG_STATE_HOME/devguard
# (usually ~/.local/state/devguard).
state_dir = ""

[watch]
# Log a warning when one dev server's process tree uses more than this many GiB.
soft_gb = 3.0
# Stop a dev server whose process tree uses more than this many GiB.
hard_gb = 5.0
# When the machine has less than this many GiB available, stop the largest dev server...
floor_gb = 1.5
# ...but only if it uses more than this many GiB (stopping a small one would not help).
min_victim_gb = 1.0
# Stop dev and preview servers whose worktree is gone or whose branch is merged.
stop_finished = true
# Seconds between SIGTERM and SIGKILL when stopping a server.
grace_seconds = 10
# Seconds between passes for `devguard watch` without --once.
interval_seconds = 60
# Minimum minutes between `git fetch --prune origin` runs per repository (0 disables fetching).
fetch_interval_minutes = 10
# Only manage servers whose working directory is under one of these paths.
# Empty means every dev server of your user.
roots = []
# Never stop servers whose working directory is under one of these paths.
protected_paths = []
# Dev servers whose environment contains one of these variables skip the per-server
# limits (soft_gb, hard_gb). The low-memory floor still applies to them.
# Example: a test stack that is known to need more memory for a full run.
memory_exempt_env = []

[port]
# Ports are base + (sha1(absolute path) mod range), so 4000-4999 by default.
base = 4000
range = 1000
# Also write the port to <dir>/.dev-port (add it to .gitignore if you turn this on).
write_file = false

# Fixed ports for paths that collide, as "absolute path" = port.
[port.overrides]

[slot]
# How many heavy jobs (builds, Playwright, Lighthouse) may run at once across all agents.
# $DEVGUARD_SLOTS and `devguard slot -n N` override this.
count = 4
# Directory for the slot lock files. Empty means $XDG_RUNTIME_DIR/devguard-slots,
# or /tmp/devguard-slots-<uid> when XDG_RUNTIME_DIR is unset.
dir = ""

[sweep]
# Repositories to sweep when none is given on the command line. Empty means the
# repository containing the current directory.
repos = []
# Directory names that count as build artifacts. A directory is only deleted when
# git ignores it and it contains no tracked files.
artifacts = [
  "node_modules", ".next", "dist", "build", "out", ".turbo", "target",
  ".svelte-kit", ".nuxt", ".output", ".parcel-cache", ".vite", "coverage",
]
# How deep to look for artifact directories below each worktree root (monorepos keep
# them in apps/*/ and packages/*/).
max_depth = 4
# Skip a worktree whose git index or artifacts changed within this many hours.
min_idle_hours = 24

# Extra dev server commands to recognize, in addition to the built-in list
# (next, vite, wrangler, astro, nuxt, remix, react-router, webpack, storybook).
# A process matches when every string in `match` appears in its command line.
# [[servers.custom]]
# name = "django"
# kind = "dev"          # "dev" (memory limits apply) or "preview"
# match = ["manage.py", "runserver"]
"""


class ConfigError(Exception):
    """The configuration file is invalid."""


@dataclass
class WatchConfig:
    soft_gb: float = 3.0
    hard_gb: float = 5.0
    floor_gb: float = 1.5
    min_victim_gb: float = 1.0
    stop_finished: bool = True
    grace_seconds: float = 10
    interval_seconds: float = 60
    fetch_interval_minutes: float = 10
    roots: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    memory_exempt_env: list[str] = field(default_factory=list)


@dataclass
class PortConfig:
    base: int = 4000
    range: int = 1000
    write_file: bool = False
    overrides: dict[str, int] = field(default_factory=dict)


@dataclass
class SlotConfig:
    count: int = 4
    dir: str = ""


@dataclass
class SweepConfig:
    repos: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=lambda: list(DEFAULT_ARTIFACTS))
    max_depth: int = 4
    min_idle_hours: float = 24


@dataclass
class CustomServer:
    name: str
    kind: str
    match: list[str]


@dataclass
class Config:
    state_dir: str = ""
    watch: WatchConfig = field(default_factory=WatchConfig)
    port: PortConfig = field(default_factory=PortConfig)
    slot: SlotConfig = field(default_factory=SlotConfig)
    sweep: SweepConfig = field(default_factory=SweepConfig)
    custom_servers: list[CustomServer] = field(default_factory=list)
    path: Path | None = None

    def state_path(self) -> Path:
        if self.state_dir:
            return expand(self.state_dir)
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        return Path(base) / "devguard"


def expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).absolute()


def default_config_path() -> Path:
    explicit = os.environ.get("DEVGUARD_CONFIG")
    if explicit:
        return expand(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "devguard" / "config.toml"


def _check(section: str, key: str, value: Any, expected: Any) -> Any:
    name = f"{section}.{key}" if section else key
    if isinstance(expected, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{name} must be true or false")
        return value
    if isinstance(expected, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"{name} must be a non-negative number")
        return float(value)
    if isinstance(expected, int):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"{name} must be a non-negative whole number")
        return value
    if isinstance(expected, str):
        if not isinstance(value, str):
            raise ConfigError(f"{name} must be a string")
        return value
    if isinstance(expected, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{name} must be a list of strings")
        return list(value)
    raise ConfigError(f"{name}: unsupported value")  # pragma: no cover


def _fill(section: str, obj: Any, table: Any, skip: tuple[str, ...] = ()) -> None:
    if not isinstance(table, dict):
        raise ConfigError(f"[{section}] must be a table")
    for key, value in table.items():
        if key in skip:
            continue
        if not hasattr(obj, key) or key.startswith("_"):
            raise ConfigError(f"unknown key {section}.{key}")
        setattr(obj, key, _check(section, key, value, getattr(obj, key)))


def parse(data: dict[str, Any], path: Path | None = None) -> Config:
    config = Config(path=path)
    for key, value in data.items():
        if key == "state_dir":
            config.state_dir = _check("", key, value, "")
        elif key == "watch":
            _fill("watch", config.watch, value)
        elif key == "port":
            _fill("port", config.port, value, skip=("overrides",))
            overrides = value.get("overrides", {}) if isinstance(value, dict) else {}
            if not isinstance(overrides, dict):
                raise ConfigError("[port.overrides] must be a table")
            for where, port in overrides.items():
                if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                    raise ConfigError(f"port.overrides: {where!r} must map to a port number")
                config.port.overrides[str(expand(where))] = port
        elif key == "slot":
            _fill("slot", config.slot, value)
        elif key == "sweep":
            _fill("sweep", config.sweep, value)
        elif key == "servers":
            if not isinstance(value, dict) or set(value) - {"custom"}:
                raise ConfigError("[servers] only supports [[servers.custom]] entries")
            for entry in value.get("custom", []):
                if not isinstance(entry, dict) or set(entry) != {"name", "kind", "match"}:
                    raise ConfigError("each [[servers.custom]] needs exactly name, kind and match")
                if entry["kind"] not in ("dev", "preview"):
                    raise ConfigError('servers.custom kind must be "dev" or "preview"')
                match = _check("servers.custom", "match", entry["match"], [])
                if not match:
                    raise ConfigError("servers.custom match must not be empty")
                config.custom_servers.append(CustomServer(str(entry["name"]), entry["kind"], match))
        else:
            raise ConfigError(f"unknown key {key}")
    if config.port.range < 1 or config.port.base + config.port.range > 65536:
        raise ConfigError("port.base + port.range must stay within 1-65535 and range must be at least 1")
    if config.slot.count < 1:
        raise ConfigError("slot.count must be at least 1")
    return config


def load(path: Path | None = None) -> Config:
    """Load the config file, or the defaults when it does not exist."""
    path = path or default_config_path()
    if not path.exists():
        return Config(path=None)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        return parse(data, path)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
