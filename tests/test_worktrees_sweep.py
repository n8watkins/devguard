"""worktrees and sweep against temporary repositories with real worktrees."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import ROOT, Repo, TempCase, git, make_artifact

from devguard import gitinfo, sweep, worktrees
from devguard.config import Config
from devguard.procs import LinuxProcSource, Proc, Snapshot

ENV = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
EMPTY = Snapshot({})


class GitStateTest(TempCase):
    def setUp(self):
        super().setUp()
        self.repo = Repo(self.tmp)

    def state(self, path):
        return gitinfo.branch_state(path, gitinfo.current_branch(path)).state

    def test_states(self):
        merged = self.repo.worktree("merged")
        self.repo.merge("merged")
        gone = self.repo.worktree("gone")
        self.repo.merge("gone", delete=True, squash=True)
        git(gone, "fetch", "-q", "--prune")
        opened = self.repo.worktree("open")
        fresh = self.repo.worktree("fresh", commit=False, push=False)  # tracks origin/main
        local = self.repo.worktree("local", push=False)
        self.assertEqual(self.state(merged), "merged")
        self.assertEqual(self.state(gone), "gone")
        self.assertEqual(self.state(opened), "open")
        self.assertEqual(self.state(fresh), "unpushed")
        self.assertEqual(self.state(local), "unpushed")
        self.assertEqual(self.state(self.repo.main), "default")
        git(opened, "checkout", "-q", "--detach")
        self.assertEqual(self.state(opened), "detached")

    def test_parse_worktrees(self):
        out = "worktree /a\nHEAD 1\nbranch refs/heads/main\n\nworktree /b\nHEAD 2\ndetached\nlocked reason\n\nworktree /c\nbare\n"
        trees = gitinfo.parse_worktrees(out)
        got = [(t.path, t.branch, t.primary, t.locked, t.bare) for t in trees]
        expected = [
            ("/a", "main", True, False, False),
            ("/b", None, False, True, False),
            ("/c", None, False, False, True),
        ]
        self.assertEqual(got, expected)

    def test_worktrees_table(self):
        merged = self.repo.worktree("merged")
        self.repo.merge("merged")
        dirty = self.repo.worktree("dirty")
        (dirty / "new.txt").write_text("x")
        missing = self.repo.worktree("missing")
        import shutil

        shutil.rmtree(missing)
        default, rows = worktrees.status(str(self.repo.main))
        self.assertEqual(default, "origin/main")
        by = {Path(r.path).name: r for r in rows}
        self.assertTrue(by["app"].primary)
        self.assertEqual(by["app"].merged, "default")
        self.assertEqual((by["wt-merged"].merged, by["wt-merged"].pushed, by["wt-merged"].dirty), ("merged", "yes", 0))
        self.assertEqual((by["wt-dirty"].merged, by["wt-dirty"].dirty, by["wt-dirty"].ahead), ("open", 1, 1))
        self.assertEqual(by["wt-missing"].merged, "missing")
        fresh = self.repo.worktree("fresh", commit=False, push=False)
        local = self.repo.worktree("local", push=False)
        git(local, "branch", "--unset-upstream")
        self.assertEqual(gitinfo.pushed(fresh, "fresh"), "tracks-default")
        self.assertEqual(gitinfo.pushed(local, "local"), "no-upstream")
        (merged / "more.txt").write_text("more")
        git(merged, "add", ".")
        git(merged, "commit", "-q", "-m", "more")
        self.assertEqual(gitinfo.pushed(merged, "merged"), "NO")
        cli = subprocess.run([sys.executable, "-m", "devguard", "worktrees", str(self.repo.main)],
                             capture_output=True, text=True, env=ENV)  # fmt: skip
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertIn("wt-merged", cli.stdout)
        self.assertIn("(primary)", cli.stdout)
        data = json.loads(subprocess.run([sys.executable, "-m", "devguard", "worktrees", "--json", str(self.repo.main)],
                                         capture_output=True, text=True, env=ENV).stdout)  # fmt: skip
        self.assertEqual(len(data["worktrees"]), 6)

    def test_fetch_is_throttled(self):
        stamps = self.tmp / "stamps"
        gitinfo.fetch_if_stale(self.repo.main, stamps, 600)
        [stamp] = list(stamps.iterdir())
        first = stamp.stat().st_mtime
        time.sleep(0.01)
        gitinfo.fetch_if_stale(self.repo.main, stamps, 600)
        self.assertEqual(stamp.stat().st_mtime, first)


class SweepTest(TempCase):
    def setUp(self):
        super().setUp()
        self.repo = Repo(self.tmp / "r")
        self.config = Config(state_dir=str(self.tmp / "state"))
        self.config.sweep.min_idle_hours = 0

    def finished(self, name):
        wt = self.repo.worktree(name)
        self.repo.merge(name)
        make_artifact(wt / "node_modules")
        make_artifact(wt / "apps" / "web" / ".next")
        return wt

    def plan(self, snap=EMPTY, include_primary=False):
        return {Path(c.worktree.path).name: c for c in sweep.plan_repo(str(self.repo.main), snap, self.config, include_primary)}

    def apply(self, cands, snap=EMPTY, include_primary=False):
        return sweep.apply(list(cands.values()), lambda: snap, self.config, include_primary)

    def test_deletes_artifacts_of_finished_clean_worktree(self):
        wt = self.finished("done")
        cands = self.plan()
        c = cands["wt-done"]
        self.assertTrue(c.eligible, c.blockers)
        self.assertEqual(sorted(os.path.relpath(a.path, wt) for a in c.deletable), ["apps/web/.next", "node_modules"])
        self.assertTrue(all(a.bytes >= 150_000 for a in c.deletable))
        actions = self.apply(cands)
        self.assertEqual([a["result"] for a in actions], ["deleted", "deleted"])
        self.assertFalse((wt / "node_modules").exists())
        self.assertFalse((wt / "apps" / "web" / ".next").exists())
        self.assertTrue((wt / "done.txt").exists())
        self.assertEqual(git(wt, "status", "--porcelain"), "")
        self.assertEqual(list(wt.glob(".devguard-sweep-*")), [])

    def test_primary_is_kept_unless_explicit(self):
        make_artifact(self.repo.main / "node_modules")
        cands = self.plan()
        self.assertIn("primary worktree", cands["app"].blockers[0])
        # Even when explicitly included, main is not a finished branch.
        cands = self.plan(include_primary=True)
        self.assertIn("default branch", cands["app"].blockers[0])
        self.assertEqual(self.apply(cands, include_primary=True), [])
        self.assertTrue((self.repo.main / "node_modules").exists())

    def test_blocked_worktrees_are_untouched(self):
        dirty = self.finished("dirty")
        (dirty / "notes.txt").write_text("unsaved")
        opened = self.repo.worktree("open")
        make_artifact(opened / "node_modules")
        unpushed = self.repo.worktree("unpushed", push=False)
        make_artifact(unpushed / "node_modules")
        locked = self.finished("locked")
        git(self.repo.main, "worktree", "lock", str(locked))
        cands = self.plan()
        self.assertIn("uncommitted", cands["wt-dirty"].blockers[0])
        self.assertIn("not merged", cands["wt-open"].blockers[0])
        self.assertIn("no upstream", cands["wt-unpushed"].blockers[0])
        self.assertIn("locked", cands["wt-locked"].blockers[0])
        self.assertEqual(self.apply(cands), [])
        for wt in (dirty, opened, unpushed, locked):
            self.assertTrue((wt / "node_modules" / "sub" / "f0.bin").exists())

    def test_running_process_blocks(self):
        wt = self.finished("busy")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=wt)
        try:
            source = LinuxProcSource()
            cands = self.plan(source.snapshot())
            self.assertTrue(any(f"process {child.pid}" in b for b in cands["wt-busy"].blockers))
            # A process that starts after planning is caught by the revalidation before deleting.
            child.kill()
            child.wait()
            cands = self.plan(source.snapshot())
            self.assertTrue(cands["wt-busy"].eligible)
            late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=wt)
            try:
                actions = sweep.apply(list(cands.values()), source.snapshot, self.config, False)
                self.assertEqual({a["result"] for a in actions}, {"refused"})
                self.assertTrue((wt / "node_modules").exists())
            finally:
                late.kill()
                late.wait()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_incomplete_process_list_blocks(self):
        self.finished("x")
        cands = self.plan(Snapshot({}, complete=False))
        self.assertIn("process list", " ".join(cands["wt-x"].blockers))

    def test_symlinks_are_never_followed_or_deleted(self):
        wt = self.repo.worktree("links")
        self.repo.merge("links")
        precious = self.tmp / "precious"
        make_artifact(precious)
        os.symlink(precious, wt / "node_modules")  # symlinked node_modules (shared install)
        make_artifact(wt / "dist")
        os.symlink(precious, wt / "dist" / "escape")  # symlink inside an artifact
        cands = self.plan()
        c = cands["wt-links"]
        self.assertEqual([p for p, _ in c.skipped], [str(wt / "node_modules")])
        actions = self.apply(cands)
        self.assertEqual([a["result"] for a in actions], ["deleted"])
        self.assertTrue((precious / "sub" / "f0.bin").exists())
        self.assertTrue((wt / "node_modules").is_symlink())
        self.assertFalse((wt / "dist").exists())

    def test_tracked_directories_are_kept(self):
        wt = self.repo.worktree("tracked")
        (wt / "dist").mkdir()
        (wt / "dist" / "keep.js").write_text("tracked")
        git(wt, "add", "-f", "dist/keep.js")  # dist/ is ignored, but this file is tracked
        git(wt, "commit", "-q", "-m", "track dist")
        git(wt, "push", "-q")
        self.repo.merge("tracked")
        c = self.plan()["wt-tracked"]
        self.assertTrue(c.eligible, c.blockers)
        [dist] = c.artifacts
        self.assertIn("contains files tracked by git", dist.blockers)
        self.assertEqual(self.apply({"x": c}), [])
        self.assertTrue((wt / "dist" / "keep.js").exists())

    def test_unignored_artifact_name_makes_worktree_dirty(self):
        wt = self.finished("build")
        make_artifact(wt / "build")  # "build" is in the artifact list but not in .gitignore
        c = self.plan()["wt-build"]
        self.assertIn("uncommitted or untracked", " ".join(c.blockers))
        self.assertEqual(sweep.git_blockers(str(wt), str(wt / "build")), ["not ignored by git"])

    def test_hidden_process_mentioning_worktree_blocks(self):
        wt = self.finished("hidden")
        proc = Proc(4242, 1, os.getuid(), 0, ["/usr/bin/agent", f"--root={wt}", str(wt / "x")], 1.0, "1", None)
        c = self.plan(Snapshot({4242: proc}, hidden=[4242]))["wt-hidden"]
        self.assertIn("process 4242", " ".join(c.blockers))
        proc.argv = ["/usr/bin/gpg-agent", "--daemon"]
        self.assertTrue(self.plan(Snapshot({4242: proc}, hidden=[4242]))["wt-hidden"].eligible)

    def test_identity_change_is_refused(self):
        wt = self.finished("swap")
        cands = self.plan()
        (wt / "node_modules").rename(self.tmp / "old-node_modules")  # keep the old inode alive
        make_artifact(wt / "node_modules")  # same path, new directory
        actions = self.apply(cands)
        by = {os.path.basename(a["path"]): a for a in actions}
        self.assertEqual(by["node_modules"]["result"], "refused")
        self.assertIn("identity changed", by["node_modules"]["reasons"][0])
        self.assertTrue((wt / "node_modules").exists())
        self.assertEqual(by[".next"]["result"], "deleted")

    def test_nested_git_blocks_artifact(self):
        wt = self.finished("nested")
        (wt / "node_modules" / "pkg" / ".git").mkdir(parents=True)
        c = self.plan()["wt-nested"]
        [nm] = [a for a in c.artifacts if a.path.endswith("node_modules")]
        self.assertTrue(any("nested .git" in b for b in nm.blockers))

    def test_interrupted_quarantine_is_finished_next_time(self):
        wt = self.finished("resume")
        make_artifact(wt / ".devguard-sweep-node_modules-20260101T000000Z-1")
        c = self.plan()["wt-resume"]
        self.assertTrue(c.eligible, c.blockers)  # the quarantine does not count as dirty
        actions = self.apply({"x": c})
        self.assertEqual({a["result"] for a in actions}, {"deleted"})
        self.assertEqual(list(wt.glob(".devguard-sweep-*")), [])

    def test_min_idle_hours(self):
        self.finished("recent")
        self.config.sweep.min_idle_hours = 24
        c = self.plan()["wt-recent"]
        self.assertIn("git index changed", " ".join(c.blockers))

    def test_remove_tree_does_not_cross_into_symlink_targets(self):
        outside = self.tmp / "outside"
        make_artifact(outside)
        victim = self.tmp / "victim"
        make_artifact(victim)
        os.symlink(outside, victim / "link")
        info = os.lstat(victim)
        sweep.remove_tree(str(victim), info.st_dev, info.st_ino)
        self.assertFalse(victim.exists())
        self.assertTrue((outside / "sub" / "f2.bin").exists())
        make_artifact(victim)
        with self.assertRaises(OSError):
            sweep.remove_tree(str(victim), info.st_dev, info.st_ino + 1)
        self.assertTrue(victim.exists())

    def test_hardlinks_are_reported_as_shared(self):
        wt = self.finished("pnpm")
        store = self.tmp / "store"
        store.mkdir()
        (store / "big.bin").write_bytes(b"y" * 200_000)
        os.link(store / "big.bin", wt / "node_modules" / "big.bin")
        c = self.plan()["wt-pnpm"]
        [nm] = [a for a in c.artifacts if a.path.endswith("node_modules")]
        self.assertGreaterEqual(nm.shared_bytes, 200_000)

    def test_receipts_and_lock(self):
        self.finished("r1")
        state = self.config.state_path()
        actions = self.apply(self.plan())
        path = sweep.write_receipt(state, actions, [str(self.repo.main)])
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt["schemaVersion"], 1)
        self.assertEqual(len(receipt["actions"]), 2)
        self.assertGreater(receipt["freedBytes"], 0)
        for i in range(sweep.RECEIPTS_KEPT + 5):
            (state / "sweep" / f"2000{i:04d}.json").write_text("{}")
        sweep.write_receipt(state, [], [])
        self.assertEqual(len(list((state / "sweep").glob("*.json"))), sweep.RECEIPTS_KEPT)
        with sweep.SweepLock(state), self.assertRaises(RuntimeError), sweep.SweepLock(state):
            pass

    def test_cli_dry_run_then_apply(self):
        wt = self.finished("cli")
        config = self.tmp / "c.toml"
        config.write_text(f'state_dir = "{self.tmp / "state"}"\n[sweep]\nmin_idle_hours = 0\n')
        base = [sys.executable, "-m", "devguard", "--config", str(config), "sweep", str(self.repo.main)]
        dry = subprocess.run(base, capture_output=True, text=True, env=ENV, cwd=self.tmp)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("sweep  ", dry.stdout)
        self.assertIn("Dry run: nothing was deleted", dry.stdout)
        self.assertIn("keep   ~" if str(self.tmp).startswith(str(Path.home())) else "keep   /", dry.stdout)
        self.assertTrue((wt / "node_modules").exists())
        applied = subprocess.run([*base, "--apply"], capture_output=True, text=True, env=ENV, cwd=self.tmp)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("DELETED", applied.stdout)
        self.assertIn("Receipt:", applied.stdout)
        self.assertFalse((wt / "node_modules").exists())
        again = subprocess.run([*base, "--apply"], capture_output=True, text=True, env=ENV, cwd=self.tmp)
        self.assertIn("Nothing to delete", again.stdout)


if __name__ == "__main__":
    unittest.main()
