"""python -m warden --config PATH [--check] [--service]"""

import argparse
import sys

from . import __version__
from .config import ConfigError, load, read_token

# Exit codes when run by hand. Under --service every "a person must fix this"
# exit is 0 instead; see main.
EXIT_CONFIG = 2
EXIT_LOGIN = 3


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="warden", description="Deterministic ban bot for one Discord server.")
    p.add_argument("--config", default="~/.warden/warden.toml", help="config file (default ~/.warden/warden.toml)")
    p.add_argument("--check", action="store_true",
                   help="validate the config and token file, print a summary, and exit without connecting")
    p.add_argument("--service", action="store_true",
                   help="run under a supervisor (launchd): exit 0 on problems a person must fix, so the "
                        "supervisor stops instead of retrying; only a crash exits non-zero")
    args = p.parse_args(argv)

    def needs_a_person(code: int) -> int:
        # launchd's KeepAlive can only tell success from failure. Retrying a bad
        # config, a rejected token or a missing intent fixes nothing, and every
        # retry of a gateway login spends one of Discord's daily session starts.
        # So under a supervisor these exit 0, and the log says what to fix.
        if args.service:
            print("warden: stopping; a person must fix the problem above, then restart the service.",
                  file=sys.stderr)
            return 0
        return code

    try:
        cfg = load(args.config)
        token = read_token(cfg)
    except ConfigError as exc:
        print(f"warden: {exc}", file=sys.stderr)
        return needs_a_person(EXIT_CONFIG)

    if args.check:
        print(f"warden {__version__}: config OK")
        print(f"  mode:            {'DRY RUN (nothing is banned)' if cfg.dry_run else 'LIVE (bans are on)'}")
        print(f"  guild:           {cfg.guild_id}")
        print(f"  evil role:       {cfg.evil_role_id}")
        print(f"  honeypot:        {cfg.honeypot_channel_id}")
        print(f"  reports to:      {cfg.report_channel_id}")
        print(f"  delete on ban:   {cfg.delete_message_seconds}s of history")
        print(f"  exempt:          {len(cfg.staff_role_ids)} staff role(s), "
              f"{len(cfg.exempt_user_ids)} user(s), {len(cfg.exempt_bot_ids)} bot(s)")
        print(f"  token file:      {cfg.token_file} (permissions OK, {len(token)} chars)")
        return 0

    # Imported here so --check works without discord.py's network stack loaded.
    import discord

    from .bot import Warden, WardenClient
    from .store import Audit, State

    warden = Warden(cfg, Audit(cfg.audit_log), State(cfg.state_file))
    try:
        WardenClient(warden).run(token)
    except discord.PrivilegedIntentsRequired:
        warden.audit.write("fatal", reason="privileged intents not enabled")
        print("warden: Discord refused the login because the Server Members Intent is off.\n"
              "  Developer Portal > your application > Bot > Privileged Gateway Intents:\n"
              "  turn ON Server Members Intent (leave Message Content Intent off), save, and start warden again.",
              file=sys.stderr)
        return needs_a_person(EXIT_LOGIN)
    except discord.LoginFailure:
        warden.audit.write("fatal", reason="login failed: token rejected")
        print(f"warden: Discord rejected the token in {cfg.token_file}. Reset it in the Developer Portal\n"
              "  (Bot > Reset Token), save the new one to the token file, and start warden again.",
              file=sys.stderr)
        return needs_a_person(EXIT_LOGIN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
