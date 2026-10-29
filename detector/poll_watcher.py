"""
PollWatcher — the FALLBACK change source.

When fanotify is not available (a non-Linux dev machine, or a kernel older than
5.9), this walks the tree on a fixed interval and reports any file whose
signature (size, mtime, exec-bit) changed since the last pass.

It is deliberately the fallback: on a real fleet of millions of files a full
walk is too slow and too I/O-heavy to meet the ~2s latency target — which is
exactly why fanotify is the primary mechanism. But it keeps the rest of the
pipeline runnable and testable anywhere, and gives the detector a graceful
degradation path instead of simply failing on an old kernel.
"""
from __future__ import annotations

import os
import stat
import sys


def _log(msg: str) -> None:
    print(f"[detector/poll] {msg}", file=sys.stderr, flush=True)


class PollWatcher:
    def __init__(self, root: str, callback, stop_event, interval: float = 1.0):
        self.root = root
        self.callback = callback      # callback(path, reason, size, mtime_ns, is_exec)
        self.stop = stop_event
        self.interval = interval

    def run(self) -> None:
        _log(f"polling {self.root} every {self.interval}s (fanotify unavailable)")
        baseline = {}                 # path -> signature
        first_pass = True             # record the existing tree silently, don't scan it
        while not self.stop.is_set():
            seen = set()
            for dirpath, _dirs, files in os.walk(self.root):
                for name in files:
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    seen.add(path)
                    is_exec = bool(st.st_mode & 0o111)
                    sig = (st.st_size, st.st_mtime_ns, is_exec)
                    old = baseline.get(path)
                    if old != sig:
                        baseline[path] = sig
                        if not first_pass:
                            reason = "created" if old is None else "modified"
                            self.callback(path, reason, st.st_size, st.st_mtime_ns, is_exec)
            for gone in set(baseline) - seen:   # forget deleted files
                del baseline[gone]
            first_pass = False
            self.stop.wait(self.interval)
