"""Process table parsing and dev server detection (ps) against a fake /proc."""

from __future__ import annotations

import os
import unittest

from helpers import GIB, HZ, FakeProc, TempCase

from devguard.config import Config, CustomServer
from devguard.procs import LinuxProcSource, PsSource, parse_etime
from devguard.servers import classify, discover, port_from_argv, script_and_args

NEXT = "/w/app/node_modules/.bin/next"
VITE = "/w/app/node_modules/.bin/vite"


class ClassifyTest(unittest.TestCase):
    def test_next_dev_and_start(self):
        self.assertEqual(classify(["node", NEXT, "dev", "-p", "4123"]), ("next", "dev"))
        self.assertEqual(classify(["node", "/w/node_modules/next/dist/bin/next", "start"]), ("next", "preview"))
        self.assertIsNone(classify(["node", NEXT, "build"]))

    def test_vite_modes(self):
        self.assertEqual(classify(["node", VITE]), ("vite", "dev"))
        self.assertEqual(classify(["node", VITE, "--port", "3000"]), ("vite", "dev"))
        self.assertEqual(classify(["node", "/w/node_modules/vite/bin/vite.js", "preview"]), ("vite", "preview"))
        self.assertIsNone(classify(["node", VITE, "build", "--watch"]))

    def test_other_tools(self):
        self.assertEqual(classify(["node", "/w/node_modules/.bin/wrangler", "dev"]), ("wrangler", "dev"))
        self.assertIsNone(classify(["node", "/w/node_modules/.bin/wrangler", "deploy"]))
        self.assertEqual(classify(["node", "/w/node_modules/.bin/astro", "dev"]), ("astro", "dev"))
        self.assertEqual(classify(["/usr/bin/node", "--max-old-space-size=8192", NEXT, "dev"]), ("next", "dev"))
        self.assertEqual(classify(["node", "-r", "dotenv/config", NEXT, "dev"]), ("next", "dev"))
        self.assertEqual(classify(["bun", "run", NEXT, "dev"]), ("next", "dev"))

    def test_wrappers_are_not_servers(self):
        for argv in (
            ["npm", "run", "dev"],
            ["sh", "-c", "next dev"],
            ["pnpm", "next", "dev"],
            ["npx", "next", "dev"],
            ["/bin/zsh", "-c", "next dev -p 4000"],
            ["devguard", "slot", "--", "next", "dev"],
        ):
            self.assertIsNone(classify(argv), argv)
        self.assertIsNone(script_and_args([]))
        self.assertIsNone(script_and_args(["node"]))

    def test_custom_servers(self):
        custom = [CustomServer("django", "dev", ["manage.py", "runserver"])]
        self.assertEqual(classify(["python", "manage.py", "runserver", "8000"], custom), ("django", "dev"))
        self.assertIsNone(classify(["sh", "-c", "python manage.py runserver"], custom))

    def test_port_from_argv(self):
        self.assertEqual(port_from_argv(["next", "dev", "-p", "4100"]), "4100")
        self.assertEqual(port_from_argv(["vite", "--port=5000"]), "5000")
        self.assertIsNone(port_from_argv(["vite"]))


class ProcSourceTest(TempCase):
    def setUp(self):
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc", uptime=10_000)
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.source = LinuxProcSource(self.proc.root)

    def test_snapshot_fields(self):
        self.proc.add(10, ["node", NEXT, "dev"], ppid=1, rss=2 * GIB, cwd=str(self.wt), start_ticks=HZ * 4000)
        snap = self.source.snapshot()
        p = snap.procs[10]
        self.assertEqual((p.ppid, p.argv[1], p.cwd), (1, NEXT, str(self.wt)))
        self.assertEqual(p.rss // GIB, 2)
        self.assertAlmostEqual(p.age, 6000, delta=1)
        self.assertEqual(self.source.identity(10), str(HZ * 4000))
        self.assertIsNone(self.source.identity(999))

    def test_zombie_has_no_identity(self):
        self.proc.add(13, ["node"], state="Z")
        self.assertIsNone(self.source.identity(13))

    def test_hidden_cwd_is_recorded_not_incomplete(self):
        from unittest import mock

        self.proc.add(14, ["/usr/bin/gpg-agent", "--daemon"])
        real = os.readlink

        def readlink(path, *a, **kw):
            if str(path).endswith("/14/cwd"):
                raise PermissionError(13, "Permission denied")
            return real(path, *a, **kw)

        with mock.patch("devguard.procs.os.readlink", readlink):
            snap = self.source.snapshot()
        self.assertTrue(snap.complete)
        self.assertEqual(snap.hidden, [14])
        self.assertIsNone(snap.procs[14].cwd)

    def test_deleted_cwd(self):
        self.proc.add(11, ["node", NEXT, "dev"], cwd="/gone/wt", cwd_deleted=True)
        p = self.source.snapshot().procs[11]
        self.assertEqual(p.cwd, "/gone/wt")
        self.assertTrue(p.cwd_deleted)

    def test_meminfo_environ_ports(self):
        self.proc.meminfo(total_gb=20, available_gb=1.25, swap_total_gb=8, swap_free_gb=6)
        self.proc.add(12, ["node", VITE], environ={"JOURNEY": "1", "PORT": "5173"}, listen=[5173])
        mem = self.source.meminfo()
        self.assertEqual(mem.total, 20 * GIB)
        self.assertEqual(mem.available, int(1.25 * GIB))
        self.assertEqual(mem.swap_used, 2 * GIB)
        self.assertTrue(self.source.environ_has(12, ["JOURNEY"]))
        self.assertFalse(self.source.environ_has(12, ["OTHER"]))
        self.assertFalse(self.source.environ_has(12, []))
        self.assertEqual(self.source.environ_get(12, "PORT"), "5173")
        self.assertEqual(self.source.listening_ports([12]), {12: [5173]})

    def test_tree_and_lineage(self):
        self.proc.add(20, ["node", NEXT, "dev"], ppid=1)
        self.proc.add(21, ["node", "worker"], ppid=20)
        self.proc.add(22, ["node", "worker2"], ppid=21)
        snap = self.source.snapshot()
        self.assertEqual(sorted(snap.tree(20)), [20, 21, 22])
        self.assertEqual(snap.ancestors(22), [21, 20])
        self.assertEqual(snap.lineage(22), {20, 21, 22})


class DiscoverTest(TempCase):
    def setUp(self):
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc")
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.source = LinuxProcSource(self.proc.root)
        self.config = Config()

    def discover(self, **kw):
        return discover(self.source, self.config, self_pid=kw.pop("self_pid", 999_999), **kw)

    def test_tree_memory_and_outermost_only(self):
        self.proc.add(100, ["sh", "-c", "next dev -p 4100"], cwd=str(self.wt))
        self.proc.add(101, ["node", NEXT, "dev", "-p", "4100"], ppid=100, rss=1 * GIB, cwd=str(self.wt))
        self.proc.add(102, ["node", NEXT, "dev", "-p", "4100"], ppid=101, rss=2 * GIB, cwd=str(self.wt))
        self.proc.add(103, ["next-server (v15.0.0)"], ppid=101, rss=1 * GIB, cwd=str(self.wt))
        servers = self.discover()
        self.assertEqual(len(servers), 1)
        s = servers[0]
        self.assertEqual((s.pid, s.port, s.kind, s.tool), (101, "4100", "dev", "next"))
        self.assertEqual(s.rss, 4 * GIB)
        self.assertEqual(sorted(s.tree), [101, 102, 103])

    def test_port_from_socket_then_env(self):
        self.proc.add(200, ["node", VITE], cwd=str(self.wt), listen=[5173])
        self.proc.add(201, ["node", "/x/node_modules/.bin/astro", "dev"], cwd=str(self.wt), environ={"PORT": "4321"})
        ports = {s.pid: s.port for s in self.discover()}
        self.assertEqual(ports, {200: "5173", 201: "4321"})

    def test_next_server_with_sh_parent(self):
        self.proc.add(300, ["sh", "-c", "'next' start -p 4300"], cwd=str(self.wt))
        self.proc.add(301, ["next-server (v15.1.0)"], ppid=300, cwd=str(self.wt))
        [s] = self.discover()
        self.assertEqual((s.pid, s.kind, s.port), (301, "preview", "4300"))

    def test_ignores_self_lineage_and_other_users(self):
        self.proc.add(400, ["node", NEXT, "dev"], cwd=str(self.wt))
        self.assertEqual(self.discover(self_pid=400), [])
        other = LinuxProcSource(self.proc.root, uid=os.getuid() + 1)
        self.assertEqual(discover(other, self.config, self_pid=1), [])

    def test_roots_filter_and_exempt_env_and_gone(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.proc.add(500, ["node", NEXT, "dev"], cwd=str(self.wt), environ={"JOURNEY_BINDINGS_URL": "x"})
        self.proc.add(501, ["node", NEXT, "dev"], cwd=str(elsewhere))
        self.proc.add(502, ["node", NEXT, "dev"], cwd=str(self.tmp / "removed"))
        self.config.watch.memory_exempt_env = ["JOURNEY_BINDINGS_URL"]
        by_pid = {s.pid: s for s in self.discover()}
        self.assertTrue(by_pid[500].exempt_memory)
        self.assertFalse(by_pid[501].exempt_memory)
        self.assertTrue(by_pid[502].cwd_gone)
        self.config.watch.roots = [str(self.wt)]
        self.assertEqual([s.pid for s in self.discover()], [500])


class PsSourceTest(unittest.TestCase):
    def fake(self, outputs):
        def runner(argv):
            for prefix, out in outputs.items():
                if " ".join(argv).startswith(prefix):
                    return out
            return ""

        return runner

    def test_parse_etime(self):
        self.assertEqual(parse_etime("05:03"), 303)
        self.assertEqual(parse_etime("02:00:01"), 7201)
        self.assertEqual(parse_etime("3-00:00:10"), 3 * 86400 + 10)

    def test_snapshot_ports_and_meminfo(self):
        uid = os.getuid()
        source = PsSource(
            self.fake(
                {
                    "ps -axww": (
                        f"  10     1 {uid} 204800   01:02:03 Sat Nov  1 09:00:00 2026 "
                        "node /w/node_modules/.bin/next dev -p 4100\n"
                        f"  11    10 {uid}  10240      00:10 Sat Nov  1 09:00:05 2026 node worker\n"
                    ),
                    "lsof -a -d cwd": "p10\nfcwd\nn/w/app\np11\nfcwd\nn/w/app\n",
                    "lsof -nP -iTCP": "p10\nf20\nn*:4100\n",
                    "sysctl": "17179869184\n",
                    "vm_stat": "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                    "Pages free: 1000.\nPages inactive: 1000.\n",
                    "ps -o lstart=": "Sat Nov  1 09:00:00 2026\n",
                }
            ),
            uid=uid,
        )
        snap = source.snapshot()
        self.assertEqual(snap.procs[10].cwd, "/w/app")
        self.assertEqual(snap.procs[10].rss, 204800 * 1024)
        self.assertEqual(snap.procs[10].age, 3723)
        self.assertEqual(source.listening_ports([10]), {10: [4100]})
        mem = source.meminfo()
        self.assertEqual((mem.total, mem.available), (16 * GIB, 2000 * 16384))
        self.assertEqual(source.identity(10), "Sat Nov  1 09:00:00 2026")
        self.assertFalse(source.environ_has(10, ["X"]))
        servers = discover(source, Config(), snap=snap, self_pid=1)
        self.assertEqual([(s.pid, s.port, s.rss) for s in servers], [(10, "4100", (204800 + 10240) * 1024)])


if __name__ == "__main__":
    unittest.main()
