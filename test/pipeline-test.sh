#!/usr/bin/env bash
# Full pipeline test — runs INSIDE the privileged Linux container.
# Uses the PROVIDED fixtures as-is (challenge seed/scanner/load); the only thing
# under test is detector/. Exercises: fanotify detection, the watch-set filter,
# dedup + cooldown, busy/backpressure under a burst, and SQLite crash-recovery.
set -u
CH="python3 /app/challenge"
SOCK=/work/run/scanner/intake.sock
TREE=/work/home
JOBS=/work/jobs.log
SCANLOG=/work/scanner.log
DETLOG=/work/detector.log
export DETECTOR_DB=/work/detector-queue.db     # keep the durable queue on the volume
mkdir -p /work/run/scanner "$TREE"
rm -f "$JOBS" "$DETECTOR_DB"* 2>/dev/null

say() { printf '\n========== %s ==========\n' "$1"; }
start_scanner() { $CH scanner --socket "$SOCK" --slow --jobs-log "$JOBS" >>"$SCANLOG" 2>&1 & SCAN_PID=$!; }
start_detector() { SEED_TREE="$TREE" SCANNER_SOCKET="$SOCK" bash /app/detector/run.sh >>"$DETLOG" 2>&1 & DET_PID=$!; }
wait_socket() { for i in $(seq 1 50); do [ -S "$SOCK" ] && return; sleep 0.1; done; }

say "ENV"; uname -r; echo "uid=$(id -u)"; python3 --version

say "1) SEED a small account tree (provided seed generator)"
$CH seed --root "$TREE" --accounts 2 --files-per-account 30 --seed 0 --manifest /work/seed.json >/dev/null
echo "seeded files: $(find "$TREE" -type f | wc -l)"

say "2) START provided scanner (slow=20/s) + our detector"
start_scanner; wait_socket; start_detector; sleep 2
cat "$DETLOG"
grep -q "watching filesystem" "$DETLOG" && echo "RESULT: fanotify ACTIVE" || echo "RESULT: fanotify NOT active (polling fallback)"

say "3) MANUAL changes covering every watch-set rule"
A="$TREE/u9999/public_html"; mkdir -p "$A/wp-content/cache"
echo "<?php echo 1;" > "$A/shell.php"
echo "<h1>"         > "$A/page.html"
echo "// js"        > "$A/app.js"
echo "deny"         > "$A/inj_000061.htaccess"     # *.htaccess PATTERN
echo "#!/bin/sh"    > "$A/thing.bin"; chmod +x "$A/thing.bin"   # +x rule
echo x > "$A/photo.jpg"; echo x > "$A/debug.log"; echo x > "$A/note.php~"
echo x > "$A/.shell.php.swp"; echo "<?php" > "$A/wp-content/cache/c.php"   # all ignored
for i in $(seq 1 8); do echo "<?php // v$i" > "$A/rapid.php"; done           # dedup
sleep 3

say "4) COOLDOWN test (cooldown=5s) on cool.php"
sleep 3                                  # let earlier deferred work settle first
MARK=$(wc -l < "$JOBS")
echo "<?php //A" > "$A/cool.php"; sleep 1   # scan #1 (immediate)
echo "<?php //B" > "$A/cool.php"; sleep 1   # within cooldown -> deferred
echo "<?php //C" > "$A/cool.php"; sleep 1   # coalesced into the deferred slot
echo "waiting out cooldown..."; sleep 6     # deferred release -> scan #2 (latest=C)
echo "cool.php scans: $(tail -n +"$((MARK+1))" "$JOBS" | grep -c '/cool.php\"')  (3 rapid writes collapse to 1-2 scans via coalescing + cooldown; never 3)"

say "5) REPORT: what the scanner RECEIVED (manual phase)"
python3 - "$JOBS" <<'PY'
import json,os,sys
from collections import Counter
rows=[json.loads(l) for l in open(sys.argv[1])]
c=Counter(os.path.basename(r["path"]) for r in rows if r["accepted"])
for n in sorted(c): print(f"  scanned x{c[n]:<2} {n}")
leak=[n for n in c if n in {"photo.jpg","debug.log","note.php~",".shell.php.swp","c.php"}]
print("  ignored-correctly:", "yes" if not leak else f"LEAK {leak}")
PY

say "6) BURST load (provided load generator) — expect busy + backoff, no crash"
BMARK=$(wc -l < "$JOBS")
$CH load --manifest /work/seed.json --scenario burst --duration 8 >/work/load.log 2>&1
echo "draining 10s..."; sleep 10
echo "busy/s samples: $(grep -o 'busy/s=[0-9.]*' "$SCANLOG" | sort -u | tr '\n' ' ')"
python3 - "$JOBS" "$BMARK" <<'PY'
import json,sys
rows=[json.loads(l) for l in open(sys.argv[1]).read().splitlines()[int(sys.argv[2]):]]
acc=sum(r["accepted"] for r in rows); busy=sum(not r["accepted"] for r in rows)
print(f"  burst: accepted={acc}  busy(rejected,then retried)={busy}")
PY
kill -0 "$DET_PID" 2>/dev/null && echo "  detector: RUNNING" || echo "  detector: DIED"

say "7) DURABILITY / CRASH-RECOVERY (the SQLite net)"
# Isolated, deterministic demo: tear down phase 6, start from a CLEAN db with
# the scanner DOWN so exactly our 5 new files pile up undelivered.
kill -9 "$DET_PID" 2>/dev/null; kill "$SCAN_PID" 2>/dev/null; wait 2>/dev/null
rm -f "$DETECTOR_DB"*
count_db() { python3 -c "import sqlite3;print(sqlite3.connect('$DETECTOR_DB').execute('select count(*) from pending').fetchone()[0])"; }
echo "scanner DOWN; starting a fresh detector..."
start_detector; sleep 2
for i in 1 2 3 4 5; do echo "<?php // recover $i" > "$A/recover_$i.php"; done
sleep 2
echo "  jobs queued to SQLite while scanner is down: $(count_db)  (expect 5)"
echo "hard-killing the detector (SIGKILL — no graceful shutdown)..."
kill -9 "$DET_PID" 2>/dev/null; wait 2>/dev/null
echo "  jobs still in SQLite after the crash: $(count_db)  (expect 5 — survived)"
echo "restarting scanner + detector -> should RECOVER from SQLite and deliver..."
start_scanner; wait_socket; start_detector; sleep 4   # scanner re-truncates jobs.log
grep "recovered" "$DETLOG" | tail -1 | sed 's/^/  /'
echo "  recover_*.php delivered after restart: $(grep -c 'recover_' "$JOBS")  (expect 5)"
echo "  jobs left in SQLite after drain: $(count_db)  (expect 0)"

say "DONE"
kill "$DET_PID" "$SCAN_PID" 2>/dev/null; wait 2>/dev/null; exit 0
