"""warden's config file.

Every field feeds a ban decision, so anything warden does not understand stops
it: an unknown key, a wrong type, a missing id. A typo must never quietly widen
or narrow who gets banned. The bot token never goes in this file; the config
names a token file instead, because a config file ends up in a repo one day.
"""

import os
import stat
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Discord deletes at most 7 days of a banned user's messages.
MAX_DELETE_SECONDS = 7 * 24 * 3600


class ConfigError(Exception):
    """The config cannot be used. The message says why, and where."""


@dataclass(frozen=True)
class Config:
    guild_id: int
    report_channel_id: int
    evil_role_id: int
    honeypot_channel_id: int
    token_file: str
    dry_run: bool = True
    delete_message_seconds: int = 3600
    staff_role_ids: FrozenSet[int] = field(default_factory=frozenset)
    exempt_user_ids: FrozenSet[int] = field(default_factory=frozenset)
    exempt_bot_ids: FrozenSet[int] = field(default_factory=frozenset)
    batch_seconds: float = 30.0
    state_file: str = "~/.warden/state.json"
    audit_log: str = "~/.warden/audit.jsonl"


# Section -> {key -> required}. Anything else is an error.
_SCHEMA: Dict[Optional[str], Dict[str, bool]] = {
    None: {"token_file": True, "dry_run": False},
    "guild": {"id": True, "report_channel": True},
    "rules": {"evil_role": True, "honeypot_channel": True, "delete_message_seconds": False},
    "exempt": {"staff_roles": False, "users": False, "bots": False},
    "report": {"batch_seconds": False},
    "paths": {"state_file": False, "audit_log": False},
}


def _where(section: Optional[str], key: str) -> str:
    return f"{section}.{key}" if section else key


def _snowflake(value: Any, where: str) -> int:
    # bool is an int in Python; `id = true` is a typo, not an id.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where} must be a Discord id (a positive integer), got {value!r}")
    return value


def _snowflakes(value: Any, where: str) -> FrozenSet[int]:
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be a list of Discord ids, got {value!r}")
    return frozenset(_snowflake(v, f"{where}[{i}]") for i, v in enumerate(value))


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string, got {value!r}")
    return value


def parse(data: Dict[str, Any]) -> Config:
    """Validate a parsed TOML document and build a Config."""
    _refuse_token(data)
    for key, value in data.items():
        if isinstance(value, dict):
            if key not in _SCHEMA:
                raise ConfigError(f"unknown section [{key}]")
            for sub in value:
                if sub not in _SCHEMA[key]:
                    raise ConfigError(f"unknown key {key}.{sub}")
        elif key not in _SCHEMA[None]:
            raise ConfigError(f"unknown key {key}")
    for section, keys in _SCHEMA.items():
        table = data if section is None else data.get(section, {})
        for key, required in keys.items():
            if required and key not in table:
                raise ConfigError(f"missing required key {_where(section, key)}")

    guild = data["guild"]
    rules = data["rules"]
    exempt = data.get("exempt", {})
    report = data.get("report", {})
    paths = data.get("paths", {})

    dry_run = data.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ConfigError(f"dry_run must be true or false, got {dry_run!r}")

    seconds = rules.get("delete_message_seconds", 3600)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= MAX_DELETE_SECONDS:
        raise ConfigError(
            f"rules.delete_message_seconds must be an integer from 0 to {MAX_DELETE_SECONDS} "
            f"(7 days, Discord's limit), got {seconds!r}"
        )

    batch = report.get("batch_seconds", 30)
    if isinstance(batch, bool) or not isinstance(batch, (int, float)) or not 0 <= batch <= 3600:
        raise ConfigError(f"report.batch_seconds must be a number from 0 to 3600, got {batch!r}")

    cfg = Config(
        guild_id=_snowflake(guild["id"], "guild.id"),
        report_channel_id=_snowflake(guild["report_channel"], "guild.report_channel"),
        evil_role_id=_snowflake(rules["evil_role"], "rules.evil_role"),
        honeypot_channel_id=_snowflake(rules["honeypot_channel"], "rules.honeypot_channel"),
        token_file=_string(data["token_file"], "token_file"),
        dry_run=dry_run,
        delete_message_seconds=seconds,
        staff_role_ids=_snowflakes(exempt.get("staff_roles", []), "exempt.staff_roles"),
        exempt_user_ids=_snowflakes(exempt.get("users", []), "exempt.users"),
        exempt_bot_ids=_snowflakes(exempt.get("bots", []), "exempt.bots"),
        batch_seconds=float(batch),
        state_file=_string(paths.get("state_file", Config.state_file), "paths.state_file"),
        audit_log=_string(paths.get("audit_log", Config.audit_log), "paths.audit_log"),
    )
    if cfg.honeypot_channel_id == cfg.report_channel_id:
        raise ConfigError("rules.honeypot_channel and guild.report_channel are the same channel")
    if cfg.evil_role_id in cfg.staff_role_ids:
        raise ConfigError("rules.evil_role is also listed in exempt.staff_roles")
    return cfg


def _refuse_token(data: Any, path: str = "") -> None:
    """A key that looks like it holds the token itself is an error, anywhere."""
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if key.lower() in ("token", "bot_token", "discord_token"):
            raise ConfigError(
                f"{path}{key}: the bot token never goes in the config; "
                "put it in a 0600 file and name that file in token_file"
            )
        _refuse_token(value, f"{path}{key}.")


def load(path: str) -> Config:
    path = os.path.expanduser(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    try:
        return parse(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def read_token(cfg: Config) -> str:
    """The bot token, from token_file. Refused if anyone but the owner can read it."""
    path = os.path.expanduser(cfg.token_file)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise ConfigError(f"token_file {path} does not exist") from None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"token_file {path} is readable by group or others "
            f"(mode {stat.S_IMODE(st.st_mode):o}); run: chmod 600 {path}"
        )
    with open(path, encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise ConfigError(f"token_file {path} is empty")
    return token
