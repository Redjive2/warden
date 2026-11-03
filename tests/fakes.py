"""Small stand-ins for the discord.py objects warden touches.

Only what warden reads is modelled. Errors are discord.py's own exception
classes, raised the way discord.py raises them, so warden's except clauses are
exercised for real.
"""

import tempfile
from datetime import datetime, timezone

import discord

from warden.config import Config


class _Resp:
    def __init__(self, status, reason):
        self.status = status
        self.reason = reason


def not_found():
    return discord.NotFound(_Resp(404, "Not Found"), "Unknown Ban")


def forbidden():
    return discord.Forbidden(_Resp(403, "Forbidden"), "Missing Permissions")


class Role:
    def __init__(self, id, position, members=None):
        self.id = id
        self.position = position
        self.members = members or []

    def __gt__(self, other):
        return self.position > other.position

    def __le__(self, other):
        return self.position <= other.position


class Perms:
    def __init__(self, administrator=False, manage_guild=False, ban_members=False,
                 send_messages=True, read_message_history=True, view_channel=True,
                 manage_messages=True):
        self.administrator = administrator
        self.manage_guild = manage_guild
        self.ban_members = ban_members
        self.send_messages = send_messages
        self.read_message_history = read_message_history
        self.view_channel = view_channel
        self.manage_messages = manage_messages


EVERYONE = Role(1, 0)


class Member:
    def __init__(self, id, name="spammer", roles=(), perms=None, guild=None, bot=False,
                 joined_at=None):
        self.id = id
        self.name = name
        self.bot = bot
        self.roles = [EVERYONE] + list(roles)
        self.guild_permissions = perms or Perms()
        self.guild = guild
        self.joined_at = joined_at or datetime(2026, 11, 2, tzinfo=timezone.utc)

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)


class User:
    """Someone who is not (or no longer) a member: no roles, no permissions."""

    def __init__(self, id, name="gone", bot=False):
        self.id = id
        self.name = name
        self.bot = bot


class Channel:
    def __init__(self, id, parent_id=None, perms=None, history=(), last_message_id=None):
        self.id = id
        self.parent_id = parent_id
        self.sent = []
        self._perms = perms or Perms()
        self._history = list(history)
        self.last_message_id = last_message_id
        self.history_calls = []

    def permissions_for(self, member):
        return self._perms

    async def send(self, text, allowed_mentions=None):
        self.sent.append((text, allowed_mentions))

    def history(self, after=None, oldest_first=True, limit=None):
        self.history_calls.append(after.id if after is not None else None)
        items = [m for m in self._history if after is None or m.id > after.id]
        items.sort(key=lambda m: m.id)

        async def gen():
            for m in items[:limit]:
                yield m
        return gen()


class Message:
    def __init__(self, id, author, channel, guild, webhook_id=None, delete_error=None):
        self.id = id
        self.author = author
        self.channel = channel
        self.guild = guild
        self.webhook_id = webhook_id
        self.deleted = False
        self.delete_error = delete_error  # raise this from delete(), if set

    async def delete(self):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True


class Guild:
    def __init__(self, id, owner_id=999, me=None, roles=(), channels=()):
        self.id = id
        self.owner_id = owner_id
        self.me = me
        self._roles = {r.id: r for r in roles}
        self._channels = {c.id: c for c in channels}
        self.banned = {}       # user id -> (reason, delete_message_seconds)
        self.ban_error = None  # raise this from ban(), if set

    def get_role(self, id):
        return self._roles.get(id)

    def get_channel(self, id):
        return self._channels.get(id)

    async def ban(self, user, reason=None, delete_message_seconds=None):
        if self.ban_error is not None:
            raise self.ban_error
        self.banned[user.id] = (reason, delete_message_seconds)

    async def fetch_ban(self, user):
        if user.id not in self.banned:
            raise not_found()
        return object()


# Ids. Real-looking snowflakes, so created_at() gives plausible dates.
GUILD = 1566895127254007970
REPORT = 1566900000000000001
HONEYPOT = 1566900000000000002
EVIL = 1566900000000000003
STAFF = 1566900000000000004
WARDEN = 1566900000000000005
SPAMMER = 1566950000000000000


def config(**overrides):
    d = tempfile.mkdtemp()
    values = dict(
        guild_id=GUILD, report_channel_id=REPORT, evil_role_id=EVIL,
        honeypot_channel_id=HONEYPOT, token_file=f"{d}/token", dry_run=False,
        delete_message_seconds=3600, staff_role_ids=frozenset({STAFF}),
        batch_seconds=0, state_file=f"{d}/state.json", audit_log=f"{d}/audit.jsonl",
    )
    values.update(overrides)
    return Config(**values)


def server(evil_holders=(), honeypot_history=(), honeypot_last=None, warden_perms=None,
           warden_position=50, evil_position=10):
    """A guild with warden in it, the two channels and the two roles."""
    evil = Role(EVIL, evil_position)
    staff = Role(STAFF, 40)
    warden_role = Role(WARDEN + 1, warden_position)
    report = Channel(REPORT)
    honeypot = Channel(HONEYPOT, history=honeypot_history, last_message_id=honeypot_last)
    me = Member(WARDEN, "warden", roles=[warden_role], perms=warden_perms or Perms(ban_members=True), bot=True)
    guild = Guild(GUILD, me=me, roles=[evil, staff, warden_role], channels=[report, honeypot])
    me.guild = guild
    for m in evil_holders:
        m.guild = guild
        evil.members.append(m)
    return guild, report, honeypot, evil, staff
