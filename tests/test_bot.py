"""warden's behaviour against fake Discord objects: every rule, every outcome."""

import asyncio
import json
import unittest

import discord

from tests import fakes
from tests.fakes import (EVIL, GUILD, HONEYPOT, SPAMMER, STAFF, WARDEN, Channel, Member, Message,
                         Perms, Role, User, config, forbidden, server)
from warden.bot import Warden, intents
from warden.store import Audit, State


def make(cfg=None, **server_kw):
    cfg = cfg or config()
    guild, report, honeypot, evil, staff = server(**server_kw)
    w = Warden(cfg, Audit(cfg.audit_log), State(cfg.state_file))
    w._guild = guild
    w.self_id = WARDEN
    return w, guild, report, honeypot, evil, staff


def run(coro):
    return asyncio.run(coro)


def audit_events(w):
    with open(w.audit.path) as f:
        return [json.loads(line) for line in f]


class HoneypotTest(unittest.TestCase):
    def test_a_post_bans_the_author_and_reports_it_without_mentions(self):
        w, guild, report, honeypot, *_ = make()
        spammer = Member(SPAMMER, guild=guild)
        run(w.message(Message(5001, spammer, honeypot, guild)))

        self.assertIn(SPAMMER, guild.banned)
        reason, seconds = guild.banned[SPAMMER]
        self.assertIn("honeypot", reason)
        self.assertEqual(seconds, 3600)
        self.assertEqual(len(report.sent), 1)
        text, mentions = report.sent[0]
        self.assertIn("🔨 Banned", text)
        self.assertFalse(mentions.everyone or mentions.users or mentions.roles)
        self.assertEqual(w.state.last_honeypot_message_id, 5001)
        self.assertEqual(audit_events(w)[-1]["status"], "banned")

    def test_dry_run_reports_but_never_bans(self):
        w, guild, report, honeypot, *_ = make(config(dry_run=True))
        run(w.message(Message(5001, Member(SPAMMER, guild=guild), honeypot, guild)))
        self.assertEqual(guild.banned, {})
        self.assertIn("Dry run, would ban", report.sent[0][0])

    def test_staff_are_exempt_and_reported(self):
        w, guild, report, honeypot, evil, staff = make()
        mod = Member(SPAMMER, roles=[staff], guild=guild)
        run(w.message(Message(5001, mod, honeypot, guild)))
        self.assertEqual(guild.banned, {})
        self.assertIn("Not banned", report.sent[0][0])
        self.assertIn(f"staff role {STAFF}", report.sent[0][0])

    def test_a_thread_inside_the_honeypot_counts(self):
        w, guild, report, honeypot, *_ = make()
        thread = Channel(7777, parent_id=HONEYPOT)
        run(w.message(Message(5001, Member(SPAMMER, guild=guild), thread, guild)))
        self.assertIn(SPAMMER, guild.banned)
        # Catch-up reads the channel, not threads, so a thread post does not move it.
        self.assertIsNone(w.state.last_honeypot_message_id)

    def test_other_channels_other_guilds_webhooks_and_itself_are_ignored(self):
        w, guild, report, honeypot, *_ = make()
        other_guild = fakes.Guild(123)
        run(w.message(Message(1, Member(SPAMMER, guild=guild), Channel(42), guild)))
        run(w.message(Message(2, Member(SPAMMER), honeypot, other_guild)))
        run(w.message(Message(3, Member(SPAMMER, guild=guild), honeypot, guild, webhook_id=9)))
        run(w.message(Message(4, guild.me, honeypot, guild)))
        self.assertEqual(guild.banned, {})
        self.assertEqual(report.sent, [])

    def test_five_posts_are_one_ban_and_one_report(self):
        w, guild, report, honeypot, *_ = make()
        spammer = Member(SPAMMER, guild=guild)
        for i in range(5):
            run(w.message(Message(5001 + i, spammer, honeypot, guild)))
        self.assertEqual(len(report.sent), 1)
        self.assertEqual([e["event"] for e in audit_events(w)].count("duplicate"), 4)
        # Every post still advances catch-up, so a restart does not replay them.
        self.assertEqual(w.state.last_honeypot_message_id, 5005)

    def test_an_author_who_already_left_is_banned_by_id(self):
        w, guild, report, honeypot, *_ = make()
        run(w.message(Message(5001, User(SPAMMER), honeypot, guild)))
        self.assertIn(SPAMMER, guild.banned)
        self.assertIn("no longer in the server", report.sent[0][0])

    def test_an_author_who_left_and_is_already_banned_is_reported_once(self):
        w, guild, report, honeypot, *_ = make()
        guild.banned[SPAMMER] = ("earlier", 0)
        run(w.message(Message(5001, User(SPAMMER), honeypot, guild)))
        self.assertIn("Already banned", report.sent[0][0])
        self.assertEqual(guild.banned[SPAMMER], ("earlier", 0))


class HoneypotDeletionTest(unittest.TestCase):
    def test_live_deletes_the_message_and_says_so(self):
        w, guild, report, honeypot, *_ = make()
        m = Message(5001, Member(SPAMMER, guild=guild), honeypot, guild)
        run(w.message(m))
        self.assertTrue(m.deleted)
        self.assertIn(SPAMMER, guild.banned)
        self.assertIn("Deleted the message.", report.sent[0][0])
        events = [e for e in audit_events(w) if e["event"] == "message_delete"]
        self.assertEqual(events[0]["status"], "deleted")

    def test_dry_run_deletes_nothing_and_says_it_would(self):
        w, guild, report, honeypot, *_ = make(config(dry_run=True))
        m = Message(5001, Member(SPAMMER, guild=guild), honeypot, guild)
        run(w.message(m))
        self.assertFalse(m.deleted)
        self.assertIn("Would delete the message.", report.sent[0][0])

    def test_an_exempt_authors_message_stays(self):
        w, guild, report, honeypot, evil, staff = make()
        m = Message(5001, Member(SPAMMER, roles=[staff], guild=guild), honeypot, guild)
        run(w.message(m))
        self.assertFalse(m.deleted)
        text = report.sent[0][0]
        self.assertIn("Not banned", text)
        for said in ("Deleted the message", "Would delete", "Could not delete", "already gone"):
            self.assertNotIn(said, text)

    def test_every_repeat_is_deleted_though_there_is_one_ban(self):
        w, guild, report, honeypot, *_ = make()
        spammer = Member(SPAMMER, guild=guild)
        posts = [Message(5001 + i, spammer, honeypot, guild) for i in range(5)]
        for m in posts:
            run(w.message(m))
        self.assertTrue(all(m.deleted for m in posts))
        self.assertEqual(len(report.sent), 1)

    def test_the_message_goes_even_when_the_ban_fails(self):
        w, guild, report, honeypot, *_ = make()
        guild.ban_error = forbidden()
        m = Message(5001, Member(SPAMMER, guild=guild), honeypot, guild)
        run(w.message(m))
        self.assertTrue(m.deleted)
        self.assertIn("FAILED to ban", report.sent[0][0])
        self.assertIn("Deleted the message.", report.sent[0][0])

    def test_a_refused_deletion_is_reported_and_the_ban_still_happens(self):
        w, guild, report, honeypot, *_ = make()
        m = Message(5001, Member(SPAMMER, guild=guild), honeypot, guild, delete_error=forbidden())
        run(w.message(m))
        self.assertIn(SPAMMER, guild.banned)
        self.assertIn("Could not delete the message: warden needs Manage Messages", report.sent[0][0])

    def test_a_message_already_gone_is_not_a_failure(self):
        w, guild, report, honeypot, *_ = make()
        m = Message(5001, Member(SPAMMER, guild=guild), honeypot, guild, delete_error=fakes.not_found())
        run(w.message(m))
        self.assertIn("The message was already gone.", report.sent[0][0])
        self.assertNotIn("Could not delete", report.sent[0][0])

    def test_catch_up_deletes_what_was_posted_while_down(self):
        w, guild, report, honeypot, *_ = make()
        w.state.advance_honeypot(4000)
        m = Message(4001, Member(SPAMMER, guild=guild), honeypot, guild)
        honeypot._history = [m]
        run(w.ready(guild, WARDEN))
        self.assertTrue(m.deleted)

    def test_startup_flags_missing_manage_messages(self):
        w, guild, report, honeypot, *_ = make()
        honeypot._perms = Perms(manage_messages=False)
        self.assertIn("warden cannot Manage Messages in the honeypot channel, so offending "
                      "messages will stay up", w.startup_problems(guild))


class DedupePerRuleTest(unittest.TestCase):
    """The case that went silent live: the owner posted in the honeypot, then
    claimed Evil Bot 21 s later, and the role claim produced nothing at all."""

    def test_honeypot_then_role_claim_reports_both(self):
        w, guild, report, honeypot, evil, staff = make()
        guild.owner_id = SPAMMER
        owner = Member(SPAMMER, guild=guild)
        run(w.message(Message(5001, owner, honeypot, guild)))
        run(w.member_update(owner, Member(SPAMMER, roles=[evil], guild=guild)))
        self.assertEqual(len(report.sent), 2)
        self.assertIn("posted in", report.sent[0][0])
        self.assertIn("holds the Evil Bot role, but they own the server", report.sent[1][0])
        self.assertNotIn("duplicate", [e["event"] for e in audit_events(w)])

    def test_role_claim_then_honeypot_reports_both(self):
        w, guild, report, honeypot, evil, staff = make(config(dry_run=True))
        holder = Member(SPAMMER, roles=[evil], guild=guild)
        run(w.member_join(holder))
        run(w.message(Message(5001, holder, honeypot, guild)))
        self.assertEqual(len(report.sent), 2)

    def test_the_same_rule_is_still_deduped(self):
        w, guild, report, honeypot, evil, staff = make()
        guild.ban_error = forbidden()  # stays a member, so a second claim could repeat
        before = Member(SPAMMER, guild=guild)
        after = Member(SPAMMER, roles=[evil], guild=guild)
        run(w.member_update(before, after))
        run(w.member_update(before, after))
        self.assertEqual(len(report.sent), 1)


class RoleTest(unittest.TestCase):
    def test_role_added_bans(self):
        w, guild, report, honeypot, evil, staff = make()
        before = Member(SPAMMER, guild=guild)
        after = Member(SPAMMER, roles=[evil], guild=guild)
        run(w.member_update(before, after))
        self.assertIn(SPAMMER, guild.banned)
        self.assertIn("Evil Bot", guild.banned[SPAMMER][0])
        self.assertIn("holds the Evil Bot role", report.sent[0][0])

    def test_unrelated_update_of_a_holder_does_nothing(self):
        w, guild, report, honeypot, evil, staff = make()
        before = Member(SPAMMER, roles=[evil], guild=guild)
        after = Member(SPAMMER, roles=[evil], guild=guild)
        run(w.member_update(before, after))
        self.assertEqual(guild.banned, {})

    def test_joining_with_the_role_bans(self):
        w, guild, report, honeypot, evil, staff = make()
        run(w.member_join(Member(SPAMMER, roles=[evil], guild=guild)))
        self.assertIn(SPAMMER, guild.banned)

    def test_a_target_above_warden_is_reported_failed_and_not_attempted(self):
        w, guild, report, honeypot, evil, staff = make()
        high = Role(555, 99)
        member = Member(SPAMMER, roles=[evil, high], guild=guild)
        run(w.member_join(member))
        self.assertEqual(guild.banned, {})
        self.assertIn("FAILED", report.sent[0][0])
        self.assertIn("move warden's role above it", report.sent[0][0])

    def test_discord_refusing_is_reported_failed(self):
        w, guild, report, honeypot, evil, staff = make()
        guild.ban_error = forbidden()
        run(w.member_join(Member(SPAMMER, roles=[evil], guild=guild)))
        self.assertIn("FAILED", report.sent[0][0])
        self.assertIn("Ban Members", report.sent[0][0])
        self.assertEqual(audit_events(w)[-1]["status"], "failed")


class StartupTest(unittest.TestCase):
    def test_startup_announces_mode_and_setup_problems(self):
        cfg = config(dry_run=True)
        w, guild, report, honeypot, *_ = make(cfg, warden_perms=Perms(ban_members=False), evil_position=60)
        run(w.ready(guild, WARDEN))
        start = report.sent[0][0]
        self.assertIn("DRY RUN", start)
        self.assertIn("does not have Ban Members", start)
        self.assertIn("not above the Evil Bot role", start)

    def test_startup_flags_channels_warden_cannot_see(self):
        # The case Discord's channel cache hides: the channel exists, warden is
        # in the server, and warden still cannot see it.
        w, guild, report, honeypot, *_ = make(config(dry_run=True))
        honeypot._perms = Perms(view_channel=False, read_message_history=False)
        report._perms = Perms(view_channel=False)
        problems = w.startup_problems(guild)
        self.assertIn("warden cannot View the honeypot channel, so Discord never sends it "
                      "those messages and the honeypot rule cannot fire", problems)
        self.assertIn("warden cannot View the report channel, so no report can be posted", problems)
        self.assertTrue(any("Read Message History" in p for p in problems))

    def test_a_clean_setup_has_no_problems(self):
        w, guild, *_ = make()
        self.assertEqual(w.startup_problems(guild), [])

    def test_startup_sweeps_existing_role_holders(self):
        holders = [Member(SPAMMER + i) for i in range(3)]
        w, guild, report, honeypot, evil, staff = make(evil_holders=holders)
        for m in holders:
            m.roles.append(evil)
        run(w.ready(guild, WARDEN))
        self.assertEqual(set(guild.banned), {m.id for m in holders})

    def test_first_run_sets_a_baseline_and_acts_on_no_old_history(self):
        old = [Message(4000, Member(SPAMMER), None, None)]
        w, guild, report, honeypot, *_ = make(honeypot_history=old, honeypot_last=4000)
        run(w.ready(guild, WARDEN))
        self.assertEqual(guild.banned, {})
        self.assertEqual(w.state.last_honeypot_message_id, 4000)

    def test_restart_catches_up_on_what_was_posted_while_down(self):
        cfg = config()
        w, guild, report, honeypot, *_ = make(cfg)
        w.state.advance_honeypot(4000)
        a, b = Member(SPAMMER, guild=guild), Member(SPAMMER + 1, guild=guild)
        honeypot._history = [Message(3999, Member(SPAMMER + 9, guild=guild), honeypot, guild),
                             Message(4001, a, honeypot, guild),
                             Message(4002, b, honeypot, guild)]
        run(w.ready(guild, WARDEN))
        self.assertEqual(set(guild.banned), {a.id, b.id})  # 3999 predates the stored id
        self.assertEqual(honeypot.history_calls, [4000])
        self.assertEqual(w.state.last_honeypot_message_id, 4002)

        # A second ready (a reconnect) finds nothing new and posts no second start notice.
        before = len(report.sent)
        run(w.ready(guild, WARDEN))
        self.assertEqual(len(report.sent), before)


class IntentsTest(unittest.TestCase):
    def test_least_privilege_no_message_content(self):
        i = intents()
        self.assertTrue(i.guilds and i.members and i.guild_messages)
        self.assertFalse(i.message_content)
        self.assertFalse(i.presences)


if __name__ == "__main__":
    unittest.main()
