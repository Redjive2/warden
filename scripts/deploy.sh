#!/bin/bash
# Deploy warden as a macOS launchd service from a COMMITTED version of this repo.
#
# Usage: scripts/deploy.sh [--ref REF] [--uninstall]
#   --ref REF     deploy this commit (default HEAD). Uncommitted edits are never deployed.
#   --uninstall   stop and remove the service. Your config, token, state and audit log stay.
#
# What it does:
#   1. Copies the commit's files into ~/.warden/releases/<sha>. Files come straight from
#      git's object store (ls-tree + cat-file), so no checkout filter or attribute,
#      committed or not, can rewrite them on the way.
#   2. Builds a venv there from the system python, installs requirements.txt, and runs
#      the tests inside the release. A failing release is never switched to.
#   3. First run only: creates ~/.warden (0700), copies warden.toml.example to
#      ~/.warden/warden.toml, and creates an empty 0600 token file. It stops there until
#      you fill them in.
#   4. Runs `warden --check` against your config, from the release.
#   5. Points ~/.warden/current at the release, renders the launchd plist, and (re)starts
#      the service. It waits for warden's own "started" record before calling it done.
#
# The service runs the release, not this checkout, so editing the checkout cannot change
# what is running until you commit and deploy again.
set -euo pipefail

LABEL=com.warden.daemon
HOME_DIR=${WARDEN_HOME:-$HOME/.warden}
CONFIG=$HOME_DIR/warden.toml
TOKEN=$HOME_DIR/token
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
LOG_DIR=$HOME/Library/Logs/warden
LOG=$LOG_DIR/warden.log
PYTHON=${WARDEN_PYTHON:-/usr/bin/python3}
DOMAIN=gui/$(id -u)

REPO=$(cd "$(dirname "$0")/.." && pwd -P)
REF=HEAD
UNINSTALL=0

die() { echo "deploy: $*" >&2; exit 1; }
say() { echo "deploy: $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) [ $# -ge 2 ] || die "--ref needs a value"; REF=$2; shift 2 ;;
    --ref=*) REF=${1#--ref=}; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

stop_service() {
  if loaded; then
    launchctl bootout "$DOMAIN/$LABEL" || die "launchctl bootout failed"
    # bootout returns before the process is gone; wait so two wardens never overlap.
    i=0
    while pgrep -f "bin/warden --config $CONFIG" >/dev/null 2>&1 && [ $i -lt 20 ]; do sleep 1; i=$((i + 1)); done
  fi
}

if [ "$UNINSTALL" = 1 ]; then
  stop_service
  rm -f "$PLIST"
  say "service removed. Kept: $HOME_DIR (config, token, state, audit, releases) and $LOG_DIR."
  exit 0
fi

# -- 1. the release, from git's objects --------------------------------------------------
SHA=$(git -C "$REPO" rev-parse --verify "$REF^{commit}") || die "no such commit: $REF"
SHORT=$(printf %.12s "$SHA")
REL=$HOME_DIR/releases/$SHORT

mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"
if [ -d "$REL" ] && [ -f "$REL/.deployed" ]; then
  say "release $SHORT already built; reusing it"
else
  rm -rf "$REL.tmp" && mkdir -p "$REL.tmp"
  git -C "$REPO" ls-tree -r -z --full-tree "$SHA" | while IFS= read -r -d '' entry; do
    meta=${entry%%	*}; path=${entry#*	}
    set -- $meta   # mode type object
    [ "$2" = blob ] || continue
    [ "$1" != 120000 ] || die "refusing symlink $path: releases contain regular files only"
    case "$path" in /*|*../*) die "refusing path $path" ;; esac
    mkdir -p "$REL.tmp/$(dirname "$path")"
    git -C "$REPO" cat-file blob "$3" > "$REL.tmp/$path"
    # `if`, not `[ ... ] && chmod`: a false test as the loop's last command would
    # fail the pipeline, and set -e -o pipefail would end the script silently.
    if [ "$1" = 100755 ]; then chmod 755 "$REL.tmp/$path"; fi
  done

  # -- 2. venv, dependencies, tests ------------------------------------------------------
  say "building release $SHORT"
  "$PYTHON" -m venv "$REL.tmp/.venv" || die "venv failed with $PYTHON"
  "$REL.tmp/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$REL.tmp/requirements.txt" \
    || die "pip install failed"
  (cd "$REL.tmp" && .venv/bin/python -I -m unittest discover -s tests -t . >/dev/null 2>&1) \
    || die "tests fail in release $SHORT; not deploying it (run ./test.sh to see why)"
  echo "$SHA" > "$REL.tmp/.deployed"
  rm -rf "$REL" && mv "$REL.tmp" "$REL"
fi
PY=$REL/.venv/bin/python

# -- 3. first-run config and token ----------------------------------------------------------
first_run=0
if [ ! -f "$CONFIG" ]; then
  cp "$REL/warden.toml.example" "$CONFIG" && chmod 600 "$CONFIG"
  say "created $CONFIG: fill in the ids (see docs/discord-setup.md)"
  first_run=1
fi
if [ ! -f "$TOKEN" ]; then
  install -m 600 /dev/null "$TOKEN"
  say "created $TOKEN (empty, 0600): paste the bot token into it, as plain text"
  first_run=1
fi
[ "$first_run" = 0 ] || { say "then run this script again."; exit 0; }

# -- 4. check the config from the release ------------------------------------------------------
"$PY" -I "$REL/bin/warden" --config "$CONFIG" --check || die "config check failed; fix it and deploy again"

# -- 5. switch and (re)start ---------------------------------------------------------------------
mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
# ln -n replaces the link itself. (`mv tmp current` would not: with current a link to a
# directory, BSD mv moves tmp INTO that directory and the switch silently never happens.)
ln -sfn "$REL" "$HOME_DIR/current"
CURRENT=$HOME_DIR/current
[ "$(readlink "$CURRENT")" = "$REL" ] || die "could not point $CURRENT at $REL"

stop_service
# Checked after the service is stopped, so the service itself can never match. Anything
# left is a warden started some other way, and two wardens handle every hit twice.
if pgrep -f -- "-m warden|bin/warden" >/dev/null 2>&1; then
  die "another warden is running ($(pgrep -f -- '-m warden|bin/warden' | tr '\n' ' ')); stop it, then deploy again"
fi
[ -f "$PLIST" ] && cp "$PLIST" "$PLIST.bak"
sed -e "s#@PYTHON@#$CURRENT/.venv/bin/python#g" -e "s#@CURRENT@#$CURRENT#g" \
    -e "s#@CONFIG@#$CONFIG#g" -e "s#@LOG@#$LOG#g" "$REL/launchd/$LABEL.plist.in" > "$PLIST"
plutil -lint "$PLIST" >/dev/null || die "rendered plist is invalid: $PLIST"

AUDIT=$HOME_DIR/audit.jsonl
before=$( [ -f "$AUDIT" ] && wc -l < "$AUDIT" | tr -d ' ' || echo 0 )
launchctl bootstrap "$DOMAIN" "$PLIST" || die "launchctl bootstrap failed"

i=0
while [ $i -lt 30 ]; do
  if [ -f "$AUDIT" ] && tail -n +"$((before + 1))" "$AUDIT" | grep -q '"event": "started"'; then
    say "deployed $SHORT; warden is running (log: $LOG)"
    tail -n +"$((before + 1))" "$AUDIT" | grep '"event": "started"' | tail -1
    exit 0
  fi
  if ! loaded; then break; fi
  sleep 1; i=$((i + 1))
done
die "warden did not report started within 30s; see $LOG"
