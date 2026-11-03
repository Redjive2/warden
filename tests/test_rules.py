import unittest
from datetime import datetime, timezone

from tests.fakes import STAFF, WARDEN, config
from warden.rules import HONEYPOT, ROLE, Target, Trigger, ban_reason, created_at, decide

SELF = WARDEN


def target(**kw):
    values = dict(user_id=1566950000000000000, name="spammer")
    values.update(kw)
    return Target(**values)


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config(exempt_user_ids=frozenset({77}), exempt_bot_ids=frozenset({88}))

    def test_a_plain_member_is_banned_for_either_rule(self):
        for kind in (ROLE, HONEYPOT):
            d = decide(self.cfg, target(), Trigger(kind, 5 if kind == HONEYPOT else None), SELF)
            self.assertEqual(d.action, "ban", kind)

    def test_every_exemption(self):
        cases = {
            "it is warden itself": target(user_id=SELF),
            "they own the server": target(is_owner=True),
            "they are listed in exempt.users": target(user_id=77),
            "they are listed in exempt.bots": target(user_id=88, is_bot=True),
            "they have Administrator": target(administrator=True),
            "they have Manage Server": target(manage_guild=True),
            "they have Ban Members": target(ban_members=True),
            f"they hold staff role {STAFF}": target(role_ids=frozenset({STAFF, 5})),
        }
        for why, t in cases.items():
            d = decide(self.cfg, t, Trigger(HONEYPOT, 1), SELF)
            self.assertEqual((d.action, d.why), ("exempt", why))

    def test_an_unlisted_bot_is_not_exempt(self):
        # exempt.bots is an allow-list: a bot account not on it is banned like anyone.
        d = decide(self.cfg, target(user_id=89, is_bot=True), Trigger(HONEYPOT, 1), SELF)
        self.assertEqual(d.action, "ban")

    def test_exemption_wins_over_outranking(self):
        d = decide(self.cfg, target(administrator=True, outranks_warden=True), Trigger(ROLE), SELF)
        self.assertEqual(d.action, "exempt")

    def test_a_target_above_warden_is_refused_up_front_with_the_fix(self):
        d = decide(self.cfg, target(outranks_warden=True), Trigger(ROLE), SELF)
        self.assertEqual(d.action, "refuse")
        self.assertIn("move warden's role above it", d.why)

    def test_unknown_trigger_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            decide(self.cfg, target(), Trigger("vibes"), SELF)

    def test_ban_reasons_name_the_rule(self):
        self.assertIn("Evil Bot", ban_reason(Trigger(ROLE)))
        self.assertIn("message 42", ban_reason(Trigger(HONEYPOT, 42)))
        self.assertLessEqual(len(ban_reason(Trigger(HONEYPOT, 2 ** 63))), 512)


class CreatedAtTest(unittest.TestCase):
    def test_reads_the_time_out_of_a_discord_id(self):
        # The example from Discord's documentation on snowflakes.
        self.assertEqual(
            created_at(175928847299117063),
            datetime(2016, 4, 30, 11, 18, 25, 796000, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
