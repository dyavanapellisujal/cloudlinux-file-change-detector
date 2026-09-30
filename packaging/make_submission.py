#!/usr/bin/env python3
"""Package your challenge submission — one command, no git incantations.

Run from the package directory once your detector is built under detector/:

    python3 packaging/make_submission.py             # -> submission.patch
    python3 packaging/make_submission.py --archive    # + submission.tar.gz (patch + RATIONALE.md)

The reviewer applies submission.patch onto a fresh detector/ baseline and runs your
detector/run.sh, so ONLY changes under detector/ are graded. Add a short RATIONALE.md
describing your approach.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


def _repo_root() -> str:
    r = _git(".", "rev-parse", "--show-toplevel")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Package your challenge submission.")
    ap.add_argument("--archive", action="store_true",
                    help="also bundle submission.tar.gz (submission.patch + RATIONALE.md)")
    ap.add_argument("--out", default="submission.patch", help="output patch path (default: submission.patch)")
    args = ap.parse_args()

    root = _repo_root()
    run_sh = os.path.join(root, "detector", "run.sh")
    if not os.path.isfile(run_sh):
        print("ERROR: detector/run.sh not found — your detector needs an executable run.sh "
              "entrypoint (see detector/README.md).", file=sys.stderr)
        return 1
    if not os.access(run_sh, os.X_OK):
        print("WARNING: detector/run.sh is not executable — run: chmod +x detector/run.sh",
              file=sys.stderr)

    if _git(root, "add", "-A").returncode != 0:
        print("ERROR: `git add` failed — is this the challenge package directory?", file=sys.stderr)
        return 1

    names = _git(root, "diff", "--cached", "--name-only").stdout.split()
    outside = [n for n in names if not n.startswith("detector/")]
    if outside:
        print("NOTE: only changes under detector/ are graded. Ignored (outside detector/):",
              file=sys.stderr)
        for n in outside:
            print(f"  - {n}", file=sys.stderr)

    diff = _git(root, "diff", "--cached", "--", "detector")
    if diff.returncode != 0:
        print(f"ERROR: `git diff` failed: {diff.stderr.strip()}", file=sys.stderr)
        return 1
    if not diff.stdout.strip():
        print("ERROR: no changes under detector/ to submit — build your component there first.",
              file=sys.stderr)
        return 1

    out_path = os.path.join(root, args.out)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(diff.stdout)
    print(f"wrote {args.out} ({len(diff.stdout.splitlines())} lines)")

    if args.archive:
        rationale = os.path.join(root, "RATIONALE.md")
        with tarfile.open(os.path.join(root, "submission.tar.gz"), "w:gz") as tar:
            tar.add(out_path, arcname="submission.patch")
            if os.path.isfile(rationale):
                tar.add(rationale, arcname="RATIONALE.md")
            else:
                print("NOTE: no RATIONALE.md found — add a short write-up of your approach.",
                      file=sys.stderr)
        print("wrote submission.tar.gz")

    print("\nNext: submit the file where the Product Owner chat tells you. Good luck!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
