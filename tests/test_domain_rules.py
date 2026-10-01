"""领域纯规则测试：权限、政策时点适用、承担方裁决、预警解释。"""

import unittest

from src import domain


class PermissionTest(unittest.TestCase):
    def test_role_matrix(self):
        self.assertTrue(domain.can("market_regulator", "merchant.suspend"))
        self.assertTrue(domain.can("culture_tourism", "merchant.suspend"))
        self.assertFalse(domain.can("organizer", "merchant.suspend"))
        self.assertFalse(domain.can("duty_staff", "refund.approve"))
        self.assertTrue(domain.can("merchant", "redemption.redeem"))

    def test_require_permission_raises(self):
        with self.assertRaises(domain.PermissionDenied) as ctx:
            domain.require_permission("organizer", "refund.approve")
        self.assertEqual(ctx.exception.http_status, 403)


class PolicyPointInTimeTest(unittest.TestCase):
    POLICIES = [
        {"policy_id": "p", "version": 1, "effective_at": "2026-01-01T00:00:00+08:00"},
        {"policy_id": "p", "version": 2, "effective_at": "2026-09-01T00:00:00+08:00"},
    ]

    def test_before_change_uses_old_version(self):
        d = domain.applicable_policy(self.POLICIES, "2026-08-31T23:59:59+08:00")
        self.assertEqual(d.version, 1)

    def test_after_change_uses_new_version(self):
        d = domain.applicable_policy(self.POLICIES, "2026-09-01T00:00:00+08:00")
        self.assertEqual(d.version, 2)
        d2 = domain.applicable_policy(self.POLICIES, "2026-10-03T22:00:00+08:00")
        self.assertEqual(d2.version, 2)

    def test_too_early_is_rejected(self):
        with self.assertRaises(domain.StateError):
            domain.applicable_policy(self.POLICIES, "2025-12-31T23:59:59+08:00")


class BearerDecisionTest(unittest.TestCase):
    def test_merchant_violations(self):
        for cause in ("price_surge", "package_unredeemable",
                      "venue_early_close", "clustered_rejection"):
            self.assertEqual(
                domain.decide_bearer(root_cause=cause), domain.Bearer.MERCHANT)

    def test_reschedule_normal_weather_is_organizer(self):
        self.assertEqual(
            domain.decide_bearer(root_cause="event_reschedule",
                                 weather_level="normal"),
            domain.Bearer.ORGANIZER)

    def test_extreme_weather_goes_force_majeure(self):
        self.assertEqual(
            domain.decide_bearer(root_cause="event_cancel",
                                 weather_level="extreme"),
            domain.Bearer.FORCE_MAJEURE)

    def test_appeal_upheld_bears_none(self):
        self.assertEqual(
            domain.decide_bearer(root_cause="appeal_upheld"), domain.Bearer.NONE)


class AlertExplanationTest(unittest.TestCase):
    def test_price_spike_explanation_is_interpretable(self):
        ctx = domain.explain_price_spike(
            event_id="e1", merchant_id="m1", sector="lodging",
            old_price=400, new_price=700, commitment_id="c1",
            observed_at="2026-10-03T22:30:00+08:00")
        self.assertEqual(ctx.alert_type, domain.AlertType.PRICE_SPIKE)
        self.assertEqual(ctx.severity, "critical")
        self.assertIn("75.0%", ctx.title)
        self.assertIn("c1", ctx.related)
        self.assertGreaterEqual(ctx.evidence["increase_ratio"],
                                domain.PRICE_SPIKE_RATIO)

    def test_clustered_rejection_lists_orders(self):
        ctx = domain.explain_clustered_rejection(
            event_id="e1", merchant_id="m1", order_refs=["o1", "o2", "o3"],
            window_minutes=60, observed_at="t")
        self.assertEqual(ctx.evidence["count"], 3)
        self.assertEqual(ctx.related, ["o1", "o2", "o3"])

    def test_benefit_invalidation_weather_wording(self):
        ctx = domain.explain_benefit_invalidation(
            event_id="e1", benefit_code="FAN", reason="台风改期",
            weather_level="extreme", valid_from="a", valid_to="b",
            unredeemed=12)
        self.assertIn("极端气象", ctx.explanation)
        self.assertEqual(ctx.evidence["unredeemed_count"], 12)


if __name__ == "__main__":
    unittest.main()
