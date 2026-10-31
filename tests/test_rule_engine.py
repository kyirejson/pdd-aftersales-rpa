import unittest

from pdd_rpa.rule_engine import evaluate_rules


class RuleEngineTests(unittest.TestCase):
    def test_disabled_rule_never_matches(self):
        decision = evaluate_rules(
            {"refund_type": "仅退款"},
            [{
                "rule_id": "R1",
                "enabled": False,
                "all_conditions": [{"field": "refund_type", "operator": "eq", "value": "仅退款"}],
            }],
        )
        self.assertFalse(decision.matched)

    def test_enabled_rule_matches_and_preserves_approval(self):
        decision = evaluate_rules(
            {"shop_id": "shop_01", "status": "IN_TRANSIT", "amount": 20},
            [{
                "rule_id": "R1",
                "name": "候选规则",
                "enabled": True,
                "priority": 1,
                "shops": ["*"],
                "all_conditions": [
                    {"field": "status", "operator": "eq", "value": "IN_TRANSIT"},
                    {"field": "amount", "operator": "lte", "value": 100},
                ],
                "actions": ["SEND_INTERCEPT_REQUEST"],
                "approval_required": True,
            }],
        )
        self.assertTrue(decision.matched)
        self.assertTrue(decision.approval_required)
        self.assertEqual(decision.actions, ["SEND_INTERCEPT_REQUEST"])


if __name__ == "__main__":
    unittest.main()

