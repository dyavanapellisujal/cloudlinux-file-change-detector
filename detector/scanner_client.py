"""
ScannerClient — the consumer side.

Pulls jobs off the WorkQueue and delivers each one to the scanner over the UNIX
socket (one JSON object per line). It handles the three things that make this a
real-world problem:

  * backpressure — an `accepted:false / busy` ack means the scanner DROPPED the
                   job, so we put it back and pause the whole sender before
                   retrying. The pause starts at 50ms, doubles on each repeated
                   busy up to a 1s cap, and resets to 0 the moment a send
                   succeeds (classic exponential backoff — low latency when
                   healthy, gentle on the scanner when overloaded).

  * disconnects  — if the scanner goes down it closes the socket. We put the
                   in-flight job back on the queue (it was never accepted, so it
                   must not be lost) and reconnect, retrying until it returns.

  * verdicts     — replies carrying a `verdict` field arrive asynchronously and
                   are not acks; for Phase 1 we just count and skip them.
"""
from __future__ import annotations

import json
import socket
import sys


INITIAL_PAUSE = 0.05   # 50ms — first pause after a busy
MAX_PAUSE = 1.0        # 1s cap
CONNECT_PAUSE = 0.2    # first reconnect delay
MAX_CONNECT_PAUSE = 5.0
SOCKET_TIMEOUT = 15.0  # detect a hung scanner and reconnect


def _log(msg: str) -> None:
    print(f"[detector/client] {msg}", file=sys.stderr, flush=True)


class _Link:
    """A tiny newline-delimited JSON reader/writer over one socket."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def write_json(self, obj: dict) -> None:
        self.sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    def read_json(self) -> dict:
        while True:
            nl = self.buf.find(b"\n")
            if nl != -1:
                line = bytes(self.buf[:nl])
                del self.buf[:nl + 1]
                if line.strip():
                    return json.loads(line)
                continue
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("scanner closed the connection")
            self.buf.extend(chunk)


class ScannerClient:
    def __init__(self, socket_path: str, queue, stop_event, verbose: bool = False):
        self.socket_path = socket_path
        self.queue = queue
        self.stop = stop_event
        self.verbose = verbose  # log each accepted job (handy for watching a demo)
        self.pause = 0.0        # current backoff pause (the PO's pause_ms, in seconds)
        self.verdicts = 0

    def run(self) -> None:
        """Outer loop: (re)connect forever, draining the queue while connected."""
        connect_pause = CONNECT_PAUSE
        while not self.stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(SOCKET_TIMEOUT)
                sock.connect(self.socket_path)
            except OSError as e:
                _log(f"connect failed ({e}); retry in {connect_pause:.1f}s")
                self.stop.wait(connect_pause)
                connect_pause = min(connect_pause * 2, MAX_CONNECT_PAUSE)
                continue

            _log("connected to scanner")
            connect_pause = CONNECT_PAUSE
            try:
                self._drain(_Link(sock))
            except (OSError, ConnectionError) as e:
                _log(f"connection lost ({e}); will reconnect")
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

    def _drain(self, link: _Link) -> None:
        """Send jobs until stopped or the connection breaks."""
        while not self.stop.is_set():
            job = self.queue.pop()
            if job is None:               # stopped
                return

            if self.pause > 0:            # honor the current backoff before sending
                self.stop.wait(self.pause)

            try:
                link.write_json(job.to_wire())
                ack = self._read_ack(link)
            except (OSError, ConnectionError):
                self.queue.requeue(job)   # never acked -> retry after reconnect
                raise

            if ack is None:               # stopped mid-read
                self.queue.requeue(job)
                return

            if ack.get("accepted"):
                self.queue.mark_sent(job)
                self.pause = 0.0          # recovered -> full speed
                if self.verbose:
                    _log(f"SCAN  {job.reason:9} {job.path}")
            elif ack.get("reason") == "busy":
                self.queue.requeue(job)   # scanner dropped it -> send again later
                self.pause = INITIAL_PAUSE if self.pause == 0 else min(self.pause * 2, MAX_PAUSE)
            else:                         # bad_request etc. — shouldn't happen; don't loop
                _log(f"scanner rejected {job.path}: {ack.get('reason')}")
                self.queue.mark_sent(job)

    def _read_ack(self, link: _Link):
        """Return the next ack; verdicts share the socket, so skip them."""
        while not self.stop.is_set():
            msg = link.read_json()
            if "verdict" in msg:
                self.verdicts += 1
                continue
            return msg
        return None
