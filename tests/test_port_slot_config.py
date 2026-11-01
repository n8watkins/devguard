"""port, slot and config."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import ROOT, TempCase

from devguard import port, slot
from devguard.config import DEFAULT_TOML, Config, ConfigError, load, parse

ENV = dict(os.environ, PYTHONPATH=str(ROOT / "src"))


def devguard(*args, **kw):
    return subprocess.run([sys.executable, "-m", "devguard", *args], capture_output=True, text=True, env=kw.pop("env", ENV), **kw)


class PortTest(TempCase):
    def test_matches_dev_port_sh(self):
        # Values from the original dev-port.sh: 4000 + (sha1(path)[:8] as hex) % 1000.
        self.assertEqual(port.port_for("/home/natkins/projects/devguard"), 4993)
        self.assertEqual(port.port_for("/home/natkins"), 4665)
        self.assertEqual(port.port_for("/x", base=8000, span=10) // 10, 800)

    def test_resolve_dir_and_overrides(self):
        os.mkdir(self.tmp / "a")
        self.assertEqual(port.resolve_dir(str(self.tmp / "a" / ".." / "a")), str(self.tmp / "a"))
        with self.assertRaises(NotADirectoryError):
            port.resolve_dir(str(self.tmp / "missing"))
        config = Config()
        config.port.overrides[str(self.tmp / "a")] = 4444
        self.assertEqual(port.port_of(str(self.tmp / "a"), config), 4444)

    def test_logical_cwd_keeps_symlinked_path(self):
        os.mkdir(self.tmp / "real")
        os.symlink(self.tmp / "real", self.tmp / "link")
        result = devguard("port", "-q", cwd=self.tmp / "link", env=dict(ENV, PWD=str(self.tmp / "link")))
        self.assertEqual(result.stdout.strip(), str(port.port_for(str(self.tmp / "link"))))

    def test_listening_and_warning(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            number = server.getsockname()[1]
            self.assertTrue(port.listening(number))
            config = self.tmp / "c.toml"
            config.write_text(f'[port.overrides]\n"{self.tmp}" = {number}\n')
            result = devguard("--config", str(config), "port", str(self.tmp))
            self.assertEqual(result.stdout.strip(), str(number))
            self.assertIn("already listens", result.stderr)

    def test_write_file_is_opt_in(self):
        devguard("port", str(self.tmp))
        self.assertFalse((self.tmp / ".dev-port").exists())
        result = devguard("port", "--write", str(self.tmp))
        self.assertEqual((self.tmp / ".dev-port").read_text().strip(), result.stdout.strip())

    def test_collision_chance(self):
        self.assertEqual(port.collision_chance(1), 0)
        self.assertAlmostEqual(port.collision_chance(20), 0.174, places=2)
        self.assertEqual(port.collision_chance(1001), 1)


class SlotTest(TempCase):
    def setUp(self):
        super().setUp()
        self.config = self.tmp / "c.toml"
        self.config.write_text(f'[slot]\ndir = "{self.tmp / "slots"}"\ncount = 2\n')

    def slot(self, *args, **kw):
        return devguard("--config", str(self.config), "slot", *args, **kw)

    def test_exit_code_passes_through(self):
        self.assertEqual(self.slot("--", sys.executable, "-c", "raise SystemExit(37)").returncode, 37)
        self.assertEqual(self.slot("--", "definitely-not-a-command-xyz").returncode, 127)
        self.assertEqual(self.slot().returncode, 2)

    def test_at_most_n_run_at_once(self):
        log = self.tmp / "log"
        job = (
            f"import time,sys; open({str(log)!r},'a').write('start\\n'); time.sleep(0.6); open({str(log)!r},'a').write('end\\n')"
        )
        procs = [subprocess.Popen([sys.executable, "-m", "devguard", "--config", str(self.config), "slot", "-n", "1", "--",
                                   sys.executable, "-c", job], env=ENV, stderr=subprocess.PIPE) for _ in range(3)]  # fmt: skip
        for p in procs:
            self.assertEqual(p.wait(timeout=20), 0)
        self.assertEqual(log.read_text().split(), ["start", "end"] * 3)
        errors = [p.stderr.read() for p in procs]
        for p in procs:
            p.stderr.close()
        self.assertTrue(any(b"busy, waiting" in e for e in errors))

    def test_status_timeout_and_release_after_sigkill(self):
        holder = subprocess.Popen([sys.executable, "-m", "devguard", "--config", str(self.config), "slot", "-n", "1", "--",
                                   sys.executable, "-c", "import time; time.sleep(30)"], env=ENV)  # fmt: skip
        try:
            deadline = time.time() + 10
            while time.time() < deadline and "slot 1: free" in self.slot("--status", "-n", "1").stdout:
                time.sleep(0.05)
            status = self.slot("--status").stdout
            self.assertIn(f"slot 1: {holder.pid}", status)
            self.assertIn("slot 2: free", status)
            result = self.slot("-n", "1", "--timeout", "0.3", "--", "true")
            self.assertEqual(result.returncode, slot.EX_TEMPFAIL)
            holder.send_signal(signal.SIGKILL)
            holder.wait()
            self.assertEqual(self.slot("-n", "1", "--timeout", "5", "--", "true").returncode, 0)
        finally:
            if holder.poll() is None:
                holder.kill()

    def test_signal_reaches_the_command(self):
        p = subprocess.Popen([sys.executable, "-m", "devguard", "--config", str(self.config), "slot", "--",
                              sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                             env=ENV, stdout=subprocess.PIPE)  # fmt: skip
        self.assertEqual(p.stdout.readline().strip(), b"ready")
        p.send_signal(signal.SIGTERM)  # devguard exec'd the command, so this pid IS the command
        self.assertEqual(p.wait(timeout=10), -signal.SIGTERM)
        p.stdout.close()

    def test_refuses_foreign_or_symlinked_dir(self):
        os.symlink(self.tmp, self.tmp / "evil")
        with self.assertRaises(PermissionError):
            slot.ensure_dir(self.tmp / "evil")

    def test_count_precedence(self):
        config = Config()
        os.environ["DEVGUARD_SLOTS"] = "3"
        try:
            self.assertEqual(slot.slot_count(config, None), 3)
            self.assertEqual(slot.slot_count(config, 1), 1)
            os.environ["DEVGUARD_SLOTS"] = "zero"
            with self.assertRaises(ValueError):
                slot.slot_count(config, None)
        finally:
            del os.environ["DEVGUARD_SLOTS"]
        self.assertEqual(slot.slot_count(config, None), 4)


class ConfigTest(TempCase):
    def test_default_template_parses_to_defaults(self):
        import tomllib

        parsed = parse(tomllib.loads(DEFAULT_TOML))
        defaults = Config()
        for section in ("watch", "port", "slot", "sweep"):
            self.assertEqual(getattr(parsed, section), getattr(defaults, section), section)
        self.assertEqual(parsed.custom_servers, [])

    def test_missing_file_gives_defaults(self):
        config = load(self.tmp / "none.toml")
        self.assertIsNone(config.path)
        self.assertEqual(config.watch.hard_gb, 5.0)

    def test_values_and_errors(self):
        path = self.tmp / "c.toml"
        path.write_text(
            'state_dir = "~/st"\n[watch]\nhard_gb = 8\nmemory_exempt_env = ["JOURNEY_BINDINGS_URL"]\n'
            '[port.overrides]\n"~/x" = 4500\n[[servers.custom]]\nname = "dj"\nkind = "dev"\nmatch = ["runserver"]\n'
        )
        config = load(path)
        self.assertEqual(config.watch.hard_gb, 8.0)
        self.assertEqual(config.watch.memory_exempt_env, ["JOURNEY_BINDINGS_URL"])
        self.assertEqual(config.port.overrides, {str(Path.home() / "x"): 4500})
        self.assertEqual(config.state_path(), Path.home() / "st")
        self.assertEqual(config.custom_servers[0].match, ["runserver"])
        for bad, message in (
            ("[watch]\nhard_gbb = 1\n", "unknown key watch.hard_gbb"),
            ("[watch]\nhard_gb = 'x'\n", "number"),
            ("[watch]\nstop_finished = 1\n", "true or false"),
            ("[slot]\ncount = 0\n", "at least 1"),
            ("[port]\nbase = 65000\n", "within"),
            ("[port.overrides]\n'/x' = 70000\n", "port number"),
            ("[[servers.custom]]\nname = 'x'\nkind = 'other'\nmatch = ['a']\n", "kind"),
            ("nonsense = 1\n", "unknown key"),
            ("[watch\n", "c.toml"),
        ):
            path.write_text(bad)
            with self.assertRaises(ConfigError) as ctx:
                load(path)
            self.assertIn(message, str(ctx.exception))

    def test_cli_reports_config_errors(self):
        path = self.tmp / "c.toml"
        path.write_text("[watch]\nnope = 1\n")
        result = devguard("--config", str(path), "config")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown key watch.nope", result.stderr)
        self.assertIn("[watch]", devguard("config", "--default").stdout)


if __name__ == "__main__":
    unittest.main()
