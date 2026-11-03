#!/bin/sh
# Runs warden's tests. Stdlib unittest, no network, no token.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "test.sh: no .venv; run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 1; }
exec .venv/bin/python -m unittest discover -s tests -t . "$@"
