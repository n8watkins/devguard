"""Git facts about worktrees and branches, shared by ps, watch, worktrees and sweep.

"Finished" follows one rule everywhere:
  - merged: the branch tip is an ancestor of origin's default branch AND the branch
    was pushed to its own upstream (a fresh branch cut from main is an ancestor of
    main too, and must be left alone until it has work of its own);
  - gone: the branch's upstream was deleted on origin (usually after a squash merge).
Branches that were never pushed are never treated as finished.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

REMOTE = "origin"


def git(cwd: str | Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, result.stdout.strip()


def toplevel(path: str | Path) -> str | None:
    code, out = git(path, "rev-parse", "--show-toplevel")
    return out if code == 0 and out else None


def common_dir(path: str | Path) -> str | None:
    code, out = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return out if code == 0 and out else None


def project_name(path: str | Path) -> str:
    common = common_dir(path)
    if not common:
        return "?"
    p = Path(common)
    return p.parent.name if p.name == ".git" else p.name.removesuffix(".git")


@dataclass
class Worktree:
    path: str
    head: str | None
    branch: str | None  # short name, None when detached
    primary: bool
    bare: bool = False
    locked: bool = False
    prunable: bool = False


def parse_worktrees(output: str) -> list[Worktree]:
    trees: list[Worktree] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if "worktree" in block:
            branch = block.get("branch")
            trees.append(
                Worktree(
                    path=block["worktree"],
                    head=block.get("HEAD"),
                    branch=branch.removeprefix("refs/heads/") if branch else None,
                    primary=not trees,
                    bare="bare" in block,
                    locked="locked" in block,
                    prunable="prunable" in block,
                )
            )
        block.clear()

    for line in output.splitlines():
        if not line:
            flush()
            continue
        key, _, value = line.partition(" ")
        block[key] = value
    flush()
    return trees


def worktrees(repo: str | Path) -> list[Worktree]:
    code, out = git(repo, "worktree", "list", "--porcelain")
    if code:
        raise RuntimeError(f"{repo} is not a git repository")
    return parse_worktrees(out)


def default_ref(path: str | Path) -> str:
    """origin's default branch as a ref name (origin/main), or a local main/master."""
    code, out = git(path, "symbolic-ref", "--short", f"refs/remotes/{REMOTE}/HEAD")
    if code == 0 and out:
        return out
    for candidate in (f"{REMOTE}/main", f"{REMOTE}/master", "main", "master"):
        code, _ = git(path, "rev-parse", "--verify", "--quiet", candidate)
        if code == 0:
            return candidate
    return "main"


def current_branch(path: str | Path) -> str | None:
    code, out = git(path, "symbolic-ref", "--short", "-q", "HEAD")
    return out if code == 0 and out else None


@dataclass
class BranchState:
    state: str  # merged | gone | open | unpushed | default | detached | unknown
    reason: str

    @property
    def finished(self) -> bool:
        return self.state in ("merged", "gone")


def branch_state(path: str | Path, branch: str | None, default: str | None = None) -> BranchState:
    if branch is None:
        return BranchState("detached", "detached HEAD")
    default = default or default_ref(path)
    if branch in ("main", "master", default.split("/", 1)[-1]):
        return BranchState("default", f"{branch} is the default branch")
    code, track = git(path, "for-each-ref", "--format=%(upstream:short)|%(upstream:track)", f"refs/heads/{branch}")
    if code:
        return BranchState("unknown", "git failed")
    upstream, _, track = track.partition("|")
    if track == "[gone]":
        return BranchState("gone", f"branch {branch} was deleted on {REMOTE} (usually after a merge)")
    own_upstream = bool(upstream) and upstream != default and upstream.startswith(f"{REMOTE}/")
    code, _ = git(path, "merge-base", "--is-ancestor", f"refs/heads/{branch}", default)
    if code == 0 and own_upstream:
        return BranchState("merged", f"branch {branch} is merged into {default}")
    if not own_upstream:
        return BranchState("unpushed", f"branch {branch} has no upstream of its own")
    return BranchState("open", f"branch {branch} is not merged")


def dirty_count(path: str | Path) -> int | None:
    code, out = git(path, "status", "--porcelain", "--untracked-files=normal")
    if code:
        return None
    return len([line for line in out.splitlines() if line.strip()])


def ahead_behind(path: str | Path, branch: str, default: str) -> tuple[int, int] | None:
    code, out = git(path, "rev-list", "--left-right", "--count", f"refs/heads/{branch}...{default}")
    if code:
        return None
    left, _, right = out.partition("\t")
    try:
        return int(left), int(right)
    except ValueError:
        return None


def pushed(path: str | Path, branch: str, default: str | None = None) -> str:
    """yes | NO (local commits not on the upstream) | no-upstream | tracks-default | gone"""
    code, upstream = git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}")
    if code or not upstream:
        return "no-upstream"
    if upstream == (default or default_ref(path)):
        return "tracks-default"  # cut from main and never pushed to a branch of its own
    code, out = git(path, "rev-list", "--count", f"{upstream}..refs/heads/{branch}")
    if code:
        return "gone"
    return "yes" if out == "0" else "NO"


def fetch_if_stale(path: str | Path, stamp_dir: Path, interval_seconds: float) -> None:
    """`git fetch --prune origin` at most once per interval per repository."""
    if interval_seconds <= 0:
        return
    common = common_dir(path)
    if not common:
        return
    code, remotes = git(path, "remote")
    if code or REMOTE not in remotes.split():
        return
    stamp_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp_dir / common.strip("/").replace("/", "_")
    if stamp.exists() and time.time() - stamp.stat().st_mtime < interval_seconds:
        return
    stamp.touch()
    git(path, "fetch", "--prune", "--quiet", REMOTE, timeout=120)
