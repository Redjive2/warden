"""The ban rules, as pure functions over plain data.

Nothing here imports discord. The bot layer turns Discord objects into a Target
and a Trigger, asks `decide`, and acts on the answer, so every decision warden
makes can be tested without a server.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import FrozenSet, Optional

from .config import Config

ROLE = "role"
HONEYPOT = "honeypot"

# Discord ids encode their creation time: milliseconds since this epoch, shifted
# left 22 bits.
DISCORD_EPOCH_MS = 1420070400000


@dataclass(frozen=True)
class Target:
    """Who a rule fired on, as far as a ban decision needs to know."""

    user_id: int
    name: str
    is_bot: bool = False
    # False when the user is no longer in the server (left after posting). They
    # can still be banned by id, which stops them rejoining.
    is_member: bool = True
    is_owner: bool = False
    role_ids: FrozenSet[int] = field(default_factory=frozenset)
    administrator: bool = False
    manage_guild: bool = False
    ban_members: bool = False
    joined_at: Optional[datetime] = None
    # Their top role is at or above warden's, so Discord would refuse the ban.
    outranks_warden: bool = False


@dataclass(frozen=True)
class Trigger:
    """Which rule fired."""

    kind: str  # ROLE or HONEYPOT
    message_id: Optional[int] = None  # the honeypot message, when there is one


@dataclass(frozen=True)
class Decision:
    action: str  # "ban", "exempt" or "refuse"
    why: str


@dataclass(frozen=True)
class Outcome:
    """What happened, for the report and the audit log."""

    status: str  # "banned", "dry_run", "exempt", "failed", "already_banned"
    target: Target
    trigger: Trigger
    detail: str = ""
    delete_seconds: int = 0
    # What happened to the honeypot message itself: "deleted", "already_gone",
    # "would_delete" (dry run), "failed: <why>", or None when there is no message
    # to delete (a role trigger, or an exempt author whose message stays).
    message_deletion: Optional[str] = None
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def created_at(user_id: int) -> datetime:
    """When a Discord account was created, read from its id."""
    ms = (user_id >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def exemption(cfg: Config, target: Target, self_id: int) -> Optional[str]:
    """Why target must not be banned, or None. Checked before anything else."""
    if target.user_id == self_id:
        return "it is warden itself"
    if target.is_owner:
        return "they own the server"
    if target.user_id in cfg.exempt_user_ids:
        return "they are listed in exempt.users"
    if target.is_bot and target.user_id in cfg.exempt_bot_ids:
        return "they are listed in exempt.bots"
    if target.administrator:
        return "they have Administrator"
    if target.manage_guild:
        return "they have Manage Server"
    if target.ban_members:
        return "they have Ban Members"
    staff = target.role_ids & cfg.staff_role_ids
    if staff:
        return f"they hold staff role {min(staff)}"
    return None


def ban_reason(trigger: Trigger) -> str:
    """The reason recorded in Discord's audit log. Discord caps it at 512 characters."""
    if trigger.kind == ROLE:
        return "warden: holds the Evil Bot role"
    if trigger.message_id is not None:
        return f"warden: posted in the honeypot channel (message {trigger.message_id})"
    return "warden: posted in the honeypot channel"


def decide(cfg: Config, target: Target, trigger: Trigger, self_id: int) -> Decision:
    """Ban, exempt, or refuse up front. Dry run is the caller's business, not a decision."""
    if trigger.kind not in (ROLE, HONEYPOT):
        raise ValueError(f"unknown trigger kind {trigger.kind!r}")
    why = exemption(cfg, target, self_id)
    if why is not None:
        return Decision("exempt", why)
    if target.outranks_warden:
        return Decision(
            "refuse",
            "their top role is at or above warden's, so Discord would refuse the ban; "
            "move warden's role above it",
        )
    return Decision("ban", ban_reason(trigger))
