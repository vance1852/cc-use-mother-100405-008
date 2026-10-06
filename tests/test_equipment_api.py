import unittest
from datetime import datetime, timedelta, timezone

from science_strategy_foundation.storage import Database

from disaster_equipment.api import route
from disaster_equipment.clock import MutableClock
from disaster_equipment.service import EquipmentService

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def ts(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


REQUIREMENT = {
    "environment": "urban_flood",
    "duration_hours": 6,
    "combinations": [
        {"label": "主力组合",
         "items": [{"category": "pump", "min_capability": {"flow_rate_m3h": 3000}, "count": 1}],
         "teams": 1, "qualification": "pump_large"},
    ],
}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = EquipmentService(self.database, clock=MutableClock(T0))
        domain = self.service.domain
        domain.register_organization(request_id="orgA", actor_id="bootstrap",
                                     organization_id="orgA", name="甲市应急中心")
        domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                              display_name="管理员", role="admin", organization_id="orgA")
        domain.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                              display_name="调度员", role="operator", organization_id="orgA")
        domain.register_actor(request_id="aud", actor_id="admin1", new_actor_id="aud1",
                              display_name="审计员", role="auditor", organization_id="orgA")
        domain.register_site(request_id="sA", actor_id="admin1", site_id="sA",
                             organization_id="orgA", name="甲市", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def post(self, path, body, actor="admin1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def get(self, path):
        return route(self.service, "GET", path, None, {})

    def seed_equipment(self):
        status, _ = self.post("/equipment", {
            "request_id": "eq-P1", "equipment_id": "P1", "organization_id": "orgA",
            "site_id": "sA", "category": "pump", "name": "泵P1",
            "capability": {"flow_rate_m3h": 3000}, "environments": ["urban_flood"]})
        self.assertEqual(status, 201)
        status, _ = self.post("/equipment/P1/maintenance", {
            "request_id": "mt-P1", "valid_until": ts(T0 + timedelta(days=30)),
            "result": "passed", "inspector": "检验员"})
        self.assertEqual(status, 201)
        status, _ = self.post("/teams", {
            "request_id": "team-T1", "team_id": "T1", "organization_id": "orgA",
            "site_id": "sA", "name": "队伍T1",
            "qualifications": [{"code": "pump_large",
                                "valid_until": ts(T0 + timedelta(days=365))}]})
        self.assertEqual(status, 201)

    def test_health_reports_audit_and_recovery(self):
        status, payload = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["audit_valid"])
        self.assertIn("recovery", payload)

    def test_full_flow_over_http(self):
        self.seed_equipment()
        status, capability = self.get("/regions/sA/capability")
        self.assertEqual(status, 200)
        self.assertEqual(capability["deliverable"]["categories"]["pump"]["count"], 1)
        status, alert = self.post("/alerts", {
            "request_id": "alert-1", "alert_id": "AL-1", "site_id": "sA",
            "severity": "orange", "priority": 5, "requirement": REQUIREMENT}, actor="op1")
        self.assertEqual(status, 201)
        self.assertTrue(alert["reserved"])
        # 重复告警：同一 alert_id 返回既有任务。
        status, duplicate = self.post("/alerts", {
            "request_id": "alert-2", "alert_id": "AL-1", "site_id": "sA",
            "severity": "orange", "priority": 5, "requirement": REQUIREMENT}, actor="op1")
        self.assertEqual(status, 201)
        self.assertTrue(duplicate["duplicate_alert"])
        # 幂等重放：同一 request_id 返回首次响应。
        status, replay = self.post("/alerts", {
            "request_id": "alert-1", "alert_id": "AL-1", "site_id": "sA",
            "severity": "orange", "priority": 5, "requirement": REQUIREMENT}, actor="op1")
        self.assertEqual(status, 200)
        self.assertEqual(replay["task_id"], alert["task_id"])
        status, dispatched = self.post(f"/tasks/{alert['task_id']}/dispatch",
                                       {"request_id": "disp-1"}, actor="op1")
        self.assertEqual(status, 201)
        self.assertTrue(dispatched["dispatched"])
        status, arrived = self.post(f"/dispatches/{dispatched['dispatch_id']}/arrival",
                                    {"request_id": "arr-1", "equipment_ids": ["P1"]}, actor="op1")
        self.assertEqual(status, 201)
        self.assertEqual(arrived["state"], "on_site")
        status, task = self.get(f"/tasks/{alert['task_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(task["status"], "active")
        status, _ = self.post(f"/tasks/{alert['task_id']}/finish",
                              {"request_id": "fin-1"}, actor="op1")
        self.assertEqual(status, 201)
        status, returned = self.post(f"/dispatches/{dispatched['dispatch_id']}/return",
                                     {"request_id": "ret-1",
                                      "items": [{"equipment_id": "P1", "passed": True}]},
                                     actor="op1")
        self.assertEqual(status, 201)
        self.assertEqual(returned["task_status"], "completed")
        status, capability = self.get("/regions/sA/capability")
        self.assertEqual(capability["deliverable"]["categories"]["pump"]["count"], 1)

    def test_waitlist_and_rejection_reasons_visible(self):
        status, alert = self.post("/alerts", {
            "request_id": "alert-1", "alert_id": "AL-1", "site_id": "sA",
            "severity": "red", "priority": 9, "requirement": REQUIREMENT}, actor="op1")
        self.assertEqual(status, 201)
        self.assertEqual(alert["status"], "waitlisted")
        self.assertTrue(alert["reasons"])
        status, waitlist = self.get("/waitlist?site_id=sA")
        self.assertEqual(status, 200)
        self.assertEqual(len(waitlist["items"]), 1)
        self.assertEqual(waitlist["items"][0]["position"], 1)
        self.assertTrue(waitlist["items"][0]["reasons"])
        status, tasks = self.get("/tasks?site_id=sA&status=waitlisted")
        self.assertEqual(len(tasks["items"]), 1)

    def test_permission_and_error_mapping(self):
        status, payload = self.post("/equipment", {
            "request_id": "eq-x", "equipment_id": "X1", "organization_id": "orgA",
            "site_id": "sA", "category": "pump", "name": "泵",
            "capability": {}, "environments": []}, actor="aud1")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "permission_denied")
        status, payload = self.post("/equipment", {"request_id": "eq-y"}, actor="admin1")
        self.assertEqual(status, 400)
        status, payload = self.get("/tasks/no-such-task")
        self.assertEqual(status, 404)
        status, payload = self.get("/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "route_not_found")


if __name__ == "__main__":
    unittest.main()
