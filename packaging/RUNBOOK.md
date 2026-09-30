# Challenge fixtures — runbook

Everything you need to develop and demonstrate your component locally, on your own machine, with
your own tools. The package gives you three things: a realistic account **tree**, the **scanner**
you submit jobs to, and a **change driver** that mutates the tree so there's something to detect.

You build the component in between. **How you detect changes is your decision.**

## Option A — Docker (recommended, works on macOS/Windows/Linux)

```bash
# build once (from the repo root)
docker build -f packaging/Dockerfile -t challenge-fixtures .

# 1) lay down the account tree + a manifest into ./work
docker run --rm -v "$PWD/work:/work" challenge-fixtures \
    seed --root /work/home --accounts 10 --files-per-account 800 --seed 0 --manifest /work/seed.json

# 2) run the scanner (listens on a UNIX socket in the shared volume)
docker run --rm -d --name scanner -v "$PWD/work:/work" challenge-fixtures \
    scanner --socket /work/run/scanner/intake.sock --rate 200

# 3) drive some changes (steady drip; try --scenario burst too)
docker run --rm -v "$PWD/work:/work" challenge-fixtures \
    load --manifest /work/seed.json --scenario steady --duration 60
```

> **Disk & scale.** These numbers (10 accounts × 800 files) lay down a laptop-friendly tree —
> **~0.5 GB** here. Always **estimate before a bigger run**: append `--dry-run` to the `seed`
> command to print the planned file count and total bytes *without writing anything*. Large
> `--accounts` / `--files-per-account` combos run to **many GB** (the old `--accounts 100` default
> filled the disk in testing), so check the estimate first.

Now point **your detector** at:
- the account tree: `./work/home`
- the scanner socket: `./work/run/scanner/intake.sock`

If your approach uses kernel features (`auditd`, `fanotify`, eBPF) and you run your detector in a
container, add `--privileged` (Docker Desktop runs a real Linux kernel inside its VM). Or run your
detector directly on the host against the same paths.

## Option B — pip (native Linux)

```bash
pip install .
challenge-seed --root ./work/home --accounts 10 --files-per-account 800 --manifest ./work/seed.json
challenge-scanner --socket /run/scanner/intake.sock --rate 200 &
challenge-load --manifest ./work/seed.json --scenario steady --duration 60
```

## The scanner contract (the one fixed interface)

Submit one JSON job per line over the socket:

```json
{"path":"/work/home/u1001/public_html/wp-content/x.php","reason":"modified","size":2048,"mtime":1718100000.1,"observed_at":1718100000.4}
```

You get one ack per job (`{"accepted":true}` or `{"accepted":false,"reason":"busy"}` when it's
overloaded — that backpressure is real), and verdicts arrive asynchronously later. The scanner
processes at a bounded rate, so bursts back up.

## Change scenarios

`challenge load --scenario <name>`: `steady` (normal drip), `burst` (incident spike),
`incident` (steady → burst → steady), `dedup-stress` (same files rewritten), `idle` (noise only).
`--needle-token` marks a planted change. See `challenge load --help`.

## What's the watch set?

Relevant: `*.php *.phtml *.php5 *.phar *.inc`, `*.htaccess`, `*.js *.html *.htm`, and any file that
gains execute permission. Note `*.htaccess` is a **pattern**, not just the literal `.htaccess` file:
the workload also creates files like `inj_000061.htaccess`, and every `*.htaccess` counts as relevant. Noise to ignore: metadata-only changes, `*.log *.tmp *.swp *~` / editor
locks, cache/session trees, and media (`*.jpg *.png *.css`) unless you can defend including it.

## Submitting your work

Build your component under `detector/` with an executable `detector/run.sh` entry point (see
`detector/README.md`), then run the submission helper:

```bash
./packaging/make-submission.sh              # writes submission.patch
./packaging/make-submission.sh --archive    # also bundles submission.tar.gz (patch + RATIONALE.md)
```

It stages your work and writes **`submission.patch`** — the diff of your `detector/` changes
against this package. Hand that in where the Product Owner chat tells you, with a short written
rationale (drop a `RATIONALE.md` in the package and `--archive` bundles it).

We apply it on our side (`git apply`) onto a fresh `detector/` and run your `detector/run.sh`, so
only changes under `detector/` are graded and the result is reproducible. Prefer to do it by hand?
`git add -A && git diff --cached -- detector > submission.patch` produces the same file. A
repo/demo link is also accepted.
