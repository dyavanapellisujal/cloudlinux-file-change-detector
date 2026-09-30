# CloudLinux Engineering Discovery Challenge — package

Everything you need to build and demonstrate your component locally.

1. **Read `packaging/RUNBOOK.md`** — how to run the fixtures (Docker or pip) and what the
   watch set is.
2. **Build your detector under `detector/`** — provide an executable `detector/run.sh`
   (see `detector/README.md`).
3. **Submit:** run `./packaging/make-submission.sh` (or `python3 packaging/make_submission.py`).
   It packages your changes into `submission.patch` — hand that in where the Product Owner
   chat tells you.

This directory is a git repo; your submission is just the diff of what you added under
`detector/`.
