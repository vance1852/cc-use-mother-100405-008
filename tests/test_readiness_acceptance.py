"""灾害装备战备与调拨服务的离线验收测试。"""

import unittest

from disaster_readiness.acceptance import run


class ReadinessAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["initial_infeasible"])
        self.assertTrue(result["reserve_waitlisted"])
        self.assertTrue(result["waitlist_promoted"])
        self.assertTrue(result["dispatched"])
        self.assertTrue(result["partial_arrival_operating"])
        self.assertTrue(result["substitute_attached"])
        self.assertTrue(result["handed_over"])
        self.assertTrue(result["dual_person"])
        self.assertTrue(result["completed"])
        self.assertTrue(result["executed_immutable"])
        self.assertTrue(result["survives_restart"])


if __name__ == "__main__":
    unittest.main()
