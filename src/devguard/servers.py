"""Find running dev and preview servers in a process snapshot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import gitinfo
from .config import Config, CustomServer
from .fmt import is_within
from .procs import MemInfo, Proc, Snapshot

# Commands that only launch the real server; the server is their child.
WRAPPERS = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "fish",
    "npm",
    "npx",
    "pnpm",
    "pnpx",
    "yarn",
    "corepack",
    "env",
    "timeout",
    "nohup",
    "nice",
    "devguard",
    "concurrently",
    "turbo",
    "nx",
}
RUNTIMES = {"node", "nodejs", "bun", "deno", "tsx"}
RUNTIME_FLAGS_WITH_VALUE = {"-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions"}

# tool -> {subcommand: kind}. None means "no subcommand".
TOOLS: dict[str, dict[str | None, str]] = {
    "next": {"dev": "dev", "start": "preview"},
    "vite": {None: "dev", "dev": "dev", "serve": "dev", "preview": "preview"},
    "wrangler": {"dev": "dev"},
    "astro": {"dev": "dev", "preview": "preview"},
    "nuxt": {"dev": "dev", "preview": "preview"},
    "nuxi": {"dev": "dev", "preview": "preview"},
    "remix": {"dev": "dev"},
    "react-router": {"dev": "dev"},
    "webpack": {"serve": "dev"},
    "webpack-dev-server": {None: "dev"},
    "storybook": {"dev": "dev"},
}
NOT_SERVER = {"build", "optimize", "test", "lint", "deploy", "publish", "--version", "--help"}


class Source(Protocol):
    def snapshot(self) -> Snapshot: ...
    def identity(self, pid: int) -> str | None: ...
    def environ_has(self, pid: int, names: list[str]) -> bool: ...
    def environ_get(self, pid: int, name: str) -> str | None: ...
    def listening_ports(self, pids: list[int]) -> dict[int, list[int]]: ...
    def meminfo(self) -> MemInfo: ...


@dataclass
class Server:
    pid: int
    kind: str  # dev | preview
    tool: str
    argv: list[str]
    port: str
    rss: int
    tree: list[int]
    identities: dict[int, str]
    age: float
    cwd: str | None
    cwd_gone: bool
    exempt_memory: bool = False
    # filled by annotate()
    project: str = "?"
    worktree: str | None = None
    branch: str | None = None
    state: str = "unknown"
    reason: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.state in ("merged", "gone", "removed")


def _base(arg: str) -> str:
    return os.path.basename(arg)


def script_and_args(argv: list[str]) -> tuple[str, list[str]] | None:
    """The program a process runs (resolving `node .../bin/next`) and its arguments."""
    if not argv:
        return None
    prog = _base(argv[0])
    if prog in WRAPPERS:
        return None
    if prog in RUNTIMES or prog.startswith("node"):
        rest = argv[1:]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in RUNTIME_FLAGS_WITH_VALUE else 1
        if i >= len(rest):
            return None
        if prog == "bun" and rest[i] in ("run", "x"):
            i += 1
            if i >= len(rest):
                return None
        return _base(rest[i]), rest[i + 1 :]
    return prog, argv[1:]


def classify(argv: list[str], custom: list[CustomServer] | None = None) -> tuple[str, str] | None:
    """(tool, kind) for a server process, or None."""
    joined = " ".join(argv)
    for entry in custom or []:
        if argv and _base(argv[0]) not in WRAPPERS and all(m in joined for m in entry.match):
            return entry.name, entry.kind
    parsed = script_and_args(argv)
    if not parsed:
        return None
    script, args = parsed
    script = script.removesuffix(".js").removesuffix(".mjs").removesuffix(".cjs")
    modes = TOOLS.get(script)
    if not modes:
        return None
    for arg in args:
        if not arg.startswith("-") and arg in modes:
            return script, modes[arg]
    if None in modes and not any(a in NOT_SERVER for a in args):
        return script, modes[None]  # plain `vite` (maybe with flags) is the dev server
    return None


def port_from_argv(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg in ("-p", "--port") and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--port="):
            return arg.split("=", 1)[1]
    return None


def _is_next_start_wrapper(argv: list[str]) -> bool:
    if not argv or _base(argv[0]) not in ("sh", "bash", "dash"):
        return False
    words = " ".join(argv[1:]).replace("'", " ").replace('"', " ").split()
    return "next" in words and ("start" in words or "dev" in words)


def discover(source: Source, config: Config, snap: Snapshot | None = None, self_pid: int | None = None) -> list[Server]:
    snap = snap or source.snapshot()
    lineage = snap.lineage(self_pid if self_pid is not None else os.getpid())
    kids = snap.children()
    uid = getattr(source, "uid", os.getuid())
    hits: dict[int, tuple[str, str, list[str]]] = {}
    for proc in snap.procs.values():
        if proc.uid != uid or proc.pid in lineage:
            continue
        found = classify(proc.argv, config.custom_servers)
        if found:
            hits[proc.pid] = (found[0], found[1], proc.argv)
        elif proc.argv and proc.argv[0].startswith("next-server"):
            # `npx next start` renames its node process "next-server (vX)"; the
            # command and port are on its `sh -c` parent.
            parent = snap.procs.get(proc.ppid)
            if parent and _is_next_start_wrapper(parent.argv):
                words = " ".join(parent.argv[1:]).replace("'", " ").replace('"', " ").split()
                hits[proc.pid] = ("next", "dev" if "dev" in words else "preview", words)
    # Keep only the outermost server of each process tree.
    tops = [pid for pid in hits if not any(a in hits for a in snap.ancestors(pid))]
    servers = []
    for pid in tops:
        tool, kind, argv = hits[pid]
        proc: Proc = snap.procs[pid]
        tree = snap.tree(pid, kids)
        port = port_from_argv(argv)
        if port is None:
            listening = source.listening_ports(tree)
            ports = sorted({p for pid_ports in listening.values() for p in pid_ports})
            port = ",".join(str(p) for p in ports) if ports else None
        if port is None:
            port = source.environ_get(pid, "PORT")
        cwd = proc.cwd
        gone = proc.cwd_deleted or (cwd is not None and not os.path.isdir(cwd))
        servers.append(
            Server(
                pid=pid,
                kind=kind,
                tool=tool,
                argv=proc.argv,
                port=port or "?",
                rss=sum(snap.procs[p].rss for p in tree if p in snap.procs),
                tree=tree,
                identities={p: snap.procs[p].identity for p in tree if p in snap.procs},
                age=proc.age,
                cwd=cwd,
                cwd_gone=gone,
                exempt_memory=kind == "dev" and source.environ_has(pid, config.watch.memory_exempt_env),
            )
        )
    roots = [str(Path(os.path.expanduser(r)).absolute()) for r in config.watch.roots]
    if roots:
        servers = [s for s in servers if s.cwd and any(is_within(s.cwd, r) for r in roots)]
    return sorted(servers, key=lambda s: -s.rss)


def annotate(servers: list[Server], fetch_stamp_dir: Path | None = None, fetch_interval: float = 0) -> None:
    """Fill in project, worktree, branch and finished state from git."""
    cache: dict[str, tuple[str, str | None, gitinfo.BranchState]] = {}
    for s in servers:
        if s.cwd is None:
            s.state, s.reason = "unknown", "working directory unreadable"
            continue
        if s.cwd_gone:
            s.state, s.reason = "removed", "its worktree was removed"
            continue
        top = gitinfo.toplevel(s.cwd)
        if not top:
            s.state, s.reason = "no-git", "not in a git repository"
            continue
        s.worktree = top
        if top not in cache:
            if fetch_stamp_dir is not None:
                gitinfo.fetch_if_stale(top, fetch_stamp_dir, fetch_interval)
            branch = gitinfo.current_branch(top)
            cache[top] = (gitinfo.project_name(top), branch, gitinfo.branch_state(top, branch))
        s.project, s.branch, state = cache[top]
        s.state, s.reason = state.state, state.reason
