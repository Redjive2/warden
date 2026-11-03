# warden

A deterministic ban bot for one Discord server. It:

1. bans any member holding a configured role (Enth: **Evil Bot**);
2. bans anyone who posts in a honeypot channel (Enth: **#❌-do-not-use-❌**), and deletes the post;
3. reports every outcome to a report channel (Enth: **#system-updates**), including the bans it
   could not make.

There is no language model in it. The rules are mechanical, the honeypot is exactly where
hostile text lands, and a bot that can ban should not be one that anyone can talk into
anything. That is also why it is separate from harold, which never bans without a person's ✅.

## Quick start (macOS)

```sh
git clone <this repo> ~/Dev/warden && cd ~/Dev/warden
scripts/deploy.sh   # first run: creates ~/.warden/warden.toml and an empty ~/.warden/token
# do docs/discord-setup.md: the bot, its permissions, the ids, the token
scripts/deploy.sh   # checks the config, then installs and starts the launchd service
```

It starts in **dry run**: reports say "would ban" and "would delete", and nothing is touched.
Read the reports for a day or two, then set `dry_run = false` in `~/.warden/warden.toml` and
restart it.

## How it decides

- **Role:** a ban when the role is added, or when someone joins holding it. At start, warden
  sweeps every current holder.
- **Honeypot:** the author is banned, including someone who has already left (by id). Their
  post is deleted, and so is every repeat. On a restart, warden catches up on what was posted
  while it was down; the first run only sets a baseline.
- **Never banned, reported instead:**
  - the owner;
  - anyone with Administrator, Manage Server or Ban Members;
  - configured staff roles, users and bots.

  Their honeypot posts stay up, because staff post the warning there.
- **Refused up front, reported as FAILED with the fix:** anyone whose top role is at or above
  warden's.
- **Reports:** mentions are off, names are escaped, and a burst becomes one summary. Every
  decision is also appended to `~/.warden/audit.jsonl`.

## Operating it

| | |
|---|---|
| Update | commit, then `scripts/deploy.sh` (it deploys a commit, never uncommitted edits) |
| Restart | `launchctl kickstart -k gui/$(id -u)/com.warden.daemon` (after editing the config) |
| Status | `launchctl print gui/$(id -u)/com.warden.daemon \| grep -E 'state\|pid'` |
| Log | `tail -f ~/Library/Logs/warden/warden.log` |
| Remove | `scripts/deploy.sh --uninstall` (keeps `~/.warden`) |

**How it runs.**
- `deploy.sh` copies a commit into `~/.warden/releases/<sha>` and builds a venv there. It runs
  the tests and `--check`, then points `~/.warden/current` at the release and (re)starts the
  launchd agent `com.warden.daemon`.
- The service runs `python -I bin/warden --service` from the release. Under `--service`, a
  problem a person must fix exits 0, so launchd does not retry it; only a crash restarts it.
- warden is offline while the Mac sleeps. On wake it sweeps and catches up, so bans are late,
  not missed.
- Run only one warden. A second copy would handle every hit twice.

**Keep the token from agents.** Coding agents running with broad file access, such as pogo's
bypass-mode agents, can read `~/.warden/token` and write `~/.warden/current`. Deny `~/.warden`
in their sandbox, or run warden on a host no agent can reach.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./test.sh                                     # stdlib unittest; no network, no token
.venv/bin/python -m warden --check            # validate ~/.warden/warden.toml
```

The modules are small and separate, so a ban decision can be tested without Discord:

| Module | Holds |
|---|---|
| `warden/config.py` | strict config parsing, which refuses a token in the file |
| `warden/rules.py` | decisions, as pure functions |
| `warden/report.py` | report text and batching |
| `warden/store.py` | the audit log and catch-up state |
| `warden/bot.py` | the Discord side |
| `bin/warden` | the launcher used by deployed releases |
| `scripts/deploy.sh` | release, check and install the service |

Python 3.9 is the floor (`tomli` stands in for `tomllib`); the one runtime dependency is
discord.py.
