# Detector — Rationale (Phase 2)

A component that sits between customer file systems and the scanning service: it
watches for *relevant* file changes and submits them to the scanner, without
overwhelming it, without re-submitting the same file in a tight window, without
ever silently losing a change, and without losing work while the scanner is down.

## Approach in one picture

```
fanotify watcher ──(relevant change)──▶ WorkQueue (durable) ──▶ ScannerClient ──▶ scanner
  one FS-wide mark     filter+dedup       coalesce/cooldown       busy-backoff
  read fast, no loss                      SQLite-backed           reconnect+replay
```

### Detection — fanotify, not inotify
One `FAN_MARK_FILESYSTEM` mark watches the whole filesystem under the tree —
every account, every sub-directory, and directories created later — in a single
call. inotify needs one watch per directory, doesn't recurse, exhausts
`max_user_watches`, and misses changes during the initial walk. For 2000
accounts / millions of files, fanotify is the one that scales.

- Mode `FAN_REPORT_DFID_NAME` (Linux 5.9+) gives the parent-dir handle + filename
  so we rebuild the absolute path via `open_by_handle_at` + `/proc/self/fd`.
- Events: `FAN_CREATE` → *created*, `FAN_MOVED_TO` → *replaced*,
  `FAN_CLOSE_WRITE` → *modified*, `FAN_ATTRIB` → re-`stat()` and apply the +x rule.
- `FAN_CLOSE_WRITE` (fires once, on close) is used instead of `FAN_MODIFY`
  (fires per `write()`), which removes most duplicate noise at the source.
- The reader does the bare minimum per event and uses `FAN_UNLIMITED_QUEUE`, so
  a 50k-file burst can't overflow the kernel queue. If it ever did,
  `FAN_Q_OVERFLOW` is detected and logged loudly (never a silent loss).

### Watch set (precision)
Relevant: `*.php *.phtml *.php5 *.phar *.inc`, `*.htaccess` (a pattern, so
`inj_00.htaccess` counts), `*.js *.html *.htm`, and **any file that gains +x**.
Ignored: metadata-only changes, `*.log *.tmp *.swp` / `*~` / editor locks,
`wp-content/cache/` and `sessions/`, and media `*.jpg *.png *.css`. The +x signal
overrides extension, because a newly-executable file is the strongest payload tell.

### Dedup (don't waste the scanner)
Three layers, each simple:
1. **Coalesce** — the same path queued twice collapses into one job.
2. **Signature** — a job's identity is `(size, mtime, exec-bit)`. A change that
   doesn't move any of these (e.g. a chown, an atime bump) is *not* a meaningful
   change and is dropped. New content or a new +x always differs → always sent.
3. **Cooldown (5s)** — after a file is scanned, further changes are held briefly
   so one busy file can't flood the scanner. Crucially the *latest* version is
   still delivered when the window ends (deferred, never dropped), so a genuinely
   new change within the window is delayed at most 5s, never lost.

### Backpressure (never overwhelm)
`busy` means the scanner dropped the job, so we put it back and pause the whole
sender: 0 → 50ms → double → 1s cap, reset to 0 on the next success. We only ever
send while connected, so we never exceed the scanner's accept rate.

### Durability (never lose, incl. downtime and our own crash)
The queue's not-yet-delivered set is persisted to **SQLite (WAL)**: enqueue =
`INSERT OR REPLACE` (path is the key, so coalescing is free), delivery = `DELETE`
the exact version sent, startup = reload everything (recovery). If the scanner
goes down, jobs wait safely and in-flight work is re-queued and replayed on
reconnect; if the *detector* is killed, the backlog is reloaded from SQLite on
restart. Cooldown/last-sent state is deliberately **not** persisted — losing it
can only cause a harmless re-scan, never a miss.

## The central trade-off

You cannot simultaneously guarantee **never-lose** and **strictly-bounded
resources**. **I chose never-lose**, because the stated worst outcome is burying
the one malicious change; silently dropping under pressure defeats the product's
entire purpose.

What keeps this from being reckless: the backlog is bounded by **O(distinct
undelivered files)**, not by event volume — coalescing collapses a rewrite-storm
on the same files to one entry each — and it lives on disk, so RAM is not the hard
ceiling. The accepted cost: a *prolonged* scanner outage that touches a *huge
number of distinct files* grows disk usage; if disk were truly exhausted we fail
loudly rather than drop. The operational answer is alerting on queue depth (it is
logged every 5s) and restoring/scaling the scanner — not discarding
security-relevant changes.

### (a) The strongest objection to my own approach
Never-lose with a disk-backed queue is **weaponizable**: an attacker who triggers
a massive distinct-file change storm *while the scanner is down* (or who can DoS
the scanner) can balloon the backlog to exhaust disk/inodes or make recovery slow
— turning a scanner outage into a detector/host outage. A strictly-bounded design
degrades more predictably under that exact attack. (Secondary concern: SQLite
commit-per-op sits on the hot path; WAL keeps it cheap, but under extreme
sustained load batched commits would be needed.)
My rebuttal: in this domain a noisy resource alert beats a silent miss, and the
coalesced O(distinct-files) bound plus depth alerting keeps it manageable. For a
hostile edge where resource-DoS is the bigger threat, I'd ship a bounded variant
(hard cap + shed-oldest, each shed paired with a scheduled rescan so "shed" still
isn't "silently lost").

### (b) What I took from, and rejected in, my AI assistant's output
**Took:** the fanotify-over-inotify argument; `FAN_CLOSE_WRITE` over `FAN_MODIFY`;
signature-based "meaningful change" dedup; coalesce + cooldown; the exponential
backoff shape; reconnect-and-replay; `FAN_UNLIMITED_QUEUE` / overflow awareness;
the producer/consumer structure.
**Rejected / changed — design tradeoffs where its first answer was weaker:**
- **Durability: in-memory → disk-backed.** It modelled the queue as **in-memory
  only** and filed persistence as "future work," which quietly trades away
  never-lose the moment the *detector itself* crashes (OOM-kill, deploy, panic). I
  made it **SQLite (WAL) now**. The tradeoff I accepted: a small write on the hot
  path in exchange for a backlog that survives a process restart — the right side
  for a never-lose security tool.
- **Dedup identity: time-window → meaningful-change signature.** Its first dedup
  was a **pure time debounce** — "seen this path recently, skip it" — which both
  rescans on metadata-only noise (chown/atime) *and* can skip a real edit that
  happens to fall in the window. I changed the dedup key to a **content signature
  `(size, mtime, +x)`**: a chown/atime change collapses (no wasted scan), but any
  new content or new execute bit is a different signature and always goes through.
  Tradeoff: a tiny bit of per-event `stat` work for correct "did it *meaningfully*
  change" semantics.
- **Cooldown must defer, not drop.** Its cooldown would have **suppressed** repeat
  changes inside the window (simplest), which can silently bury the one change that
  matters if a file is rewritten maliciously right after a scan and then goes
  quiet. I made the cooldown **hold the latest version and release it** when the
  window ends — so a same-file change is delayed at most 5s, **never lost**.
  Tradeoff: up to 5s added detection latency on a re-scan, chosen over any risk of
  a miss. (We also tuned the window itself: its **2s** → **5s**, balancing
  duplicate-scan pressure against that latency.)
- **Never-lose at the kernel boundary.** It relied on the default (bounded) kernel
  event queue, where a 50k-file burst can overflow and drop events **invisibly** —
  a silent-loss hole. I required `FAN_UNLIMITED_QUEUE` plus explicit
  `FAN_Q_OVERFLOW` detection, so the boundary either keeps every event or says so
  loudly (triggering a rescan), never fails quietly. Tradeoff: unbounded kernel
  memory for the queue vs. guaranteed completeness — again the never-lose side.

## Requirements mapping
| Requirement | How it's met |
| :-- | :-- |
| Scanner never overwhelmed | send only while connected; `busy` → requeue + exponential backoff |
| No repeat submits in a short window | coalesce + signature + 5s cooldown |
| New/changed file never silently lost | never-lose queue + retry; new signature always enqueued (even mid-cooldown); overflow is logged, not swallowed |
| No loss during scanner downtime | persistent SQLite queue + reconnect/replay; survives detector SIGKILL |

## Known limits / future work
- In-memory index mirrors the DB (RAM ≈ O(distinct undelivered files)); for
  millions of simultaneously-pending files, page the index from disk.
- Cooldown can delay a legitimate re-scan by up to 5s (tunable).
- The overflow handler logs but doesn't yet auto-rescan; production would trigger
  a targeted rescan of the affected subtree.
- FID path reporting needs Linux 5.9+; older kernels fall back to polling.
- Single-filesystem assumption for the `open_by_handle_at` mount fd; multiple
  mounts would need one mark + mount-fd per filesystem.

## Run it
See `TESTING.md` for the end-to-end container walkthrough. The graded entry point
is `detector/run.sh`, driven by `SEED_TREE` and `SCANNER_SOCKET`.
