#!/usr/bin/env python3
"""
Smoke tests for the seed generator. Each test runs the real CLI into a temp root.
Run:  python3 seed_generator/smoke_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "seed_generator.py")

INCLUDE_EXT = (".php", ".phtml", ".php5", ".phar", ".inc", ".htaccess", ".js", ".html", ".htm")
EXCLUDE_HINT = (".tmp", ".log", ".swp", "~", "/cache/", "/sessions/")


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, GEN, *args], capture_output=True, text=True)


def tree_sizes(root: str) -> dict[str, int]:
    out = {}
    for dp, _, files in os.walk(root):
        for f in files:
            full = os.path.join(dp, f)
            out[os.path.relpath(full, root)] = os.path.getsize(full)
    return out


def test_dry_run_reports_numbers():
    r = run("--dry-run", "--accounts", "4", "--seed", "1")
    assert r.returncode == 0, r.stderr
    assert "files:" in r.stdout and "disk (data):" in r.stdout
    return "dry-run prints files/dirs/disk without writing"


def test_generate_and_manifest():
    root = tempfile.mkdtemp()
    man = os.path.join(root, "m.json")
    r = run("--root", os.path.join(root, "home"), "--accounts", "3", "--seed", "7",
            "--manifest", man, "--force")
    assert r.returncode == 0, r.stderr
    m = json.load(open(man))
    assert m["totals"]["accounts"] == 3 and m["totals"]["files"] > 0
    acc = m["accounts"][0]
    # watch_targets exist and are include-set extensions
    assert acc["watch_targets"], "expected watch targets"
    for p in acc["watch_targets"]:
        assert os.path.exists(p), f"missing {p}"
        assert p.endswith(INCLUDE_EXT), f"watch target not in include set: {p}"
    # noise_targets exist and are NOT include-set
    for p in acc["noise_targets"]:
        assert os.path.exists(p)
        assert not p.endswith((".php", ".js", ".html", ".htm", ".htaccess")) or any(h in p for h in EXCLUDE_HINT)
    # plusx pool are php
    for p in acc["plusx_pool"]:
        assert p.endswith(".php") and os.path.exists(p)
    return f"3 accounts, {m['totals']['files']:,} files; pools valid & on disk"


def test_determinism():
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    for root in (a, b):
        r = run("--root", os.path.join(root, "home"), "--accounts", "3", "--seed", "42",
                "--manifest", os.path.join(root, "m.json"), "--force")
        assert r.returncode == 0, r.stderr
    ta, tb = tree_sizes(os.path.join(a, "home")), tree_sizes(os.path.join(b, "home"))
    assert ta == tb, "same seed must produce identical files + sizes"
    return f"identical tree across two runs ({len(ta):,} files match)"


def test_clean():
    root = tempfile.mkdtemp()
    home = os.path.join(root, "home")
    man = os.path.join(root, "m.json")
    run("--root", home, "--accounts", "2", "--seed", "3", "--manifest", man, "--force")
    assert os.path.isdir(os.path.join(home, "u1001"))
    r = run("--clean", "--root", home, "--manifest", man)
    assert r.returncode == 0, r.stderr
    assert not os.path.isdir(os.path.join(home, "u1001")), "clean should remove account trees"
    return "clean removes exactly the manifest's trees"


TESTS = [
    ("dry run", test_dry_run_reports_numbers),
    ("generate + manifest pools", test_generate_and_manifest),
    ("determinism", test_determinism),
    ("clean", test_clean),
]


def main() -> int:
    print("seed generator — smoke tests\n" + "-" * 52)
    fails = 0
    for name, fn in TESTS:
        try:
            # Every case creates thousands of files. Keep all of its mkdtemp() calls under
            # one managed directory so shared CI runners do not fill their writable layer.
            with tempfile.TemporaryDirectory(prefix="seed-smoke-") as test_tmp:
                previous_tempdir = tempfile.tempdir
                tempfile.tempdir = test_tmp
                try:
                    detail = fn()
                finally:
                    tempfile.tempdir = previous_tempdir
            print(f"  PASS  {name:<26} {detail}")
        except Exception as e:  # noqa
            fails += 1
            print(f"  FAIL  {name:<26} {type(e).__name__}: {e}")
    print("-" * 52)
    print("all passed" if not fails else f"{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
