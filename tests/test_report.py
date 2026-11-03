import asyncio
import unittest
from datetime import datetime, timezone

from tests.fakes import HONEYPOT
from warden.report import DISCORD_LIMIT, Reporter, format_outcome, format_summary, safe_name
from warden.rules import HONEYPOT as HP, ROLE, Outcome, Target, Trigger

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
UID = 1566950000000000000  # created 2026-11-02, 13 h before NOW


def outcome(status="banned", name="spammer", kind=HP, **kw):
    t = Target(user_id=UID, name=name, joined_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    return Outcome(status, t, Trigger(kind, 555 if kind == HP else None), **kw)


class FormatTest(unittest.TestCase):
    def test_names_cannot_mention_or_format(self):
        s = safe_name("@everyone **bold** <@123>")
        self.assertNotIn("@everyone", s)
        self.assertNotIn("<@123>", s)
        self.assertNotIn("**bold**", s)
        self.assertIn("everyone", s)

    def test_each_status_reads_plainly(self):
        banned = format_outcome(outcome(delete_seconds=3600), HONEYPOT, NOW)
        self.assertIn("🔨 Banned **spammer**", banned)
        self.assertIn(f"posted in <#{HONEYPOT}> (message `555`)", banned)
        self.assertIn("Account created 2026-11-02 (13 h old)", banned)
        self.assertIn("joined 2026-11-02", banned)
        self.assertIn("Deleted their last 1h of messages.", banned)

        self.assertIn("🧪 Dry run, would ban", format_outcome(outcome("dry_run"), HONEYPOT, NOW))
        self.assertIn("holds the Evil Bot role", format_outcome(outcome("dry_run", kind=ROLE), HONEYPOT, NOW))
        exempt = format_outcome(outcome("exempt", detail="they have Administrator"), HONEYPOT, NOW)
        self.assertIn("🛡️ Not banned", exempt)
        self.assertIn("but they have Administrator.", exempt)
        failed = format_outcome(outcome("failed", detail="Discord refused."), HONEYPOT, NOW)
        self.assertIn("⚠️ FAILED to ban", failed)
        self.assertIn("Discord refused.", failed)
        self.assertIn("ℹ️ Already banned", format_outcome(outcome("already_banned"), HONEYPOT, NOW))

    def test_departed_users_are_said_to_have_left(self):
        o = Outcome("banned", Target(user_id=UID, name="x", is_member=False), Trigger(HP, 1))
        self.assertIn("no longer in the server", format_outcome(o, HONEYPOT, NOW))

    def test_summary_tallies_and_fits_discords_limit(self):
        many = [outcome() for _ in range(40)] + [outcome("failed", detail="x")]
        messages = format_summary(many, HONEYPOT, NOW)
        self.assertTrue(messages[0].startswith("📋 41 actions in one burst: 40 banned, 1 FAILED."))
        self.assertGreater(len(messages), 1)
        for m in messages:
            self.assertLessEqual(len(m), DISCORD_LIMIT)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class ReporterTest(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_one_outcome_is_posted_at_once(self):
        sent = []

        async def send(t):
            sent.append(t)

        async def go():
            clock = FakeClock()
            r = Reporter(send, 30, HONEYPOT, clock=clock, sleep=clock.sleep)
            await r.report(outcome())
            self.assertEqual(len(sent), 1)
        self.run_async(go())

    def test_a_burst_becomes_one_summary_after_the_window(self):
        sent = []

        async def send(t):
            sent.append(t)

        async def go():
            clock = FakeClock()
            r = Reporter(send, 30, HONEYPOT, clock=clock, sleep=clock.sleep)
            for _ in range(5):
                await r.report(outcome())
                clock.now += 1
            self.assertEqual(len(sent), 1)         # the first, at once
            await r._flusher                        # the window closes
            self.assertEqual(len(sent), 2)
            self.assertTrue(sent[1].startswith("📋 4 actions in one burst"))
        self.run_async(go())

    def test_quiet_after_a_window_posts_at_once_again(self):
        sent = []

        async def send(t):
            sent.append(t)

        async def go():
            clock = FakeClock()
            r = Reporter(send, 30, HONEYPOT, clock=clock, sleep=clock.sleep)
            await r.report(outcome())
            clock.now += 31
            await r.report(outcome())
            self.assertEqual(len(sent), 2)
        self.run_async(go())

    def test_batch_zero_posts_everything_at_once(self):
        sent = []

        async def send(t):
            sent.append(t)

        async def go():
            r = Reporter(send, 0, HONEYPOT)
            for _ in range(3):
                await r.report(outcome())
            self.assertEqual(len(sent), 3)
        self.run_async(go())

    def test_close_flushes_what_is_pending(self):
        sent = []

        async def send(t):
            sent.append(t)

        async def go():
            clock = FakeClock()
            r = Reporter(send, 30, HONEYPOT, clock=clock, sleep=clock.sleep)
            await r.report(outcome())
            await r.report(outcome("dry_run"))
            await r.close()
            self.assertEqual(len(sent), 2)
            self.assertIn("Dry run", sent[1])
        self.run_async(go())


if __name__ == "__main__":
    unittest.main()
