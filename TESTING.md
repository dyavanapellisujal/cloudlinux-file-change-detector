# Testing the detector end-to-end (Linux container)

The detector's real detection path uses **fanotify**, a Linux-only kernel
feature, so it must run on a Linux kernel. Docker Desktop provides one. These
steps build a small image, let you `exec` into it, run the **provided** fixtures
(seed / scanner / load) yourself, and **watch the detector submit scans live**.

> Everything here uses the provided fixtures as-is via the `challenge`
> dispatcher. The only thing under test is `detector/`.

## 0. Prerequisites
- Docker Desktop running (it runs a real Linux kernel inside its VM).
- Run all `docker build` / `docker run` commands from the **repo root**.

## 1. Build the image

```bash
docker build -f test/Dockerfile -t detector-test .
docker volume create cl_work          # ext4 volume for the tree + socket (fanotify-friendly)
```

## 2. Start a long-lived container and exec into it
Start it idle so you can open several shells into the same container:
```bash
docker run -d --privileged --name det -v cl_work:/work --entrypoint sleep detector-test infinity
```
`--privileged` is what lets fanotify place a filesystem-wide mark.

Open **three terminals**, each a shell inside the container:
```bash
# run this in each of the three terminals
docker exec -it det bash
```
Call them **A (scanner)**, **B (detector)**, **C (driver)**.

## 3. Terminal A — seed the tree, then run the provided scanner
```bash
# lay down a small account tree with the PROVIDED seed generator
python3 /app/challenge seed --root /work/home --accounts 2 --files-per-account 30 \
    --seed 0 --manifest /work/seed.json

# run the PROVIDED scanner. --slow (20/s) makes backpressure easy to see.
# it prints live metrics: recv/s, accepted/s, busy/s, queue depth.
mkdir -p /work/run/scanner
python3 /app/challenge scanner --socket /work/run/scanner/intake.sock --slow \
    --jobs-log /work/jobs.log
```
Leave this running — you'll see the scanner's per-second metrics here.

## 4. Terminal B — run YOUR detector (verbose, so scans stream by)
```bash
export SEED_TREE=/work/home
export SCANNER_SOCKET=/work/run/scanner/intake.sock
export DETECTOR_DB=/work/detector-queue.db   # durable queue on the volume
export DETECTOR_VERBOSE=1                     # log every scan it submits
bash /app/detector/run.sh
```
Expected startup lines (fanotify, not polling):
```
[detector/fanotify] watching filesystem at /work/home (one mark, whole tree)
[detector] watching /work/home -> /work/run/scanner/intake.sock
[detector/client] connected to scanner
```
Keep this terminal visible — each submitted scan prints as:
```
[detector/client] SCAN  created   /work/home/u9999/public_html/shell.php
```

## 5. Terminal C — make changes and WATCH terminal B react
Try these one at a time and watch B print (or stay silent for ignored files):
```bash
A=/work/home/u9999/public_html; mkdir -p "$A/wp-content/cache"

# --- these SHOULD be scanned (watch B) ---
echo "<?php echo 1;" > "$A/shell.php"          # created  .php
echo "// x"          > "$A/app.js"             # created  .js
echo "deny"          > "$A/inj_000061.htaccess" # *.htaccess PATTERN
echo "#!/bin/sh"     > "$A/thing.bin"; chmod +x "$A/thing.bin"   # +x rule (non-code ext!)

# --- these should be IGNORED (B stays silent) ---
echo x > "$A/photo.jpg"                          # media
echo x > "$A/debug.log"                          # noise
echo x > "$A/.shell.php.swp"                      # editor temp
echo "<?php" > "$A/wp-content/cache/c.php"        # excluded dir

# --- dedup: rewrite the same file fast -> far fewer than 8 scans ---
for i in $(seq 1 8); do echo "<?php // v$i" > "$A/rapid.php"; done
```

## 6. Terminal C — drive a real burst with the PROVIDED load generator
```bash
# incident spike: thousands of changes in seconds
python3 /app/challenge load --manifest /work/seed.json --scenario burst --duration 8
```
Now watch:
- **Terminal A** (scanner): `busy/s` climbs above 0 — real backpressure.
- **Terminal B** (detector): scans keep flowing; `queue depth=…` heartbeat shows
  the backlog rising then draining. It never crashes and loses nothing.

Other scenarios: `steady`, `incident`, `dedup-stress`, `idle`
(`python3 /app/challenge load --help`).

## 7. Prove durability (survives scanner outage AND a detector crash)
```bash
# Terminal A: stop the scanner (Ctrl-C).  New changes now have nowhere to go.
# Terminal C:
for i in 1 2 3 4 5; do echo "<?php // r$i" > "$A/recover_$i.php"; done

# confirm they're safely on disk even though nothing was delivered:
python3 -c "import sqlite3;print('pending in SQLite:',sqlite3.connect('/work/detector-queue.db').execute('select count(*) from pending').fetchone()[0])"

# Terminal B: hard-kill the detector (Ctrl-C, or from C: pkill -9 -f detector.py)
# Terminal A: start the scanner again (same command as step 3).
# Terminal B: start the detector again (same command as step 4).
#   -> it logs:  [detector/queue] recovered 5 pending jobs from /work/detector-queue.db
#   -> and the 5 recover_*.php are delivered. Nothing was lost.
```

## 8. One-shot automated version
The same flow, scripted with pass/fail checks, runs in a single command:
```bash
docker run --rm --privileged -v cl_work:/work detector-test
```
(That's `test/pipeline-test.sh` baked into the image.)

## 9. Cleanup
```bash
docker rm -f det
docker volume rm cl_work
```

---
### Notes
- On an old kernel (<5.9) or without privileges, the detector logs
  `fanotify unavailable … using polling fallback` and still works (slower) — a
  deliberate graceful-degradation path, not the production mode.
- The graded entry point is `detector/run.sh` with `SEED_TREE` and
  `SCANNER_SOCKET` in the environment; everything else above is just scaffolding
  to exercise it.
