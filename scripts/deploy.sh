#!/bin/bash
# Deploy warden on macOS from a COMMITTED version of a repo. Two modes:
#
#   sudo scripts/deploy.sh --system [--repo PATH] [--ref REF] [--rotate-token]
#       Recommended wherever coding agents run as you. warden runs as the hidden system
#       user _warden (a LaunchDaemon). Its token, config, state and audit log live in
#       /var/lib/warden, readable only by _warden; its code lives in /usr/local/warden,
#       writable only by root. Nothing running as you can read the token or change the
#       code. The first run creates the user, and moves an existing per-user install
#       (~/.warden) over, deleting ~/.warden once the daemon is up.
#
#   scripts/deploy.sh [--ref REF]
#       Per-user: a LaunchAgent running as you, with everything in ~/.warden. Simpler, but
#       any process running as you, agents included, can read the token.
#
#   [sudo] scripts/deploy.sh [--system] --uninstall
#       Stop and remove the service. Data, code and the _warden user are kept.
#
#   --ref REF        deploy this commit (default HEAD). Uncommitted edits are never deployed.
#   --repo PATH      the git repo to deploy from (default: the repo this script is in).
#   --rotate-token   system mode: ask for a new bot token, even if one is installed.
#
# Every deploy:
#   1. Copies the commit's files into <base>/releases/<sha>. Files come straight from git's
#      object store (ls-tree + cat-file), so no checkout filter or attribute, committed or
#      not, can rewrite them on the way.
#   2. Builds a venv there, installs requirements.txt, and runs the tests inside the
#      release. A failing release is never switched to.
#   3. Runs `warden --check` against the config, from the release, as the service's user.
#   4. Points <base>/current at the release, renders the plist, (re)starts the service,
#      and waits for warden's own "started" record.
#
# After the first system deploy, run the root-owned copy of this script, not the one in
# your checkout, since agents can edit the checkout:
#   sudo /usr/local/warden/current/scripts/deploy.sh --system --repo ~/Dev/warden
# Either way you are deploying the repo's commits, so look at what you deploy.
set -euo pipefail

LABEL=com.warden.daemon
SYSUSER=_warden
PYTHON=${WARDEN_PYTHON:-/usr/bin/python3}

SELF_ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
REPO=$SELF_ROOT
REF=HEAD
SYSTEM=0
UNINSTALL=0
ROTATE=0

die() { echo "deploy: $*" >&2; exit 1; }
say() { echo "deploy: $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) [ $# -ge 2 ] || die "--ref needs a value"; REF=$2; shift 2 ;;
    --ref=*) REF=${1#--ref=}; shift ;;
    --repo) [ $# -ge 2 ] || die "--repo needs a path"; REPO=$(cd "$2" && pwd -P); shift 2 ;;
    --repo=*) REPO=$(cd "${1#--repo=}" && pwd -P); shift ;;
    --system) SYSTEM=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --rotate-token) ROTATE=1; shift ;;
    -h|--help) sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

if [ "$SYSTEM" = 1 ]; then
  [ "$(id -u)" = 0 ] || die "--system needs root: run it with sudo"
  BASE=/usr/local/warden
  DATA=/var/lib/warden
  LOG_DIR=/var/log/warden
  PLIST=/Library/LaunchDaemons/$LABEL.plist
  DOMAIN=system
  TEMPLATE=$LABEL.system.plist.in
  RUNAS=(sudo -u "$SYSUSER")
else
  [ "$(id -u)" != 0 ] || die "per-user mode must not run as root; for a system install pass --system"
  [ "$ROTATE" = 0 ] || die "--rotate-token is for --system; per-user, edit ~/.warden/token"
  BASE=${WARDEN_HOME:-$HOME/.warden}
  DATA=$BASE
  LOG_DIR=$HOME/Library/Logs/warden
  PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
  DOMAIN=gui/$(id -u)
  TEMPLATE=$LABEL.plist.in
  RUNAS=()
fi
CONFIG=$DATA/warden.toml
TOKEN=$DATA/token
LOG=$LOG_DIR/warden.log
AUDIT=$DATA/audit.jsonl

loaded() { launchctl print "$1/$LABEL" >/dev/null 2>&1; }
running_wardens() { pgrep -f -- "-m warden|bin/warden" 2>/dev/null || true; }

stop_job() {  # domain, plist-to-remove-or-empty
  if loaded "$1"; then
    launchctl bootout "$1/$LABEL" || die "launchctl bootout $1/$LABEL failed"
  fi
  [ -z "${2:-}" ] || rm -f "$2"
}

wait_for_no_warden() {
  # bootout returns before the process is gone; wait so two wardens never overlap.
  local i=0
  while [ -n "$(running_wardens)" ] && [ $i -lt 20 ]; do sleep 1; i=$((i + 1)); done
  [ -z "$(running_wardens)" ] || die "another warden is still running (pid $(running_wardens | tr '\n' ' ')); stop it, then deploy again"
}

# The login user's home and per-user install, when this runs under sudo.
OWNER=${SUDO_USER:-$(id -un)}
OWNER_HOME=$(dscl . -read "/Users/$OWNER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
OLD=${OWNER_HOME:-/nonexistent}/.warden

if [ "$UNINSTALL" = 1 ]; then
  stop_job "$DOMAIN" "$PLIST"
  if [ "$SYSTEM" = 1 ]; then
    say "daemon removed. Kept: $DATA (token, config, state, audit), $BASE (code), $LOG_DIR, and the $SYSUSER user."
    say "to remove those too: sudo rm -rf $DATA $BASE $LOG_DIR && sudo dscl . -delete /Users/$SYSUSER && sudo dscl . -delete /Groups/$SYSUSER"
  else
    say "service removed. Kept: $BASE (config, token, state, audit, releases) and $LOG_DIR."
  fi
  exit 0
fi

# -- the system user ------------------------------------------------------------------------------
ensure_system_user() {
  dscl . -read "/Users/$SYSUSER" >/dev/null 2>&1 && return 0
  local id
  for id in $(seq 450 499); do
    dscl . -list /Users UniqueID | awk '{print $2}' | grep -qx "$id" && continue
    dscl . -list /Groups PrimaryGroupID | awk '{print $2}' | grep -qx "$id" && continue
    break
  done
  [ "$id" -le 499 ] || die "no free uid/gid in 450-499 for $SYSUSER"
  dscl . -create "/Groups/$SYSUSER"
  dscl . -create "/Groups/$SYSUSER" PrimaryGroupID "$id"
  dscl . -create "/Groups/$SYSUSER" RealName "warden"
  dscl . -create "/Groups/$SYSUSER" Password "*"
  dscl . -create "/Users/$SYSUSER"
  dscl . -create "/Users/$SYSUSER" UniqueID "$id"
  dscl . -create "/Users/$SYSUSER" PrimaryGroupID "$id"
  dscl . -create "/Users/$SYSUSER" UserShell /usr/bin/false
  dscl . -create "/Users/$SYSUSER" NFSHomeDirectory "$DATA"
  dscl . -create "/Users/$SYSUSER" RealName "warden ban bot"
  dscl . -create "/Users/$SYSUSER" Password "*"
  dscl . -create "/Users/$SYSUSER" IsHidden 1
  say "created hidden system user $SYSUSER (uid $id), with no login shell and no password"
}

# -- 1, 2. the release ------------------------------------------------------------------------------
GIT=(git -c "safe.directory=$REPO" -C "$REPO")
SHA=$("${GIT[@]}" rev-parse --verify "$REF^{commit}" 2>/dev/null) || die "no such commit '$REF' in $REPO (pass --repo)"
SHORT=$(printf %.12s "$SHA")
REL=$BASE/releases/$SHORT

[ "$SYSTEM" = 0 ] || ensure_system_user
mkdir -p "$BASE/releases"
if [ "$SYSTEM" = 1 ]; then chown root:wheel "$BASE" "$BASE/releases"; chmod 755 "$BASE" "$BASE/releases"
else chmod 700 "$BASE"; fi

if [ -d "$REL" ] && [ -f "$REL/.deployed" ]; then
  say "release $SHORT already built; reusing it"
else
  rm -rf "$REL.tmp" && mkdir -p "$REL.tmp"
  "${GIT[@]}" ls-tree -r -z --full-tree "$SHA" | while IFS= read -r -d '' entry; do
    meta=${entry%%	*}; path=${entry#*	}
    set -- $meta   # mode type object
    [ "$2" = blob ] || continue
    [ "$1" != 120000 ] || die "refusing symlink $path: releases contain regular files only"
    case "$path" in /*|*../*) die "refusing path $path" ;; esac
    mkdir -p "$REL.tmp/$(dirname "$path")"
    "${GIT[@]}" cat-file blob "$3" > "$REL.tmp/$path"
    # `if`, not `[ ... ] && chmod`: a false test as the loop's last command would
    # fail the pipeline, and set -e -o pipefail would end the script silently.
    if [ "$1" = 100755 ]; then chmod 755 "$REL.tmp/$path"; fi
  done

  say "building release $SHORT"
  "$PYTHON" -m venv "$REL.tmp/.venv" || die "venv failed with $PYTHON"
  "$REL.tmp/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$REL.tmp/requirements.txt" \
    || die "pip install failed"
  (cd "$REL.tmp" && .venv/bin/python -I -m unittest discover -s tests -t . >/dev/null 2>&1) \
    || die "tests fail in release $SHORT; not deploying it (run ./test.sh to see why)"
  if [ "$SYSTEM" = 1 ]; then
    # Bytecode now, as root: the service user cannot write here, and must not need to.
    "$REL.tmp/.venv/bin/python" -I -m compileall -q -x '/\.venv/' "$REL.tmp" >/dev/null || true
    chown -R root:wheel "$REL.tmp"
    chmod -R u+rwX,go+rX,go-w "$REL.tmp"
  fi
  echo "$SHA" > "$REL.tmp/.deployed"
  rm -rf "$REL" && mv "$REL.tmp" "$REL"
fi
PY=$REL/.venv/bin/python

# -- data: first run, migration, token ----------------------------------------------------------------
MIGRATED=0
mkdir -p "$DATA"
if [ "$SYSTEM" = 1 ]; then
  if [ ! -f "$CONFIG" ] && [ -f "$OLD/warden.toml" ]; then
    # Move the per-user install over. Paths in its config point into ~/.warden; rewrite them.
    sed -e "s#~/\.warden/#$DATA/#g" -e "s#$OLD/#$DATA/#g" "$OLD/warden.toml" > "$CONFIG"
    for f in state.json audit.jsonl; do
      if [ -f "$OLD/$f" ]; then cp "$OLD/$f" "$DATA/$f"; fi
    done
    MIGRATED=1
    say "moved config, state and audit log from $OLD to $DATA"
  fi
  if [ ! -s "$TOKEN" ] || [ "$ROTATE" = 1 ]; then
    tok=""
    if [ -t 0 ]; then
      echo "deploy: paste the bot token (input hidden). If any agent could have read the old one,"
      echo "        reset it first: Developer Portal > your app > Bot > Reset Token."
      if [ -s "$OLD/token" ]; then printf 'deploy: token (Enter alone reuses the old one): '; else printf 'deploy: token: '; fi
      IFS= read -rs tok || true
      echo
      tok=$(printf '%s' "$tok" | tr -d '[:space:]')
    fi
    if [ -n "$tok" ]; then
      ( umask 077; printf '%s\n' "$tok" > "$TOKEN" )
      unset tok
      say "token installed"
    elif [ -s "$OLD/token" ] && [ ! -s "$TOKEN" ]; then
      cp "$OLD/token" "$TOKEN"
      say "WARNING: reused the old token, which agents could read. Reset it, then run: sudo $0 --system --rotate-token"
    elif [ ! -s "$TOKEN" ]; then
      ( umask 077; : > "$TOKEN" )
    fi
  fi
  if [ ! -f "$CONFIG" ]; then
    sed -e "s#~/\.warden/#$DATA/#g" "$REL/warden.toml.example" > "$CONFIG"
  fi
  chown -R "$SYSUSER:$SYSUSER" "$DATA"
  chmod 700 "$DATA"
  chmod 600 "$CONFIG" "$TOKEN"
  mkdir -p "$LOG_DIR"
  chown "$SYSUSER:admin" "$LOG_DIR"
  chmod 750 "$LOG_DIR"
  if [ ! -s "$TOKEN" ] || grep -qE '^[[:space:]]*id[[:space:]]*=[[:space:]]*0[[:space:]]*$' "$CONFIG"; then
    say "fill in $CONFIG (sudo -e $CONFIG) and the token (sudo $0 --system --rotate-token),"
    say "see docs/discord-setup.md, then run this again."
    exit 0
  fi
else
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
fi

# -- 3. check the config, as the service's user ------------------------------------------------------
# From $DATA: the service user may not be allowed into the directory this was run from.
(cd "$DATA" && "${RUNAS[@]+"${RUNAS[@]}"}" "$PY" -I "$REL/bin/warden" --config "$CONFIG" --check) \
  || die "config check failed; fix it and deploy again"

# -- 4. switch and (re)start ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
# ln -n replaces the link itself. (`mv tmp current` would not: with current a link to a
# directory, BSD mv moves tmp INTO that directory and the switch silently never happens.)
ln -sfn "$REL" "$BASE/current"
CURRENT=$BASE/current
[ "$(readlink "$CURRENT")" = "$REL" ] || die "could not point $CURRENT at $REL"

# Stop every warden this machine might be running: this mode's service, and in system mode
# also the login user's per-user LaunchAgent, which this replaces.
stop_job "$DOMAIN"
if [ "$SYSTEM" = 1 ] && [ -n "${OWNER_HOME:-}" ]; then
  stop_job "gui/$(id -u "$OWNER")" "$OWNER_HOME/Library/LaunchAgents/$LABEL.plist"
fi
wait_for_no_warden

[ -f "$PLIST" ] && cp "$PLIST" "$PLIST.bak"
sed -e "s#@CURRENT@#$CURRENT#g" -e "s#@CONFIG@#$CONFIG#g" -e "s#@LOG@#$LOG#g" \
    -e "s#@DATA@#$DATA#g" -e "s#@USER@#$SYSUSER#g" -e "s#@PYTHON@#$CURRENT/.venv/bin/python#g" \
    "$REL/launchd/$TEMPLATE" > "$PLIST"
plutil -lint "$PLIST" >/dev/null || die "rendered plist is invalid: $PLIST"
if [ "$SYSTEM" = 1 ]; then chown root:wheel "$PLIST"; chmod 644 "$PLIST"; fi

before=$( [ -f "$AUDIT" ] && wc -l < "$AUDIT" | tr -d ' ' || echo 0 )
launchctl bootstrap "$DOMAIN" "$PLIST" || die "launchctl bootstrap $DOMAIN failed"

i=0
while [ $i -lt 30 ]; do
  if [ -f "$AUDIT" ] && tail -n +"$((before + 1))" "$AUDIT" | grep -q '"event": "started"'; then
    say "deployed $SHORT; warden is running (log: $LOG)"
    tail -n +"$((before + 1))" "$AUDIT" | grep '"event": "started"' | tail -1
    if [ "$MIGRATED" = 1 ]; then
      rm -rf "$OLD"
      say "removed $OLD: the old copy of the token is gone"
    fi
    exit 0
  fi
  if ! loaded "$DOMAIN"; then break; fi
  sleep 1; i=$((i + 1))
done
[ "$MIGRATED" = 0 ] || say "left $OLD in place; to go back: launchctl bootout $DOMAIN/$LABEL, then run scripts/deploy.sh without --system"
die "warden did not report started within 30s; see $LOG"
