"""worktrees: one row per worktree of a repository."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from . import gitinfo


@dataclass
class Row:
    path: str
    branch: str | None
    primary: bool
    exists: bool
    dirty: int | None
    ahead: int | None
    behind: int | None
    merged: str  # merged | gone | open | unpushed | default | detached | missing
    pushed: str
    locked: bool

    def to_dict(self) -> dict:
        return asdict(self)


def status(repo: str) -> tuple[str, list[Row]]:
    trees = gitinfo.worktrees(repo)
    default = gitinfo.default_ref(repo)
    rows = []
    for t in trees:
        if t.bare:
            continue
        if not os.path.isdir(t.path):
            rows.append(Row(t.path, t.branch, t.primary, False, None, None, None, "missing", "-", t.locked))
            continue
        ab = gitinfo.ahead_behind(t.path, t.branch, default) if t.branch else None
        state = gitinfo.branch_state(t.path, t.branch, default)
        rows.append(
            Row(
                path=t.path,
                branch=t.branch,
                primary=t.primary,
                exists=True,
                dirty=gitinfo.dirty_count(t.path),
                ahead=ab[0] if ab else None,
                behind=ab[1] if ab else None,
                merged=state.state,
                pushed=gitinfo.pushed(t.path, t.branch, default) if t.branch else "-",
                locked=t.locked,
            )
        )
    return default, rows
