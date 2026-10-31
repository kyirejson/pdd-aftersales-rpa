import unittest

from pdd_rpa.models import ExecutionMode
from pdd_rpa.safety import (
    MONEY_ACTIONS,
    SafetyContext,
    SafetyViolation,
    assert_action_allowed,
    validate_shop_context,
)


class SafetyTests(unittest.TestCase):
    def test_shop_mismatch_is_blocked(self):
        with self.assertRaises(SafetyViolation):
            validate_shop_context(
                SafetyContext(
                    configured_shop_name="示例书店 A",
                    page_shop_name="示例书店 B",
                    task_shop_name="示例书店 A",
                    configured_profile="Profile",
                    active_profile="Profile",
                )
            )

    def test_observe_mode_blocks_group_send(self):
        with self.assertRaises(SafetyViolation):
            assert_action_allowed(
                "SEND_INTERCEPT_REQUEST",
                ExecutionMode.OBSERVE,
                live_actions_enabled=True,
            )

    def test_live_switch_blocks_refund_in_any_mode(self):
        with self.assertRaises(SafetyViolation):
            assert_action_allowed(
                "AGREE_REFUND",
                ExecutionMode.AUTO,
                live_actions_enabled=False,
                approved=True,
            )

    def test_return_after_refund_is_guarded_as_money_action(self):
        self.assertIn("AGREE_RETURN_AFTER_REFUND", MONEY_ACTIONS)
        with self.assertRaises(SafetyViolation):
            assert_action_allowed(
                "AGREE_RETURN_AFTER_REFUND",
                ExecutionMode.AUTO,
                live_actions_enabled=False,
                approved=True,
            )

    def test_recall_after_refund_is_guarded_as_money_action(self):
        self.assertIn("AGREE_RECALL_AFTER_REFUND", MONEY_ACTIONS)
        with self.assertRaises(SafetyViolation):
            assert_action_allowed(
                "AGREE_RECALL_AFTER_REFUND",
                ExecutionMode.AUTO,
                live_actions_enabled=False,
                approved=True,
            )


if __name__ == "__main__":
    unittest.main()
