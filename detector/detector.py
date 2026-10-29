#!/usr/bin/env python3
"""
detector.py — entry point that wires the pieces together.

The harness runs us with two environment variables:
    SEED_TREE       root of the account tree to watch   (required)
    SCANNER_SOCKET  UNIX socket to submit jobs to        (required)

Pipeline:
    watcher --(every change)--> is_relevant() --> WorkQueue --> ScannerClient

  * watcher        fanotify on Linux, polling fallback elsewhere
  * is_relevant()  keeps only the files the scanner cares about (the watch set)
  * WorkQueue      coalesces / dedups / cools down (avoids duplicate scans)
  * ScannerClient  delivers with busy-backoff and reconnect (never loses a change)
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import threading
import time

from work_queue import WorkQueue, Job
from scanner_client import ScannerClient

# --------------------------------------------------------------------------- #
#  The watch set — what counts as a relevant change
# --------------------------------------------------------------------------- #
RELEVANT_EXTS = (".php", ".phtml", ".php5", ".phar", ".inc", ".js", ".html", ".htm")
NOISE_EXTS = (".log", ".tmp", ".swp")
MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".css")
EXCLUDED_DIRS = ("/wp-content/cache/", "/sessions/")

RESCAN_COOLDOWN = 5.0   # seconds a file is held from re-scanning; see RATIONALE


def is_relevant(path: str, is_exec: bool) -> bool:
    low = path.lower()
    if any(part in low for part in EXCLUDED_DIRS):
        return False
    # A file gaining execute permission is the strongest single signal of a
    # dropped payload, so it is relevant whatever its name.
    if is_exec:
        return True
    name = os.path.basename(low)
    if name.endswith("~") or name.endswith(NOISE_EXTS):
        return False
    if name.endswith(".htaccess"):          # a pattern: inj_01.htaccess counts too
        return True
    if name.endswith(MEDIA_EXTS):
        return False
    return name.endswith(RELEVANT_EXTS)


# --------------------------------------------------------------------------- #
#  Wiring
# --------------------------------------------------------------------------- #
def make_watcher(seed_tree, on_change, stop):
    """fanotify on Linux if we can set it up; otherwise the polling fallback."""
    if sys.platform.startswith("linux"):
        try:
            from fanotify_watcher import FanotifyWatcher
            return FanotifyWatcher(seed_tree, on_change, stop)
        except Exception as e:                # not root, old kernel, etc.
            print(f"[detector] fanotify unavailable ({e}); using polling fallback",
                  file=sys.stderr, flush=True)
    from poll_watcher import PollWatcher
    return PollWatcher(seed_tree, on_change, stop)


def main() -> int:
    seed_tree = os.environ.get("SEED_TREE")
    socket_path = os.environ.get("SCANNER_SOCKET")
    if not seed_tree or not socket_path:
        print("SEED_TREE and SCANNER_SOCKET must be set", file=sys.stderr)
        return 2

    # Durable queue so a detector restart doesn't drop a backlog. Override the
    # location with DETECTOR_DB; set it empty to run purely in-memory.
    db_path = os.environ.get("DETECTOR_DB",
                             os.path.join(tempfile.gettempdir(), "detector-queue.db"))
    try:
        queue = WorkQueue(cooldown=RESCAN_COOLDOWN, db_path=db_path or None)
    except Exception as e:                       # unwritable path, etc.
        print(f"[detector] durable queue unavailable ({e}); using in-memory",
              file=sys.stderr, flush=True)
        queue = WorkQueue(cooldown=RESCAN_COOLDOWN)
    stop = threading.Event()

    def on_change(path, reason, size, mtime_ns, is_exec):
        if is_relevant(path, is_exec):
            queue.offer(Job(path, reason, size, mtime_ns, is_exec, time.time()))

    verbose = os.environ.get("DETECTOR_VERBOSE", "") not in ("", "0", "false")
    watcher = make_watcher(seed_tree, on_change, stop)
    client = ScannerClient(socket_path, queue, stop, verbose=verbose)

    def shutdown(*_):
        stop.set()
        queue.stop()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threads = [
        threading.Thread(target=watcher.run, name="watcher", daemon=True),
        threading.Thread(target=client.run, name="client", daemon=True),
    ]
    for t in threads:
        t.start()
    print(f"[detector] watching {seed_tree} -> {socket_path}", file=sys.stderr, flush=True)

    # Light heartbeat: surface the backlog so overload is observable (alertable).
    last_stat = 0.0
    while not stop.is_set():
        stop.wait(0.5)
        now = time.monotonic()
        if now - last_stat >= 5.0:
            last_stat = now
            d = queue.depth()
            if d or verbose:
                print(f"[detector] queue depth={d}", file=sys.stderr, flush=True)
    for t in threads:
        t.join(timeout=2.0)
    queue.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
