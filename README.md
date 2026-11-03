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
git clone https://github.com/Redjive2/warden ~/Dev/warden && cd ~/Dev/warden
sudo scripts/deploy.sh --system   # first run: creates user _warden and /var/lib/warden/warden.toml
# do docs/discord-setup.md: the bot, its permissions and the ids, then:
sudo -e /var/lib/warden/warden.toml
sudo scripts/deploy.sh --system   # asks for the token (hidden), checks, starts the daemon
```

**`--system`** runs warden as a hidden system user, `_warden`. Its token and data are in
`/var/lib/warden`, which only that user can read, and its code is in `/usr/local/warden`, which
only root can change. Use it wherever coding agents run as you: they cannot read the token or
change the code. Without `--system`, `scripts/deploy.sh` installs a per-user LaunchAgent with
everything in `~/.warden`. That is simpler, but anything running as you can read the token.
Running `--system` on a machine with a per-user install moves it over, then deletes `~/.warden`.

It starts in **dry run**: reports say "would ban" and "would delete", and nothing is touched.
Read the reports for a day or two, then set `dry_run = false` in the config and restart it.

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

## Operating it (system mode)

| | |
|---|---|
| Update | commit, then `sudo /usr/local/warden/current/scripts/deploy.sh --system --repo ~/Dev/warden` |
| New token | `sudo /usr/local/warden/current/scripts/deploy.sh --system --repo ~/Dev/warden --rotate-token` |
| Edit config | `sudo -e /var/lib/warden/warden.toml`, then restart |
| Restart | `sudo launchctl kickstart -k system/com.warden.daemon` |
| Status | `sudo launchctl print system/com.warden.daemon \| grep -E 'state\|pid'` |
| Log | `tail -f /var/log/warden/warden.log` (readable by admins; never contains the token) |
| Audit | `sudo tail /var/lib/warden/audit.jsonl` |
| Remove | `sudo scripts/deploy.sh --system --uninstall` (keeps data, code and the user) |

Per-user mode uses the same commands without `sudo` and `--system`. Its domain is
`gui/$(id -u)`, its data is in `~/.warden` and its log in `~/Library/Logs/warden`.

**How it runs.**
- `deploy.sh` copies a **commit**, never uncommitted edits, into `<base>/releases/<sha>`, and
  builds a venv there. It runs the tests, then `--check` as the service's user, then points
  `<base>/current` at the release and (re)starts the launchd job `com.warden.daemon`.
- The service runs `python -I bin/warden --service` from the release. Under `--service`, a
  problem a person must fix exits 0, so launchd does not retry it; only a crash restarts it.
- warden is offline while the Mac sleeps. On wake it sweeps and catches up, so bans are late,
  not missed.
- Run only one warden. A second copy would handle every hit twice. `deploy.sh` stops the other
  mode's job and refuses to start while any other warden is running.

**Deploy from the root-owned copy.** After the first system deploy, run
`/usr/local/warden/current/scripts/deploy.sh` rather than the one in your checkout. Agents can
edit the checkout, and `sudo` runs whatever is there as root. Either way you deploy the repo's
commits, so look at what you deploy.

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
