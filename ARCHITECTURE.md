# Detector — Architecture & Flows

How changes are detected, filtered, de-duplicated, and delivered — with the exact
flow for every scenario (retries, backpressure, scanner downtime, detector crash,
kernel overflow). Diagrams are Mermaid; they render in the IDE and on GitHub.

- Detection: [detector/fanotify_watcher.py](detector/fanotify_watcher.py) (fallback [detector/poll_watcher.py](detector/poll_watcher.py))
- Filter: `is_relevant()` in [detector/detector.py](detector/detector.py)
- De-dup + queue + durability: [detector/work_queue.py](detector/work_queue.py)
- Delivery + retry + reconnect: [detector/scanner_client.py](detector/scanner_client.py)

---

## 1. The approach in one line

Watch the whole filesystem with **one fanotify mark**, keep the kernel reader
dumb and fast so nothing is ever missed, push *relevant* changes into a
**durable, de-duplicating queue**, and drain that queue to the scanner with
**backpressure-aware retries** that survive the scanner going down and the
detector itself crashing.

---

## 2. System architecture

Three threads share one durable queue. The watcher produces; the client consumes;
the main thread just reports backlog depth.

```mermaid
flowchart LR
  subgraph K["Linux kernel"]
    FS[("customer files<br/>/home/*/public_html")]
    FA["fanotify<br/>FS-wide mark<br/>UNLIMITED_QUEUE"]
    FS -->|"create / write / chmod / move"| FA
  end

  subgraph D["detector process"]
    direction TB
    W["Watcher thread<br/>read events FAST"]
    FILT{"is_relevant?<br/>(watch set)"}
    Q[["WorkQueue<br/>coalesce + signature + cooldown"]]
    DB[("SQLite WAL<br/>undelivered jobs")]
    C["ScannerClient thread<br/>send + read ack"]
    HB["Main thread<br/>depth heartbeat (5s)"]
    W --> FILT
    FILT -->|yes| Q
    FILT -->|no| X["drop (noise)"]
    Q <-->|"persist / recover"| DB
    C -->|"pop()"| Q
    HB -.->|"depth()"| Q
  end

  FA -->|events| W
  C -->|"one JSON job per line"| SOCK[/"scanner socket"/]
  SOCK -->|"ack + verdict"| C
  SOCK --> SC["scanner stub"]
```

**Why this shape:** the queue decouples the fast side (kernel firing 50k events in
seconds) from the slow side (scanner accepting ~200/s). The kernel never waits on
the scanner; bursts, backpressure, and downtime all become "the queue holds it."

---

## 3. Change detection — one fanotify event → one job

The reader does the minimum per event (so the kernel queue can't overflow), then
rebuilds the absolute path from the directory file-handle + name.

```mermaid
flowchart TD
  E["fanotify event batch (one read)"] --> OV{"FAN_Q_OVERFLOW set?"}
  OV -->|yes| LOG["log loud WARNING<br/>(rescan needed — never silent)"] --> NXT["next event"]
  OV -->|no| PARSE["parse info record:<br/>dir file-handle + filename"]
  PARSE --> RES["open_by_handle_at(dir handle)<br/>→ readlink /proc/self/fd/N<br/>→ join filename = absolute path"]
  RES --> STAT{"stat ok AND<br/>regular file?"}
  STAT -->|no| NXT
  STAT -->|yes| MAP["map event mask → reason"]
  MAP --> SIG["read signature:<br/>size, mtime_ns, is_exec"]
  SIG --> CB["callback → watch-set filter"]
```

**Event → reason mapping** (and why these events):

| fanotify event | reason | why it's watched |
| :-- | :-- | :-- |
| `FAN_CREATE` | `created` | a new file appeared (dropped shell) |
| `FAN_MOVED_TO` | `replaced` | atomic save / move into place |
| `FAN_CLOSE_WRITE` | `modified` | fires **once on close**, not per `write()` — kills duplicate noise at the source |
| `FAN_ATTRIB` | `modified` | permission change → re-`stat` and apply the **+x** rule |

---

## 4. Watch set — is this change relevant?

`+x` wins over extension (a newly-executable file is the strongest payload tell);
excluded dirs win over everything.

```mermaid
flowchart TD
  A["path + is_exec"] --> EXD{"under /wp-content/cache/<br/>or /sessions/ ?"}
  EXD -->|yes| IG["IGNORE"]
  EXD -->|no| XB{"is_exec? (gained +x)"}
  XB -->|yes| REL["RELEVANT → enqueue"]
  XB -->|no| TMP{"ends with ~ or<br/>.log / .tmp / .swp ?"}
  TMP -->|yes| IG
  TMP -->|no| HT{"matches *.htaccess ?"}
  HT -->|yes| REL
  HT -->|no| MED{".jpg / .png / .css ?"}
  MED -->|yes| IG
  MED -->|no| CODE{".php .phtml .php5 .phar .inc<br/>.js .html .htm ?"}
  CODE -->|yes| REL
  CODE -->|no| IG
```

---

## 5. Deduplication — what counts as a duplicate

A job's **identity is its signature `(size, mtime, is_exec)`**. "Duplicate" means
*no meaningful change* since the last scan; a new signature is never a duplicate.

```mermaid
flowchart TD
  J["offer(job) — path + signature"] --> PQ{"already in ready queue?"}
  PQ -->|yes| CO["COALESCE: keep newest version<br/>(one scan when its turn comes)"]
  PQ -->|no| DF{"in cooldown / deferred?"}
  DF -->|yes| CO2["update deferred payload<br/>(keep release time)"]
  DF -->|no| LS{"seen before?<br/>(last_sent exists)"}
  LS -->|no| EN["ENQUEUE now"]
  LS -->|yes| SAME{"same signature?"}
  SAME -->|yes| DROP["DROP — metadata-only,<br/>no meaningful change"]
  SAME -->|no| CD{"within 5s cooldown?"}
  CD -->|yes| DEF["DEFER — hold latest,<br/>release after window (never dropped)"]
  CD -->|no| EN
  EN --> PUT[("persist to SQLite")]
```

### Duplicate / not-duplicate scenarios

| Scenario | Signature moves? | Result |
| :-- | :-- | :-- |
| Content edited (write + close) | size/mtime change | **SCAN** |
| Gains execute bit (`chmod +x`) | is_exec flips | **SCAN** |
| Replaced via move/atomic-save | new content at path | **SCAN** (`replaced`) |
| `chown`/`chgrp` only | nothing (exec unchanged) | **DROP** (ATTRIB fires, signature identical) |
| `touch` / atime bump, same content | no write → no `CLOSE_WRITE` event at all | **ignored at source** |
| Read / open without write | no watched event | **ignored at source** |
| Same file rewritten 8× in 1s | each write new signature | **COALESCE + COOLDOWN → 1–2 scans** |
| Rewritten again after a scan, within 5s | new signature | **DEFER → 1 more scan after window (not lost)** |
| `.swp` / `~` / `.log` / `.tmp` | n/a | **excluded** (watch set) |
| `wp-content/cache/`, `sessions/` | n/a | **excluded** (dir) |
| `.jpg` / `.png` / `.css` | n/a | **excluded** (unless it gains +x) |

### Job lifecycle (per path)

```mermaid
stateDiagram-v2
  [*] --> Queued: offer (new or changed signature)
  Queued --> Queued: offer again (coalesce to newest)
  Queued --> InFlight: pop + send
  InFlight --> Delivered: ack = accepted
  InFlight --> Queued: ack = busy (requeue + backoff)
  InFlight --> Queued: connection lost (requeue)
  Delivered --> Deferred: same path changes within 5s
  Delivered --> Queued: same path changes after 5s
  Deferred --> Queued: cooldown expires (release latest)
  Delivered --> [*]: no further change
  note right of Deferred
    held at most 5s,
    latest version then sent —
    never silently dropped
  end note
```

---

## 6. Backpressure — the scanner says `busy`

`busy` means the scanner **dropped** the job. The whole sender pauses (not per
file), because one scanner means backpressure is global. Pause: 0 → 50ms → ×2 →
1s cap; reset to 0 on the next success.

```mermaid
sequenceDiagram
  participant Q as WorkQueue
  participant C as ScannerClient
  participant S as Scanner
  C->>Q: pop()
  Q-->>C: job A
  C->>S: send A
  S-->>C: accepted
  Note over C: pause = 0 (full speed)
  C->>S: send B
  S-->>C: busy (dropped)
  C->>Q: requeue(B)
  Note over C: pause = 50ms
  C-->>C: sleep 50ms
  C->>S: retry B
  S-->>C: busy
  Note over C: pause = 100ms (×2, cap 1s)
  C-->>C: sleep 100ms
  C->>S: retry B
  S-->>C: accepted
  Note over C: pause = 0 (reset) → mark_sent(B)
```

---

## 7. Scanner downtime — reconnect + replay

The scanner closing the socket shows up as an EOF / write error. The in-flight job
was never acked, so it goes **back on the queue**; we reconnect with backoff and
resume. Nothing is lost because everything waits in the (durable) queue.

```mermaid
sequenceDiagram
  participant Q as WorkQueue
  participant C as ScannerClient
  participant S as Scanner
  C->>S: send A
  S-->>C: accepted
  Note over S: scanner goes DOWN<br/>(closes sockets, drops its own queue)
  C->>S: send B
  S--xC: EOF / connection reset
  Note over C: B never acked → requeue(B)
  C->>Q: requeue(B)
  loop reconnect: 0.2s → ×2 → 5s cap
    C->>S: connect
    S--xC: refused
  end
  Note over S: scanner back UP
  C->>S: connect (ok)
  C->>Q: pop()
  Q-->>C: job B (still queued)
  C->>S: send B
  S-->>C: accepted
```

---

## 8. Detector crash — SQLite recovery

The queue persists every *undelivered* job. A hard `SIGKILL` loses in-memory
state, but the backlog is on disk and is reloaded on restart.

```mermaid
sequenceDiagram
  participant FS as files
  participant D1 as detector (run 1)
  participant DB as SQLite (WAL)
  participant S as scanner
  Note over S: scanner DOWN
  FS->>D1: 5 relevant files change
  D1->>DB: INSERT 5 (on enqueue)
  Note over D1: SIGKILL — no graceful stop
  Note over DB: 5 rows remain on disk
  Note over S: scanner UP
  Note over D1: detector restarts (run 2)
  DB-->>D1: load_all() → recover 5 jobs
  D1->>S: deliver 5
  S-->>D1: accepted ×5
  D1->>DB: DELETE 5 → table empty
```

**What is / isn't persisted:** only the undelivered set (enqueue = `INSERT OR
REPLACE` on `path`; delivery = `DELETE` the exact version). `last_sent`/cooldown
state is in-memory only — losing it can cause a harmless re-scan, never a miss.

---

## 9. Burst / active infection — end to end

Tens of thousands of changes in seconds across many accounts:

```mermaid
flowchart LR
  B["50k changes<br/>in seconds"] --> W["watcher drains kernel FAST<br/>(unlimited queue, minimal work)"]
  W --> COAL["coalesce + signature:<br/>repeats on same file collapse<br/>→ O(distinct files), not O(events)"]
  COAL --> QD["queue buffers backlog<br/>(RAM + SQLite)"]
  QD --> DR["sender drains at scanner rate<br/>busy → backoff, never overwhelm"]
  DR --> REC["depth rises then falls<br/>latency ↑ during burst, then recovers"]
  REC --> DONE["nothing lost"]
```

**Central trade-off (see RATIONALE.md):** this path chooses **never-lose** over
strictly-bounded resources — the backlog is bounded by *distinct undelivered
files* (coalescing) and spilled to disk, and overflow is shouted, not swallowed.
