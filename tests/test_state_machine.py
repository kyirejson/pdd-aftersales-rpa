import unittest

from pdd_rpa.models import TaskState
from pdd_rpa.state_machine import InvalidTransition, assert_transition


class StateMachineTests(unittest.TestCase):
    def test_valid_transition(self):
        assert_transition(TaskState.DISCOVERED, TaskState.COLLECTING_DATA)

    def test_money_flow_cannot_skip_states(self):
        with self.assertRaises(InvalidTransition):
            assert_transition(TaskState.DISCOVERED, TaskState.COMPLETED)


if __name__ == "__main__":
    unittest.main()

