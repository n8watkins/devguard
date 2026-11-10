"""devguard command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, gitinfo
from . import port as portmod
from . import report as reportmod
from . import slot as slotmod
from . import spend as spendmod
from . import status as statusmod
from . import sweep as sweepmod
from . import watch as watchmod
from . import worktrees as wtmod
from .config import DEFAULT_TOML, Config, ConfigError, default_config_path, load
from .fmt import duration, home, size, table
from .procs import default_source
from .servers import annotate, discover

FINISHED_NOTE = "STATE merged/gone/removed means the work is done; `devguard watch --apply` stops those servers."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devguard",
        description="Keep parallel AI coding agents from running a machine out of memory, ports or disk.",
        epilog="Run devguard <command> -h for that command's options. Docs: https://github.com/n8watkins/devguard",
    )
    parser.add_argument("--version", action="version", version=f"devguard {__version__}")
    parser.add_argument("--config", type=Path, help="config file (default: ~/.config/devguard/config.toml)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("ps", help="list running dev servers with memory, port, branch and state")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--fetch", action="store_true", help="git fetch --prune first so merged state is current")
    p.add_argument("--all", action="store_true", help="ignore watch.roots and show every dev server")

    p = sub.add_parser("watch", help="apply the memory and cleanup policy to dev servers")
    p.add_argument("--once", action="store_true", help="one pass, then exit (what the timer runs)")
    p.add_argument("--apply", action="store_true", help="really stop servers (default: dry run, only log)")
    p.add_argument("--install", action="store_true", help="install a systemd user timer that runs every minute")
    p.add_argument("--uninstall", action="store_true", help="remove the systemd user timer")
    p.add_argument("--log", action="store_true", help="show recent watch events")

    p = sub.add_parser("status", help="memory by use, dev servers by project, last stop and loop alerts")
    p.add_argument("--json", action="store_true", help="the same document watch writes to status.json")

    p = sub.add_parser("stop", help="stop the dev servers on a port or under a directory")
    p.add_argument("target", help="a port number, or a directory (every dev server under it)")

    p = sub.add_parser("report", help="how the watcher behaved over time, and what to change")
    p.add_argument("--since", default="7d", help="period to cover, like 7d, 12h or 2w (default: 7d)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--import-dev-watch", metavar="PATH", type=Path, help="also read the log of the older dev-watch script (read-only)"
    )

    p = sub.add_parser("port", help="print a stable port for a directory: next dev -p $(devguard port)")
    p.add_argument("path", nargs="?", help="directory (default: current directory)")
    p.add_argument("--write", action="store_true", help="also write the port to <dir>/.dev-port")
    p.add_argument("--list", metavar="REPO", nargs="?", const=".", help="ports of every worktree of a repo, with collisions")
    p.add_argument("--quiet", "-q", action="store_true", help="do not warn when the port is already in use")

    p = sub.add_parser("slot", help="run a heavy command when one of N global slots is free")
    p.add_argument("-n", "--slots", type=int, help="number of slots (default: slot.count or $DEVGUARD_SLOTS, 4)")
    p.add_argument("--timeout", type=float, help="give up after this many seconds waiting (exit 75)")
    p.add_argument("--status", action="store_true", help="show which slots are busy and with what")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command and arguments")

    p = sub.add_parser("worktrees", help="branch, dirty, ahead/behind, merged and pushed for every worktree")
    p.add_argument("repo", nargs="?", default=".", help="repository (default: current directory)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("sweep", help="delete build artifacts in finished worktrees (dry run by default)")
    p.add_argument("repos", nargs="*", help="repositories (default: sweep.repos, else the current one)")
    p.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
    p.add_argument("--include-primary", action="store_true", help="also consider the primary worktree")
    p.add_argument("--verbose", "-v", action="store_true", help="measure and list artifacts of blocked worktrees too")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("spend", help="tokens each running agent session spent since you last typed")
    p.add_argument("--json", action="store_true", help="print JSON")
    p = sub.add_parser("config", help="show the config file path and effective settings")
    p.add_argument("--default", action="store_true", help="print the documented default config")
    return parser


def cmd_ps(args: argparse.Namespace, config: Config) -> int:
    if args.all:
        config.watch.roots = []
    source = default_source()
    servers = discover(source, config)
    annotate(servers, config.state_path() / "fetched" if args.fetch else None, 1 if args.fetch else 0)
    mem = source.meminfo()
    if args.json:
        print(
            json.dumps(
                {
                    "memory": {"total": mem.total, "available": mem.available, "swapUsed": mem.swap_used},
                    "servers": [
                        {
                            "pid": s.pid,
                            "kind": s.kind,
                            "tool": s.tool,
                            "port": s.port,
                            "rss": s.rss,
                            "uptimeSeconds": round(s.age),
                            "project": s.project,
                            "worktree": s.worktree,
                            "cwd": s.cwd,
                            "branch": s.branch,
                            "state": s.state,
                            "reason": s.reason,
                            "finished": s.finished,
                            "memoryExempt": s.exempt_memory,
                            "pids": s.tree,
                        }
                        for s in servers
                    ],
                },
                indent=2,
            )
        )
        return 0
    if mem.total:
        print(
            f"Memory: {size(mem.total - mem.available)} used of {size(mem.total)}, "
            f"{size(mem.available)} available, {size(mem.swap_used)} swap in use"
        )
    if not servers:
        print("No dev servers running.")
        return 0
    rows = [
        [
            str(s.pid),
            f"{s.tool} {s.kind}" + (" (exempt)" if s.exempt_memory else ""),
            s.port,
            size(s.rss),
            duration(s.age),
            s.project,
            s.branch or "-",
            s.state,
            home(s.worktree or s.cwd),
        ]
        for s in servers
    ]
    print(table(["PID", "SERVER", "PORT", "MEM", "UPTIME", "PROJECT", "BRANCH", "STATE", "WORKTREE"], rows, right={0, 3, 4}))
    if any(s.finished for s in servers):
        print("\n" + FINISHED_NOTE)
    return 0


def cmd_watch(args: argparse.Namespace, config: Config) -> int:
    log_path = config.state_path() / "watch.log"
    if args.log:
        text = log_path.read_text() if log_path.exists() else ""
        print("\n".join(text.splitlines()[-40:]) if text else "No watch events yet.")
        return 0
    if args.install:
        return watchmod.install(args.apply, config.path)
    if args.uninstall:
        return watchmod.uninstall()
    source = default_source()
    log = watchmod.Log(log_path)
    if args.once:
        actions = watchmod.run_once(source, config, args.apply, log)
        if not actions:
            print("All dev servers are within limits.")
        return 0
    mode = "stopping servers" if args.apply else "dry run, logging only"
    print(f"devguard watch: checking every {config.watch.interval_seconds:g}s ({mode}); Ctrl-C to stop.")
    try:
        watchmod.loop(source, config, args.apply, log)
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    source = default_source()
    snap = source.snapshot()
    servers = discover(source, config, snap)
    annotate(servers)
    state = watchmod.WatchState.load(config.state_path() / watchmod.STATE_FILE)
    document = watchmod.status_document(source, config, servers, source.meminfo(), snap, state, time.time(), "status")
    if args.json:
        print(json.dumps(document, indent=2))
        return 0
    print(statusmod.render(document))
    written = config.state_path() / statusmod.FILE
    try:
        last = json.loads(written.read_text()).get("updated_at", "?")
        print(f"\nWatch writes {home(written)} every pass (last: {last}).")
    except (OSError, ValueError):
        print(f"\nNo {home(written)} yet; `devguard watch` writes it on every pass.")
    windows = statusmod.windows_path(config)
    if windows:
        print(f"Windows copy: {windows}")
    return 0


def cmd_stop(args: argparse.Namespace, config: Config) -> int:
    log = watchmod.Log(config.state_path() / "watch.log")
    stopped = watchmod.stop_matching(default_source(), config, args.target, log)
    if not stopped:
        print(f"devguard stop: no dev server on port {args.target} or under it", file=sys.stderr)
        return 1
    return 0


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    now = time.time()
    since = now - reportmod.parse_since(args.since)
    inputs = reportmod.load(config.state_path(), since, args.import_dev_watch.expanduser() if args.import_dev_watch else None)
    result = reportmod.build(inputs, config, since, now)
    print(json.dumps(result, indent=2) if args.json else reportmod.render(result))
    return 0


def cmd_port(args: argparse.Namespace, config: Config) -> int:
    if args.list is not None:
        return port_list(args.list, config)
    directory = portmod.resolve_dir(args.path)
    number = portmod.port_of(directory, config)
    if args.write or config.port.write_file:
        portmod.write_port_file(directory, number)
    if not args.quiet and portmod.listening(number):
        print(
            f"devguard port: something already listens on http://localhost:{number} "
            f"(reuse that server, or set a [port.overrides] entry for {directory})",
            file=sys.stderr,
        )
    print(number)
    return 0


def port_list(repo: str, config: Config) -> int:
    trees = [t for t in gitinfo.worktrees(repo) if not t.bare]
    by_port: dict[int, list[str]] = {}
    for t in trees:
        by_port.setdefault(portmod.port_of(t.path, config), []).append(t.path)
    rows = []
    for t in trees:
        number = portmod.port_of(t.path, config)
        clash = "COLLISION" if len(by_port[number]) > 1 else ""
        rows.append([str(number), t.branch or "(detached)", clash, home(t.path)])
    print(table(["PORT", "BRANCH", "", "WORKTREE"], rows))
    chance = portmod.collision_chance(len(trees), config.port.range)
    print(f"\n{len(trees)} worktrees in {config.port.range} ports: {chance:.1%} chance that any two collide.")
    if any(len(v) > 1 for v in by_port.values()):
        print("Fix a collision with a [port.overrides] entry in the config file.")
        return 1
    return 0


def cmd_slot(args: argparse.Namespace, config: Config) -> int:
    count = slotmod.slot_count(config, args.slots)
    if args.status:
        for number, holder in slotmod.status(slotmod.slot_dir(config), count):
            print(f"slot {number}: {holder or 'free'}")
        return 0
    command = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not command:
        print("usage: devguard slot [-n N] -- <command> [args...]", file=sys.stderr)
        return 2
    return slotmod.run(command, config, count, args.timeout)


def cmd_worktrees(args: argparse.Namespace, config: Config) -> int:
    default, rows = wtmod.status(args.repo)
    if args.json:
        print(json.dumps({"default": default, "worktrees": [r.to_dict() for r in rows]}, indent=2))
        return 0
    out = []
    for r in rows:
        ab = f"+{r.ahead}/-{r.behind}" if r.ahead is not None else "-"
        flags = (" (primary)" if r.primary else "") + (" (locked)" if r.locked else "")
        out.append(
            [home(r.path) + flags, r.branch or "(detached)", "-" if r.dirty is None else str(r.dirty), ab, r.merged, r.pushed]
        )
    print(table(["WORKTREE", "BRANCH", "DIRTY", "AHEAD/BEHIND", "MERGED", "PUSHED"], out, right={2, 3}))
    print(
        f"\nAhead/behind is against {default}. MERGED: merged = merged into {default}; gone = deleted on origin "
        "(usually squash-merged); unpushed = never pushed, so unknown; open = not merged."
    )
    return 0


def sweep_repos(args: argparse.Namespace, config: Config) -> list[str]:
    raw = args.repos or [os.path.expanduser(r) for r in config.sweep.repos] or ["."]
    repos = []
    for r in raw:
        top = gitinfo.toplevel(r)
        if not top:
            raise ConfigError(f"{r} is not a git repository")
        common = gitinfo.common_dir(top)
        if common and Path(common).name == ".git":
            top = str(Path(common).parent)  # always report the primary worktree's repo
        if top not in repos:
            repos.append(top)
    return repos


def cmd_sweep(args: argparse.Namespace, config: Config) -> int:
    repos = sweep_repos(args, config)
    source = default_source()
    candidates = []
    for repo in repos:
        candidates += sweepmod.plan_repo(repo, source.snapshot(), config, args.include_primary, measure_all=args.verbose)
    if args.apply:
        try:
            with sweepmod.SweepLock(config.state_path()):
                actions = sweepmod.apply(candidates, source.snapshot, config, args.include_primary)
        except RuntimeError as exc:
            print(f"devguard sweep: {exc}", file=sys.stderr)
            return 1
        receipt = sweepmod.write_receipt(config.state_path(), actions, repos)
        if args.json:
            print(json.dumps({"receipt": str(receipt), "actions": actions}, indent=2))
        else:
            for a in actions:
                why = f" ({'; '.join(a.get('reasons', []))})" if a.get("reasons") else ""
                print(f"{a['result'].upper():8} {size(a.get('bytes'))}  {home(a['path'])}{why}")
            freed = sum(a.get("bytes", 0) for a in actions if a["result"] == "deleted")
            print(f"\nFreed {size(freed)}. Receipt: {home(receipt)}" if actions else "Nothing to delete.")
        return 0 if all(a["result"] == "deleted" for a in actions) else 1
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "repo": c.repo,
                        "worktree": c.worktree.path,
                        "branch": c.worktree.branch,
                        "eligible": c.eligible,
                        "blockers": c.blockers,
                        "skipped": [{"path": p, "why": w} for p, w in c.skipped],
                        "artifacts": [
                            {"path": a.path, "bytes": a.bytes, "sharedBytes": a.shared_bytes, "blockers": a.blockers}
                            for a in c.artifacts
                        ],
                    }
                    for c in candidates
                ],
                indent=2,
            )
        )
        return 0
    print_sweep_plan(candidates, args.verbose)
    return 0


def print_sweep_plan(candidates: list[sweepmod.Candidate], verbose: bool) -> None:
    total = shared = count = 0
    for c in candidates:
        label = f"{home(c.worktree.path)} [{c.worktree.branch or 'detached'}]"
        if not c.eligible:
            print(f"keep   {label}: {'; '.join(c.blockers)}")
            if not verbose:
                continue
        else:
            print(f"sweep  {label}")
        for a in c.artifacts:
            rel = os.path.relpath(a.path, c.worktree.path)
            if a.blockers:
                print(f"         keep  {size(a.bytes):>6}  {rel}: {'; '.join(a.blockers)}")
            elif c.eligible:
                print(f"         del   {size(a.bytes):>6}  {rel}")
                total += a.bytes
                shared += a.shared_bytes
                count += 1
            else:
                print(f"               {size(a.bytes):>6}  {rel}")
        for path, why in c.skipped:
            print(f"         skip          {os.path.relpath(path, c.worktree.path)}: {why}")
        if c.eligible and not c.artifacts:
            print("         (no build artifacts)")
    print(f"\nWould delete {count} director{'y' if count == 1 else 'ies'}, {size(total)}.", end="")
    if shared:
        print(f" {size(shared)} of that is hard-linked elsewhere (for example a pnpm store) and stays on disk.", end="")
    print("\nDry run: nothing was deleted. Run again with --apply to delete.")


def cmd_spend(args: argparse.Namespace, config: Config) -> int:
    sp = config.spend
    now = time.time()
    sessions = spendmod.scan(config.state_path(), sp.active_minutes, now)
    if args.json:
        print(json.dumps([{**spendmod.alert(s, now), "log": home(s.path)} for s in sessions], indent=2))
        return 0
    if not sessions:
        print(f"No agent session wrote to its log in the last {sp.active_minutes:g} minutes.")
        return 0
    rows = []
    for s in sessions:
        flag = "OVER" if sp.warn_tokens and s.turn >= sp.warn_tokens else ""
        rows.append(
            [s.agent, spendmod.tokens(s.turn), str(s.calls), spendmod.tokens(s.total), flag, home(s.cwd) if s.cwd else "?"]
        )
    print(table(["AGENT", "TURN", "CALLS", "SESSION", "", "WHERE"], rows, right={1, 2, 3}))
    limit = f"warn at {spendmod.tokens(sp.warn_tokens)} per turn" if sp.enabled and sp.warn_tokens else "warnings off"
    print(f"\nTURN = tokens since the last prompt (fresh input, cache writes, output); {limit}.")
    return 0


def cmd_config(args: argparse.Namespace, config: Config) -> int:
    if args.default:
        print(DEFAULT_TOML, end="")
        return 0
    path = config.path or default_config_path()
    print(f"Config file: {path}" + ("" if config.path else " (not found, using defaults)"))
    print(f"State dir:   {config.state_path()}")
    print(f"Slot dir:    {slotmod.slot_dir(config)} ({config.slot.count} slots)")
    w = config.watch
    print(
        f"Watch:       warn {w.soft_gb:g} GiB, stop {w.hard_gb:g} GiB per dev server; floor {w.floor_gb:g} GiB; "
        f"stop finished: {'yes' if w.stop_finished else 'no'}"
    )
    print(f"             metrics: {config.state_path() / 'metrics.jsonl' if w.metrics else 'off'}")
    windows = statusmod.windows_path(config)
    print(f"             status: {config.state_path() / statusmod.FILE}" + (f", Windows copy {windows}" if windows else ""))
    if w.roots:
        print(f"             only under: {', '.join(w.roots)}")
    if w.protected_paths:
        print(f"             never stop under: {', '.join(w.protected_paths)}")
    if w.memory_exempt_env:
        print(f"             memory-exempt env: {', '.join(w.memory_exempt_env)}")
    print(
        f"Ports:       {config.port.base}-{config.port.base + config.port.range - 1}"
        + (f", {len(config.port.overrides)} override(s)" if config.port.overrides else "")
    )
    print(
        f"Sweep:       {', '.join(config.sweep.artifacts)} (depth {config.sweep.max_depth}, "
        f"idle {config.sweep.min_idle_hours:g}h)"
    )
    sp = config.spend
    print(
        "Spend:       "
        + (
            f"warn at {spendmod.tokens(sp.warn_tokens)} tokens per turn, sessions active in the last {sp.active_minutes:g} min"
            if sp.enabled and sp.warn_tokens
            else "off"
        )
    )
    return 0


COMMANDS = {
    "ps": cmd_ps,
    "watch": cmd_watch,
    "status": cmd_status,
    "stop": cmd_stop,
    "report": cmd_report,
    "port": cmd_port,
    "slot": cmd_slot,
    "worktrees": cmd_worktrees,
    "sweep": cmd_sweep,
    "spend": cmd_spend,
    "config": cmd_config,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        config = load(args.config.expanduser().absolute() if args.config else None)
        return COMMANDS[args.command](args, config)
    except ConfigError as exc:
        print(f"devguard: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, NotADirectoryError, ValueError, PermissionError) as exc:
        print(f"devguard: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
