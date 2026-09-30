#!/usr/bin/env python3
"""
Smoke tests for the change-load generator. Seeds a tiny tree, runs scenarios, validates the
oracle. Run:  python3 change_load_generator/smoke_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LOADGEN = os.path.join(HERE, "load_generator.py")
SEEDGEN = os.path.join(HERE, "..", "seed_generator", "seed_generator.py")

REL_OPS = {"created", "modified", "replaced", "chmod-x"}
NOISE_OPS = {"write-log", "write-cache", "atime", "write-swap"}


def run(script, *args):
    return subprocess.run([sys.executable, script, *args], capture_output=True, text=True)


def seed(tmp: str) -> str:
    home = os.path.join(tmp, "home"); man = os.path.join(tmp, "seed.json")
    # This suite validates change generation, not fixture scale. Keep its prerequisite tree
    # bounded so five independent cases fit comfortably on a shared CI runner.
    r = run(
        SEEDGEN,
        "--root", home,
        "--accounts", "2",
        "--files-per-account", "400",
        "--heavy-fraction", "0",
        "--max-file-kb", "8",
        "--workers", "2",
        "--seed", "5",
        "--manifest", man,
        "--force",
    )
    assert r.returncode == 0, r.stderr
    return man


def load_oracle(path: str) -> list[dict]:
    return [json.loads(l) for l in open(path) if l.strip()]


def test_burst_oracle_and_needles():
    tmp = tempfile.mkdtemp(); man = seed(tmp); orc = os.path.join(tmp, "o.ndjson")
    r = run(LOADGEN, "--manifest", man, "--scenario", "burst", "--burst-size", "500",
            "--burst-duration", "0", "--needles", "2", "--needle-token", "evil",
            "--oracle", orc, "--seed", "1")
    assert r.returncode == 0, r.stderr
    rows = load_oracle(orc)
    assert len(rows) == 500, f"expected 500 ops, got {len(rows)}"
    needles = [x for x in rows if x["is_needle"]]
    assert len(needles) == 2, f"expected 2 needles, got {len(needles)}"
    for n in needles:
        assert n["op"] == "created" and n["class"] == "relevant" and "evil" in n["path"]
        assert os.path.exists(n["path"]), "needle file must really exist on disk"
    return f"burst -> 500 ops, 2 needles tagged + on disk"


def test_op_class_invariants():
    tmp = tempfile.mkdtemp(); man = seed(tmp); orc = os.path.join(tmp, "o.ndjson")
    run(LOADGEN, "--manifest", man, "--scenario", "burst", "--burst-size", "300",
        "--burst-duration", "0", "--needles", "0", "--oracle", orc, "--seed", "2")
    # add noise via idle
    orc2 = os.path.join(tmp, "n.ndjson")
    run(LOADGEN, "--manifest", man, "--scenario", "idle", "--noise-ratio", "20",
        "--oracle", orc2, "--seed", "2")
    for x in load_oracle(orc):
        assert x["class"] == "relevant" and x["op"] in REL_OPS, x
    noise = load_oracle(orc2)
    assert noise and all(x["class"] == "noise" and x["op"] in NOISE_OPS for x in noise)
    return "relevant ops are create/modify/replace/chmod-x; noise ops are log/cache/atime/swap"


def test_determinism():
    tmp = tempfile.mkdtemp(); man = seed(tmp)
    o1, o2 = os.path.join(tmp, "a.ndjson"), os.path.join(tmp, "b.ndjson")
    for o in (o1, o2):
        run(LOADGEN, "--manifest", man, "--scenario", "burst", "--burst-size", "400",
            "--burst-duration", "0", "--needles", "3", "--oracle", o, "--seed", "99")
    key = lambda rows: [(x["seq"], x["op"], x["class"], x["path"], x["is_needle"]) for x in rows]
    assert key(load_oracle(o1)) == key(load_oracle(o2)), "same seed must yield identical oracle"
    return "two runs -> byte-identical op/path/needle sequence"


def test_dedup_stress_repeats():
    tmp = tempfile.mkdtemp(); man = seed(tmp); orc = os.path.join(tmp, "o.ndjson")
    run(LOADGEN, "--manifest", man, "--scenario", "dedup-stress", "--dedup-window-ms", "0",
        "--dedup-repeats", "8", "--oracle", orc, "--seed", "4")
    rows = load_oracle(orc)
    from collections import Counter
    c = Counter(x["path"] for x in rows)
    assert rows and max(c.values()) >= 8, f"same path should repeat >= 8x, got {max(c.values())}"
    return f"dedup-stress -> same paths rewritten {max(c.values())}x (dedup measurable)"


def test_oracle_integrity():
    tmp = tempfile.mkdtemp(); man = seed(tmp); orc = os.path.join(tmp, "o.ndjson")
    run(LOADGEN, "--manifest", man, "--scenario", "burst", "--burst-size", "250",
        "--burst-duration", "0", "--oracle", orc, "--seed", "7")
    rows = load_oracle(orc)
    seqs = [x["seq"] for x in rows]
    assert seqs == list(range(1, len(rows) + 1)), "seq must be monotonic 1..N"
    assert all({"ts", "op", "class", "path", "account"} <= x.keys() for x in rows)
    return f"oracle well-formed, seq monotonic 1..{len(rows)}"


TESTS = [
    ("burst + needles", test_burst_oracle_and_needles),
    ("op/class invariants", test_op_class_invariants),
    ("determinism", test_determinism),
    ("dedup-stress repeats", test_dedup_stress_repeats),
    ("oracle integrity", test_oracle_integrity),
]


def main() -> int:
    print("change-load generator — smoke tests\n" + "-" * 56)
    fails = 0
    for name, fn in TESTS:
        try:
            with tempfile.TemporaryDirectory(prefix="change-load-smoke-") as test_tmp:
                previous_tempdir = tempfile.tempdir
                tempfile.tempdir = test_tmp
                try:
                    detail = fn()
                finally:
                    tempfile.tempdir = previous_tempdir
            print(f"  PASS  {name:<24} {detail}")
        except Exception as e:  # noqa
            fails += 1
            print(f"  FAIL  {name:<24} {type(e).__name__}: {e}")
    print("-" * 56)
    print("all passed" if not fails else f"{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
