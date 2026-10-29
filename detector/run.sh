#!/bin/sh
# Entry point the harness runs. It passes SEED_TREE and SCANNER_SOCKET in the
# environment; detector.py reads them from there. We exec so signals (SIGTERM on
# shutdown) reach Python directly.
exec python3 "$(dirname "$0")/detector.py"
