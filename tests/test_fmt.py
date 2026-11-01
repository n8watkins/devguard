"""Formatting helpers."""

from __future__ import annotations

import unittest
from pathlib import Path

import helpers  # noqa: F401 (sets sys.path)

from devguard.fmt import duration, home, is_within, size, table


class FmtTest(unittest.TestCase):
    def test_size(self):
        self.assertEqual([size(None), size(10), size(2048), size(5 * 1024**2), size(int(4.25 * 1024**3))],
                         ["?", "10B", "2K", "5M", "4.2G"])  # fmt: skip

    def test_duration(self):
        self.assertEqual([duration(12), duration(150), duration(5400), duration(3 * 86400)], ["12s", "2m", "1.5h", "3.0d"])

    def test_home_and_within(self):
        self.assertEqual(home(Path.home() / "x"), "~/x")
        self.assertEqual(home("/elsewhere"), "/elsewhere")
        self.assertTrue(is_within("/a/b/c", "/a/b"))
        self.assertTrue(is_within("/a/b", "/a/b/"))
        self.assertFalse(is_within("/a/bc", "/a/b"))

    def test_table_alignment(self):
        out = table(["A", "NUM", "LAST"], [["x", "1", "tail"], ["long", "100", "t"]], right={1})
        self.assertEqual(out.splitlines(), ["A     NUM  LAST", "x       1  tail", "long  100  t"])


if __name__ == "__main__":
    unittest.main()
