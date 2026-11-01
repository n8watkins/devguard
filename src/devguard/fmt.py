"""Small formatting helpers shared by the commands."""

from __future__ import annotations

import os
from pathlib import Path

GIB = 1024**3
MIB = 1024**2


def size(n: int | float | None) -> str:
    """Binary units, compact: 812M, 4.3G, 512K."""
    if n is None:
        return "?"
    if n >= GIB:
        return f"{n / GIB:.1f}G"
    if n >= MIB:
        return f"{n / MIB:.0f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{int(n)}B"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 60:.0f}m"


def home(path: str | Path | None) -> str:
    """Shorten a path under $HOME to ~/..."""
    if path is None:
        return "?"
    text = str(path)
    prefix = str(Path.home())
    if text == prefix:
        return "~"
    if text.startswith(prefix + os.sep):
        return "~" + text[len(prefix) :]
    return text


def table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> str:
    """A plain text table; the last column is not padded."""
    right = right or set()
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = []
    for row in [headers, *rows]:
        cells = []
        for i, cell in enumerate(row):
            if i == len(row) - 1:
                cells.append(cell)
            elif i in right:
                cells.append(cell.rjust(widths[i]))
            else:
                cells.append(cell.ljust(widths[i]))
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def is_within(path: str | Path, root: str | Path) -> bool:
    path, root = os.path.normpath(str(path)), os.path.normpath(str(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)
