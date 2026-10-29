"""
WorkQueue — the heart of the detector.

It is a to-do list of files waiting to be sent to the scanner, with three jobs
rolled into one small, thread-safe class:

  1. coalescing  — the same file queued twice collapses into one entry, so a
                   burst of writes before we send becomes a single job.
  2. signature   — skip a change that did not *meaningfully* alter the file.
                   A file's signature is (size, mtime, exec-bit): a content edit
                   moves size/mtime, a `chmod +x` flips the exec-bit, but a
                   chown- or atime-only change moves none of them, so we don't
                   waste a scan on it.
  3. cooldown    — after a file is scanned, rapid re-writes are held back for a
                   short window so one busy file can't flood the limited scanner.
                   The *latest* version is still delivered once the window ends,
                   so we never silently drop the last change (no missed scans).

One lock guards everything, so the watcher thread (producer) and the
scanner-client thread (consumer) can share it safely.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Job:
    path: str
    reason: str          # "created" | "modified" | "replaced"
    size: int
    mtime_ns: int
    is_exec: bool
    observed_at: float   # wall-clock time we first noticed this change

    @property
    def sig(self):
        # What counts as a "meaningful" change, per the scanner's definition:
        # new/replaced content (size or mtime moves) or a new execute bit.
        return (self.size, self.mtime_ns, self.is_exec)

    def to_wire(self) -> dict:
        return {
            "path": self.path,
            "reason": self.reason,
            "size": self.size,
            "mtime": round(self.mtime_ns / 1e9, 6),
            "observed_at": round(self.observed_at, 6),
        }


class _Store:
    """
    Durable backing for jobs that have NOT yet been delivered (SQLite, WAL mode).

    This is the crash-safety net: if the detector process itself dies while a
    big backlog is waiting (e.g. the scanner was down), the backlog is on disk
    and is reloaded on restart. We persist only the "still-to-send" set:

      * enqueue  -> INSERT OR REPLACE   (path is the primary key, so re-queuing
                    the same file just overwrites — coalescing for free)
      * deliver  -> DELETE the exact version we sent (matched on path+observed_at,
                    so a newer change that arrived meanwhile is kept)
      * startup  -> load every row back onto the queue (recovery)

    Cooldown/last-sent state is intentionally NOT persisted: losing it on a crash
    can only cause a harmless extra scan, never a missed one.
    """

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")      # fast, durable commits
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS pending ("
            "path TEXT PRIMARY KEY, reason TEXT, size INTEGER, "
            "mtime_ns INTEGER, is_exec INTEGER, observed_at REAL)")
        self.db.commit()

    def put(self, job: "Job") -> None:
        self.db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?,?)",
                        (job.path, job.reason, job.size, job.mtime_ns,
                         int(job.is_exec), job.observed_at))
        self.db.commit()

    def delete(self, job: "Job") -> None:
        self.db.execute("DELETE FROM pending WHERE path=? AND observed_at=?",
                        (job.path, job.observed_at))
        self.db.commit()

    def load_all(self):
        cur = self.db.execute("SELECT path, reason, size, mtime_ns, is_exec, "
                              "observed_at FROM pending")
        return [Job(p, r, s, m, bool(x), o) for (p, r, s, m, x, o) in cur.fetchall()]

    def close(self) -> None:
        self.db.close()


class WorkQueue:
    def __init__(self, cooldown: float = 5.0, db_path: str | None = None):
        self.cooldown = cooldown
        self._cond = threading.Condition()
        self._ready = deque()        # paths ready to send, in arrival order
        self._pending = {}           # path -> Job   (for every path in _ready)
        self._deferred = {}          # path -> (Job, release_time)  held by cooldown
        self._last_sent = {}         # path -> (sig, sent_time)     for dedup
        self._stopped = False

        # Optional durability. If no db_path, the queue is purely in-memory.
        self._store = _Store(db_path) if db_path else None
        if self._store:
            recovered = self._store.load_all()
            for job in recovered:
                self._pending[job.path] = job
                self._ready.append(job.path)
            if recovered:
                print(f"[detector/queue] recovered {len(recovered)} pending jobs "
                      f"from {db_path}", flush=True)

    # ------------------------------------------------------------------ #
    #  Producer side (called by the watcher)
    # ------------------------------------------------------------------ #
    def offer(self, job: Job) -> None:
        now = time.monotonic()
        with self._cond:
            p = job.path
            if p in self._pending:                  # already queued -> keep newest
                self._pending[p] = job
                if self._store:
                    self._store.put(job)
                return
            if p in self._deferred:                 # in cooldown -> keep newest
                _, release = self._deferred[p]
                self._deferred[p] = (job, release)
                return

            last = self._last_sent.get(p)
            if last is not None:
                last_sig, last_time = last
                if last_sig == job.sig:             # nothing meaningful changed
                    return
                if now - last_time < self.cooldown:  # too soon -> hold for later
                    self._deferred[p] = (job, last_time + self.cooldown)
                    return

            self._pending[p] = job                  # enqueue now
            self._ready.append(p)
            if self._store:
                self._store.put(job)
            self._cond.notify()

    # ------------------------------------------------------------------ #
    #  Consumer side (called by the scanner client)
    # ------------------------------------------------------------------ #
    def pop(self):
        """Block until a job is ready to send; return None once stopped."""
        with self._cond:
            while not self._stopped:
                self._promote_due()
                if self._ready:
                    p = self._ready.popleft()
                    return self._pending.pop(p)
                self._cond.wait(self._wait_time())
            return None

    def requeue(self, job: Job) -> None:
        """Put a job back after a busy ack or a lost connection: retry first."""
        with self._cond:
            self._pending[job.path] = job
            self._ready.appendleft(job.path)
            if self._store:
                self._store.put(job)            # still undelivered -> keep on disk
            self._cond.notify()

    def mark_sent(self, job: Job) -> None:
        """Record a successful delivery: drop it from disk, remember it for dedup."""
        with self._cond:
            if self._store:
                self._store.delete(job)         # delivered -> no longer needs recovery
            self._last_sent[job.path] = (job.sig, time.monotonic())

    def depth(self) -> int:
        """Current backlog: jobs waiting to send plus jobs held by cooldown."""
        with self._cond:
            return len(self._ready) + len(self._deferred)

    def close(self) -> None:
        with self._cond:
            if self._store:
                self._store.close()
                self._store = None

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    # ------------------------------------------------------------------ #
    #  Helpers (always called with the lock held)
    # ------------------------------------------------------------------ #
    def _promote_due(self) -> None:
        """Move any cooled-down files back onto the ready queue."""
        if not self._deferred:
            return
        now = time.monotonic()
        due = [p for p, (_, release) in self._deferred.items() if release <= now]
        for p in due:
            job, _ = self._deferred.pop(p)
            if p not in self._pending:
                self._ready.append(p)
            self._pending[p] = job
            if self._store:                     # now authoritative to-send -> persist
                self._store.put(job)

    def _wait_time(self):
        """How long pop() may sleep: until the next cooldown expires, or forever."""
        if not self._deferred:
            return None
        soonest = min(release for _, release in self._deferred.values())
        return max(0.0, min(1.0, soonest - time.monotonic()))
