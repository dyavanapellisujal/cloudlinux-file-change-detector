#!/usr/bin/env python3
"""
Smoke tests for the scanner stub — validates the acceptance criteria that need no kernel
features. Each scenario spawns its own stub instance on a private socket, exercises it, and
asserts. Run:  python3 scanner_stub/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STUB = os.path.join(HERE, "scanner_stub.py")


# --------------------------------------------------------------------------- #
class Client:
    """Opens a connection; a background task collects acks and verdicts."""

    def __init__(self):
        self.reader = self.writer = None
        self.acks: list[dict] = []
        self.verdicts: list[dict] = []
        self._task = None

    async def connect(self, sock: str):
        self.reader, self.writer = await asyncio.open_unix_connection(sock)
        self._task = asyncio.create_task(self._recv())

    async def _recv(self):
        try:
            while True:
                line = await self.reader.readuntil(b"\n")
                msg = json.loads(line)
                (self.verdicts if "verdict" in msg else self.acks).append(msg)
        except (asyncio.IncompleteReadError, asyncio.CancelledError, ConnectionError):
            return

    async def send_raw(self, lines: list[bytes]):
        for ln in lines:
            self.writer.write(ln + b"\n")
        await self.writer.drain()

    async def send_jobs(self, paths, reason="modified"):
        await self.send_raw([
            json.dumps({"path": p, "reason": reason, "size": 10, "mtime": 1.0,
                        "observed_at": time.time()}).encode()
            for p in paths
        ])

    async def close(self):
        with contextlib_suppress():
            self.writer.close()
        if self._task:
            self._task.cancel()


class contextlib_suppress:
    def __enter__(self): return self
    def __exit__(self, *a): return True


# --------------------------------------------------------------------------- #
async def start_stub(sock: str, *extra) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, STUB, "--socket", sock, "--log-interval-ms", "100000", *extra,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    for _ in range(100):
        if os.path.exists(sock):
            try:
                r, w = await asyncio.open_unix_connection(sock)
                w.close()
                return proc
            except OSError:
                pass
        await asyncio.sleep(0.05)
    raise RuntimeError("stub did not come up")


async def stop(proc):
    proc.terminate()
    with contextlib_suppress():
        await asyncio.wait_for(proc.wait(), timeout=5)


async def wait_until(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        await asyncio.sleep(0.02)
    return False


def tmp_sock(name): return os.path.join(tempfile.mkdtemp(), name + ".sock")


# --------------------------------------------------------------------------- #
async def test_happy_path():
    sock = tmp_sock("happy")
    proc = await start_stub(sock, "--rate", "1000")
    c = Client(); await c.connect(sock)
    paths = [f"/home/u1/public_html/f{i}.php" for i in range(50)]
    await c.send_jobs(paths)
    ok_acks = await wait_until(lambda: len(c.acks) == 50)
    ok_verd = await wait_until(lambda: len(c.verdicts) == 50)
    accepted = [a for a in c.acks if a.get("accepted")]
    order_ok = [a["path"] for a in c.acks] == paths
    await c.close(); await stop(proc)
    assert ok_acks and ok_verd, f"acks={len(c.acks)} verdicts={len(c.verdicts)}"
    assert len(accepted) == 50, "all should be accepted under capacity"
    assert order_ok, "ack order must match submission order"
    return "50 jobs -> 50 accepted acks (in order) + 50 verdicts"


async def test_backpressure():
    sock = tmp_sock("bp")
    proc = await start_stub(sock, "--rate", "20", "--queue-size", "50")
    c = Client(); await c.connect(sock)
    N = 2000
    await c.send_jobs([f"/home/u2/public_html/p{i}.php" for i in range(N)])
    got = await wait_until(lambda: len(c.acks) == N, timeout=15)
    accepted = sum(1 for a in c.acks if a.get("accepted"))
    busy = sum(1 for a in c.acks if a.get("reason") == "busy")
    await c.close(); await stop(proc)
    assert got, f"missing acks: {len(c.acks)}/{N}"
    assert accepted + busy == N, f"every job needs exactly one ack ({accepted}+{busy})"
    assert busy > 0, "a 2000-job burst into a 50-slot queue must produce busy backpressure"
    assert accepted <= 200, f"accepted ({accepted}) should be bounded by the queue, not unbounded"
    return f"{N}-job burst -> {accepted} accepted, {busy} busy, 0 lost"


async def test_malformed_recovery():
    sock = tmp_sock("bad")
    proc = await start_stub(sock, "--rate", "1000")
    c = Client(); await c.connect(sock)
    lines = [
        json.dumps({"path": "/a.php", "reason": "created"}).encode(),  # ok
        b"{not json at all",                                            # bad json
        json.dumps({"path": "/b.php"}).encode(),                       # missing reason
        json.dumps({"path": "/c.php", "reason": "exploded"}).encode(),  # bad reason
        json.dumps({"path": "/d.php", "reason": "replaced"}).encode(),  # ok (stream in sync)
    ]
    await c.send_raw(lines)
    got = await wait_until(lambda: len(c.acks) == 5)
    accepted = [a for a in c.acks if a.get("accepted")]
    bad = [a for a in c.acks if a.get("reason") == "bad_request"]
    await c.close(); await stop(proc)
    assert got, f"acks={len(c.acks)}"
    assert len(accepted) == 2 and len(bad) == 3, f"accepted={len(accepted)} bad={len(bad)}"
    assert accepted[-1]["path"] == "/d.php", "valid job after bad lines must still be accepted"
    return "valid/invalid mix -> 2 accepted, 3 bad_request, stream stayed in sync"


async def _collect_verdicts(sock, paths):
    c = Client(); await c.connect(sock)
    await c.send_jobs(paths)
    await wait_until(lambda: len(c.verdicts) == len(paths))
    m = {v["path"]: v["verdict"] for v in c.verdicts}
    await c.close()
    return m


async def test_determinism_and_glob():
    paths = [f"/home/u3/x{i}.php" for i in range(100)] + ["/home/u3/evil_shell.php"]
    flags = ("--rate", "2000", "--malicious-rate", "0.5", "--seed", "7",
             "--malicious-glob", "*evil*")
    s1 = tmp_sock("det1"); p1 = await start_stub(s1, *flags)
    m1 = await _collect_verdicts(s1, paths); await stop(p1)
    s2 = tmp_sock("det2"); p2 = await start_stub(s2, *flags)
    m2 = await _collect_verdicts(s2, paths); await stop(p2)
    assert m1 == m2, "same seed must yield identical verdicts"
    assert m1["/home/u3/evil_shell.php"] == "malicious", "glob match must be malicious"
    mal = sum(1 for v in m1.values() if v == "malicious")
    assert 0 < mal < len(paths), f"malicious-rate 0.5 should split the set (got {mal})"
    return f"deterministic verdicts reproduced; glob honoured; {mal}/{len(paths)} malicious"


async def test_concurrency():
    sock = tmp_sock("conc")
    proc = await start_stub(sock, "--rate", "1000", "--queue-size", "1000")
    clients = [Client() for _ in range(4)]
    for i, c in enumerate(clients):
        await c.connect(sock)
        await c.send_jobs([f"/home/u{i}/f{j}.php" for j in range(100)])
    ok = await wait_until(lambda: all(len(c.verdicts) == 100 for c in clients), timeout=15)
    totals = [(len([a for a in c.acks if a.get("accepted")]), len(c.verdicts)) for c in clients]
    for c in clients:
        await c.close()
    await stop(proc)
    assert ok, f"per-conn verdicts: {[len(c.verdicts) for c in clients]}"
    assert all(a == 100 and v == 100 for a, v in totals), f"totals={totals}"
    return "4 concurrent connections -> 400 accepted + 400 verdicts, none starved"


# --------------------------------------------------------------------------- #
TESTS = [
    ("happy path", test_happy_path),
    ("backpressure / busy", test_backpressure),
    ("malformed recovery", test_malformed_recovery),
    ("determinism + glob verdicts", test_determinism_and_glob),
    ("concurrency", test_concurrency),
]


async def main():
    print("scanner stub — smoke tests\n" + "-" * 48)
    failures = 0
    for name, fn in TESTS:
        try:
            detail = await asyncio.wait_for(fn(), timeout=40)
            print(f"  PASS  {name:<28} {detail}")
        except Exception as e:  # noqa
            failures += 1
            print(f"  FAIL  {name:<28} {type(e).__name__}: {e}")
    print("-" * 48)
    print("all passed" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
