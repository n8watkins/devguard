"""report: how the watcher behaved over time, and what to change.

Sources, all read-only:
  - <state_dir>/metrics.jsonl and its archives: one sample per watch pass with memory,
    every dev server and the actions taken;
  - <state_dir>/watch.log (and watch.log.1): events, used only for times the metrics
    do not cover (before metrics existed, or with watch.metrics = false);
  - optionally the log of the older dev-watch script (--import-dev-watch), which has
    WARN and STOP events but no per-pass samples; it is marked as imported.

`build()` returns a plain dict (the --json output); `render()` turns it into text.
"""

from __future__ import annotations

import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import gitinfo, metrics
from .config import Config
from .fmt import duration, home, table

RESTART_WINDOW = 10 * 60  # a new server in the same place this soon after a stop
LOOP_GAP = 30 * 60  # stops of one place closer than this form a chain
LOOP_MIN_STOPS = 3
NEAR_FLOOR_GB = 1.0  # "near the floor" means less than floor + this available
MAX_PASS_SECONDS = 300  # a gap longer than this between passes means the watcher was off
STREAK_GAP = 150  # consecutive warnings of one server at most this far apart form a streak
FINISHED = ("merged", "gone", "removed")
SOURCES = ("devguard", "dev-watch")

UNITS = {"GiB": 1.0, "G": 1.0, "M": 1 / 1024, "K": 1 / 1024**2, "B": 1 / 1024**3}
LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (WARN|STOP|WOULD-STOP|LOOP) port (\S+) pid (\d+) "
    r"(?:\S+ (?:dev|preview) )?([\d.]+) ?(GiB|G|M|K|B) (.+?): (.*)$"
)
AVAILABLE = re.compile(r"only ([\d.]+) GiB available")
VERBS = {"WARN": "warn", "STOP": "stop", "WOULD-STOP": "would-stop", "LOOP": "loop"}

LABELS = {
    ("stop", "hard"): "stopped: over hard_gb",
    ("stop", "floor"): "stopped: memory below floor_gb",
    ("stop", "merged"): "stopped: branch merged",
    ("stop", "gone"): "stopped: branch deleted on origin",
    ("stop", "removed"): "stopped: worktree removed",
    ("warn", "soft"): "warned: over soft_gb",
    ("loop", "loop"): "restart loop: left running",
    ("hold", "merged"): "held during a restart loop",
    ("hold", "gone"): "held during a restart loop",
    ("hold", "removed"): "held during a restart loop",
}


def label(verb: str, rule: str) -> str:
    if verb == "would-stop":
        return LABELS.get(("stop", rule), f"stopped: {rule}").replace("stopped:", "would stop (dry run):")
    return LABELS.get((verb, rule), f"{verb}: {rule}")


@dataclass
class Event:
    t: float
    verb: str  # warn | stop | would-stop | hold | loop
    rule: str  # soft | hard | floor | merged | gone | removed | loop | other
    pid: int
    gb: float
    where: str  # working directory, shortened to ~/...
    source: str  # devguard | dev-watch
    app: str = ""
    quiet: bool = False  # rate-limited: not written to watch.log
    servers: int | None = None  # dev servers running in that pass (metrics only)
    avail: float | None = None  # GiB available, from the pass or from a floor stop's reason


@dataclass
class Run:
    """One dev server process, seen in one or more metrics samples."""

    app: str
    project: str
    where: str
    tool: str
    kind: str
    exempt: bool
    peak: float = 0.0
    to_soft: float | None = None
    to_hard: float | None = None
    samples: int = 0


@dataclass
class Inputs:
    samples: list[dict] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    imported_path: str | None = None


def parse_since(text: str) -> float:
    """'7d', '12h', '30m', '2w' -> seconds."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*", text or "")
    if not match:
        raise ValueError(f"--since {text!r}: use a number and m, h, d or w, like 7d or 12h")
    return float(match.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[match.group(2)]


def rule_of(verb: str, reason: str) -> str:
    if verb == "loop":
        return "loop"
    if reason.startswith("its work is done"):
        if "deleted on" in reason:
            return "gone"
        if "worktree was removed" in reason:
            return "removed"
        return "merged"
    if "(floor" in reason:
        return "floor"
    if reason.startswith("over the") and "limit" in reason:
        return "hard"
    if "restart it soon" in reason:
        return "soft"
    return "other"


def parse_log(lines: list[str], source: str) -> list[Event]:
    """WARN/STOP/WOULD-STOP/LOOP lines of devguard's watch.log or the old dev-watch log."""
    events = []
    for line in lines:
        m = LINE.match(line.rstrip("\n"))
        if not m:
            continue
        stamp, verb, _port, pid, amount, unit, where, reason = m.groups()
        try:
            t = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        verb = VERBS[verb]
        avail = AVAILABLE.search(reason)
        event = Event(t, verb, rule_of(verb, reason), int(pid), round(float(amount) * UNITS[unit], 2), home(where), source)
        event.avail = float(avail.group(1)) if avail else None
        events.append(event)
    return events


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def app_of(server: dict) -> str:
    """A name for the app a server runs, the same across worktrees: project plus subdirectory."""
    project = server.get("proj") or "?"
    if project == "?":
        return location(server)
    return f"{project} {server['dir']}" if server.get("dir") else project


def location(server: dict) -> str:
    return f"{server.get('wt') or '?'}/{server['dir']}" if server.get("dir") else server.get("wt") or "?"


MONOREPO_DIRS = ("apps", "packages", "services", "sites", "examples")


def app_names(wheres: set[str], known: dict[str, set[str]]) -> dict[str, str]:
    """App names for log events, which only carry a path.

    A path that still exists is resolved with git (project plus subdirectory). A removed
    worktree is matched by name to a known project: ~/code/app-login/apps/web becomes
    `app apps/web` when `app` is a project seen elsewhere (`known` maps projects to the
    subdirectories seen in them). Otherwise the path is the name."""
    names: dict[str, str] = {}
    known = {k: set(v) for k, v in known.items() if k and k != "?"}
    for where in wheres:
        path = os.path.expanduser(where)
        top = gitinfo.toplevel(path) if os.path.isdir(path) else None
        project = gitinfo.project_name(top) if top else "?"
        if top and project != "?":
            rel = os.path.relpath(path, top)
            names[where] = project if rel == "." else f"{project} {rel}"
            known.setdefault(project, set()).add("" if rel == "." else rel)
    for where in wheres - names.keys():
        names[where] = where
        parts = where.split("/")
        for i, part in enumerate(parts):
            match = max((p for p in known if part == p or part.startswith(p + "-")), key=len, default=None)
            if not match:
                continue
            tail = parts[i + 1 :]
            padded = "/" + "/".join(tail) + "/"
            seen = sorted((d for d in known[match] if d), key=len, reverse=True)
            sub = next((d for d in seen if padded.endswith("/" + d + "/")), None)
            if sub is None:  # cut at the first apps/, packages/... directory, else the worktree root
                cuts = [tail.index(m) for m in MONOREPO_DIRS if m in tail]
                sub = "/".join(tail[min(cuts) :]) if cuts else ""
            names[where] = f"{match} {sub}" if sub else match
            break
    return names


def load(state_dir: Path, since: float, import_path: Path | None = None) -> Inputs:
    inputs = Inputs(samples=list(metrics.read(state_dir, since)))
    log_events = []
    for name in ("watch.log.1", "watch.log"):
        log_events += parse_log(read_lines(state_dir / name), "devguard")
    if inputs.samples:  # metrics already hold the actions for the times they cover
        first, last = inputs.samples[0]["t"], inputs.samples[-1]["t"]
        log_events = [e for e in log_events if e.t < first - 1 or e.t > last + MAX_PASS_SECONDS]
    if import_path is not None:
        lines = read_lines(import_path)
        if not lines and not import_path.is_file():
            raise ValueError(f"cannot read {import_path}")
        log_events += parse_log(lines, "dev-watch")
        inputs.imported_path = home(import_path)
    known: dict[str, set[str]] = defaultdict(set)
    for r in inputs.samples:
        for s in r.get("srv", []):
            if isinstance(s, dict) and s.get("proj"):
                known[s["proj"]].add(s.get("dir", ""))
    names = app_names({e.where for e in log_events}, known)
    for e in log_events:
        e.app = names[e.where]
    inputs.events = [e for e in log_events if e.t >= since]
    return inputs


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _r(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def build(inputs: Inputs, config: Config, since: float, now: float) -> dict[str, Any]:
    w = config.watch
    samples = sorted(inputs.samples, key=lambda r: r["t"])
    events = list(inputs.events)

    # --- per pass: memory, concurrency, runs and actions from metrics -------------
    gaps = [b["t"] - a["t"] for a, b in zip(samples, samples[1:], strict=False) if 0 < b["t"] - a["t"] <= MAX_PASS_SECONDS]
    typical_gap = _median(gaps) or w.interval_seconds
    runs: dict[tuple[int, str], Run] = {}
    observed: dict[str, list[tuple[float, int, float | None]]] = defaultdict(list)  # where -> (t, pid, start)
    gib_hours: Counter[str] = Counter()
    under = near = 0.0
    under_passes = near_passes = 0
    min_avail = peak_used = peak_swap = None
    counts: list[int] = []
    floor_counts: list[int] = []
    modes: Counter[str] = Counter()
    for i, r in enumerate(samples):
        gap = samples[i + 1]["t"] - r["t"] if i + 1 < len(samples) else typical_gap
        dt = gap if 0 < gap <= MAX_PASS_SECONDS else typical_gap
        modes[r.get("mode", "?")] += 1
        mem, lim = r.get("mem", {}), r.get("lim", {})
        floor = float(lim.get("floor", w.floor_gb))
        soft, hard = float(lim.get("soft", w.soft_gb)), float(lim.get("hard", w.hard_gb))
        servers = [s for s in r.get("srv", []) if isinstance(s, dict)]
        dev = [s for s in servers if s.get("kind") == "dev"]
        counts.append(len(dev))
        if mem.get("total"):
            avail = float(mem.get("avail", 0))
            min_avail = avail if min_avail is None else min(min_avail, avail)
            used = float(mem["total"]) - avail
            peak_used = used if peak_used is None else max(peak_used, used)
            swap = float(mem.get("swap", 0))
            peak_swap = swap if peak_swap is None else max(peak_swap, swap)
            if avail < floor:
                under += dt
                under_passes += 1
                floor_counts.append(len(dev))
            elif avail < floor + NEAR_FLOOR_GB:
                near += dt
                near_passes += 1
        by_pid = {}
        for s in servers:
            where, app, size, up = location(s), app_of(s), float(s.get("gb", 0)), float(s.get("up", 0))
            by_pid[s.get("pid")] = s
            observed[where].append((r["t"], s.get("pid"), r["t"] - up))
            gib_hours[s.get("proj") if s.get("proj") not in (None, "?") else app] += size * dt / 3600
            key = (s.get("pid"), where)
            run = runs.get(key)
            if run is None:
                run = runs[key] = Run(
                    app, s.get("proj") or "?", where, s.get("tool", "?"), s.get("kind", "?"), bool(s.get("exempt"))
                )
            run.samples += 1
            run.peak = max(run.peak, size)
            if run.to_soft is None and size > soft:
                run.to_soft = up
            if run.to_hard is None and size > hard:
                run.to_hard = up
        for a in r.get("act", []):
            s = by_pid.get(a.get("pid"), {})
            events.append(
                Event(
                    r["t"],
                    a.get("verb", "?"),
                    a.get("rule") or rule_of(a.get("verb", ""), a.get("why", "")),
                    int(a.get("pid", 0)),
                    float(s.get("gb", 0)),
                    location(s),
                    "devguard",
                    app_of(s),
                    bool(a.get("quiet")),
                    len(dev),
                    float(mem["avail"]) if mem.get("total") else None,
                )
            )
    events.sort(key=lambda e: e.t)
    for e in events:
        observed[e.where].append((e.t, e.pid, None))
    for obs in observed.values():
        obs.sort(key=lambda o: o[0])

    # --- actions by reason --------------------------------------------------------
    tally: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for e in events:
        cell = tally.setdefault((e.verb, e.rule), {}).setdefault(e.source, {"events": 0, "servers": set()})
        cell["events"] += 1
        cell["servers"].add((e.pid, e.where))
    actions = [
        {
            "verb": verb,
            "rule": rule,
            "label": label(verb, rule),
            **{src: {"events": c["events"], "servers": len(c["servers"])} for src, c in per.items()},
        }
        for (verb, rule), per in sorted(tally.items(), key=lambda kv: (-sum(c["events"] for c in kv[1].values()), kv[0]))
    ]
    stops = [e for e in events if e.verb == "stop"]

    # --- repeat offenders ---------------------------------------------------------
    apps: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "runs": 0,
            "softRuns": 0,
            "hardStops": 0,
            "floorStops": 0,
            "peaks": [],
            "toSoft": [],
            "toHard": [],
            "worktrees": set(),
            "tools": Counter(),
            "logServers": {},
        }
    )
    for run in runs.values():
        if run.kind != "dev" or run.exempt:
            continue
        a = apps[run.app]
        a["runs"] += 1
        a["worktrees"].add(run.where)
        a["tools"][run.tool] += 1
        if run.to_soft is not None:
            a["softRuns"] += 1
            a["peaks"].append(run.peak)
            a["toSoft"].append(run.to_soft)
        if run.to_hard is not None:
            a["toHard"].append(run.to_hard)
    for e in events:
        if e.rule not in ("soft", "hard", "floor"):
            continue
        a = apps[e.app or e.where]
        a["worktrees"].add(e.where)
        if e.verb == "stop" and e.rule in ("hard", "floor"):
            a["hardStops" if e.rule == "hard" else "floorStops"] += 1
        if e.source != "devguard" or e.servers is None:  # log-only data: no runs, count servers by pid
            a["logServers"][e.pid] = max(a["logServers"].get(e.pid, 0.0), e.gb)
    offenders = []
    for name, a in apps.items():
        log_peaks = [g for g in a["logServers"].values() if g > 0]
        over = a["softRuns"] + len(a["logServers"])
        if not over and not a["hardStops"]:
            continue
        peaks = a["peaks"] + log_peaks
        offenders.append(
            {
                "app": name,
                "runs": a["runs"],
                "runsOverSoft": a["softRuns"],
                "importedServersOverSoft": len(a["logServers"]),
                "hardStops": a["hardStops"],
                "floorStops": a["floorStops"],
                "worktrees": len(a["worktrees"]),
                "typicalPeakGb": _r(_median(peaks)),
                "maxPeakGb": _r(max(peaks)) if peaks else None,
                "medianSecondsToSoft": _r(_median(a["toSoft"]), 0),
                "medianSecondsToHard": _r(_median(a["toHard"]), 0),
                "tool": a["tools"].most_common(1)[0][0] if a["tools"] else None,
            }
        )
    offenders.sort(key=lambda o: (-(o["hardStops"] + o["floorStops"]), -(o["runsOverSoft"] + o["importedServersOverSoft"])))

    # --- restarts soon after a stop, and stop-restart loops -----------------------
    restarted: dict[str, dict[str, Any]] = {}
    for e in stops:
        for t, pid, start in observed.get(e.where, []):
            if t <= e.t:
                continue
            if t > e.t + RESTART_WINDOW:
                break
            if pid != e.pid and (start is None or start >= e.t - 30):
                r = restarted.setdefault(
                    e.where, {"where": e.where, "app": e.app, "stops": 0, "restarted": 0, "rules": Counter()}
                )
                r["restarted"] += 1
                r["rules"][e.rule] += 1
                break
    for e in stops:
        if e.where in restarted:
            restarted[e.where]["stops"] += 1
    restarts = sorted(({**r, "rules": dict(r["rules"])} for r in restarted.values()), key=lambda r: (-r["restarted"], r["where"]))
    loops = []
    by_where: dict[str, list[Event]] = defaultdict(list)
    for e in stops:
        by_where[e.where].append(e)
    for where, evs in by_where.items():
        chain: list[Event] = []
        for e in [*evs, None]:
            if e is not None and (not chain or e.t - chain[-1].t <= LOOP_GAP):
                chain.append(e)
                continue
            # Several servers stopped in the same pass are one stop, not a restart.
            moments = 1 + sum(1 for a, b in zip(chain, chain[1:], strict=False) if b.t - a.t > 60)
            if moments >= LOOP_MIN_STOPS:
                rule = Counter(c.rule for c in chain).most_common(1)[0][0]
                loops.append(
                    {
                        "where": where,
                        "stops": len(chain),
                        "minutes": round((chain[-1].t - chain[0].t) / 60),
                        "rule": rule,
                        "start": round(chain[0].t),
                        "source": chain[0].source,
                    }
                )
            chain = [e] if e is not None else []
    loops.sort(key=lambda lp: (-lp["stops"], lp["where"]))
    held_loops = sum(1 for e in events if e.verb == "loop")

    # --- concurrency and the floor ------------------------------------------------
    floor_events = [e for e in events if e.rule == "floor" and e.verb in ("stop", "would-stop")]
    floor_event_counts = [e.servers for e in floor_events if e.servers is not None]

    # --- exempt servers, warning streaks, cost ------------------------------------
    exempt: dict[str, list[float]] = defaultdict(list)
    for run in runs.values():
        if run.exempt:
            exempt[run.app].append(run.peak)
    streak_best = {"count": 0, "where": None, "source": None}
    series: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    for e in events:
        if e.verb == "warn" and not e.quiet:
            series[(e.where, e.pid, e.source)].append(e.t)
    for (where, _pid, source), times in series.items():
        run_len = best = 1
        for a, b in zip(times, times[1:], strict=False):
            run_len = run_len + 1 if b - a <= STREAK_GAP else 1
            best = max(best, run_len)
        if best > streak_best["count"]:
            streak_best = {"count": best, "where": where, "source": source}
    total_gibh = sum(gib_hours.values())

    sources = {}
    for src in SOURCES:
        evs = [e for e in events if e.source == src]
        if evs:
            sources[src] = {"events": len(evs), "first": round(evs[0].t), "last": round(evs[-1].t)}
    report: dict[str, Any] = {
        "since": round(since),
        "until": round(now),
        "limits": {"soft": w.soft_gb, "hard": w.hard_gb, "floor": w.floor_gb},
        "passes": len(samples),
        "modes": dict(modes),
        "firstPass": samples[0]["t"] if samples else None,
        "lastPass": samples[-1]["t"] if samples else None,
        "imported": inputs.imported_path,
        "sources": sources,
        "memory": {
            "secondsUnderFloor": round(under),
            "passesUnderFloor": under_passes,
            "secondsNearFloor": round(near),
            "passesNearFloor": near_passes,
            "nearFloorGb": NEAR_FLOOR_GB,
            "lowestAvailableGb": min_avail,
            "peakUsedGb": _r(peak_used, 2),
            "peakSwapGb": peak_swap,
        },
        "concurrency": {
            "medianDevServers": _median(counts),
            "peakDevServers": max(counts) if counts else None,
            "medianDevServersUnderFloor": _median(floor_counts),
            "medianDevServersAtFloorStops": _median(floor_event_counts),
            "floorStops": len([e for e in floor_events if e.verb == "stop"]),
            "lowestAvailableAtFloorStopsGb": min((e.avail for e in floor_events if e.avail is not None), default=None),
        },
        "actions": actions,
        "stops": {
            "total": len(stops),
            "finished": len([e for e in stops if e.rule in FINISHED]),
            "hard": len([e for e in stops if e.rule == "hard"]),
            "floor": len([e for e in stops if e.rule == "floor"]),
        },
        "offenders": offenders[:10],
        "restarts": restarts[:10],
        "restartedStops": sum(r["restarted"] for r in restarts),
        "loops": loops[:10],
        "loopsHeld": held_loops,
        "exempt": sorted(
            ({"app": app, "runs": len(p), "peakGb": max(p), "medianPeakGb": _r(_median(p))} for app, p in exempt.items()),
            key=lambda x: -x["peakGb"],
        ),
        "warningStreak": streak_best,
        "gibHours": [
            {"project": name, "gibHours": round(v, 1), "share": round(v / total_gibh, 3)}
            for name, v in gib_hours.most_common(5)
            if total_gibh
        ],
    }
    report["suggestions"] = suggest(report)
    return report


# --- suggestions ------------------------------------------------------------


def suggest(r: dict[str, Any]) -> list[dict[str, str]]:
    """Rule-based: each fires only when the numbers support it."""
    out: list[dict[str, str]] = []
    lim = r["limits"]

    def add(text: str, evidence: str) -> None:
        out.append({"text": text, "evidence": evidence})

    for o in r["offenders"]:
        if len(out) >= 3:
            break
        # Measured on a Next.js 16 app: a heap cap made Next restart its own dev
        # server near the cap (failed requests) while memory still overshot it,
        # whereas Turbopack cut the peak from 8.9 to 2.9 GiB. Lead with Turbopack.
        hint = (
            "Turbopack (`next dev --turbopack`, or drop a forced `--webpack`); avoid heap caps, which make Next restart and fail requests"
            if o["tool"] in (None, "next")
            else "NODE_OPTIONS=--max-old-space-size=3072"
        )
        if o["runs"] >= 3 and o["runsOverSoft"] / o["runs"] >= 0.5:
            after = f", median {duration(o['medianSecondsToSoft'])} after start" if o["medianSecondsToSoft"] is not None else ""
            add(
                f"{o['app']} passed {lim['soft']:g} GiB in {o['runsOverSoft']} of {o['runs']} runs{after}: try {hint}.",
                f"typical peak {o['typicalPeakGb']} GiB, {o['hardStops']} hard-limit stops, {_n(o['worktrees'], 'worktree')}",
            )
        elif o["hardStops"] >= 3:
            add(
                f"{o['app']} hit the hard limit {o['hardStops']} times: try {hint}.",
                f"typical peak {o['typicalPeakGb']} GiB, max {o['maxPeakGb']} GiB, {_n(o['worktrees'], 'worktree')}",
            )

    c, floor_stops = r["concurrency"], r["stops"]["floor"]
    at_floor = c["medianDevServersAtFloorStops"] or c["medianDevServersUnderFloor"]
    if floor_stops >= 2 and at_floor is not None and at_floor >= max(4, (c["medianDevServers"] or 0) + 2):
        add(
            f"Floor stops happened with {at_floor:g}+ dev servers running: lower slot.count or the number of parallel agents, "
            "or raise WSL's memory cap (memory= in .wslconfig).",
            f"{floor_stops} floor stops; median {c['medianDevServers']:g} servers otherwise, peak {c['peakDevServers']}",
        )
    elif floor_stops >= 3:
        lowest = c["lowestAvailableAtFloorStopsGb"]
        add(
            f"{floor_stops} stops happened because memory fell below {lim['floor']:g} GiB: run fewer dev servers or agents "
            "at once, or raise WSL's memory cap (memory= in .wslconfig).",
            f"{floor_stops} of {r['stops']['total']} stops were floor stops"
            + (f"; as little as {lowest:g} GiB available" if lowest is not None else ""),
        )

    for x in r["restarts"]:
        limit_restarts = x["rules"].get("hard", 0) + x["rules"].get("floor", 0)
        if limit_restarts >= 2:
            add(
                f"{limit_restarts} stops of {x['where']} were followed by a restart within 10 minutes: "
                f"consider raising hard_gb or adding it to watch.protected_paths.",
                f"{x['restarted']} of {x['stops']} stops there were restarted soon after",
            )

    finished_loops, seen = [], set()
    for lp in r["loops"]:  # the longest loop of each place
        if lp["rule"] in FINISHED and lp["where"] not in seen:
            seen.add(lp["where"])
            finished_loops.append(lp)
    for lp in finished_loops[:3]:
        add(
            f"{lp['where']} was stopped {lp['stops']} times in {lp['minutes']} minutes after its work was done: "
            "something keeps restarting it; remove the worktree or find what restarts it.",
            f"stop-restart loop, reason {lp['rule']}, {lp['source']} data"
            + (
                f"; {_n(len(finished_loops) - 3, 'more place')} with a loop"
                if len(finished_loops) > 3 and lp is finished_loops[2]
                else ""
            ),
        )

    s = r["stops"]
    if s["finished"] >= 5 and s["finished"] * 2 >= s["total"]:
        add(
            f"{s['finished']} of {s['total']} stops were dev servers left running in finished worktrees: "
            "have agents stop their dev server when the branch merges, and remove merged worktrees.",
            ", ".join(
                f"{a['label']}: {sum(v['events'] for k, v in a.items() if k in SOURCES)}"
                for a in r["actions"]
                if a["verb"] == "stop" and a["rule"] in FINISHED
            ),
        )

    streak = r["warningStreak"]
    if streak["count"] >= 10:
        fixed = "devguard now logs a warning once, then only on +0.5 GiB or every 15 minutes"
        if streak["source"] == "devguard":
            fixed = "rate-limit warnings"
        add(
            f"Soft warnings repeated every minute for the same server: {fixed}.",
            f"{streak['count']} warnings in a row for {streak['where']} ({streak['source']} data)",
        )

    modes = r["modes"]
    would = sum(sum(v["events"] for k, v in a.items() if k in SOURCES) for a in r["actions"] if a["verb"] == "would-stop")
    if would and not modes.get("apply"):
        add(
            "watch runs in dry run: if the would-stops below look right, run `devguard watch --install --apply`.",
            f"{would} would-stop events",
        )
    return out


# --- text -------------------------------------------------------------------


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _when(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "?"


def _gb(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}G"


def render(r: dict[str, Any]) -> str:
    lines = [f"devguard report: {_when(r['since'])} to {_when(r['until'])}"]
    lim = r["limits"]
    lines.append(f"Limits: soft {lim['soft']:g} GiB, hard {lim['hard']:g} GiB, floor {lim['floor']:g} GiB")
    if r["passes"]:
        modes = ", ".join(f"{n} {m}" for m, n in sorted(r["modes"].items()))
        lines.append(f"Watch passes recorded: {r['passes']} ({modes}), {_when(r['firstPass'])} to {_when(r['lastPass'])}")
    else:
        lines.append("Watch passes recorded: none (watch writes them when watch.metrics = true)")
    for src, info in r["sources"].items():
        label = (
            f"imported from dev-watch log {r['imported']} (events only, no memory samples)" if src == "dev-watch" else "devguard"
        )
        lines.append(f"Events: {info['events']} {label}, {_when(info['first'])} to {_when(info['last'])}")
    if not r["passes"] and not r["sources"]:
        lines.append("\nNo data in this period yet.")
        return "\n".join(lines)

    if r["passes"]:
        m, c = r["memory"], r["concurrency"]
        lines += [
            "",
            "Memory",
            f"  under the floor:  {duration(m['secondsUnderFloor'])} ({m['passesUnderFloor']} passes)",
            f"  near the floor:   {duration(m['secondsNearFloor'])} ({m['passesNearFloor']} passes with less than "
            f"floor + {m['nearFloorGb']:g} GiB available)",
            f"  lowest available: {_gb(m['lowestAvailableGb'])}; peak used {_gb(m['peakUsedGb'])}; "
            f"peak swap {_gb(m['peakSwapGb'])}",
            f"  dev servers at once: median {c['medianDevServers']:g}, peak {c['peakDevServers']}"
            + (
                f"; median {c['medianDevServersUnderFloor']:g} while under the floor"
                if c["medianDevServersUnderFloor"] is not None
                else ""
            )
            + (
                f"; median {c['medianDevServersAtFloorStops']:g} at floor stops"
                if c["medianDevServersAtFloorStops"] is not None
                else ""
            ),
        ]

    if r["actions"]:
        present = [s for s in SOURCES if any(s in a for a in r["actions"])]
        heads = {"devguard": "DEVGUARD", "dev-watch": "DEV-WATCH (IMPORTED)"}
        rows = [
            [a["label"], *[f"{a[s]['events']} ({a[s]['servers']})" if s in a else "-" for s in present]] for a in r["actions"]
        ]
        lines += [
            "",
            "Actions: events (distinct servers)",
            _indent(table(["ACTION", *[heads[s] for s in present]], rows, right=set(range(1, len(present))))),
        ]

    if r["offenders"]:
        rows = []
        for o in r["offenders"]:
            runs = f"{o['runsOverSoft']}/{o['runs']}" if o["runs"] else "-"
            rows.append(
                [
                    o["app"],
                    runs,
                    str(o["importedServersOverSoft"]) if o["importedServersOverSoft"] else "-",
                    str(o["hardStops"]),
                    str(o["floorStops"]),
                    _gb(o["typicalPeakGb"]),
                    duration(o["medianSecondsToSoft"]) if o["medianSecondsToSoft"] is not None else "-",
                    duration(o["medianSecondsToHard"]) if o["medianSecondsToHard"] is not None else "-",
                    str(o["worktrees"]),
                ]
            )
        lines += [
            "",
            "Repeat offenders (dev servers over soft_gb)",
            _indent(
                table(
                    ["APP", "RUNS>SOFT", "LOG>SOFT", "HARD", "FLOOR", "PEAK", "TO-SOFT", "TO-HARD", "WORKTREES"],
                    rows,
                    right=set(range(1, 8)),
                )
            ),
            "  RUNS>SOFT: runs over soft_gb / runs seen in metrics. LOG>SOFT: servers over soft_gb in log-only data.",
            "  PEAK: typical peak of those servers. TO-SOFT, TO-HARD: median uptime when they passed the limit.",
        ]

    if r["restarts"] or r["loops"]:
        lines += ["", "Stops followed by a restart in the same place within 10 minutes"]
        lines.append(f"  {r['restartedStops']} of {r['stops']['total']} stops")
        for x in r["restarts"][:5]:
            rules = ", ".join(f"{n} {k}" for k, n in sorted(x["rules"].items()))
            lines.append(f"  {x['restarted']:>3} of {x['stops']:>3}  {x['where']} ({rules})")
        if r["loops"]:
            lines.append(f"  Stop-restart loops ({LOOP_MIN_STOPS}+ stops, each within {LOOP_GAP // 60} minutes of the last):")
            for lp in r["loops"][:5]:
                lines.append(
                    f"    {lp['stops']:>3} stops in {lp['minutes']:>3}m  {lp['where']} ({lp['rule']}, {_when(lp['start'])})"
                )
        if r["loopsHeld"]:
            lines.append(
                f"  devguard detected {r['loopsHeld']} loop(s) and left those servers running (LOOP lines in watch.log)."
            )

    if r["exempt"]:
        lines += ["", "Memory-exempt servers"]
        for x in r["exempt"]:
            lines.append(f"  {x['app']}: {x['runs']} runs, peak {_gb(x['peakGb'])}, median peak {_gb(x['medianPeakGb'])}")

    if r["gibHours"]:
        lines += ["", "Most expensive projects (GiB-hours of dev server memory)"]
        for g in r["gibHours"]:
            lines.append(f"  {g['gibHours']:>7.1f}  {g['share']:>4.0%}  {g['project']}")

    lines += ["", "What to change"]
    if r["suggestions"]:
        for s in r["suggestions"]:
            lines.append(f"  - {s['text']}")
            lines.append(f"    evidence: {s['evidence']}")
    else:
        lines.append("  Nothing stands out in this period.")
    return "\n".join(lines)


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines())
