"""Reports to the report channel.

Formatting is pure. The Reporter batches: the first outcome after a quiet period
is posted at once, and outcomes arriving inside the next batch window are posted
together as one summary when the window closes. One ban reads as one message; a
raid of fifty reads as a handful.

Every post is made with mentions disabled by the caller (see bot.py). On top of
that, names are escaped here, so a hostile display name renders as text rather
than as formatting or a mention.
"""

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

from .rules import HONEYPOT, ROLE, Outcome, created_at

DISCORD_LIMIT = 2000

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#-])")


def safe_name(name: str) -> str:
    """A display name as inert text: markdown escaped, mentions broken."""
    name = " ".join(str(name).split())[:64] or "?"
    name = _MARKDOWN.sub(r"\\\1", name)
    # A zero-width space after @ and inside <...> stops them parsing as mentions.
    return name.replace("@", "@​").replace("<", "<​")


def _ago(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    days = seconds // 86400
    return f"{days} day" + ("" if days == 1 else "s")


def _window(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def _trigger(o: Outcome, honeypot_channel_id: int) -> str:
    if o.trigger.kind == ROLE:
        return "holds the Evil Bot role"
    if o.trigger.kind == HONEYPOT:
        where = f"posted in <#{honeypot_channel_id}>"
        if o.trigger.message_id is not None:
            where += f" (message `{o.trigger.message_id}`)"
        return where
    return o.trigger.kind


def _who(o: Outcome) -> str:
    return f"**{safe_name(o.target.name)}** (`{o.target.user_id}`)"


def _account(o: Outcome, now: datetime) -> str:
    made = created_at(o.target.user_id)
    text = f"Account created {made:%Y-%m-%d} ({_ago(made, now)} old)"
    if not o.target.is_member:
        return text + "; no longer in the server"
    if o.target.joined_at is not None:
        return text + f"; joined {o.target.joined_at:%Y-%m-%d}"
    return text


def _message(o: Outcome) -> str:
    """What happened to the honeypot message itself, as a trailing sentence."""
    d = o.message_deletion
    if d is None:
        return ""
    if d == "deleted":
        return " Deleted the message."
    if d == "already_gone":
        return " The message was already gone."
    if d == "would_delete":
        return " Would delete the message."
    if d.startswith("failed: "):
        return f" ⚠️ Could not delete the message: {d[len('failed: '):]}"
    return f" Message: {d}."


def format_outcome(o: Outcome, honeypot_channel_id: int, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    trig = _trigger(o, honeypot_channel_id)
    msg = _message(o)
    if o.status == "banned":
        text = f"🔨 Banned {_who(o)}: {trig}. {_account(o, now)}."
        if o.delete_seconds:
            text += f" Deleted their last {_window(o.delete_seconds)} of messages."
        return text + msg
    if o.status == "dry_run":
        return f"🧪 Dry run, would ban {_who(o)}: {trig}. {_account(o, now)}." + msg
    if o.status == "exempt":
        return f"🛡️ Not banned: {_who(o)} {trig}, but {o.detail}." + msg
    if o.status == "already_banned":
        return f"ℹ️ Already banned: {_who(o)}: {trig}." + msg
    if o.status == "failed":
        return f"⚠️ FAILED to ban {_who(o)}: {trig}. {o.detail}" + msg
    return f"{o.status}: {_who(o)}: {trig}. {o.detail}" + msg


def format_summary(outcomes: List[Outcome], honeypot_channel_id: int,
                   now: Optional[datetime] = None) -> List[str]:
    """One summary for a batch, split into Discord-sized messages."""
    counts = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    order = ["banned", "dry_run", "failed", "exempt", "already_banned"]
    names = {"banned": "banned", "dry_run": "would ban (dry run)", "failed": "FAILED",
             "exempt": "exempt", "already_banned": "already banned"}
    tally = ", ".join(f"{counts[s]} {names[s]}" for s in order if s in counts)
    head = f"📋 {len(outcomes)} actions in one burst: {tally}."
    lines = [format_outcome(o, honeypot_channel_id, now) for o in outcomes]

    messages: List[str] = []
    current = head
    for line in lines:
        line = line[: DISCORD_LIMIT - 10]
        if len(current) + 1 + len(line) > DISCORD_LIMIT:
            messages.append(current)
            current = line
        else:
            current += "\n" + line
    messages.append(current)
    return messages


Send = Callable[[str], Awaitable[None]]


class Reporter:
    """Posts outcomes, batching bursts. send() is given finished text."""

    def __init__(self, send: Send, batch_seconds: float, honeypot_channel_id: int,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._send = send
        self._batch = batch_seconds
        self._honeypot = honeypot_channel_id
        self._clock = clock
        self._sleep = sleep
        self._pending: List[Outcome] = []
        self._window_end: Optional[float] = None
        self._flusher: Optional[asyncio.Task] = None

    async def report(self, outcome: Outcome) -> None:
        if self._batch <= 0:
            await self._send(format_outcome(outcome, self._honeypot))
            return
        now = self._clock()
        if self._window_end is None or now >= self._window_end:
            # Quiet before this one: post it now, and open a window behind it.
            self._window_end = now + self._batch
            await self._send(format_outcome(outcome, self._honeypot))
            return
        self._pending.append(outcome)
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.ensure_future(self._flush_at_window_end())

    async def _flush_at_window_end(self) -> None:
        while self._window_end is not None:
            delay = self._window_end - self._clock()
            if delay > 0:
                await self._sleep(delay)
            batch, self._pending = self._pending, []
            if not batch:
                self._window_end = None
                return
            # Keep the window open behind the summary, so a raid that is still
            # going produces one summary per window, not one per ban.
            self._window_end = self._clock() + self._batch
            await self._post(batch)

    async def _post(self, batch: List[Outcome]) -> None:
        if len(batch) == 1:
            await self._send(format_outcome(batch[0], self._honeypot))
            return
        for text in format_summary(batch, self._honeypot):
            await self._send(text)

    async def close(self) -> None:
        """Post anything still pending, now. For shutdown."""
        if self._flusher is not None and not self._flusher.done():
            self._flusher.cancel()
        batch, self._pending = self._pending, []
        self._window_end = None
        if batch:
            await self._post(batch)
