# Detector — Design & Rationale

The component that sits between customer file systems and the scanning service.
It watches for **relevant** file changes and submits them to the scanner:
without overwhelming it, without re-submitting the same file in a tight window,
without ever **silently** losing a change, and without losing work while the
scanner is down (or while the detector itself restarts).

- Code: `detector/` — run via `detector/run.sh` with `SEED_TREE` + `SCANNER_SOCKET`.
- End-to-end test walkthrough: `TESTING.md`.

---

## 1. Architecture at a glance

One process, two worker threads, one durable queue in the middle. The queue
**decouples** the fast side (the kernel firing tens of thousands of events in
seconds) from the slow side (the scanner accepting ~200/s).

```mermaid
flowchart LR
    FS["Customer file systems<br/>/home/&lt;user&gt;/public_html/*"]

    subgraph DET["Detector — one process (run.sh)"]
        direction TB
        W["fanotify watcher<br/><i>producer thread</i><br/>read fast, never block"]
        F{"watch-set<br/>filter"}
        Q[("WorkQueue<br/>coalesce + signature + cooldown<br/><i>SQLite-backed</i>")]
        C["scanner client<br/><i>consumer thread</i><br/>backoff + reconnect"]
        W -->|"change(path,reason,size,mtime,+x)"| F
        F -->|"relevant"| Q
        F -.->|"ignored"| DROP["drop"]
        Q -->|"pop()"| C
        C -->|"busy / lost conn"| Q
    end

    DB[("detector-queue.db<br/>WAL journal")]

    FS -->|"kernel events"| W
    Q <-->|"persist / recover"| DB
    C -->|"one JSON job per line<br/>(UNIX socket)"| S["Scanner service"]
    S -->|"ack: accepted | busy"| C
    S -.->|"verdict (async — ignored in Phase 1)"| C
```

**Why this shape:** the queue is a shock absorber. The kernel never waits for the
scanner. Every failure path (`busy`, disconnect, our own crash) funnels back into
"it's still safely in the queue, try again" — that is how "every relevant change
eventually reaches the scanner" is guaranteed.

---

## 2. How changes are detected (fanotify)

### Why fanotify, not inotify
One `FAN_MARK_FILESYSTEM` mark watches the **whole filesystem** under the tree —
every account, every sub-directory, and directories created *later* — in a single
call. inotify needs one watch **per directory**, doesn't recurse, exhausts
`max_user_watches`, and misses changes during the initial walk. For 2000 accounts
/ millions of files, fanotify is the one that scales.

| | inotify | **fanotify (chosen)** |
| :-- | :-- | :-- |
| Watch whole tree in one call | ❌ per-directory | ✅ one FS-wide mark |
| New sub-dirs auto-covered | ❌ race to add watch | ✅ automatic |
| Scales to millions of files | ❌ watch limit / slow start | ✅ built for it |
| Needs root | no | **yes (CAP_SYS_ADMIN)** |

### Detection flow

```mermaid
flowchart TB
    K["fanotify fd<br/>FAN_MARK_FILESYSTEM · FAN_REPORT_DFID_NAME · FAN_UNLIMITED_QUEUE"]
    K --> R["reader loop — minimal work per event"]
    R --> OV{"FAN_Q_OVERFLOW?"}
    OV -->|"yes"| WARN["log WARNING loudly<br/>→ rescan required (never silent)"]
    OV -->|"no"| PI["parse info record:<br/>parent-dir handle + filename"]
    PI --> OH["open_by_handle_at()<br/>+ readlink /proc/self/fd"]
    OH --> PATH["absolute path"]
    PATH --> STAT["stat(): size, mtime, exec-bit, regular?"]
    STAT -->|"not a regular file"| SKIP["skip (dirs, sockets, …)"]
    STAT -->|"regular file"| MAP["map event → reason"]
    MAP --> OUT["callback(path, reason, size, mtime, is_exec)"]
```

### Which kernel events, and why

```mermaid
flowchart LR
    CR["FAN_CREATE"] --> created["reason = created"]
    MV["FAN_MOVED_TO"] --> replaced["reason = replaced<br/>(atomic save: write tmp + rename)"]
    CW["FAN_CLOSE_WRITE"] --> modified["reason = modified"]
    AT["FAN_ATTRIB"] --> perm["re-stat → apply +x rule"]
```

- **`FAN_CLOSE_WRITE`** (fires once, when the writer *closes* the file) is used
  instead of `FAN_MODIFY` (fires on every `write()`). A file written in 50 chunks
  is **one** event, not 50 — most duplicate noise is killed at the source.
- **`FAN_ATTRIB`** is how we catch a `chmod +x`. The event doesn't say *what*
  changed, so we `stat()` and check the execute bit.
- The reader stays deliberately dumb + `FAN_UNLIMITED_QUEUE`, so a burst can't
  overflow the kernel queue and make us miss changes.

### Monitoring multiple disks / filesystems

A fanotify `FAN_MARK_FILESYSTEM` mark covers **one filesystem (one superblock)**.
Fleet servers often spread document roots across several mounted disks
(`/home`, `/var/www`, `/data/addon`, …), so we add **one mark per filesystem** —
all on the **same** fanotify handle, so every disk's events share one queue, one
dedup, and one backpressure loop. **One service per server, not one per disk.**

```mermaid
flowchart TB
    subgraph One["single detector process"]
        FD["one fanotify handle"]
        FD --> M1["mark: /home (disk A)"]
        FD --> M2["mark: /var/www (disk B)"]
        FD --> M3["mark: /data/addon (disk C)"]
        FD --> QQ[("one WorkQueue → one scanner client")]
    end
    A["disk A events"] --> FD
    B["disk B events"] --> FD
    C["disk C events"] --> FD
```

Configure it with `WATCH_ROOTS` (comma-separated); it defaults to `SEED_TREE`:

```bash
WATCH_ROOTS=/home,/var/www,/data/addon  SCANNER_SOCKET=... ./detector/run.sh
```

Path resolution works across disks because a file handle only resolves against a
mount fd **on its own filesystem**: we keep one mount fd per watched root and try
each until the handle resolves. A mark that fails (e.g. a path that isn't a mount
point) is logged and skipped; the detector keeps watching the rest. *(Verified on
two separate filesystems — ext4 + tmpfs — in the container test.)*

---

## 3. What counts as a *relevant* change (the watch set)

```mermaid
flowchart TB
    A["change(path, is_exec)"] --> E{"in excluded dir?<br/>wp-content/cache/ · sessions/"}
    E -->|"yes"| IG["IGNORE"]
    E -->|"no"| X{"executable? (gained +x)"}
    X -->|"yes"| REL["RELEVANT → submit"]
    X -->|"no"| N{"editor/temp noise?<br/>*.log *.tmp *.swp *~"}
    N -->|"yes"| IG
    N -->|"no"| HT{"*.htaccess pattern?"}
    HT -->|"yes"| REL
    HT -->|"no"| M{"media? *.jpg *.png *.css"}
    M -->|"yes"| IG
    M -->|"no"| C{"code extension?<br/>php phtml php5 phar inc · js html htm"}
    C -->|"yes"| REL
    C -->|"no"| IG
```

**Order matters:** `+x` is checked **before** the noise/media filters because a
newly-executable file is the strongest payload signal — it wins over extension.
`*.htaccess` is a **pattern**, so `inj_000061.htaccess` counts, not just the
literal `.htaccess`.

---

## 4. Deduplication — when is a file a "duplicate"?

The PO's definition: a scan should correspond to a **meaningful change** — new
content, replaced content, or a new execute permission. Anything else (a read, an
`atime`/`mtime` touch with no new bytes, a `chown`, an editor temp file) is a
duplicate/unnecessary scan.

We enforce this with **two cooperating layers**:

1. **Event layer (fanotify):** we only subscribe to `CREATE / MOVED_TO /
   CLOSE_WRITE / ATTRIB`. A pure `touch` (atime/mtime only, no write) produces
   **no** `CLOSE_WRITE`, so it never even reaches us.
2. **Signature layer (queue):** a job's identity is
   **`signature = (size, mtime_ns, exec-bit)`**. If the signature equals the last
   one we delivered, nothing meaningful changed → drop. New content moves
   `size`/`mtime`; a `chmod +x` flips `exec`; a `chown` moves none → dropped.

Plus **coalescing** (same path queued twice collapses) and a **cooldown** (rapid
re-writes of a just-scanned file are held, newest released after the window).

### Duplicate / not-duplicate scenarios

| Scenario | Event we get | Signature changes? | Result |
| :-- | :-- | :-- | :-- |
| New `.php` created & written | CREATE + CLOSE_WRITE | n/a (new) | **scan** (created) |
| Content edited | CLOSE_WRITE | yes (size/mtime) | **scan** (modified) |
| Atomic replace (tmp + rename) | MOVED_TO | yes | **scan** (replaced) |
| File gains `+x` | ATTRIB | yes (exec bit) | **scan** |
| Already-scanned file edited **again** (new bytes) | CLOSE_WRITE | yes | **scan** (after cooldown) |
| Same file rewritten 50×/sec | 50× CLOSE_WRITE | yes each | **1–2 scans** (coalesce + cooldown) |
| Read / open-for-read | (none subscribed) | — | **ignore** |
| `atime`/`mtime` touch, no new bytes | (no CLOSE_WRITE) | — | **ignore** |
| `chown` / group change only | ATTRIB | no | **ignore** (signature identical) |
| Rename with no content change | MOVED_TO | same content | effectively ignored (same signature) |
| Editor temp `.swp` / `file.php~` | (any) | — | **ignore** (watch-set) |
| File in `wp-content/cache/` | (any) | — | **ignore** (excluded dir) |

### The `offer()` decision (exact flow)

```mermaid
flowchart TB
    A["offer(job)  ·  sig = (size, mtime, +x)"] --> P{"path already<br/>in ready queue?"}
    P -->|"yes"| CO["coalesce → keep newest · done<br/>(bounds a burst to 1 entry/file)"]
    P -->|"no"| D{"path in cooldown<br/>(deferred)?"}
    D -->|"yes"| CU["update deferred slot<br/>to newest · done"]
    D -->|"no"| L{"sig == last delivered sig?"}
    L -->|"yes"| NM["no meaningful change → drop"]
    L -->|"no"| T{"last send within 5s?"}
    T -->|"yes"| DEF["DEFER: hold newest,<br/>release at last_sent + 5s<br/>(never dropped)"]
    T -->|"no"| EN["ENQUEUE + persist to SQLite"]
```

> **Key property:** the cooldown **defers**, it does not **drop**. A genuinely new
> change that lands inside the window is delayed at most 5s, never lost — this is
> the never-lose vs. throughput tradeoff made explicit (see §10).

---

## 5. Job lifecycle (state machine)

```mermaid
stateDiagram-v2
    [*] --> Detected: fanotify event
    Detected --> Dropped: not in watch set
    Detected --> Offered: relevant

    Offered --> Dropped: same signature (no meaningful change)
    Offered --> Deferred: within 5s cooldown
    Offered --> Ready: enqueue (write to SQLite)

    Deferred --> Ready: cooldown expired (write to SQLite)

    Ready --> InFlight: pop() + send over socket
    InFlight --> Delivered: accepted ack (DELETE from SQLite)
    InFlight --> Ready: busy ack → requeue + backoff
    InFlight --> Ready: connection lost → requeue

    Delivered --> [*]
    Dropped --> [*]
```

A job is on disk from **Ready** through **InFlight**; it is only `DELETE`d when the
scanner **accepts** it. So any crash in between is recoverable.

---

## 6. Backpressure — the scanner says `busy`

`busy` means the scanner's intake queue is full and it **dropped** the job. The
rule: put it back and pause the **whole** sender (the scanner is busy for
everything, not one file). Pause: `0 → 50ms → double → 1s cap`, reset to `0` on
the next success.

```mermaid
sequenceDiagram
    autonumber
    participant Q as WorkQueue
    participant C as ScannerClient
    participant S as Scanner
    loop drain backlog
        C->>Q: pop()
        Q-->>C: job
        opt backoff active
            C->>C: sleep(pause)
        end
        C->>S: {"path","reason",...}\n
        alt accepted
            S-->>C: {accepted:true}
            C->>Q: mark_sent() → DELETE from SQLite
            C->>C: pause = 0  (full speed)
        else busy (intake full)
            S-->>C: {accepted:false, reason:"busy"}
            C->>Q: requeue(job)  (front of queue)
            C->>C: pause = 50ms → ×2 → cap 1s
        end
    end
```

During a 50k burst the sender simply drains at the scanner's pace while the queue
holds the backlog. Latency rises during the burst (expected) and recovers after.

---

## 7. Scanner downtime — disconnect, reconnect, replay

When the scanner goes down it closes the socket (and drops its own queue). We:
rescue the in-flight job, reconnect with backoff, and resume draining — the
backlog was safe in the (persistent) queue the whole time.

```mermaid
sequenceDiagram
    autonumber
    participant W as Watcher
    participant Q as WorkQueue (+SQLite)
    participant C as ScannerClient
    participant S as Scanner

    W->>Q: offer(changes)  (persisted on enqueue)
    C->>S: send job
    S--xC: socket closed (scanner down)
    C->>Q: requeue(in-flight job)  — never acked, so not lost
    loop reconnect: 0.2s → ×2 → cap 5s
        C->>S: connect()
        S--xC: refused
    end
    Note over W,Q: changes keep arriving & persisting — nothing lost
    C->>S: connect()
    S-->>C: connected
    C->>Q: pop() backlog
    C->>S: replay all pending jobs
```

---

## 8. Detector crash — SQLite recovery (the durability net)

If the **detector process itself** dies (OOM-kill, deploy, panic) while a backlog
is waiting, the backlog is on disk and is reloaded on restart.

```mermaid
sequenceDiagram
    autonumber
    participant W as Watcher
    participant Q as WorkQueue
    participant DB as SQLite (WAL)
    participant C as Client

    W->>Q: offer(5 changes)
    Q->>DB: INSERT OR REPLACE × 5  (path = primary key)
    Note over C: scanner down → nothing delivered yet
    Note over Q,DB: ⚡ SIGKILL (hard crash)

    rect rgb(238,238,238)
        Note over Q,DB: ——— restart ———
        Q->>DB: SELECT * FROM pending
        DB-->>Q: 5 rows
        Note over Q: "recovered 5 pending jobs"
    end
    C->>Q: pop() → deliver all 5
    C->>DB: DELETE each on accept → table empty
```

**What is / isn't persisted:**

```mermaid
flowchart LR
    subgraph OnDisk["SQLite — survives a crash"]
        R["ready jobs"]
        IF["in-flight job (until accepted)"]
    end
    subgraph InMem["In-memory only — rebuilt after a crash"]
        LS["last_sent signatures (dedup)"]
        DEF["cooldown timers (deferred)"]
    end
```

Cooldown/last-sent are **not** persisted on purpose: losing them can only cause a
harmless re-scan, never a miss. The authoritative "still must send" set is on disk.

---

## 9. Burst handling & kernel-overflow protection

```mermaid
flowchart LR
    B["50k changes<br/>in seconds"] --> K["kernel fanotify queue<br/><b>UNLIMITED</b>"]
    K --> RD["reader thread<br/>minimal work / event"]
    RD --> Q[("WorkQueue<br/>buffers backlog<br/>size = O(distinct files)")]
    Q --> DR["drain at scanner rate<br/>~200/s · busy-aware"]
    DR --> S["scanner"]
    K -.->|"if ever overflows"| OVR["FAN_Q_OVERFLOW<br/>logged loudly → rescan"]
```

Coalescing means the backlog is bounded by the number of **distinct** changed
files, not by the number of events — a rewrite-storm on the same files stays tiny.

---

## 10. Failure matrix

| Failure | Detection | Handling | Guarantee |
| :-- | :-- | :-- | :-- |
| Scanner `busy` | ack `accepted:false` | requeue + exponential backoff | not lost; scanner not overwhelmed |
| Scanner down | socket EOF / write error | requeue in-flight + reconnect loop + replay | not lost during downtime |
| Detector crash | — | SQLite reload on restart | backlog survives process death |
| Kernel queue overflow | `FAN_Q_OVERFLOW` | log loudly → rescan | never a **silent** loss |
| Malformed/over-long job | ack `bad_request` | log + drop (shouldn't happen; we emit valid JSON) | no infinite loop |
| File deleted before stat | `stat()` raises | skip | no crash |
| Old kernel (<5.9) / not root | `fanotify_init`/`mark` fails | fall back to polling watcher | still works (slower) |

---

## 11. The central trade-off

You cannot simultaneously guarantee **never-lose** and **strictly-bounded
resources**. **I chose never-lose**, because the stated worst outcome is burying
the one malicious change; silently dropping under pressure defeats the product.

What keeps it sane: the backlog is **O(distinct undelivered files)** (coalescing),
and it lives on disk, so RAM isn't the hard ceiling. Accepted cost: a *prolonged*
outage touching a *huge number of distinct files* grows disk usage; if disk were
truly exhausted we fail **loudly**, never drop. Operationally we alert on queue
depth (logged every 5s) and scale/restore the scanner.

### (a) Strongest objection to my own approach
Never-lose + disk-backed queue is **weaponizable**: an attacker who triggers a
massive distinct-file change storm *while the scanner is down* (or who DoSes the
scanner) can balloon the backlog to exhaust disk/inodes or slow recovery — turning
a scanner outage into a detector/host outage. A strictly-bounded design degrades
more predictably under that attack. (Secondary: SQLite commit-per-op is on the hot
path; WAL keeps it cheap, but extreme sustained load would need batched commits.)
**Rebuttal:** in this domain a noisy resource alert beats a silent miss, and the
coalesced O(distinct-files) bound + depth alerting keeps it manageable. For a
hostile edge, I'd ship a bounded variant (hard cap + shed-oldest, each shed paired
with a scheduled rescan so "shed" still isn't "silently lost").

### (b) What I took from, and rejected in, my AI assistant's output
**Took:** fanotify-over-inotify; `FAN_CLOSE_WRITE` over `FAN_MODIFY`;
signature-based dedup; coalesce + cooldown; the exponential-backoff shape;
reconnect-and-replay; `FAN_UNLIMITED_QUEUE` / overflow awareness; the
producer/consumer structure.

**Rejected / changed — design tradeoffs where its first answer was weaker:**
- **Durability: in-memory → disk-backed.** It modelled the queue as in-memory only
  and filed persistence as "future work" — which quietly trades away never-lose
  the moment the detector itself crashes. I made it **SQLite (WAL) now**. Tradeoff:
  a small hot-path write for a backlog that survives a restart.
- **Dedup identity: time-window → meaningful-change signature.** Its first dedup was
  a pure time debounce, which both rescans metadata-only noise *and* can skip a real
  edit inside the window. I changed the key to **`(size, mtime, +x)`**. Tradeoff: a
  little `stat` work for correct "did it *meaningfully* change" semantics.
- **Cooldown must defer, not drop.** Its cooldown would have *suppressed* in-window
  changes, which can bury a malicious rewrite right after a scan. I made it **hold
  the latest and release it**. Tradeoff: ≤5s added re-scan latency over any miss
  risk (we also tuned the window 2s → 5s).
- **Never-lose at the kernel boundary.** It relied on the default bounded kernel
  queue, where a burst can drop events **invisibly**. I required
  `FAN_UNLIMITED_QUEUE` + explicit `FAN_Q_OVERFLOW` detection. Tradeoff: unbounded
  kernel memory vs. guaranteed completeness.

---

## 12. Requirements mapping

| Requirement | How it's met |
| :-- | :-- |
| Scanner never overwhelmed | send only while connected; `busy` → requeue + exponential backoff |
| No repeat submit in a short window | coalesce + signature + 5s deferred cooldown |
| New/changed file never **silently** lost | never-lose queue + retry; new signature always enqueued (even mid-cooldown); overflow logged, not swallowed |
| No loss during scanner downtime | persistent SQLite queue + reconnect/replay; survives detector SIGKILL |

---

## 13. Known limits / future work
- In-memory index mirrors the DB (RAM ≈ O(distinct undelivered files)); for
  millions of simultaneously-pending files, page the index from disk.
- Cooldown can delay a legitimate re-scan by up to 5s (tunable).
- Overflow handler logs but doesn't yet auto-rescan; production would trigger a
  targeted rescan of the affected subtree.
- FID path reporting needs Linux 5.9+; older kernels fall back to polling.
- Multiple disks are supported via `WATCH_ROOTS` (one mark + mount-fd per
  filesystem; see §2). For *very* many filesystems, resolving a handle by trying
  each mount fd is O(#fs) per event — fine for a handful; map by `fsid` if a host
  mounts dozens of disks.
