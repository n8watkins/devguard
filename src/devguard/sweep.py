"""sweep: delete build artifacts (node_modules, .next, target, ...) in finished worktrees.

A worktree is swept only when ALL of these hold:
  - it is not the primary worktree (unless --include-primary);
  - it is not locked with `git worktree lock`;
  - its branch is merged into origin's default branch, or was deleted on origin;
  - it has no uncommitted or untracked changes;
  - no process of yours runs in it (working directory or a path argument inside it),
    and the process list could be read completely;
  - its git index and artifacts have not changed for sweep.min_idle_hours.
An artifact directory is deleted only when ALL of these hold:
  - it is a real directory: not a symlink, no symlinked parent (its real path equals
    its path), inside the worktree's real path;
  - git ignores it and it contains no tracked files;
  - it is not a mount point and contains no other filesystem and no nested .git;
  - right before deletion, every worktree check above passes again and the
    directory's (device, inode) identity is unchanged since it was measured.
Deletion first renames the directory to a sibling `.devguard-sweep-*` quarantine,
then removes it with directory file descriptors that never follow symlinks. An
interrupted sweep leaves a quarantine that the next sweep finishes. Every --apply
run writes a JSON receipt to <state_dir>/sweep/.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import gitinfo
from .config import Config
from .fmt import is_within
from .procs import Snapshot

QUARANTINE_PREFIX = ".devguard-sweep-"
RECEIPTS_KEPT = 50


@dataclass
class Artifact:
    path: str
    bytes: int = 0
    shared_bytes: int = 0  # hard-linked elsewhere (pnpm store), not freed by deleting
    newest_mtime: float = 0.0
    device: int = 0
    inode: int = 0
    blockers: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    repo: str
    worktree: gitinfo.Worktree
    state: str = ""
    blockers: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, why)

    @property
    def eligible(self) -> bool:
        return not self.blockers

    @property
    def deletable(self) -> list[Artifact]:
        return [a for a in self.artifacts if not a.blockers]


# --- discovery ---------------------------------------------------------------


def find_artifacts(root: str, names: set[str], max_depth: int) -> tuple[list[str], list[tuple[str, str]]]:
    """Artifact directories below root (not following symlinks, not entering nested
    repositories or the artifacts themselves) and symlinked ones that were skipped."""
    found: list[str] = []
    skipped: list[tuple[str, str]] = []

    def walk(directory: str, depth: int) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if entry.name == ".git":
                continue
            is_artifact = entry.name in names or entry.name.startswith(QUARANTINE_PREFIX)
            if entry.is_symlink():
                if is_artifact:
                    skipped.append((entry.path, "symlink (never followed or deleted)"))
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            if is_artifact:
                found.append(entry.path)
            elif depth < max_depth and not os.path.lexists(os.path.join(entry.path, ".git")):
                walk(entry.path, depth + 1)

    walk(root, 1)
    return found, skipped


def measure(path: str) -> Artifact:
    """Disk usage of a directory tree, counting each inode once, never following
    symlinks; also notes nested filesystems and nested .git entries."""
    art = Artifact(path)
    try:
        top = os.lstat(path)
    except OSError as exc:
        art.blockers.append(f"cannot read ({exc.strerror})")
        return art
    art.device, art.inode = top.st_dev, top.st_ino
    art.newest_mtime = top.st_mtime
    seen: set[tuple[int, int]] = set()
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            art.blockers.append(f"cannot read {current} ({exc.strerror})")
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if entry.name == ".git":
                art.blockers.append(f"contains a nested .git at {entry.path}")
            if info.st_dev != top.st_dev:
                art.blockers.append(f"contains another filesystem at {entry.path}")
                continue
            art.newest_mtime = max(art.newest_mtime, info.st_mtime)
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                seen.add(key)
                used = getattr(info, "st_blocks", 0) * 512 or info.st_size
                art.bytes += used
                if info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode):
                    art.shared_bytes += used
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry.path)
    art.blockers = sorted(set(art.blockers))[:5]
    return art


def mount_points() -> list[str] | None:
    """Mount points from /proc/self/mountinfo (Linux), or None elsewhere."""
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None
    points = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 5:
            value = fields[4]
            for escaped, plain in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
                value = value.replace(escaped, plain)
            points.append(value)
    return points


def path_blockers(path: str, worktree_real: str, mounts: list[str] | None) -> list[str]:
    blockers = []
    try:
        info = os.lstat(path)
    except OSError:
        return ["missing"]
    if stat.S_ISLNK(info.st_mode):
        return ["symlink"]
    if not stat.S_ISDIR(info.st_mode):
        return ["not a directory"]
    real = os.path.realpath(path)
    if real != os.path.normpath(path):
        blockers.append("a parent directory is a symlink")
    if not is_within(real, worktree_real) or real == worktree_real:
        blockers.append("outside the worktree")
    try:
        if os.lstat(os.path.dirname(path)).st_dev != info.st_dev:
            blockers.append("is a mount point")
    except OSError:
        blockers.append("parent unreadable")
    for point in mounts or []:
        if point == real:
            blockers.append("is a mount point")
        elif is_within(point, real):
            blockers.append(f"contains a mount at {point}")
    return sorted(set(blockers))


def git_blockers(worktree: str, path: str) -> list[str]:
    rel = os.path.relpath(path, worktree)
    blockers = []
    code, tracked = gitinfo.git(worktree, "ls-files", "--", rel)
    if code:
        blockers.append("git ls-files failed")
    elif tracked:
        blockers.append("contains files tracked by git")
    if not os.path.basename(path).startswith(QUARANTINE_PREFIX):
        code, _ = gitinfo.git(worktree, "check-ignore", "-q", "--", rel + "/")
        if code != 0:
            blockers.append("not ignored by git")
    return blockers


def dirty_paths(worktree: str) -> list[str] | None:
    """Uncommitted and untracked changes, ignoring devguard's own quarantines."""
    code, out = gitinfo.git(worktree, "status", "--porcelain", "--untracked-files=normal")
    if code:
        return None
    paths = []
    for line in out.splitlines():
        entry = line[3:].strip().strip('"').rstrip("/")
        if line.startswith("??") and os.path.basename(entry).startswith(QUARANTINE_PREFIX):
            continue
        paths.append(entry)
    return paths


def processes_inside(snap: Snapshot, root: str, real_root: str) -> list[str]:
    uid = os.getuid()
    found = []
    for proc in snap.procs.values():
        if proc.uid != uid or proc.pid == os.getpid():
            continue
        places = [proc.cwd] if proc.cwd else []
        for arg in proc.argv:
            if arg.startswith("/"):
                places.append(arg)
            elif "=/" in arg:
                places.append(arg.split("=", 1)[1])
        if any(is_within(p, root) or is_within(p, real_root) for p in places):
            name = os.path.basename(proc.argv[0]) if proc.argv else "?"
            found.append(f"process {proc.pid} ({name}) is running in it")
    return found


def worktree_blockers(
    cand: Candidate, snap: Snapshot, config: Config, include_primary: bool, default: str, now: float
) -> list[str]:
    wt = cand.worktree
    blockers: list[str] = []
    if wt.primary and not include_primary:
        return ["primary worktree (use --include-primary to allow)"]
    if wt.locked:
        return ["locked with git worktree lock"]
    if not os.path.isdir(wt.path):
        return ["worktree directory is missing (try git worktree prune)"]
    if os.path.islink(wt.path):
        return ["worktree path is a symlink"]
    state = gitinfo.branch_state(wt.path, gitinfo.current_branch(wt.path), default)
    cand.state = state.state
    if not state.finished:
        blockers.append(state.reason if state.state != "unpushed" else f"{state.reason}, so it is not known to be merged")
    dirty = dirty_paths(wt.path)
    if dirty is None:
        blockers.append("git status failed")
    elif dirty:
        blockers.append(f"{len(dirty)} uncommitted or untracked change(s)")
    if not snap.complete:
        blockers.append("the process list could not be read completely")
    blockers += processes_inside(snap, wt.path, os.path.realpath(wt.path))[:3]
    if config.sweep.min_idle_hours > 0:
        code, index = gitinfo.git(wt.path, "rev-parse", "--path-format=absolute", "--git-path", "index")
        try:
            age = now - os.stat(index).st_mtime if code == 0 else None
        except OSError:
            age = None
        if age is not None and age < config.sweep.min_idle_hours * 3600:
            blockers.append(f"git index changed {age / 3600:.1f}h ago (min_idle_hours = {config.sweep.min_idle_hours:g})")
    return blockers


def plan_repo(
    repo: str, snap: Snapshot, config: Config, include_primary: bool, now: float | None = None, measure_all: bool = False
) -> list[Candidate]:
    now = now or time.time()
    default = gitinfo.default_ref(repo)
    names = set(config.sweep.artifacts)
    mounts = mount_points()
    candidates = []
    for wt in gitinfo.worktrees(repo):
        if wt.bare:
            continue
        cand = Candidate(repo, wt)
        cand.blockers = worktree_blockers(cand, snap, config, include_primary, default, now)
        if os.path.isdir(wt.path) and not os.path.islink(wt.path):
            paths, cand.skipped = find_artifacts(wt.path, names, config.sweep.max_depth)
            real = os.path.realpath(wt.path)
            for path in paths:
                if cand.blockers and not measure_all:
                    cand.artifacts.append(Artifact(path))
                    continue
                art = measure(path)
                art.blockers = path_blockers(path, real, mounts) + git_blockers(wt.path, path) + art.blockers
                if config.sweep.min_idle_hours > 0 and now - art.newest_mtime < config.sweep.min_idle_hours * 3600:
                    art.blockers.append("changed recently")
                cand.artifacts.append(art)
        candidates.append(cand)
    return candidates


# --- deletion ----------------------------------------------------------------


def rename_no_replace(source: str, destination: str) -> None:
    """Atomic rename that fails if destination exists (renameat2 on Linux)."""
    libc = None
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError:
            libc = None
    renameat2 = getattr(libc, "renameat2", None) if libc else None
    if renameat2 is not None:
        renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        renameat2.restype = ctypes.c_int
        at_fdcwd, rename_noreplace = -100, 1
        if renameat2(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(destination), rename_noreplace) == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(err, os.strerror(err), source, destination)
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, "quarantine already exists", destination)
    os.rename(source, destination)


def _open_dir(name: str, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, dir_fd=dir_fd)


def remove_tree(path: str, device: int, inode: int) -> None:
    """Remove a directory tree through directory file descriptors: symlinks are
    unlinked, never followed; other filesystems and nested .git stop the removal;
    the root must still have the expected (device, inode)."""
    parent_fd = _open_dir(os.path.dirname(path))
    try:
        name = os.path.basename(path)
        root_fd = _open_dir(name, parent_fd)
        try:
            info = os.fstat(root_fd)
            if (info.st_dev, info.st_ino) != (device, inode):
                raise OSError(errno.ESTALE, "directory identity changed before removal", path)
            _remove_contents(root_fd, device, path)
        finally:
            os.close(root_fd)
        final = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (final.st_dev, final.st_ino) != (device, inode):
            raise OSError(errno.ESTALE, "directory identity changed during removal", path)
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _remove_contents(dir_fd: int, device: int, where: str) -> None:
    for name in os.listdir(dir_fd):
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if name == ".git":
            raise OSError(errno.EPERM, "nested .git found during removal", os.path.join(where, name))
        if info.st_dev != device:
            raise OSError(errno.EXDEV, "another filesystem found during removal", os.path.join(where, name))
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=dir_fd)
            continue
        child = _open_dir(name, dir_fd)
        try:
            if not os.path.samestat(info, os.fstat(child)):
                raise OSError(errno.ESTALE, "directory changed during removal", os.path.join(where, name))
            _remove_contents(child, device, os.path.join(where, name))
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=dir_fd)


def quarantine_path(path: str) -> str:
    name = os.path.basename(path)
    if name.startswith(QUARANTINE_PREFIX):
        return path
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(os.path.dirname(path), f"{QUARANTINE_PREFIX}{name}-{stamp}-{os.getpid()}")


def delete_artifact(art: Artifact, worktree_real: str, mounts: list[str] | None) -> dict:
    record = {"path": art.path, "bytes": art.bytes, "device": art.device, "inode": art.inode}
    blockers = path_blockers(art.path, worktree_real, mounts)
    if not blockers:
        info = os.lstat(art.path)
        if (info.st_dev, info.st_ino) != (art.device, art.inode):
            blockers.append("identity changed since it was measured")
    if blockers:
        return {**record, "result": "refused", "reasons": blockers}
    target = quarantine_path(art.path)
    try:
        if target != art.path:
            rename_no_replace(art.path, target)
            record["quarantine"] = target
            moved = os.lstat(target)
            if (moved.st_dev, moved.st_ino) != (art.device, art.inode):
                return {**record, "result": "refused", "reasons": ["quarantine identity mismatch"]}
        remove_tree(target, art.device, art.inode)
    except OSError as exc:
        return {**record, "result": "failed", "reasons": [f"{exc.strerror or exc}: {exc.filename or ''}".strip()]}
    return {**record, "result": "deleted"}


class SweepLock:
    """Only one sweep --apply at a time."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "sweep.lock"
        self.fd = -1

    def __enter__(self) -> SweepLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise RuntimeError("another devguard sweep --apply is running") from None
        return self

    def __exit__(self, *exc: object) -> None:
        os.close(self.fd)


def apply(
    candidates: list[Candidate], snapshot_fn, config: Config, include_primary: bool, now: float | None = None
) -> list[dict]:
    """Delete the deletable artifacts of eligible candidates, revalidating each
    worktree against a fresh process snapshot first. Returns receipt actions."""
    actions = []
    mounts = mount_points()
    for cand in candidates:
        if not cand.eligible or not cand.deletable:
            continue
        default = gitinfo.default_ref(cand.repo)
        fresh = Candidate(cand.repo, cand.worktree)
        blockers = worktree_blockers(fresh, snapshot_fn(), config, include_primary, default, now or time.time())
        real = os.path.realpath(cand.worktree.path)
        for art in cand.deletable:
            if blockers:
                actions.append({"path": art.path, "bytes": art.bytes, "result": "refused", "reasons": blockers})
                continue
            again = git_blockers(cand.worktree.path, art.path)
            if again:
                actions.append({"path": art.path, "bytes": art.bytes, "result": "refused", "reasons": again})
                continue
            actions.append(delete_artifact(art, real, mounts))
    return actions


def write_receipt(state_dir: Path, actions: list[dict], repos: list[str]) -> Path:
    directory = state_dir / "sweep"
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    path = directory / f"{now:%Y%m%dT%H%M%SZ}-{os.getpid()}.json"
    receipt = {
        "schemaVersion": 1,
        "command": "sweep --apply",
        "generatedAt": now.isoformat(),
        "repos": repos,
        "freedBytes": sum(a.get("bytes", 0) for a in actions if a["result"] == "deleted"),
        "actions": actions,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=2) + "\n")
    tmp.replace(path)
    receipts = sorted(p for p in directory.glob("*.json") if p.is_file() and not p.is_symlink())
    for old in receipts[:-RECEIPTS_KEPT]:
        old.unlink(missing_ok=True)
    return path
