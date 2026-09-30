#!/usr/bin/env python3
"""
Scanner stub — the one fixed interface of the Engineering Discovery Challenge.

Accepts inspection jobs as newline-delimited JSON over a UNIX domain socket, acknowledges
each one promptly, applies a bounded intake queue (real `busy` backpressure) feeding a
rate-limited backend, and returns verdicts asynchronously on the same connection.

It performs NO real scanning and is strictly neutral to how changes are detected.
See scanner-stub-spec.md for the full contract.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fnmatch
import hashlib
import json
import os
import sys
import time


# --------------------------------------------------------------------------- #
#  Rate limiting
# --------------------------------------------------------------------------- #
class TokenBucket:
    """Single shared token bucket — the global processing ceiling."""

    def __init__(self, rate: float, burst: float):
        self.rate = max(0.001, float(rate))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.last = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self.tokens) / self.rate)


# --------------------------------------------------------------------------- #
#  Connection state
# --------------------------------------------------------------------------- #
class Conn:
    """One client connection: an outbound queue serializes acks + verdicts."""

    __slots__ = ("writer", "out", "closed")

    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.out: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def send(self, msg: dict) -> None:
        if not self.closed:
            with contextlib.suppress(asyncio.QueueFull):
                self.out.put_nowait(msg)


# --------------------------------------------------------------------------- #
#  Server
# --------------------------------------------------------------------------- #
class ScannerStub:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.socket_path = args.socket
        self.max_line = args.max_line
        self.verdict_delay = args.verdict_delay_ms / 1000.0
        self.malicious_glob = list(args.malicious_glob or [])
        self.malicious_rate = max(0.0, min(1.0, args.malicious_rate))
        self.seed = args.seed
        self.jobs_log_path = args.jobs_log         # reviewer-side capture (not shown to candidate)
        self.jobs_log = None

        self.intake: asyncio.Queue = asyncio.Queue(maxsize=args.queue_size)
        self.bucket = TokenBucket(args.rate, args.rate_burst or args.rate)
        self.conns: set[Conn] = set()
        self.down = False  # outage flag

        self.m = dict(recv=0, accepted=0, busy=0, bad=0, processed=0,
                      v_clean=0, v_malicious=0)
        self.queue_hwm = 0

    # ---- verdict assignment (deterministic) ------------------------------- #
    def verdict_for(self, path: str) -> str:
        for g in self.malicious_glob:
            if fnmatch.fnmatch(path, g):
                return "malicious"
        if self.malicious_rate > 0.0:
            h = hashlib.sha256(f"{self.seed}:{path}".encode()).digest()
            frac = int.from_bytes(h[:8], "big") / 2 ** 64
            if frac < self.malicious_rate:
                return "malicious"
        return "clean"

    # ---- per-connection line reader (enforces max-line, recovers in sync) -- #
    async def _read_lines(self, reader: asyncio.StreamReader):
        buf = bytearray()
        overflow = False
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl == -1:
                    if len(buf) > self.max_line:
                        overflow = True
                        buf.clear()
                    break
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                if overflow:
                    overflow = False
                    yield None          # tail of an over-long line -> bad_request
                else:
                    yield line

    # ---- writer drain task ------------------------------------------------ #
    async def _writer_task(self, conn: Conn):
        try:
            while True:
                msg = await conn.out.get()
                if msg is None:
                    return
                conn.writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
                await conn.writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            conn.closed = True

    # ---- connection handler ---------------------------------------------- #
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if self.down:
            with contextlib.suppress(Exception):
                writer.close()
            return
        conn = Conn(writer)
        self.conns.add(conn)
        wtask = asyncio.create_task(self._writer_task(conn))
        try:
            async for line in self._read_lines(reader):
                if line is None:
                    conn.send({"path": None, "accepted": False, "reason": "bad_request"})
                    self.m["bad"] += 1
                    continue
                if not line.strip():
                    continue
                path = reason = None
                try:
                    job = json.loads(line)
                    path, reason = job["path"], job["reason"]
                    if not isinstance(path, str) or reason not in ("created", "modified", "replaced"):
                        raise ValueError
                except Exception:
                    p = path if isinstance(path, str) else None
                    conn.send({"path": p, "accepted": False, "reason": "bad_request"})
                    self.m["bad"] += 1
                    continue

                self.m["recv"] += 1
                try:
                    self.intake.put_nowait((conn, path))
                    conn.send({"path": path, "accepted": True})
                    self.m["accepted"] += 1
                    accepted_flag = True
                except asyncio.QueueFull:
                    conn.send({"path": path, "accepted": False, "reason": "busy"})
                    self.m["busy"] += 1
                    accepted_flag = False
                if self.jobs_log:
                    self.jobs_log.write(json.dumps(
                        {"path": path, "reason": reason, "observed_at": job.get("observed_at"),
                         "recv_ts": round(time.time(), 6), "accepted": accepted_flag},
                        separators=(",", ":")) + "\n")
                    self.jobs_log.flush()
        finally:
            conn.closed = True
            conn.out.put_nowait(None)
            with contextlib.suppress(Exception):
                await wtask
            self.conns.discard(conn)
            with contextlib.suppress(Exception):
                writer.close()

    # ---- backend processor (single, = global ceiling) -------------------- #
    async def _deliver(self, conn: Conn, path: str):
        if self.verdict_delay > 0:
            await asyncio.sleep(self.verdict_delay)
        verdict = self.verdict_for(path)
        self.m["v_" + verdict] += 1
        conn.send({"path": path, "verdict": verdict})

    async def processor(self):
        while True:
            conn, path = await self.intake.get()
            await self.bucket.acquire()
            self.m["processed"] += 1
            if self.verdict_delay > 0:
                asyncio.create_task(self._deliver(conn, path))
            else:
                await self._deliver(conn, path)

    # ---- outage (flap) ---------------------------------------------------- #
    def _go_down(self):
        if self.down:
            return
        self.down = True
        if self.args.flap_drop_queue:
            while not self.intake.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.intake.get_nowait()
        for c in list(self.conns):
            c.closed = True
            with contextlib.suppress(Exception):
                c.out.put_nowait(None)
            with contextlib.suppress(Exception):
                c.writer.close()
        self.conns.clear()
        self._log("scanner DOWN (outage)")

    def _go_up(self):
        if not self.down:
            return
        self.down = False
        self._log("scanner UP (recovered)")

    async def flapper(self):
        up = self.args.flap_up_ms / 1000.0
        down = self.args.flap_down_ms / 1000.0
        while True:
            await asyncio.sleep(up)
            self._go_down()
            await asyncio.sleep(down)
            self._go_up()

    def toggle(self):  # SIGUSR1
        self._go_up() if self.down else self._go_down()

    # ---- metrics ---------------------------------------------------------- #
    def _log(self, text: str):
        print(f"[scanner-stub] {text}", file=sys.stderr, flush=True)

    async def metrics(self):
        interval = self.args.log_interval_ms / 1000.0
        prev = dict(self.m)
        while True:
            await asyncio.sleep(interval)
            cur = dict(self.m)
            depth = self.intake.qsize()
            self.queue_hwm = max(self.queue_hwm, depth)
            d = {k: cur[k] - prev[k] for k in cur}
            prev = cur
            per_s = lambda v: round(v / interval, 1)
            if self.args.json_logs:
                rec = {
                    "t": round(time.time(), 3), "conns": len(self.conns),
                    "recv_s": per_s(d["recv"]), "accepted_s": per_s(d["accepted"]),
                    "busy_s": per_s(d["busy"]), "bad_s": per_s(d["bad"]),
                    "queue": depth, "queue_hwm": self.queue_hwm,
                    "processed_s": per_s(d["processed"]),
                    "verdict_s": {"clean": per_s(d["v_clean"]), "malicious": per_s(d["v_malicious"])},
                    "down": self.down,
                }
                print(json.dumps(rec, separators=(",", ":")), file=sys.stderr, flush=True)
            else:
                self._log(
                    f"conns={len(self.conns)} recv/s={per_s(d['recv'])} "
                    f"accepted/s={per_s(d['accepted'])} busy/s={per_s(d['busy'])} "
                    f"queue={depth}/{self.queue_hwm} processed/s={per_s(d['processed'])} "
                    f"verdicts/s(c/m)={per_s(d['v_clean'])}/{per_s(d['v_malicious'])}"
                    + (" DOWN" if self.down else "")
                )

    # ---- lifecycle -------------------------------------------------------- #
    async def serve(self):
        os.makedirs(os.path.dirname(self.socket_path) or ".", exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)

        server = await asyncio.start_unix_server(
            self.handle, path=self.socket_path, limit=max(self.max_line * 2, 1 << 20)
        )
        with contextlib.suppress(OSError):
            os.chmod(self.socket_path, 0o660)

        if self.jobs_log_path:
            self.jobs_log = open(self.jobs_log_path, "w")

        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            import signal
            loop.add_signal_handler(signal.SIGUSR1, self.toggle)

        tasks = [asyncio.create_task(self.processor()),
                 asyncio.create_task(self.metrics())]
        if self.args.flap_up_ms and self.args.flap_down_ms:
            tasks.append(asyncio.create_task(self.flapper()))

        self._log(f"listening on {self.socket_path} | rate={self.bucket.rate:.0f}/s "
                  f"queue={self.args.queue_size}")
        try:
            async with server:
                await server.serve_forever()
        finally:
            for t in tasks:
                t.cancel()
            if self.jobs_log:
                self.jobs_log.close()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.socket_path)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scanner stub (challenge fixture).")
    p.add_argument("--socket", default="/tmp/scanner/intake.sock")
    p.add_argument("--rate", type=float, default=None, help="processed jobs/second (default 200)")
    p.add_argument("--slow", action="store_true", help="shortcut for a low rate (20/s)")
    p.add_argument("--rate-burst", type=float, default=None)
    p.add_argument("--queue-size", type=int, default=1000)
    p.add_argument("--max-line", type=int, default=65536)
    p.add_argument("--verdict-delay-ms", type=float, default=0.0)
    p.add_argument("--malicious-glob", action="append", default=[])
    p.add_argument("--malicious-rate", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--flap-down-ms", type=int, default=0)
    p.add_argument("--flap-up-ms", type=int, default=0)
    p.add_argument("--flap-drop-queue", dest="flap_drop_queue", action="store_true", default=True)
    p.add_argument("--no-flap-drop-queue", dest="flap_drop_queue", action="store_false")
    p.add_argument("--log-interval-ms", type=int, default=1000)
    p.add_argument("--json-logs", action="store_true")
    p.add_argument("--jobs-log", default="", help="reviewer capture: record received jobs as NDJSON")
    args = p.parse_args(argv)
    if args.rate is None:
        args.rate = 20.0 if args.slow else 200.0
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(ScannerStub(args).serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
