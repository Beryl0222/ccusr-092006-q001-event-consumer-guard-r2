"""规则引擎纯函数测试：阈值、时效、承担方推导。"""

import unittest

import support  # noqa: F401

from guard.engine import (
    bearer_for,
    evaluate_early_closure,
    evaluate_mass_rejection,
    evaluate_price_spike,
    resolve_policy_version,
)

RULES = {
    "price_spike_ratio": 0.20,
    "mass_rejection_count": 5,
    "mass_rejection_window_minutes": 60,
    "mass_rejection_ratio": 0.50,
    "early_closure_grace_minutes": 30,
    "bearer_map": {
        "PRICE_GOUGE": {"REFUND": ["MERCHANT", "self"], "COMPENSATION": ["MERCHANT", "self"]},
        "EARLY_CLOSURE": {"REFUND": ["MERCHANT", "self"],
                          "COMPENSATION": ["GOVERNMENT_FUND", "district-relief"]},
        "EVENT_CANCELLED": {"REFUND": ["ORGANIZER", "event-risk-reserve"]},
    },
}


class PriceSpikeTest(unittest.TestCase):
    VF, VT = "2026-10-03T12:00:00+08:00", "2026-10-05T12:00:00+08:00"

    def test_over_threshold_flags_with_explanation(self):
        f = evaluate_price_spike(30000, 45000, RULES,
                                 commitment_valid_from=self.VF, commitment_valid_to=self.VT,
                                 quoted_at="2026-10-04T10:00:00+08:00")
        self.assertIsNotNone(f)
        self.assertEqual(f["severity"], "HIGH")  # 50% >= 2x 阈值
        self.assertIn("45000", f["reason"])
        self.assertEqual(f["evidence"]["increase_ratio"], 0.5)

    def test_within_threshold_no_alert(self):
        self.assertIsNone(evaluate_price_spike(30000, 35000, RULES,
                                               commitment_valid_from=self.VF,
                                               commitment_valid_to=self.VT,
                                               quoted_at="2026-10-04T10:00:00+08:00"))

    def test_price_drop_or_equal_never_flags(self):
        self.assertIsNone(evaluate_price_spike(30000, 29000, RULES,
                                               commitment_valid_from=self.VF,
                                               commitment_valid_to=self.VT))
        self.assertIsNone(evaluate_price_spike(30000, 30000, RULES,
                                               commitment_valid_from=self.VF,
                                               commitment_valid_to=self.VT))

    def test_quote_outside_validity_window_ignored(self):
        self.assertIsNone(evaluate_price_spike(30000, 90000, RULES,
                                               commitment_valid_from=self.VF,
                                               commitment_valid_to=self.VT,
                                               quoted_at="2026-10-10T10:00:00+08:00"))


class MassRejectionTest(unittest.TestCase):
    def test_concentrated_rejection_flags(self):
        base = "2026-10-04T20:"
        orders = [{"id": f"o{i}", "status": "REJECTED" if i < 6 else "FULFILLED",
                   "created_at": f"{base}{i:02d}:00+08:00"} for i in range(10)]
        f = evaluate_mass_rejection(orders, RULES, "2026-10-04T20:55:00+08:00")
        self.assertIsNotNone(f)
        self.assertEqual(f["evidence"]["rejected_orders"], 6)

    def test_few_rejections_below_count_threshold(self):
        orders = [{"id": f"o{i}", "status": "REJECTED",
                   "created_at": f"2026-10-04T20:{i:02d}:00+08:00"} for i in range(3)]
        self.assertIsNone(evaluate_mass_rejection(orders, RULES, "2026-10-04T20:55:00+08:00"))

    def test_old_rejections_outside_window_ignored(self):
        orders = [{"id": f"o{i}", "status": "REJECTED",
                   "created_at": f"2026-10-04T18:{i:02d}:00+08:00"} for i in range(8)]
        self.assertIsNone(evaluate_mass_rejection(orders, RULES, "2026-10-04T20:55:00+08:00"))


class EarlyClosureTest(unittest.TestCase):
    def test_within_grace_no_alert(self):
        self.assertIsNone(evaluate_early_closure(
            "2026-10-04T23:00:00+08:00", "2026-10-04T22:40:00+08:00", RULES))

    def test_beyond_grace_flags(self):
        f = evaluate_early_closure("2026-10-04T23:00:00+08:00",
                                   "2026-10-04T20:30:00+08:00", RULES)
        self.assertEqual(f["severity"], "HIGH")
        self.assertEqual(f["evidence"]["early_minutes"], 150)


class BearerTest(unittest.TestCase):
    def test_merchant_bears_price_gouge(self):
        t, bid, why = bearer_for("PRICE_GOUGE", "REFUND", RULES, merchant_id="mer_1")
        self.assertEqual((t, bid), ("MERCHANT", "mer_1"))
        self.assertIn("mer_1", why)

    def test_government_fund_bears_early_closure_compensation(self):
        t, bid, _ = bearer_for("EARLY_CLOSURE", "COMPENSATION", RULES)
        self.assertEqual((t, bid), ("GOVERNMENT_FUND", "district-relief"))

    def test_organizer_bears_cancellation(self):
        t, bid, _ = bearer_for("EVENT_CANCELLED", "REFUND", RULES)
        self.assertEqual((t, bid), ("ORGANIZER", "event-risk-reserve"))

    def test_unknown_category_falls_back(self):
        t, _, _ = bearer_for("WHATEVER", "REFUND", RULES)
        self.assertEqual(t, "GOVERNMENT_FUND")


class PolicyTimingTest(unittest.TestCase):
    POLICIES = [
        {"version": 1, "effective_from": "2026-01-01T00:00:00+08:00", "effective_to":
         "2026-10-01T00:00:00+08:00"},
        {"version": 2, "effective_from": "2026-10-01T00:00:00+08:00", "effective_to": None},
    ]

    def test_transaction_time_picks_version(self):
        self.assertEqual(resolve_policy_version(
            self.POLICIES, "2026-09-30T23:59:59+08:00"), 1)
        self.assertEqual(resolve_policy_version(
            self.POLICIES, "2026-10-01T00:00:00+08:00"), 2)

    def test_no_policy_when_before_all(self):
        self.assertIsNone(resolve_policy_version(
            self.POLICIES, "2025-01-01T00:00:00+08:00"))


if __name__ == "__main__":
    unittest.main()
