"""Test fixtures: a fake /proc tree and temporary git repositories with worktrees."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Isolate git from the developer's own config (hooks, signing, default branch).
_GIT_HOME = tempfile.mkdtemp(prefix="devguard-test-gitconfig-")
Path(_GIT_HOME, "gitconfig").write_text(
    "[user]\n\tname = Test\n\temail = test@example.invalid\n"
    "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n[advice]\n\tdetachedHead = false\n"
)
os.environ["GIT_CONFIG_GLOBAL"] = str(Path(_GIT_HOME, "gitconfig"))
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"

PAGE = os.sysconf("SC_PAGE_SIZE")
HZ = os.sysconf("SC_CLK_TCK")
GIB = 1024**3


class FakeProc:
    """Builds a directory that looks like /proc for LinuxProcSource(root=...)."""

    def __init__(self, root: Path, uptime: float = 100_000.0) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "uptime").write_text(f"{uptime:.2f} 0.00\n")
        (root / "net").mkdir(exist_ok=True)
        self.tcp: list[tuple[int, int]] = []  # (port, inode)
        self.meminfo(total_gb=16, available_gb=8)

    def meminfo(self, total_gb: float, available_gb: float, swap_total_gb: float = 4, swap_free_gb: float = 4) -> None:
        kb = 1024 * 1024
        (self.root / "meminfo").write_text(
            f"MemTotal: {int(total_gb * kb)} kB\nMemFree: 1000 kB\nMemAvailable: {int(available_gb * kb)} kB\n"
            f"SwapTotal: {int(swap_total_gb * kb)} kB\nSwapFree: {int(swap_free_gb * kb)} kB\n"
        )

    def add(
        self,
        pid: int,
        argv: list[str],
        ppid: int = 1,
        rss: int = 100 * 1024 * 1024,
        cwd: str | None = "/",
        start_ticks: int = 1000,
        environ: dict[str, str] | None = None,
        listen: list[int] | None = None,
        cwd_deleted: bool = False,
        state: str = "S",
    ) -> None:
        d = self.root / str(pid)
        d.mkdir()
        name = os.path.basename(argv[0])[:15] if argv else "x"
        fields = [state, str(ppid)] + ["0"] * 17 + [str(start_ticks), "0", str(rss // PAGE)] + ["0"] * 20
        (d / "stat").write_text(f"{pid} ({name}) {' '.join(fields)}\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (environ or {}).items()))
        if cwd is not None:
            os.symlink(cwd + (" (deleted)" if cwd_deleted else ""), d / "cwd")
        (d / "fd").mkdir()
        for i, port in enumerate(listen or []):
            inode = 900000 + pid * 10 + i
            self.tcp.append((port, inode))
            os.symlink(f"socket:[{inode}]", d / "fd" / str(10 + i))
        self._write_tcp()

    def _write_tcp(self) -> None:
        lines = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"]
        for i, (port, inode) in enumerate(self.tcp):
            lines.append(
                f"   {i}: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 {inode}"
            )
        (self.root / "net" / "tcp").write_text("\n".join(lines) + "\n")

    def remove(self, pid: int) -> None:
        import shutil

        shutil.rmtree(self.root / str(pid))


def run(cwd: str | Path, *args: str) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(f"{' '.join(args)} failed in {cwd}:\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def git(cwd: str | Path, *args: str) -> str:
    return run(cwd, "git", *args)


class Repo:
    """origin.git (bare) + a primary clone `app` with a .gitignore for build output."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.origin = base / "origin.git"
        self.main = base / "app"
        base.mkdir(parents=True, exist_ok=True)
        git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(base, "clone", "-q", str(self.origin), str(self.main))
        (self.main / ".gitignore").write_text("node_modules\n.next/\ndist/\ntarget/\n.turbo/\n")
        (self.main / "README.md").write_text("hello\n")
        git(self.main, "add", ".")
        git(self.main, "commit", "-q", "-m", "init")
        git(self.main, "push", "-q", "-u", "origin", "main")
        git(self.main, "remote", "set-head", "origin", "main")

    def worktree(self, name: str, commit: bool = True, push: bool = True) -> Path:
        path = self.base / f"wt-{name}"
        git(self.main, "fetch", "-q", "origin")
        git(self.main, "worktree", "add", "-q", "-b", name, str(path), "origin/main")
        if commit:
            (path / f"{name}.txt").write_text(name + "\n")
            git(path, "add", ".")
            git(path, "commit", "-q", "-m", f"work on {name}")
        if push:
            git(path, "push", "-q", "-u", "origin", name)
        return path

    def merge(self, name: str, delete: bool = False, squash: bool = False) -> None:
        git(self.main, "fetch", "-q", "origin")
        if squash:
            git(self.main, "merge", "-q", "--squash", f"origin/{name}")
            git(self.main, "commit", "-q", "-m", f"squash {name}")
        else:
            git(self.main, "merge", "-q", "--no-ff", "-m", f"merge {name}", f"origin/{name}")
        git(self.main, "push", "-q", "origin", "main")
        if delete:
            git(self.main, "push", "-q", "origin", "--delete", name)
        git(self.main, "fetch", "-q", "--prune", "origin")


def make_artifact(path: Path, files: int = 3, size: int = 50_000) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "sub").mkdir(exist_ok=True)
    for i in range(files):
        (path / "sub" / f"f{i}.bin").write_bytes(b"x" * size)


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="devguard-test-")
        self.tmp = Path(os.path.realpath(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()
