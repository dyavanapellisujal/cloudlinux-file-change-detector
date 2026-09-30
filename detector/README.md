# detector/ — your component goes here

This is where you build the component that detects relevant file changes and submits them to the
scanner. **How you detect changes is your decision** — write code here in any language, drop in
config + glue, or wire up an existing tool. Use this directory (or wherever makes sense), then
submit a **patch** of your changes.

## Entrypoint: `detector/run.sh`

So we can run your detector automatically, provide an executable **`detector/run.sh`**. We run it
with two environment variables and it should keep running until it's signalled to stop:

| env var | meaning |
| :-- | :-- |
| `SEED_TREE` | root of the account tree to watch for changes |
| `SCANNER_SOCKET` | UNIX socket to submit detected changes to (one JSON job per line) |

Example `run.sh`:

```sh
#!/bin/sh
exec python3 "$(dirname "$0")/my_detector.py"   # reads SEED_TREE + SCANNER_SOCKET from env
```

Everything your detector needs must live under `detector/` — your patch is **confined to
`detector/`** (anything touching the harness, seed, oracle, or scorecard is rejected). The
sandbox provides `python3` and POSIX `sh`; tell us if you need another runtime.

## Produce your submission

Run the helper from the package root — it packages your `detector/` changes into
`submission.patch`:

```bash
./packaging/make-submission.sh              # writes submission.patch
./packaging/make-submission.sh --archive    # also bundles submission.tar.gz (patch + RATIONALE.md)
```

(The package is a git repo, so this is just `git add -A && git diff --cached -- detector` under
the hood.) Hand in `submission.patch` plus your short written rationale (the Product Owner chat
tells you where). We apply it on our side with `git apply` and run it against the harness (seed
tree + scanner stub + change driver), so keep it **self-contained and runnable** — include any run
command or notes your component needs.

A repo or runnable-demo link is also accepted if a patch doesn't fit your approach.
