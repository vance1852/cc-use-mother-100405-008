"""灾害装备战备与调拨服务的 HTTP 路由测试。"""

import unittest

from science_strategy_foundation.errors import ConflictError

from disaster_readiness import DisasterDatabase, ReadinessService
from disaster_readiness.api import route


def base_payload():
    return {
        "request_id": "req-seed",
        "region_id": "reg-a",
        "name": "甲市",
    }


class ReadinessApiTest(unittest.TestCase):
    def setUp(self):
        self.database = DisasterDatabase(":memory:")
        self.service = ReadinessService(self.database)
        headers = {"X-Actor-Id": "sys"}
        route(self.service, "POST", "/regions",
              {"request_id": "req-r", "region_id": "reg-a", "name": "甲市"}, headers)
        route(self.service, "POST", "/equipment",
              {"request_id": "req-e", "equipment_id": "pump-1", "region_id": "reg-a",
               "name": "泵", "capability_code": "PUMP", "capability": {"flow": 1},
               "environments": ["urban"]}, headers)
        route(self.service, "POST", "/certifications",
              {"request_id": "req-c", "certification_id": "cert-1", "equipment_id": "pump-1",
               "cert_type": "insp", "valid_from": "2026-01-01T00:00:00Z",
               "valid_until": "2027-01-01T00:00:00Z"}, headers)
        route(self.service, "POST", "/crews",
              {"request_id": "req-w", "crew_id": "crew-1", "region_id": "reg-a",
               "name": "班", "qualifications": ["PUMP"]}, headers)
        route(self.service, "POST", "/vehicles",
              {"request_id": "req-v", "vehicle_id": "truck-1", "region_id": "reg-a",
               "name": "车"}, headers)
        route(self.service, "POST", "/travel-times",
              {"request_id": "req-t", "origin_region_id": "reg-a",
               "destination_region_id": "reg-a", "vehicle_id": "truck-1", "minutes": 30},
              headers)

    def tearDown(self):
        self.database.close()

    def test_health_reports_audit(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_full_lifecycle_over_http(self):
        headers = {"X-Actor-Id": "cmd"}
        status, mission = route(self.service, "POST", "/missions", {
            "request_id": "req-m", "mission_id": "mis-1", "region_id": "reg-a",
            "alarm_key": "AL-1", "title": "积水", "environment": "urban", "priority": 50,
            "requirements": [{"capability_code": "PUMP", "quantity": 1}]}, headers)
        self.assertEqual(201, status)

        status, reserve = route(self.service, "POST", "/reservations", {
            "request_id": "req-res", "mission_id": "mis-1", "ttl_minutes": 30}, headers)
        self.assertEqual(201, status)
        self.assertEqual("commitment", reserve["resource_type"])
        commitment_id = reserve["resource_id"]

        status, dispatch = route(self.service, "POST", "/dispatches", {
            "request_id": "req-disp", "commitment_id": commitment_id}, headers)
        self.assertEqual(201, status)
        self.assertTrue(dispatch["dispatched"])

        status, detail = route(self.service, "GET", "/missions/mis-1/status", None)
        self.assertEqual(200, status)
        self.assertEqual("dispatched", detail["status"])
        item_id = detail["commitments"][0]["items"][0]["item_id"]

        status, arrival = route(self.service, "POST", "/arrivals", {
            "commitment_id": commitment_id, "item_ids": [item_id]}, headers)
        self.assertEqual(200, status)
        self.assertEqual([item_id], arrival["arrived_item_ids"])

        status, accepted = route(self.service, "POST", "/returns", {
            "commitment_id": commitment_id,
            "verdicts": [{"item_id": item_id, "accepted": True}]}, headers)
        self.assertEqual(200, status)
        self.assertEqual("completed", accepted["status"])

    def test_duplicate_alarm_returns_conflict_with_rejection_detail(self):
        headers = {"X-Actor-Id": "sys"}
        mission_body = {
            "mission_id": "mis-1", "region_id": "reg-a", "alarm_key": "AL-1",
            "title": "积水", "environment": "urban", "priority": 50,
            "requirements": [{"capability_code": "PUMP", "quantity": 1}]}
        route(self.service, "POST", "/missions", {"request_id": "req-m1", **mission_body},
              headers)
        mission_body["mission_id"] = "mis-2"
        status, payload = route(self.service, "POST", "/missions",
                                {"request_id": "req-m2", **mission_body}, headers)
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_readiness_endpoint_lists_capability(self):
        status, payload = route(self.service, "GET", "/readiness?region_id=reg-a", None)
        self.assertEqual(200, status)
        self.assertEqual("PUMP", payload["capabilities"][0]["capability_code"])
        self.assertEqual(1, payload["capabilities"][0]["deliverable_to_region"])

    def test_override_rejects_same_person(self):
        headers = {"X-Actor-Id": "cmd"}
        route(self.service, "POST", "/missions", {
            "request_id": "req-m", "mission_id": "mis-1", "region_id": "reg-a",
            "alarm_key": "AL-1", "title": "积水", "environment": "urban", "priority": 50,
            "requirements": [{"capability_code": "PUMP", "quantity": 1}]},
            {"X-Actor-Id": "sys"})
        status, payload = route(self.service, "POST", "/overrides", {
            "request_id": "req-ov", "initiator_id": "same", "confirmer_id": "same",
            "mission_id": "mis-1", "ttl_minutes": 30, "reason": "险情"}, headers)
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_unknown_route_404(self):
        status, payload = route(self.service, "GET", "/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
