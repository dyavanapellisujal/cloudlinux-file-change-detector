#!/usr/bin/env sh
# Package your challenge submission into submission.patch. Thin wrapper around make_submission.py.
# Usage:  ./packaging/make-submission.sh [--archive]
exec python3 "$(dirname "$0")/make_submission.py" "$@"
