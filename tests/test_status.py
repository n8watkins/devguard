"""status: classifying processes, the status document, writing it, and devguard stop."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import unittest
from pathlib import Path
from unittest import mock

from helpers import GIB, FakeProc, Repo, TempCase

from devguard import cli, status, watch
from devguard.config import Config
from devguard.procs import LinuxProcSource, MemInfo

NEXT = "/w/node_modules/.bin/next"
NODE = "/home/u/.nvm/versions/node/v24/bin/node"


class ClassifyTest(unittest.TestCase):
    def test_agents(self):
        self.assertTrue(status.is_agent(["claude", "--continue", "--dangerously-skip-permissions"]))
        self.assertTrue(status.is_agent(["/home/u/.local/bin/codex", "exec"]))
        self.assertTrue(status.is_agent(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]))
        self.assertTrue(status.is_agent(["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"]))
        self.assertFalse(status.is_agent(["node", "/srv/app/server.js"]))
        self.assertFalse(status.is_agent(["grep", "claude"]))
        self.assertFalse(status.is_agent([]))

    def test_builds(self):
        builds = [
            ["node", "/r/node_modules/.bin/next", "build", "--webpack"],
            ["node", "/r/node_modules/.bin/playwright", "test", "--config", "e2e/p.mjs"],
            ["npm", "run", "build:worker"],
            ["npm", "run", "check:release-budgets", "hub"],
            ["pnpm", "test"],
            ["npm", "exec", "wrangler", "check", "startup"],
            [NODE, "/r/node_modules/.bin/opennextjs-cloudflare", "build"],
            ["node", "/r/node_modules/typescript/bin/tsc", "--noEmit"],
            ["cargo", "test"],
            ["node", "/r/node_modules/.bin/webpack"],
        ]
        for argv in builds:
            self.assertTrue(status.is_build(argv), argv)
        servers_and_others = [
            ["node", NEXT, "dev"],
            ["npm", "run", "dev"],
            ["node", "/r/node_modules/.bin/vite"],
            ["node", "/r/node_modules/.bin/wrangler", "dev"],
            ["node", "/r/node_modules/.bin/webpack", "serve"],
            ["npm", "install"],
            ["cargo", "run"],
            ["zsh"],
            [],
        ]
        for argv in servers_and_others:
            self.assertFalse(status.is_build(argv), argv)


class CategoriesTest(TempCase):
    def test_each_process_counts_once_in_order(self):
        proc = FakeProc(self.tmp / "proc")
        mib = 1024 * 1024
        proc.add(10, ["node", NEXT, "dev", "-p", "4010"], ppid=22, rss=1000 * mib)  # a dev server an agent started
        proc.add(11, ["node", "postcss-worker"], ppid=10, rss=200 * mib)
        proc.add(20, ["claude", "--continue"], rss=400 * mib)
        proc.add(21, ["node", "/x/mcp-server.js"], ppid=20, rss=100 * mib)
        proc.add(22, ["zsh", "-c", "npm run build"], ppid=20, rss=10 * mib)
        proc.add(23, ["npm", "run", "build"], ppid=22, rss=50 * mib)
        proc.add(24, ["node", "/r/node_modules/.bin/next", "build"], ppid=23, rss=900 * mib)
        proc.add(25, ["node", "jest-worker/processChild.js"], ppid=24, rss=300 * mib)
        proc.add(30, ["python3", "notes.py"], rss=20 * mib)
        source = LinuxProcSource(proc.root)
        snap = source.snapshot()
        servers = watch.discover(source, Config(), snap, self_pid=1)
        cats = status.categories(snap, servers, source.uid)
        self.assertEqual(cats["dev_servers"], {"gb": status.gb(1200 * mib), "processes": 2})
        self.assertEqual(cats["builds_tests"], {"gb": status.gb(1250 * mib), "processes": 3})
        self.assertEqual(cats["agent_sessions"], {"gb": status.gb(510 * mib), "processes": 3})
        self.assertEqual(cats["other"], {"gb": status.gb(20 * mib), "processes": 1})

    def test_other_users_are_ignored(self):
        proc = FakeProc(self.tmp / "proc")
        proc.add(30, ["python3", "x.py"])
        source = LinuxProcSource(proc.root, uid=os.getuid() + 1)
        cats = status.categories(source.snapshot(), [], source.uid)
        self.assertEqual(sum(c["processes"] for c in cats.values()), 0)


class DocumentTest(TempCase):
    def test_document(self):
        proc = FakeProc(self.tmp / "proc")
        proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd="/w/app/apps/web", rss=2 * GIB)
        proc.add(11, ["node", NEXT, "dev", "-p", "4011"], cwd="/w/app/apps/docs", rss=GIB)
        proc.add(12, ["node", NEXT, "dev", "-p", "4012"], cwd="/w/site", rss=GIB // 2)
        source = LinuxProcSource(proc.root)
        snap = source.snapshot()
        servers = watch.discover(source, Config(), snap, self_pid=1)
        for s in servers:  # what annotate() fills in from git
            s.worktree = "/w/app" if s.cwd.startswith("/w/app") else s.cwd
            s.project = os.path.basename(s.worktree)
        mem = MemInfo(16 * GIB, 6 * GIB, GIB, cached=2 * GIB, swap_total=4 * GIB)
        now = 1_800_000_000
        last = {"time": "2027-01-15T07:59:00Z", "server": "next dev", "port": "4013", "worktree": "~/w/x", "reason": "merged"}
        held = {"~/loop": now + 600, "~/old": now - 1}
        doc = status.build(servers, mem, snap, source.uid, now, "apply", last, held, 3600, ["devguard"])
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["updated_at"], "2027-01-15T08:00:00Z")
        self.assertEqual(doc["memory"], {"total_gb": 16.0, "available_gb": 6.0, "used_gb": 10.0, "cache_gb": 2.0,
                                         "swap_used_gb": 1.0, "swap_total_gb": 4.0})  # fmt: skip
        self.assertEqual(doc["server_count"], 3)
        self.assertEqual([p["name"] for p in doc["projects"]], ["app", "site"])
        app = doc["projects"][0]
        self.assertEqual(app["total_rss_gb"], 3.0)
        self.assertEqual([(s["port"], s["dir"]) for s in app["servers"]], [("4010", "apps/web"), ("4011", "apps/docs")])
        self.assertEqual(doc["last_stop"], last)
        self.assertEqual([a["where"] for a in doc["alerts"]], ["~/loop"])
        self.assertEqual(doc["alerts"][0]["since"], "2027-01-15T07:10:00Z")
        text = status.render(doc)
        self.assertIn("app", text)
        self.assertIn("Last stop: 2027-01-15T07:59:00Z next dev port 4013 ~/w/x: merged", text)
        self.assertLess(len(json.dumps(doc)), 4000)

    def test_meminfo_reads_cache_and_swap_total(self):
        proc = FakeProc(self.tmp / "proc")
        (proc.root / "meminfo").write_text(
            "MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\nBuffers: 1048576 kB\nCached: 2097152 kB\n"
            "SReclaimable: 1048576 kB\nSwapTotal: 4194304 kB\nSwapFree: 3145728 kB\n"
        )
        mem = LinuxProcSource(proc.root).meminfo()
        self.assertEqual((mem.cached, mem.swap_total, mem.swap_used), (4 * GIB, 4 * GIB, GIB))


class WriteTest(TempCase):
    def test_writes_both_copies_atomically(self):
        config = Config(state_dir=str(self.tmp / "state"))
        target = self.tmp / "win" / "AppData" / "Local" / "devguard" / "status.json"
        self.assertTrue(status.write({"schema_version": 1}, config, windows=target))
        self.assertEqual(json.loads((self.tmp / "state" / "status.json").read_text()), {"schema_version": 1})
        self.assertEqual(target.read_text(), (self.tmp / "state" / "status.json").read_text())
        self.assertEqual(oct((self.tmp / "state" / "status.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["status.json"])  # no temp file left

    def test_a_failed_copy_is_reported_not_raised(self):
        config = Config(state_dir=str(self.tmp / "state"))
        blocker = self.tmp / "file"
        blocker.write_text("")
        errors = []
        self.assertFalse(status.write({"a": 1}, config, windows=blocker / "status.json", err=errors.append))
        self.assertTrue((self.tmp / "state" / "status.json").exists())
        self.assertIn("could not write the Windows status copy", errors[0])


class WindowsPathTest(TempCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(status.ENV_WINDOWS_PATH, None)
        self.users = self.tmp / "Users"
        for name in ("Public", "Default", "All Users"):
            (self.users / name).mkdir(parents=True)
        self.config = Config(state_dir=str(self.tmp / "state"))

    def profile(self, name):
        (self.users / name / "AppData").mkdir(parents=True)

    def test_single_profile_is_used_and_cached(self):
        self.profile("nat")
        path = status.windows_path(self.config, self.users, wsl=True, run=lambda argv: self.fail("not needed"))
        self.assertEqual(path, self.users / "nat" / "AppData" / "Local" / "devguard" / "status.json")
        self.assertEqual((self.tmp / "state" / "windows-user").read_text(), "nat\n")

    def test_several_profiles_ask_cmd_exe(self):
        self.profile("nat")
        self.profile("guest")
        cmd = self.tmp / "cmd.exe"
        cmd.write_text("")
        user = status.windows_user(self.tmp / "state", self.users, run=lambda argv: "nat\r\n", cmd_exe=cmd)
        self.assertEqual(user, "nat")
        self.assertIsNone(status.windows_user(self.tmp / "s2", self.users, run=lambda argv: "%USERNAME%\r\n", cmd_exe=cmd))

    def test_off_explicit_and_not_wsl(self):
        self.profile("nat")
        self.assertIsNone(status.windows_path(self.config, self.users, wsl=False))
        self.config.watch.windows_status_path = "off"
        self.assertIsNone(status.windows_path(self.config, self.users, wsl=True))
        self.config.watch.windows_status_path = str(self.tmp / "x.json")
        self.assertEqual(status.windows_path(self.config, self.users, wsl=True), self.tmp / "x.json")
        os.environ[status.ENV_WINDOWS_PATH] = "off"
        self.assertIsNone(status.windows_path(self.config, self.users, wsl=True))


class WatchWritesStatusTest(TempCase):
    def setUp(self):
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc")
        self.config = Config(state_dir=str(self.tmp / "state"))
        self.config.watch.fetch_interval_minutes = 0
        self.log = watch.Log(self.tmp / "state" / "watch.log", echo=None)
        self.repo = Repo(self.tmp / "repos")

    def kill(self, pid, sig):
        self.proc.remove(pid)

    def test_each_pass_writes_status_and_keeps_the_last_stop(self):
        wt = self.repo.worktree("gone")
        self.repo.merge("gone", delete=True, squash=True)
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(wt), start_ticks=5)
        self.proc.add(11, ["node", NEXT, "dev", "-p", "4011"], cwd=str(self.repo.main), start_ticks=6)
        source = LinuxProcSource(self.proc.root)
        watch.run_once(source, self.config, apply=True, log=self.log, kill=self.kill, sleep=lambda _: None, self_pid=1)
        path = self.tmp / "state" / "status.json"
        doc = json.loads(path.read_text())
        self.assertEqual(doc["mode"], "apply")
        self.assertEqual(doc["server_count"], 1)
        self.assertEqual(doc["projects"][0]["servers"][0]["port"], "4011")
        self.assertEqual(doc["last_stop"]["port"], "4010")
        self.assertIn("deleted on origin", doc["last_stop"]["reason"])
        watch.run_once(source, self.config, apply=True, log=self.log, kill=self.kill, sleep=lambda _: None, self_pid=1)
        self.assertEqual(json.loads(path.read_text())["last_stop"]["port"], "4010")

    def test_a_status_failure_never_fails_the_pass(self):
        self.proc.add(11, ["node", NEXT, "dev", "-p", "4011"], cwd=str(self.repo.main))
        with (
            mock.patch.object(status, "build", side_effect=RuntimeError("boom")),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            watch.run_once(LinuxProcSource(self.proc.root), self.config, apply=True, log=self.log, self_pid=1)
        self.assertIn("could not build status: RuntimeError: boom", err.getvalue())


class StopMatchingTest(TempCase):
    def setUp(self):
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc")
        self.config = Config(state_dir=str(self.tmp / "state"))
        self.lines = []
        self.log = watch.Log(self.tmp / "state" / "watch.log", echo=self.lines.append)
        self.repo = Repo(self.tmp / "repos")
        self.killed = []

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        self.proc.remove(pid)

    def stop(self, target):
        source = LinuxProcSource(self.proc.root)
        return watch.stop_matching(source, self.config, target, self.log, kill=self.kill, sleep=lambda _: None, self_pid=1)

    def test_by_port_and_by_directory(self):
        wt = self.repo.worktree("feature")
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(wt / "apps" / "web"), start_ticks=5)
        self.proc.add(11, ["node", NEXT, "dev", "-p", "4011"], cwd=str(wt), start_ticks=6)
        self.proc.add(12, ["node", NEXT, "dev", "-p", "4012"], cwd=str(self.repo.main), start_ticks=7)
        self.assertEqual(self.stop("4999"), [])
        self.assertEqual([s.pid for s in self.stop("4012")], [12])
        self.assertEqual(sorted(s.pid for s in self.stop(str(wt))), [10, 11])
        self.assertEqual(sorted(self.killed), [(10, signal.SIGTERM), (11, signal.SIGTERM), (12, signal.SIGTERM)])
        self.assertIn("stopped on request (devguard stop 4012)", self.lines[0])
        state = watch.WatchState.load(self.tmp / "state" / watch.STATE_FILE)
        self.assertIn(state.last_stop["port"], ("4010", "4011"))


class CliStatusTest(TempCase):
    def setUp(self):
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc")
        self.config = self.tmp / "c.toml"
        self.config.write_text(f'state_dir = "{self.tmp / "state"}"\n')
        patcher = mock.patch.object(cli, "default_source", lambda: LinuxProcSource(self.proc.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(self.config), *args])
        return code, out.getvalue(), err.getvalue()

    def test_status_text_and_json(self):
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(self.tmp), rss=2 * GIB)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Memory: 8.0G used of 16.0G", out)
        self.assertIn(":4010 2.0G", out)
        self.assertIn("devguard watch` writes it", out)
        code, out, _ = self.run_cli("status", "--json")
        doc = json.loads(out)
        self.assertEqual((doc["mode"], doc["server_count"]), ("status", 1))
        self.assertFalse((self.tmp / "state" / "status.json").exists())  # status only reads

    def test_stop_exit_codes(self):
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(self.tmp))
        with mock.patch("devguard.watch.os.kill", side_effect=lambda pid, sig: self.proc.remove(pid)) as kill:
            self.assertEqual(self.run_cli("stop", "4010")[0], 0)
            code, _, err = self.run_cli("stop", "4010")
        self.assertEqual(kill.call_count, 1)
        self.assertEqual(code, 1)
        self.assertIn("no dev server on port 4010", err)
        self.assertTrue(Path(self.tmp / "state" / "watch.log").exists())


if __name__ == "__main__":
    unittest.main()
