"""灾害装备战备与调拨领域服务的规则测试。"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from science_strategy_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)

from disaster_readiness import DisasterDatabase, ReadinessService


class AdvancingClock:
    def __init__(self, start):
        self.value = start

    def now(self):
        return self.value

    def advance(self, minutes):
        self.value += timedelta(minutes=minutes)


class ReadinessTestBase(unittest.TestCase):
    clock: AdvancingClock
    database: DisasterDatabase
    service: ReadinessService

    def setUp(self):
        self.database = DisasterDatabase(":memory:")
        self.clock = AdvancingClock(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc))
        self.service = ReadinessService(self.database, self.clock)
        self._seed()

    def tearDown(self):
        self.database.close()

    def _seed(self):
        s = self.service
        s.register_region(request_id="req-ra", actor_id="sys", region_id="reg-a", name="甲市")
        s.register_region(request_id="req-rb", actor_id="sys", region_id="reg-b", name="乙市")
        s.register_equipment(request_id="req-ea1", actor_id="sys", equipment_id="pump-a1",
                             region_id="reg-a", name="泵A1", capability_code="PUMP",
                             capability={"flow": 3000}, environments=["urban", "underpass"])
        s.register_equipment(request_id="req-ea2", actor_id="sys", equipment_id="pump-a2",
                             region_id="reg-a", name="泵A2", capability_code="PUMP",
                             capability={"flow": 3000}, environments=["urban"])
        s.register_equipment(request_id="req-eb1", actor_id="sys", equipment_id="pump-b1",
                             region_id="reg-b", name="泵B1", capability_code="PUMP",
                             capability={"flow": 3000}, environments=["urban", "underpass"])
        s.register_certification(request_id="req-ca1", actor_id="sys", certification_id="cert-a1",
                                 equipment_id="pump-a1", cert_type="insp",
                                 valid_from="2026-01-01T00:00:00Z",
                                 valid_until="2027-01-01T00:00:00Z")
        s.register_certification(request_id="req-ca2", actor_id="sys", certification_id="cert-a2",
                                 equipment_id="pump-a2", cert_type="insp",
                                 valid_from="2025-01-01T00:00:00Z",
                                 valid_until="2026-09-01T00:00:00Z")
        s.register_certification(request_id="req-cb1", actor_id="sys", certification_id="cert-b1",
                                 equipment_id="pump-b1", cert_type="insp",
                                 valid_from="2026-01-01T00:00:00Z",
                                 valid_until="2027-01-01T00:00:00Z")
        s.register_crew(request_id="req-wa", actor_id="sys", crew_id="crew-a", region_id="reg-a",
                        name="甲班", qualifications=["PUMP"])
        s.register_crew(request_id="req-wb", actor_id="sys", crew_id="crew-b", region_id="reg-b",
                        name="乙班", qualifications=["PUMP"])
        s.register_vehicle(request_id="req-va", actor_id="sys", vehicle_id="truck-a",
                           region_id="reg-a", name="甲车")
        s.register_vehicle(request_id="req-vb", actor_id="sys", vehicle_id="truck-b",
                           region_id="reg-b", name="乙车")
        s.set_travel_time(request_id="req-taa", actor_id="sys", origin_region_id="reg-a",
                          destination_region_id="reg-a", vehicle_id="truck-a", minutes=40)
        s.set_travel_time(request_id="req-tba", actor_id="sys", origin_region_id="reg-b",
                          destination_region_id="reg-a", vehicle_id="truck-b", minutes=90)

    def _mission(self, mission_id="mis-1", region="reg-a", environment="underpass",
                 priority=50, alarm="AL-1", quantity=1):
        self.service.create_mission(
            request_id=f"req-{mission_id}", actor_id="sys", mission_id=mission_id,
            region_id=region, alarm_key=alarm, title="积水", environment=environment,
            priority=priority,
            requirements=[{"capability_code": "PUMP", "quantity": quantity}])


class FeasibilityTest(ReadinessTestBase):
    def test_expired_cert_and_missing_agreement_block_capacity(self):
        # urban 环境下 a1 合格、a2 证书过期、b1 缺互助协议，两套需求无法满足。
        self._mission(environment="urban", quantity=2)
        result = self.service.check_feasibility("mis-1")
        self.assertFalse(result["feasible"])
        codes = {r["code"] for r in result["rejections"]}
        self.assertIn("cert_expired", codes)
        self.assertIn("agreement_missing", codes)
        self.assertIn("capacity_shortfall", codes)

    def test_cross_region_requires_agreement(self):
        # 未签协议：b1 不能跨区，underpass 仅有 a1 一套，两套需求不足。
        self._mission(environment="underpass", quantity=2)
        before = self.service.check_feasibility("mis-1")
        self.assertFalse(before["feasible"])
        self.assertNotIn("pump-b1", {i["equipment_id"] for i in before["items"]})
        # 签订协议后 b1 可作为第二套兑现。
        self.service.register_aid_agreement(
            request_id="req-aid", actor_id="sys", agreement_id="aid-1",
            holder_region_id="reg-a", counterpart_region_id="reg-b")
        after = self.service.check_feasibility("mis-1")
        self.assertTrue(after["feasible"])
        self.assertEqual({"pump-a1", "pump-b1"},
                         {i["equipment_id"] for i in after["items"]})

    def test_underpass_environment_excludes_urban_only_equipment(self):
        self.service.register_aid_agreement(
            request_id="req-aid", actor_id="sys", agreement_id="aid-1",
            holder_region_id="reg-a", counterpart_region_id="reg-b")
        self._mission(environment="underpass", quantity=1)
        result = self.service.check_feasibility("mis-1")
        self.assertTrue(result["feasible"])
        self.assertNotIn("pump-a2", {i["equipment_id"] for i in result["items"]})

    def test_open_maintenance_blocks_equipment(self):
        self.service.open_maintenance(
            request_id="req-mnt", actor_id="sys", maintenance_id="mnt-1",
            equipment_id="pump-a1", title="例行检修", start_at="2026-10-01T00:00:00Z")
        self._mission(environment="urban", quantity=1)
        result = self.service.check_feasibility("mis-1")
        codes = {r["code"] for r in result["rejections"]}
        self.assertIn("maintenance_open", codes)
        # 关闭维修后 a1 恢复可兑现。
        self.service.close_maintenance(actor_id="sys", maintenance_id="mnt-1")
        result = self.service.check_feasibility("mis-1")
        self.assertTrue(result["feasible"])


class ReserveDispatchTest(ReadinessTestBase):
    def test_successful_reserve_then_dispatch_reverifies(self):
        self._mission(environment="urban", quantity=1)
        reserve = self.service.reserve(request_id="req-res", actor_id="cmd",
                                       mission_id="mis-1", ttl_minutes=30)
        self.assertFalse(reserve["replayed"])
        commitment_id = reserve["resource_id"]
        dispatch = self.service.confirm_dispatch(request_id="req-disp", actor_id="cmd",
                                                 commitment_id=commitment_id)
        self.assertFalse(dispatch["replayed"])
        status = self.service.mission_status("mis-1")
        self.assertEqual("dispatched", status["status"])
        self.assertEqual("dispatched", status["commitments"][0]["status"])

    def test_reserve_rejected_enters_waitlist_with_reasons(self):
        self._mission(quantity=2)
        result = self.service.reserve(request_id="req-res", actor_id="cmd",
                                      mission_id="mis-1", ttl_minutes=30)
        self.assertEqual("waitlist", result["resource_type"])
        self.assertTrue(result["waitlist_entry_id"])
        waitlist = self.service.waitlist()["items"]
        self.assertEqual(1, len(waitlist))
        self.assertEqual("waiting", waitlist[0]["status"])
        self.assertTrue(any(r["code"] == "capacity_shortfall"
                            for r in waitlist[0]["rejections"]))

    def test_reserve_rejected_can_skip_waitlist(self):
        self._mission(quantity=2)
        result = self.service.reserve(request_id="req-res", actor_id="cmd",
                                      mission_id="mis-1", ttl_minutes=30,
                                      join_waitlist_on_reject=False)
        self.assertEqual("rejection", result["resource_type"])
        self.assertEqual(0, len(self.service.waitlist()["items"]))

    def test_reserve_is_idempotent_and_duplicate_alarm_blocked(self):
        self._mission(environment="urban")
        first = self.service.reserve(request_id="req-res", actor_id="cmd",
                                     mission_id="mis-1", ttl_minutes=30)
        second = self.service.reserve(request_id="req-res", actor_id="cmd",
                                      mission_id="mis-1", ttl_minutes=30)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])
        with self.assertRaises(ConflictError):
            self._mission(mission_id="mis-2", alarm="AL-1")

    def test_dispatch_fails_when_cert_expires_after_reserve(self):
        self._mission(environment="urban")
        reserve = self.service.reserve(request_id="req-res", actor_id="cmd",
                                       mission_id="mis-1", ttl_minutes=30)
        # 预留之后证书被吊销：出动原子核验必须拦截。
        self.database.connection.execute(
            "UPDATE dr_certifications SET revoked=1 WHERE equipment_id='pump-a1'")
        with self.assertRaises(ConflictError):
            self.service.confirm_dispatch(request_id="req-disp", actor_id="cmd",
                                          commitment_id=reserve["resource_id"])
        # 出动失败后预留仍在，装备锁未丢失给别人。
        locks = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM dr_resource_locks").fetchone()["c"]
        self.assertEqual(3, locks)

    def test_dispatch_fails_after_reserve_expiry(self):
        self._mission(environment="urban")
        reserve = self.service.reserve(request_id="req-res", actor_id="cmd",
                                       mission_id="mis-1", ttl_minutes=30)
        self.clock.advance(31)
        self.service.run_due_processing()
        with self.assertRaises(ConflictError):
            self.service.confirm_dispatch(request_id="req-disp", actor_id="cmd",
                                          commitment_id=reserve["resource_id"])


class ExecutionAdjustmentTest(ReadinessTestBase):
    def _dispatched(self):
        self._mission(environment="urban")
        reserve = self.service.reserve(request_id="req-res", actor_id="cmd",
                                       mission_id="mis-1", ttl_minutes=60)
        self.service.confirm_dispatch(request_id="req-disp", actor_id="cmd",
                                      commitment_id=reserve["resource_id"])
        return reserve["resource_id"]

    def test_partial_arrival_then_fault_releases_only_one_item(self):
        commitment_id = self._dispatched()
        item = self.service.mission_status("mis-1")["commitments"][0]["items"][0]
        self.service.mark_arrival(actor_id="f", commitment_id=commitment_id,
                                  item_ids=[item["item_id"]])
        self.assertEqual(
            "operating",
            self.service.mission_status("mis-1")["commitments"][0]["status"])
        self.service.report_fault(actor_id="f", commitment_id=commitment_id,
                                  item_id=item["item_id"], note="异响")
        remaining_locks = {row["resource_id"] for row in
                           self.database.connection.execute(
                               "SELECT resource_id FROM dr_resource_locks")}
        self.assertNotIn(item["equipment_id"], remaining_locks)
        # 故障明细记录保留为 faulty。
        refreshed = self.service.mission_status("mis-1")["commitments"][0]
        statuses = {i["item_id"]: i["status"] for i in refreshed["items"]}
        self.assertEqual("faulty", statuses[item["item_id"]])

    def test_substitute_is_appended_and_original_kept(self):
        self.service.register_aid_agreement(
            request_id="req-aid", actor_id="sys", agreement_id="aid-1",
            holder_region_id="reg-a", counterpart_region_id="reg-b")
        commitment_id = self._dispatched()
        item = self.service.mission_status("mis-1")["commitments"][0]["items"][0]
        self.service.report_fault(actor_id="f", commitment_id=commitment_id,
                                  item_id=item["item_id"], note="故障")
        sub = self.service.attach_substitute(
            request_id="req-sub", actor_id="cmd", commitment_id=commitment_id,
            replaces_item_id=item["item_id"], equipment_id="pump-b1",
            crew_id="crew-b", vehicle_id="truck-b")
        new_id = sub["resource_id"]
        items = self.service.mission_status("mis-1")["commitments"][0]["items"]
        new_item = next(i for i in items if i["item_id"] == new_id)
        self.assertEqual(item["item_id"], new_item["replaces_item_id"])
        original = next(i for i in items if i["item_id"] == item["item_id"])
        self.assertEqual("faulty", original["status"])

    def test_substitute_rejects_expired_cert(self):
        commitment_id = self._dispatched()
        item = self.service.mission_status("mis-1")["commitments"][0]["items"][0]
        self.service.report_fault(actor_id="f", commitment_id=commitment_id,
                                  item_id=item["item_id"], note="故障")
        # pump-a2 证书过期，不能作为替代。
        with self.assertRaises(ConflictError):
            self.service.attach_substitute(
                request_id="req-sub", actor_id="cmd", commitment_id=commitment_id,
                replaces_item_id=item["item_id"], equipment_id="pump-a2",
                crew_id="crew-a", vehicle_id="truck-a")

    def test_extend_only_applies_to_executed_commitment(self):
        self._mission(environment="urban")
        reserve = self.service.reserve(request_id="req-res", actor_id="cmd",
                                       mission_id="mis-1", ttl_minutes=60)
        with self.assertRaises(ConflictError):
            self.service.extend_mission(actor_id="cmd",
                                        commitment_id=reserve["resource_id"], minutes=30)
        self.service.confirm_dispatch(request_id="req-disp", actor_id="cmd",
                                      commitment_id=reserve["resource_id"])
        extended = self.service.extend_mission(actor_id="cmd",
                                               commitment_id=reserve["resource_id"], minutes=60)
        self.assertEqual(60, extended["extended_minutes"])

    def test_return_acceptance_completes_commitment(self):
        commitment_id = self._dispatched()
        items = self.service.mission_status("mis-1")["commitments"][0]["items"]
        self.service.mark_arrival(actor_id="f", commitment_id=commitment_id,
                                  item_ids=[i["item_id"] for i in items])
        result = self.service.accept_return(
            actor_id="cmd", commitment_id=commitment_id,
            verdicts=[{"item_id": items[0]["item_id"], "accepted": True}])
        self.assertEqual("completed", result["status"])
        self.assertEqual("completed", self.service.mission_status("mis-1")["status"])

    def test_return_rejection_marks_item_faulty(self):
        commitment_id = self._dispatched()
        items = self.service.mission_status("mis-1")["commitments"][0]["items"]
        self.service.mark_arrival(actor_id="f", commitment_id=commitment_id,
                                  item_ids=[i["item_id"] for i in items])
        result = self.service.accept_return(
            actor_id="cmd", commitment_id=commitment_id,
            verdicts=[{"item_id": items[0]["item_id"], "accepted": False,
                       "note": "验收不合格"}])
        self.assertEqual("completed", result["status"])
        self.assertIn(items[0]["item_id"], result["rejected"])


class CancelAndWaitlistTest(ReadinessTestBase):
    def test_cancel_only_for_unexecuted_hold_and_promotes_waitlist(self):
        self._mission(environment="urban", mission_id="mis-1", priority=80)
        reserve = self.service.reserve(request_id="req-r1", actor_id="cmd",
                                       mission_id="mis-1", ttl_minutes=60)
        # 第二个任务因唯一泵被占用进入候补。
        self._mission(environment="urban", mission_id="mis-2", alarm="AL-2", priority=40)
        queued = self.service.reserve(request_id="req-r2", actor_id="cmd",
                                      mission_id="mis-2", ttl_minutes=30)
        self.assertEqual("waitlist", queued["resource_type"])
        self.service.cancel_unexecuted(actor_id="cmd",
                                       commitment_id=reserve["resource_id"], reason="预警解除")
        status2 = self.service.mission_status("mis-2")["commitments"]
        self.assertTrue(any(c["status"] == "held" for c in status2))

    def test_executed_commitment_cannot_cancel(self):
        self._mission(environment="urban")
        reserve = self.service.reserve(request_id="req-res", actor_id="cmd",
                                       mission_id="mis-1", ttl_minutes=60)
        self.service.confirm_dispatch(request_id="req-disp", actor_id="cmd",
                                      commitment_id=reserve["resource_id"])
        with self.assertRaises(ConflictError):
            self.service.cancel_unexecuted(actor_id="cmd",
                                           commitment_id=reserve["resource_id"])


class OverrideTest(ReadinessTestBase):
    def _two_missions(self):
        self._mission(environment="urban", mission_id="mis-low", alarm="AL-lo", priority=20)
        low = self.service.reserve(request_id="req-lo", actor_id="cmd",
                                   mission_id="mis-low", ttl_minutes=60)
        self._mission(environment="urban", mission_id="mis-high", alarm="AL-hi", priority=90)
        return low["resource_id"]

    def test_override_requires_distinct_confirmer(self):
        self._two_missions()
        with self.assertRaises(PermissionDenied):
            self.service.emergency_override(
                request_id="req-ov", initiator_id="same", confirmer_id="same",
                mission_id="mis-high", ttl_minutes=30, reason="险情")

    def test_override_preempts_lower_priority_hold(self):
        low_commitment = self._two_missions()
        result = self.service.emergency_override(
            request_id="req-ov", initiator_id="cmd1", confirmer_id="cmd2",
            mission_id="mis-high", ttl_minutes=30, reason="重大险情")
        self.assertEqual([low_commitment], result["displaced_commitment_ids"])
        low_status = self.service.mission_status("mis-low")["commitments"][0]
        self.assertEqual("preempted", low_status["status"])
        self.assertTrue(all(i["status"] == "preempted" for i in low_status["items"]))

    def test_override_expiry_restores_preempted_commitment(self):
        low_commitment = self._two_missions()
        self.service.emergency_override(
            request_id="req-ov", initiator_id="cmd1", confirmer_id="cmd2",
            mission_id="mis-high", ttl_minutes=30, reason="重大险情")
        self.clock.advance(31)
        due = self.service.run_due_processing()
        self.assertTrue(any(cid for cid in due["expired_commitment_ids"]))
        low_status = self.service.mission_status("mis-low")["commitments"][0]
        self.assertEqual("held", low_status["status"])
        self.assertEqual(low_commitment, low_status["commitment_id"])
        self.assertTrue(all(i["status"] == "planned" for i in low_status["items"]))

    def test_override_cannot_take_dispatched_commitment(self):
        # 已正式出动的承诺不受越级抢占。
        self._mission(environment="urban", mission_id="mis-low", alarm="AL-lo", priority=20)
        low = self.service.reserve(request_id="req-lo", actor_id="cmd",
                                   mission_id="mis-low", ttl_minutes=60)
        self.service.confirm_dispatch(request_id="req-disp", actor_id="cmd",
                                      commitment_id=low["resource_id"])
        self._mission(environment="urban", mission_id="mis-high", alarm="AL-hi", priority=90)
        with self.assertRaises(ConflictError):
            self.service.emergency_override(
                request_id="req-ov", initiator_id="cmd1", confirmer_id="cmd2",
                mission_id="mis-high", ttl_minutes=30, reason="重大险情")


class HandoverTest(ReadinessTestBase):
    def test_handover_requires_agreement_and_qualification(self):
        self.service.register_aid_agreement(
            request_id="req-aid", actor_id="sys", agreement_id="aid-1",
            holder_region_id="reg-a", counterpart_region_id="reg-b")
        self._mission(environment="urban")
        reserve = self.service.reserve(request_id="req-res", actor_id="cmd",
                                       mission_id="mis-1", ttl_minutes=60)
        self.service.confirm_dispatch(request_id="req-disp", actor_id="cmd",
                                      commitment_id=reserve["resource_id"])
        item = self.service.mission_status("mis-1")["commitments"][0]["items"][0]
        result = self.service.handover_to_region(
            request_id="req-ho", actor_id="cmd", commitment_id=reserve["resource_id"],
            to_region_id="reg-b",
            assignments=[{"item_id": item["item_id"], "crew_id": "crew-b",
                          "vehicle_id": "truck-b"}])
        self.assertEqual(reserve["resource_id"], result["resource_id"])
        self.assertEqual(
            "reg-b",
            self.service.mission_status("mis-1")["commitments"][0]["region_id"])


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_reserves_have_single_winner(self):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "conc.sqlite3"
        seed_db = DisasterDatabase(path)
        seed = ReadinessService(seed_db)
        seed.register_region(request_id="req-r", actor_id="sys", region_id="reg-a", name="甲")
        seed.register_equipment(request_id="req-e", actor_id="sys", equipment_id="pump-1",
                                region_id="reg-a", name="泵", capability_code="PUMP",
                                capability={"flow": 1}, environments=["urban"])
        seed.register_certification(request_id="req-c", actor_id="sys",
                                    certification_id="cert-1", equipment_id="pump-1",
                                    cert_type="insp", valid_from="2026-01-01T00:00:00Z",
                                    valid_until="2027-01-01T00:00:00Z")
        seed.register_crew(request_id="req-w", actor_id="sys", crew_id="crew-1",
                           region_id="reg-a", name="班", qualifications=["PUMP"])
        seed.register_vehicle(request_id="req-v", actor_id="sys", vehicle_id="truck-1",
                              region_id="reg-a", name="车")
        seed.set_travel_time(request_id="req-t", actor_id="sys", origin_region_id="reg-a",
                             destination_region_id="reg-a", vehicle_id="truck-1", minutes=30)
        seed.create_mission(request_id="req-m1", actor_id="sys", mission_id="mis-1",
                            region_id="reg-a", alarm_key="AL-1", title="任务一",
                            environment="urban", priority=50,
                            requirements=[{"capability_code": "PUMP", "quantity": 1}])
        seed.create_mission(request_id="req-m2", actor_id="sys", mission_id="mis-2",
                            region_id="reg-a", alarm_key="AL-2", title="任务二",
                            environment="urban", priority=50,
                            requirements=[{"capability_code": "PUMP", "quantity": 1}])
        seed_db.close()

        outcomes = []
        barrier = threading.Barrier(2)

        def worker(mission_id, request_id):
            db = DisasterDatabase(path)
            service = ReadinessService(db)
            barrier.wait()
            try:
                result = service.reserve(request_id=request_id, actor_id="cmd",
                                         mission_id=mission_id, ttl_minutes=30)
                outcomes.append((result["resource_type"] == "commitment",
                                 result["resource_type"]))
            except ConflictError:
                outcomes.append((False, "conflict"))
            finally:
                db.close()

        t1 = threading.Thread(target=worker, args=("mis-1", "req-res-1"))
        t2 = threading.Thread(target=worker, args=("mis-2", "req-res-2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        winners = [o for o in outcomes if o[0]]
        self.assertEqual(1, len(winners), outcomes)
        verify_db = DisasterDatabase(path)
        lock_count = verify_db.connection.execute(
            "SELECT COUNT(*) AS c FROM dr_resource_locks WHERE resource_type='equipment'"
        ).fetchone()["c"]
        self.assertEqual(1, lock_count)
        verify_db.close()
        os.remove(path)


if __name__ == "__main__":
    unittest.main()
