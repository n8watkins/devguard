"""Process table sources.

LinuxProcSource reads /proc (any root, so tests can point it at a fake tree).
PsSource is the macOS fallback built on `ps`, `lsof`, `sysctl` and `vm_stat`;
it cannot read other processes' environments, so memory_exempt_env is Linux-only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    rss: int  # bytes
    argv: list[str]
    age: float  # seconds since start
    identity: str  # changes if the pid is reused (start time)
    cwd: str | None  # None when unreadable
    cwd_deleted: bool = False


@dataclass
class MemInfo:
    total: int
    available: int
    swap_used: int
    cached: int = 0  # page cache and reclaimable slabs (the buff/cache column of `free`)
    swap_total: int = 0


@dataclass
class Snapshot:
    procs: dict[int, Proc]
    complete: bool = True  # False when some of our own processes could not be read
    errors: list[str] = field(default_factory=list)
    hidden: list[int] = field(default_factory=list)  # own processes whose cwd the kernel hides

    def children(self) -> dict[int, list[int]]:
        kids: dict[int, list[int]] = {}
        for proc in self.procs.values():
            kids.setdefault(proc.ppid, []).append(proc.pid)
        return kids

    def tree(self, pid: int, kids: dict[int, list[int]] | None = None) -> list[int]:
        kids = kids if kids is not None else self.children()
        out, stack, seen = [], [pid], set()
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
            stack.extend(kids.get(p, []))
        return out

    def ancestors(self, pid: int) -> list[int]:
        out, seen = [], {pid}
        proc = self.procs.get(pid)
        while proc and proc.ppid not in seen and proc.ppid in self.procs:
            seen.add(proc.ppid)
            out.append(proc.ppid)
            proc = self.procs[proc.ppid]
        return out

    def lineage(self, pid: int) -> set[int]:
        return {pid, *self.ancestors(pid)}


class LinuxProcSource:
    def __init__(self, root: str | Path = "/proc", uid: int | None = None) -> None:
        self.root = Path(root)
        self.uid = os.getuid() if uid is None else uid
        self.page = os.sysconf("SC_PAGE_SIZE")
        self.hz = os.sysconf("SC_CLK_TCK")

    def _uptime(self) -> float:
        try:
            return float((self.root / "uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return 0.0

    def _stat(self, pid: int) -> tuple[int, int, int, str] | None:
        """(ppid, starttime ticks, rss pages, state letter)"""
        try:
            text = (self.root / str(pid) / "stat").read_text()
            fields = text.rsplit(")", 1)[1].split()
            return int(fields[1]), int(fields[19]), int(fields[21]), fields[0]
        except (OSError, ValueError, IndexError):
            return None

    def identity(self, pid: int) -> str | None:
        """The start time of a live process; None once it exited (zombies count as exited)."""
        stat = self._stat(pid)
        return str(stat[1]) if stat and stat[3] not in ("Z", "X") else None

    def snapshot(self) -> Snapshot:
        snap = Snapshot({})
        uptime = self._uptime()
        try:
            entries = [e for e in os.scandir(self.root) if e.name.isdigit()]
        except OSError as exc:
            return Snapshot({}, complete=False, errors=[f"cannot read {self.root}: {exc}"])
        for entry in entries:
            pid = int(entry.name)
            base = self.root / entry.name
            try:
                uid = entry.stat(follow_symlinks=False).st_uid
            except OSError:
                continue  # exited
            stat = self._stat(pid)
            if stat is None:
                continue
            ppid, start, rss_pages, _ = stat
            try:
                raw = (base / "cmdline").read_bytes()
            except OSError:
                raw = b""
            argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
            cwd: str | None
            deleted = False
            try:
                cwd = os.readlink(base / "cwd")
                if cwd.endswith(" (deleted)"):
                    cwd, deleted = cwd[: -len(" (deleted)")], True
            except FileNotFoundError:
                continue  # exited between reads
            except PermissionError:
                # Non-dumpable processes (systemd --user, gpg-agent, ssh-agent) hide
                # their cwd even from their owner; callers fall back to their argv.
                cwd = None
                if uid == self.uid:
                    snap.hidden.append(pid)
            except OSError as exc:
                cwd = None
                if uid == self.uid and (base / "stat").exists():
                    snap.complete = False
                    snap.errors.append(f"pid {pid}: cannot read cwd ({exc.strerror})")
            snap.procs[pid] = Proc(
                pid=pid,
                ppid=ppid,
                uid=uid,
                rss=rss_pages * self.page,
                argv=argv,
                age=max(0.0, uptime - start / self.hz),
                identity=str(start),
                cwd=cwd,
                cwd_deleted=deleted,
            )
        return snap

    def environ_has(self, pid: int, names: Iterable[str]) -> bool:
        wanted = [n.encode() + b"=" for n in names]
        if not wanted:
            return False
        try:
            data = (self.root / str(pid) / "environ").read_bytes()
        except OSError:
            return False
        return any(item.startswith(w) for item in data.split(b"\0") for w in wanted)

    def environ_get(self, pid: int, name: str) -> str | None:
        try:
            data = (self.root / str(pid) / "environ").read_bytes()
        except OSError:
            return None
        prefix = name.encode() + b"="
        for item in data.split(b"\0"):
            if item.startswith(prefix):
                return item[len(prefix) :].decode(errors="replace")
        return None

    def _listening_inodes(self) -> dict[str, int]:
        inodes: dict[str, int] = {}
        for name in ("tcp", "tcp6"):
            try:
                lines = (self.root / "net" / name).read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 10 or parts[3] != "0A":  # 0A = LISTEN
                    continue
                try:
                    inodes[parts[9]] = int(parts[1].rsplit(":", 1)[1], 16)
                except (ValueError, IndexError):
                    continue
        return inodes

    def listening_ports(self, pids: Iterable[int]) -> dict[int, list[int]]:
        inodes = self._listening_inodes()
        found: dict[int, list[int]] = {}
        if not inodes:
            return found
        for pid in pids:
            fd_dir = self.root / str(pid) / "fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    link = os.readlink(fd_dir / fd)
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    port = inodes[link[8:-1]]
                    if port not in found.setdefault(pid, []):
                        found[pid].append(port)
        return found

    def meminfo(self) -> MemInfo:
        info: dict[str, int] = {}
        try:
            for line in (self.root / "meminfo").read_text().splitlines():
                key, _, value = line.partition(":")
                parts = value.split()
                if parts:
                    info[key] = int(parts[0]) * 1024
        except (OSError, ValueError):
            pass
        total = info.get("MemTotal", 0)
        swap_total = info.get("SwapTotal", 0)
        cached = info.get("Buffers", 0) + info.get("Cached", 0) + info.get("SReclaimable", 0)
        return MemInfo(total, info.get("MemAvailable", total), swap_total - info.get("SwapFree", 0), cached, swap_total)


Runner = Callable[[list[str]], str]


def run_command(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_etime(text: str) -> float:
    """ps etime: [[dd-]hh:]mm:ss"""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


class PsSource:
    """macOS (and other non-/proc systems) via ps and lsof. Argument splitting is
    approximate because ps joins argv with spaces."""

    def __init__(self, runner: Runner = run_command, uid: int | None = None) -> None:
        self.run = runner
        self.uid = os.getuid() if uid is None else uid

    def snapshot(self) -> Snapshot:
        out = self.run(["ps", "-axww", "-o", "pid=,ppid=,uid=,rss=,etime=,lstart=,args="])
        procs: dict[int, Proc] = {}
        pattern = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.*)$")
        for line in out.splitlines():
            m = pattern.match(line)
            if not m:
                continue
            pid, ppid, uid, rss = (int(m.group(i)) for i in range(1, 5))
            try:
                age = parse_etime(m.group(5))
            except ValueError:
                age = 0.0
            procs[pid] = Proc(pid, ppid, uid, rss * 1024, m.group(7).split(), age, m.group(6), None)
        own = [p for p in procs.values() if p.uid == self.uid]
        if own:
            cwd_out = self.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(p.pid) for p in own)])
            current = None
            for line in cwd_out.splitlines():
                if line.startswith("p"):
                    current = int(line[1:]) if line[1:].isdigit() else None
                elif line.startswith("n") and current in procs:
                    procs[current].cwd = line[1:]
        return Snapshot(procs)

    def identity(self, pid: int) -> str | None:
        out = self.run(["ps", "-o", "lstart=", "-p", str(pid)]).strip()
        return out or None

    def environ_has(self, pid: int, names: Iterable[str]) -> bool:
        return False

    def environ_get(self, pid: int, name: str) -> str | None:
        return None

    def listening_ports(self, pids: Iterable[int]) -> dict[int, list[int]]:
        wanted = set(pids)
        out = self.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"])
        found: dict[int, list[int]] = {}
        current = None
        for line in out.splitlines():
            if line.startswith("p"):
                current = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith("n") and current in wanted:
                try:
                    port = int(line.rsplit(":", 1)[1])
                except (ValueError, IndexError):
                    continue
                if port not in found.setdefault(current, []):
                    found[current].append(port)
        return found

    def meminfo(self) -> MemInfo:
        try:
            total = int(self.run(["sysctl", "-n", "hw.memsize"]).strip() or 0)
        except ValueError:
            total = 0
        vm = self.run(["vm_stat"])
        page = 4096
        m = re.search(r"page size of (\d+) bytes", vm)
        if m:
            page = int(m.group(1))
        pages = {}
        for line in vm.splitlines():
            key, _, value = line.partition(":")
            value = value.strip().rstrip(".")
            if value.isdigit():
                pages[key.strip()] = int(value)
        free = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
        return MemInfo(total, free * page if pages else total, 0)


def default_source() -> LinuxProcSource | PsSource:
    if Path("/proc/self/stat").exists() and sys.platform.startswith("linux"):
        return LinuxProcSource()
    return PsSource()
