"""The Discord side: events in, bans and reports out.

`Warden` holds the behaviour and takes plain objects (a guild, a member, a
message), so tests drive it with fakes. `WardenClient` is the thin discord.py
client that forwards gateway events to it.

Intents are the least that works: guilds, members and guild messages. Not
Message Content. A ban needs the author's id, never what they wrote, and the
honeypot channel is the one place hostile text is guaranteed.
"""

import sys
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

import discord

from . import __version__, rules
from .config import Config
from .report import Reporter
from .rules import HONEYPOT, ROLE, Outcome, Target, Trigger
from .store import Audit, State

# A rule that just fired for a user does not fire again for them inside this
# window. A spammer posting five times in the honeypot is one ban and one report,
# not five. Per rule, so a role claim right after a honeypot post still gets its
# own outcome and report. Short, so someone unbanned by a moderator who
# misbehaves again is still caught.
DEDUPE_SECONDS = 600

# Catch-up reads at most this many honeypot messages after a restart.
CATCHUP_LIMIT = 1000


def _is_member(user: Any) -> bool:
    return hasattr(user, "roles") and hasattr(user, "guild_permissions")


def target_for(guild: Any, user: Any) -> Target:
    """A rules.Target from a Discord member, or from a user who has left."""
    name = getattr(user, "name", None) or str(user)
    is_bot = bool(getattr(user, "bot", False))
    if not _is_member(user):
        return Target(user_id=user.id, name=name, is_bot=is_bot, is_member=False)
    perms = user.guild_permissions
    me = getattr(guild, "me", None)
    outranks = False
    if me is not None and getattr(me, "top_role", None) is not None:
        outranks = not (me.top_role > user.top_role)
    return Target(
        user_id=user.id,
        name=name,
        is_bot=is_bot,
        is_member=True,
        is_owner=getattr(guild, "owner_id", None) == user.id,
        role_ids=frozenset(r.id for r in user.roles),
        administrator=bool(perms.administrator),
        manage_guild=bool(perms.manage_guild),
        ban_members=bool(perms.ban_members),
        joined_at=getattr(user, "joined_at", None),
        outranks_warden=outranks,
    )


class Warden:
    def __init__(self, cfg: Config, audit: Audit, state: State,
                 clock=time.monotonic, reporter: Optional[Reporter] = None):
        self.cfg = cfg
        self.audit = audit
        self.state = state
        self.clock = clock
        self.self_id: int = 0
        self._guild: Any = None
        self._recent: Dict[int, float] = {}
        self._started = False
        self.reporter = reporter or Reporter(self._send, cfg.batch_seconds, cfg.honeypot_channel_id)

    # -- reporting -----------------------------------------------------------

    async def _send(self, text: str) -> None:
        channel = self._guild.get_channel(self.cfg.report_channel_id) if self._guild else None
        if channel is None:
            self.audit.write("report_failed", reason="report channel not found", text=text)
            print(f"warden: report channel not found; report was: {text}", file=sys.stderr)
            return
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            self.audit.write("report_failed", reason=str(exc), text=text)
            print(f"warden: could not post report ({exc}); report was: {text}", file=sys.stderr)

    async def _report(self, outcome: Outcome) -> None:
        self.audit.outcome(outcome, self.cfg.dry_run)
        await self.reporter.report(outcome)

    # -- the one path every rule goes through --------------------------------

    def _seen_recently(self, user_id: int, kind: str) -> bool:
        """True when this rule fired for this user inside DEDUPE_SECONDS.

        Keyed on (user, rule), not the user alone. Keyed on the user, a role
        claim inside ten minutes of a honeypot hit (or the other way round) was
        swallowed as a duplicate, with no outcome and no report.
        """
        now = self.clock()
        for key, at in list(self._recent.items()):
            if now - at > DEDUPE_SECONDS:
                del self._recent[key]
        key = (user_id, kind)
        if key in self._recent:
            return True
        self._recent[key] = now
        return False

    async def handle(self, guild: Any, user: Any, trigger: Trigger,
                     message_deletion: Optional[str] = None) -> Optional[Outcome]:
        """Decide and act on one rule hit. None when it was a recent duplicate.

        message_deletion is what already happened to the honeypot message, if
        any; it rides along on the outcome so the report says it.
        """
        if self._seen_recently(user.id, trigger.kind):
            self.audit.write("duplicate", user_id=str(user.id), trigger=trigger.kind,
                             message_id=None if trigger.message_id is None else str(trigger.message_id))
            return None

        target = target_for(guild, user)
        decision = rules.decide(self.cfg, target, trigger, self.self_id)
        seconds = self.cfg.delete_message_seconds

        if decision.action == "exempt":
            outcome = Outcome("exempt", target, trigger, detail=decision.why)
        elif decision.action == "refuse":
            outcome = Outcome("failed", target, trigger, detail=decision.why)
        elif self.cfg.dry_run:
            outcome = Outcome("dry_run", target, trigger, delete_seconds=seconds)
        else:
            outcome = await self._ban(guild, target, trigger, decision.why, seconds)

        if message_deletion is not None:
            outcome = replace(outcome, message_deletion=message_deletion)
        await self._report(outcome)
        return outcome

    async def _delete_message(self, message: Any) -> str:
        """Delete one honeypot message. Dry run deletes nothing and says so."""
        if self.cfg.dry_run:
            status = "would_delete"
        else:
            try:
                await message.delete()
                status = "deleted"
            except discord.NotFound:
                # Gone already: the ban's history purge, or a moderator, got there first.
                status = "already_gone"
            except discord.Forbidden:
                status = "failed: warden needs Manage Messages in the honeypot channel"
            except discord.HTTPException as exc:
                status = f"failed: Discord error {exc.status}: {exc.text or exc}"
        self.audit.write("message_delete", message_id=str(message.id),
                         user_id=str(message.author.id), status=status)
        return status

    async def _ban(self, guild: Any, target: Target, trigger: Trigger, reason: str,
                   seconds: int) -> Outcome:
        if not target.is_member:
            try:
                await guild.fetch_ban(discord.Object(id=target.user_id))
                return Outcome("already_banned", target, trigger)
            except discord.NotFound:
                pass
            except discord.HTTPException:
                pass  # Can't tell; try the ban, which reports its own failure.
        try:
            await guild.ban(discord.Object(id=target.user_id), reason=reason,
                            delete_message_seconds=seconds)
        except discord.Forbidden:
            return Outcome("failed", target, trigger, detail=(
                "Discord refused: warden's role needs Ban Members, and must sit above "
                "every role this user holds."))
        except discord.NotFound:
            return Outcome("failed", target, trigger, detail="Discord no longer knows this user.")
        except discord.HTTPException as exc:
            return Outcome("failed", target, trigger,
                           detail=f"Discord error {exc.status}: {exc.text or exc}")
        return Outcome("banned", target, trigger, delete_seconds=seconds)

    # -- events ---------------------------------------------------------------

    def _evil(self, member: Any) -> bool:
        return any(r.id == self.cfg.evil_role_id for r in getattr(member, "roles", []))

    def _in_honeypot(self, channel: Any) -> bool:
        if channel is None:
            return False
        if channel.id == self.cfg.honeypot_channel_id:
            return True
        # A thread inside the honeypot channel counts as the honeypot.
        return getattr(channel, "parent_id", None) == self.cfg.honeypot_channel_id

    async def member_update(self, before: Any, after: Any) -> None:
        if self._evil(after) and not self._evil(before):
            await self.handle(after.guild, after, Trigger(ROLE))

    async def member_join(self, member: Any) -> None:
        if self._evil(member):
            await self.handle(member.guild, member, Trigger(ROLE))

    async def message(self, message: Any) -> None:
        if message.guild is None or message.guild.id != self.cfg.guild_id:
            return
        if not self._in_honeypot(message.channel):
            return
        if message.author.id == self.self_id:
            return
        if getattr(message, "webhook_id", None):
            # A webhook is not a user and cannot be banned.
            self.audit.write("skipped_webhook", message_id=str(message.id))
            return
        # Every offending message goes, including repeats that the per-user
        # dedupe keeps from being a second ban, and including when the ban
        # fails. An exempt author's message stays: staff post the channel's
        # warning there.
        deletion = None
        target = target_for(message.guild, message.author)
        if rules.exemption(self.cfg, target, self.self_id) is None:
            deletion = await self._delete_message(message)
        await self.handle(message.guild, message.author, Trigger(HONEYPOT, message.id),
                          message_deletion=deletion)
        if message.channel.id == self.cfg.honeypot_channel_id:
            self.state.advance_honeypot(message.id)

    # -- startup --------------------------------------------------------------

    def startup_problems(self, guild: Any) -> List[str]:
        """What is wrong with the server setup, in words a person can act on."""
        problems: List[str] = []
        me = guild.me
        report = guild.get_channel(self.cfg.report_channel_id)
        honeypot = guild.get_channel(self.cfg.honeypot_channel_id)
        role = guild.get_role(self.cfg.evil_role_id)
        # A channel being in the cache says nothing about whether warden can see
        # it: Discord sends every channel of a server, visible or not. Visibility
        # has to be read from the permissions.
        if report is None:
            problems.append(f"report channel {self.cfg.report_channel_id} not found")
        else:
            perms = report.permissions_for(me)
            if not perms.view_channel:
                problems.append("warden cannot View the report channel, so no report can be posted")
            elif not perms.send_messages:
                problems.append("warden cannot Send Messages in the report channel")
        if honeypot is None:
            problems.append(f"honeypot channel {self.cfg.honeypot_channel_id} not found")
        else:
            perms = honeypot.permissions_for(me)
            if not perms.view_channel:
                problems.append("warden cannot View the honeypot channel, so Discord never sends it "
                                "those messages and the honeypot rule cannot fire")
            if not perms.read_message_history:
                problems.append("warden cannot Read Message History in the honeypot channel, so restart catch-up cannot work")
            if not perms.manage_messages:
                problems.append("warden cannot Manage Messages in the honeypot channel, so offending messages will stay up")
        if role is None:
            problems.append(f"Evil Bot role {self.cfg.evil_role_id} not found")
        elif not (me.top_role > role):
            problems.append("warden's role is not above the Evil Bot role, so Discord will refuse those bans")
        if not me.guild_permissions.ban_members:
            problems.append("warden does not have Ban Members, so every ban will fail")
        return problems

    async def ready(self, guild: Any, self_id: int) -> None:
        """Run on every gateway ready: sweep the role, catch up the honeypot."""
        self._guild = guild
        self.self_id = self_id
        if not self._started:
            self._started = True
            problems = self.startup_problems(guild)
            mode = ("DRY RUN: nothing is banned; reports say 'would ban'" if self.cfg.dry_run
                    else "LIVE: bans are on")
            text = f"🟢 warden {__version__} started, {mode}."
            if problems:
                text += "\n⚠️ Setup problems:\n" + "\n".join(f"- {p}" for p in problems)
            self.audit.write("started", version=__version__, dry_run=self.cfg.dry_run, problems=problems)
            await self._send(text)
        await self.sweep_role(guild)
        await self.catch_up(guild)

    async def sweep_role(self, guild: Any) -> int:
        role = guild.get_role(self.cfg.evil_role_id)
        if role is None:
            return 0
        holders = list(role.members)
        for member in holders:
            await self.handle(guild, member, Trigger(ROLE))
        self.audit.write("role_sweep", holders=len(holders))
        return len(holders)

    async def catch_up(self, guild: Any) -> int:
        """Act on honeypot messages posted while warden was down."""
        channel = guild.get_channel(self.cfg.honeypot_channel_id)
        if channel is None:
            return 0
        last = self.state.last_honeypot_message_id
        if last is None:
            # First run: set a baseline instead of acting on old history. Whoever
            # posted there before warden existed was never warned by it.
            latest = getattr(channel, "last_message_id", None)
            if latest:
                self.state.advance_honeypot(latest)
            self.audit.write("catchup_baseline", message_id=None if latest is None else str(latest))
            return 0
        handled = 0
        async for message in channel.history(after=discord.Object(id=last), oldest_first=True,
                                             limit=CATCHUP_LIMIT):
            await self.message(message)
            handled += 1
        self.audit.write("catchup", messages=handled)
        return handled


def intents() -> discord.Intents:
    i = discord.Intents.none()
    i.guilds = True
    i.members = True          # role events, join events, the startup sweep
    i.guild_messages = True   # honeypot posts; author ids only, no content intent
    return i


class WardenClient(discord.Client):
    def __init__(self, warden: Warden):
        super().__init__(intents=intents(), allowed_mentions=discord.AllowedMentions.none())
        self.warden = warden

    async def on_ready(self) -> None:
        guild = self.get_guild(self.warden.cfg.guild_id)
        if guild is None:
            print(f"warden: not in guild {self.warden.cfg.guild_id}; invite the bot first", file=sys.stderr)
            self.warden.audit.write("fatal", reason="not in guild")
            await self.close()
            return
        await self.warden.ready(guild, self.user.id)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.guild.id == self.warden.cfg.guild_id:
            await self.warden.member_update(before, after)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id == self.warden.cfg.guild_id:
            await self.warden.member_join(member)

    async def on_message(self, message: discord.Message) -> None:
        await self.warden.message(message)

    async def close(self) -> None:
        try:
            await self.warden.reporter.close()
        finally:
            await super().close()
