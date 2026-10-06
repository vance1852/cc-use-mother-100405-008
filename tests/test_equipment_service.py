import threading
import unittest
from datetime import datetime, timedelta, timezone

from science_strategy_foundation.errors import ConflictError, PermissionDenied
from science_strategy_foundation.storage import Database

from disaster_equipment.clock import MutableClock
from disaster_equipment.service import EquipmentService

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def ts(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def requirement(flow=3000, count=1, teams=1, duration=6):
    return {
        "environment": "urban_flood",
        "duration_hours": duration,
        "combinations": [
            {"label": "主力组合",
             "items": [{"category": "pump", "min_capability": {"flow_rate_m3h": flow},
                        "count": count}],
             "teams": teams, "qualification": "pump_large"},
            {"label": "备援组合",
             "items": [{"category": "pump", "min_capability": {"flow_rate_m3h": flow // 2},
                        "count": count * 2}],
             "teams": teams, "qualification": "pump_large"},
        ],
    }


class EquipmentServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(T0)
        self.service = EquipmentService(self.database, clock=self.clock)
        domain = self.service.domain
        domain.register_organization(request_id="orgA", actor_id="bootstrap",
                                     organization_id="orgA", name="甲市应急中心")
        domain.register_organization(request_id="orgB", actor_id="bootstrap",
                                     organization_id="orgB", name="乙市应急中心")
        domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                              display_name="管理员", role="admin", organization_id="orgA")
        domain.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                              display_name="调度员", role="operator", organization_id="orgA")
        domain.register_actor(request_id="rev", actor_id="admin1", new_actor_id="rev1",
                              display_name="复核员", role="reviewer", organization_id="orgA")
        domain.register_actor(request_id="aud", actor_id="admin1", new_actor_id="aud1",
                              display_name="审计员", role="auditor", organization_id="orgA")
        domain.register_site(request_id="sA", actor_id="admin1", site_id="sA",
                             organization_id="orgA", name="甲市", timezone_name="Asia/Shanghai")
        domain.register_site(request_id="sB", actor_id="admin1", site_id="sB",
                             organization_id="orgB", name="乙市", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------------
    # 场景搭建辅助
    # ------------------------------------------------------------------

    def add_equipment(self, equipment_id, org="orgA", site="sA", category="pump",
                      capability=None, valid_days=30, environments=("urban_flood",)):
        capability = capability if capability is not None else {"flow_rate_m3h": 3000}
        self.service.register_equipment(
            request_id=f"eq-{equipment_id}", actor_id="admin1", equipment_id=equipment_id,
            organization_id=org, site_id=site, category=category, name=f"装备{equipment_id}",
            capability=capability, environments=list(environments))
        self.service.record_maintenance(
            request_id=f"mt-{equipment_id}", actor_id="admin1", equipment_id=equipment_id,
            valid_until=ts(T0 + timedelta(days=valid_days)), result="passed", inspector="检验员")

    def add_vehicle(self, equipment_id, org="orgA", site="sA", valid_days=30):
        self.add_equipment(equipment_id, org=org, site=site, category="transport_vehicle",
                           capability={"load_tonnes": 30}, valid_days=valid_days)

    def add_team(self, team_id, org="orgA", site="sA", code="pump_large", valid_days=365):
        self.service.register_team(
            request_id=f"team-{team_id}", actor_id="admin1", team_id=team_id,
            organization_id=org, site_id=site, name=f"队伍{team_id}",
            qualifications=[{"code": code, "valid_until": ts(T0 + timedelta(days=valid_days))}])

    def add_routes(self):
        self.service.register_route(request_id="rAB", actor_id="admin1", route_id="rAB",
                                    from_site_id="sA", to_site_id="sB", duration_minutes=60)
        self.service.register_route(request_id="rBA", actor_id="admin1", route_id="rBA",
                                    from_site_id="sB", to_site_id="sA", duration_minutes=60)

    def add_agreement(self, categories=("pump", "transport_vehicle", "operator_team")):
        self.service.register_agreement(
            request_id="agrBA", actor_id="admin1", agreement_id="agrBA",
            provider_org_id="orgB", requester_org_id="orgA", categories=list(categories),
            valid_from=ts(T0 - timedelta(days=1)), valid_until=ts(T0 + timedelta(days=30)))

    def raise_alert(self, alert_id="AL-1", site_id="sA", priority=5, req=None, ttl=None):
        result, _ = self.service.raise_alert(
            request_id=f"alert-{alert_id}", actor_id="op1", alert_id=alert_id, site_id=site_id,
            severity="orange", priority=priority, requirement=req or requirement(),
            reservation_ttl_minutes=ttl)
        return result

    def full_mission(self, alert_id="AL-1", req=None):
        """本市资源完成一次完整任务，返回 (task_id, dispatch_id, equipment_ids)。"""
        alert = self.raise_alert(alert_id=alert_id, req=req)
        self.assertTrue(alert["reserved"], alert)
        dispatched, _ = self.service.dispatch_task(request_id=f"disp-{alert_id}", actor_id="op1",
                                                   task_id=alert["task_id"])
        self.assertTrue(dispatched["dispatched"], dispatched)
        equipment_ids = [item["resource_id"] for item in dispatched["items"]
                         if item["resource_type"] == "equipment"]
        self.service.confirm_arrival(request_id=f"arr-{alert_id}", actor_id="op1",
                                     dispatch_id=dispatched["dispatch_id"],
                                     equipment_ids=equipment_ids)
        return alert["task_id"], dispatched["dispatch_id"], equipment_ids

    # ------------------------------------------------------------------
    # 能力承诺随时间变化
    # ------------------------------------------------------------------

    def test_capability_reflects_certificate_expiry(self):
        self.add_equipment("P1", valid_days=10)
        self.add_equipment("P2", valid_days=5)
        self.add_team("T1")
        capability = self.service.region_capability("sA")
        self.assertEqual(capability["deliverable"]["categories"]["pump"]["count"], 2)
        self.assertEqual(capability["deliverable"]["categories"]["pump"]["flow_rate_m3h"], 6000)
        self.assertEqual(capability["deliverable"]["teams_available"], 1)
        # 时间推进到 P2 证书过期之后：纸面两台，可兑现只剩一台。
        self.clock.advance(days=6)
        capability = self.service.region_capability("sA")
        self.assertEqual(capability["deliverable"]["categories"]["pump"]["count"], 1)
        reasons = {item["equipment_id"]: item["reason"] for item in capability["equipment"]}
        self.assertEqual(reasons["P2"], "检验证书已过期")
        self.assertIsNone(reasons["P1"])

    def test_equipment_without_certificate_is_not_deliverable(self):
        self.service.register_equipment(
            request_id="eq-P9", actor_id="admin1", equipment_id="P9", organization_id="orgA",
            site_id="sA", category="pump", name="无证书泵", capability={"flow_rate_m3h": 3000},
            environments=["urban_flood"])
        capability = self.service.region_capability("sA")
        self.assertNotIn("pump", capability["deliverable"]["categories"])
        self.assertEqual(capability["equipment"][0]["reason"], "无有效检验记录")

    # ------------------------------------------------------------------
    # 重复告警与幂等
    # ------------------------------------------------------------------

    def test_duplicate_alert_does_not_reoccupy_equipment(self):
        self.add_equipment("P1")
        self.add_team("T1")
        first = self.raise_alert()
        self.assertTrue(first["reserved"])
        # 同一 alert_id 但不同 request_id：业务层面的重复告警。
        second, _ = self.service.raise_alert(
            request_id="alert-AL-1-dup", actor_id="op1", alert_id="AL-1", site_id="sA",
            severity="orange", priority=5, requirement=requirement())
        self.assertTrue(second["duplicate_alert"])
        self.assertEqual(second["task_id"], first["task_id"])
        task = self.service.get_task(first["task_id"])
        open_commitments = [c for c in task["commitments"] if c["state"] == "reserved"]
        self.assertEqual(len(open_commitments), 2)  # 一台泵 + 一支队伍，仅一套预留
        replayed, replay_flag = self.service.raise_alert(
            request_id="alert-AL-1", actor_id="op1", alert_id="AL-1", site_id="sA",
            severity="orange", priority=5, requirement=requirement())
        self.assertTrue(replay_flag)
        self.assertEqual(replayed["task_id"], first["task_id"])

    # ------------------------------------------------------------------
    # 限时预留与到期回收
    # ------------------------------------------------------------------

    def test_reservation_expires_and_recovers(self):
        self.add_equipment("P1")
        self.add_team("T1")
        alert = self.raise_alert(ttl=1)
        self.assertTrue(alert["reserved"])
        capability = self.service.region_capability("sA")
        self.assertEqual(capability["deliverable"]["categories"].get("pump", {}).get("count", 0), 0)
        self.clock.advance(minutes=2)
        summary = self.service.recover()
        self.assertEqual(summary["expired_reservations"], 2)
        task = self.service.get_task(alert["task_id"])
        self.assertEqual(task["status"], "waitlisted")
        self.assertIn("预留到期未出动", task["reasons"][0])
        capability = self.service.region_capability("sA")
        self.assertEqual(capability["deliverable"]["categories"]["pump"]["count"], 1)

    # ------------------------------------------------------------------
    # 原子核验
    # ------------------------------------------------------------------

    def test_atomic_dispatch_fails_when_certificate_expires(self):
        self.add_equipment("P1", valid_days=0)  # 证书当天有效
        self.add_team("T1")
        # 用 10 分钟有效期的证书覆盖：先让证书在出动前过期。
        self.service.record_maintenance(request_id="mt-P1-short", actor_id="admin1",
                                        equipment_id="P1",
                                        valid_until=ts(T0 + timedelta(minutes=10)),
                                        result="passed", inspector="检验员")
        alert = self.raise_alert(ttl=30)
        self.assertTrue(alert["reserved"])
        self.clock.advance(minutes=11)
        result, _ = self.service.dispatch_task(request_id="disp-1", actor_id="op1",
                                               task_id=alert["task_id"])
        self.assertFalse(result["dispatched"])
        self.assertTrue(any("检验证书" in reason for reason in result["reasons"]))
        task = self.service.get_task(alert["task_id"])
        self.assertEqual(task["status"], "waitlisted")
        self.assertEqual(task["dispatches"], [])  # 没有留下任何部分执行
        states = {c["state"] for c in task["commitments"]}
        self.assertEqual(states, {"released"})
        capability = self.service.region_capability("sA")
        self.assertEqual(capability["equipment"][0]["allocation"], "idle")

    def test_dispatch_success_records_verification_snapshot(self):
        self.add_equipment("P1")
        self.add_team("T1")
        alert = self.raise_alert()
        result, _ = self.service.dispatch_task(request_id="disp-1", actor_id="op1",
                                               task_id=alert["task_id"])
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["state"], "en_route")
        checked = {entry["resource_id"] for entry in result["checks"]}
        self.assertEqual(checked, {"P1", "T1"})
        self.assertTrue(all(entry["result"] == "passed" for entry in result["checks"]))

    # ------------------------------------------------------------------
    # 并发接受只有一个胜者
    # ------------------------------------------------------------------

    def test_concurrent_accept_has_single_winner(self):
        alert = self.raise_alert()  # 无资源，进入候补
        self.assertEqual(alert["status"], "waitlisted")
        self.add_equipment("P1")
        self.add_team("T1")
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def worker(request_id):
            barrier.wait()
            try:
                outcomes.append(self.service.accept_task(request_id=request_id, actor_id="op1",
                                                         task_id=alert["task_id"]))
            except ConflictError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"accept-{index}",))
                   for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(errors), 1)
        self.assertTrue(outcomes[0][0]["reserved"])
        self.assertEqual(self.service.get_task(alert["task_id"])["status"], "reserved")

    def test_concurrent_alerts_compete_for_single_pump(self):
        self.add_equipment("P1")
        self.add_team("T1")
        barrier = threading.Barrier(2)
        results = []

        def worker(alert_id):
            barrier.wait()
            results.append(self.raise_alert(alert_id=alert_id))

        threads = [threading.Thread(target=worker, args=(f"AL-{index}",))
                   for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = sorted(result["status"] for result in results)
        self.assertEqual(statuses, ["reserved", "waitlisted"])

    # ------------------------------------------------------------------
    # 执行中事件：部分到场、故障替代、任务延长
    # ------------------------------------------------------------------

    def test_partial_arrival_keeps_dispatch_en_route(self):
        self.add_equipment("P1")
        self.add_equipment("P2")
        self.add_team("T1")
        alert = self.raise_alert(req=requirement(count=2))
        dispatched, _ = self.service.dispatch_task(request_id="disp-1", actor_id="op1",
                                                   task_id=alert["task_id"])
        first, _ = self.service.confirm_arrival(request_id="arr-1", actor_id="op1",
                                                dispatch_id=dispatched["dispatch_id"],
                                                equipment_ids=["P1"])
        self.assertEqual(first["state"], "en_route")
        self.assertEqual(first["pending"], ["P2"])
        task = self.service.get_task(alert["task_id"])
        self.assertEqual(task["status"], "dispatched")
        second, _ = self.service.confirm_arrival(request_id="arr-2", actor_id="op1",
                                                 dispatch_id=dispatched["dispatch_id"],
                                                 equipment_ids=["P2"])
        self.assertEqual(second["state"], "on_site")
        self.assertEqual(self.service.get_task(alert["task_id"])["status"], "active")
        dispatch = self.service.get_dispatch(dispatched["dispatch_id"])
        self.assertEqual([event["kind"] for event in dispatch["events"]],
                         ["partial_arrival", "arrival"])

    def test_breakdown_triggers_replacement_from_alternative(self):
        self.add_equipment("P1")
        self.add_equipment("P3")  # 备用泵
        self.add_team("T1")
        task_id, dispatch_id, _ = self.full_mission()
        result, _ = self.service.report_breakdown(request_id="brk-1", actor_id="op1",
                                                  dispatch_id=dispatch_id, equipment_id="P1",
                                                  reason="泵体过热停机")
        self.assertTrue(result["broken"])
        replacement = result["replacement"]
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["equipment_id"], "P3")
        equipment = {item["equipment_id"]: item for item in self.service.list_equipment()}
        self.assertEqual(equipment["P1"]["status"], "out_of_service")
        self.assertEqual(equipment["P3"]["allocation"], "deployed")
        task = self.service.get_task(task_id)
        broken = [c for c in task["commitments"] if c["state"] == "broken"]
        self.assertEqual(len(broken), 1)
        # 替代装备到场后任务继续。
        self.service.confirm_arrival(request_id="arr-repl", actor_id="op1",
                                     dispatch_id=replacement["dispatch_id"],
                                     equipment_ids=["P3"])
        self.assertEqual(self.service.get_task(task_id)["status"], "active")

    def test_breakdown_without_replacement_keeps_reason(self):
        self.add_equipment("P1")
        self.add_team("T1")
        task_id, dispatch_id, _ = self.full_mission()
        result, _ = self.service.report_breakdown(request_id="brk-1", actor_id="op1",
                                                  dispatch_id=dispatch_id, equipment_id="P1",
                                                  reason="电机烧毁")
        self.assertIsNone(result["replacement"])
        self.assertTrue(any("暂无可用替代组合" in reason for reason in result["reasons"]))

    def test_extension_adjusts_only_open_commitments(self):
        self.add_equipment("P1")
        self.add_equipment("P3")
        self.add_team("T1")
        task_id, dispatch_id, _ = self.full_mission(req=requirement(count=2))
        self.service.report_breakdown(request_id="brk-1", actor_id="op1",
                                      dispatch_id=dispatch_id, equipment_id="P1",
                                      reason="轴承损坏")
        new_end = ts(T0 + timedelta(hours=48))
        result, _ = self.service.extend_task(request_id="ext-1", actor_id="op1",
                                             task_id=task_id, new_end_at=new_end)
        task = self.service.get_task(task_id)
        broken = [c for c in task["commitments"] if c["state"] == "broken"][0]
        open_commitments = [c for c in task["commitments"]
                            if c["state"] in ("deployed", "arrived")]
        self.assertEqual(result["extended_commitments"], len(open_commitments))
        self.assertNotEqual(broken["end_at"], new_end)  # 已终结的承诺保持原记录
        for commitment in open_commitments:
            self.assertEqual(commitment["end_at"], new_end)

    # ------------------------------------------------------------------
    # 跨区接管与归还验收
    # ------------------------------------------------------------------

    def test_takeover_releases_open_commitments_and_keeps_history(self):
        self.add_team("T2", org="orgB", site="sB")
        self.add_equipment("P2", org="orgB", site="sB")
        self.add_vehicle("V2", org="orgB", site="sB")
        self.add_routes()
        self.add_agreement()
        alert = self.raise_alert()
        self.assertTrue(alert["reserved"])
        dispatched, _ = self.service.dispatch_task(request_id="disp-1", actor_id="op1",
                                                   task_id=alert["task_id"])
        self.assertTrue(dispatched["dispatched"])
        equipment_ids = [item["resource_id"] for item in dispatched["items"]
                         if item["resource_type"] == "equipment"]
        self.service.confirm_arrival(request_id="arr-1", actor_id="op1",
                                     dispatch_id=dispatched["dispatch_id"],
                                     equipment_ids=equipment_ids)
        original = self.service.get_dispatch(dispatched["dispatch_id"])
        # 本市新泵检修完成，可以接管。
        self.add_equipment("P1")
        self.add_team("T1")
        result, _ = self.service.takeover_task(request_id="take-1", actor_id="op1",
                                               task_id=alert["task_id"], note="本市力量到位")
        self.assertTrue(result["reserved"])
        self.assertEqual(result["previous_providers"], ["orgB"])
        task = self.service.get_task(alert["task_id"])
        superseded = [d for d in task["dispatches"] if d["state"] == "superseded"]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["dispatched_at"], original["dispatched_at"])  # 原记录保留
        self.assertEqual(superseded[0]["items"], original["items"])
        released = [c for c in task["commitments"] if c["state"] == "released"]
        self.assertEqual(len(released), 3)  # 泵 + 车 + 队伍
        new_reserved = [c for c in task["commitments"] if c["state"] == "reserved"]
        self.assertTrue(all(c["provider_org_id"] == "orgA" for c in new_reserved))
        equipment = {item["equipment_id"]: item for item in self.service.list_equipment()}
        self.assertEqual(equipment["P2"]["allocation"], "idle")

    def test_return_acceptance_completes_task_and_promotes_waitlist(self):
        self.add_equipment("P1")
        self.add_team("T1")
        task_id, dispatch_id, equipment_ids = self.full_mission()
        # 第二个告警因泵不足进入候补。
        waiting = self.raise_alert(alert_id="AL-2")
        self.assertEqual(waiting["status"], "waitlisted")
        self.service.finish_task(request_id="fin-1", actor_id="op1", task_id=task_id)
        returned, _ = self.service.confirm_return(
            request_id="ret-1", actor_id="rev1", dispatch_id=dispatch_id,
            items=[{"equipment_id": item, "passed": True} for item in equipment_ids])
        self.assertEqual(returned["state"], "closed")
        self.assertEqual(returned["task_status"], "completed")
        self.assertIn(waiting["task_id"], returned["promoted_tasks"])  # 候补自动提升
        self.assertEqual(self.service.get_task(waiting["task_id"])["status"], "reserved")
        capability = self.service.region_capability("sA")
        self.assertEqual(capability["deliverable"]["categories"].get("pump", {}).get("count", 0),
                         0)  # 又被候补任务预留
        equipment = {item["equipment_id"]: item for item in self.service.list_equipment()}
        self.assertEqual(equipment["P1"]["allocation"], "reserved")

    def test_failed_acceptance_sends_equipment_to_maintenance(self):
        self.add_equipment("P1")
        self.add_team("T1")
        task_id, dispatch_id, equipment_ids = self.full_mission()
        self.service.finish_task(request_id="fin-1", actor_id="op1", task_id=task_id)
        self.service.confirm_return(request_id="ret-1", actor_id="rev1", dispatch_id=dispatch_id,
                                    items=[{"equipment_id": "P1", "passed": False,
                                            "notes": "叶轮磨损需检修"}])
        equipment = {item["equipment_id"]: item for item in self.service.list_equipment()}
        self.assertEqual(equipment["P1"]["status"], "maintenance")
        self.assertEqual(equipment["P1"]["reason"], "维修中")

    def test_executed_dispatch_record_stays_immutable(self):
        self.add_equipment("P1")
        self.add_team("T1")
        task_id, dispatch_id, equipment_ids = self.full_mission()
        before = self.service.get_dispatch(dispatch_id)
        self.service.finish_task(request_id="fin-1", actor_id="op1", task_id=task_id)
        self.service.confirm_return(request_id="ret-1", actor_id="rev1", dispatch_id=dispatch_id,
                                    items=[{"equipment_id": item, "passed": True}
                                           for item in equipment_ids])
        after = self.service.get_dispatch(dispatch_id)
        self.assertEqual(after["items"], before["items"])  # 已执行的调度保留原记录
        self.assertEqual(after["checks"], before["checks"])
        self.assertEqual(after["dispatched_at"], before["dispatched_at"])
        self.assertEqual(after["state"], "closed")

    # ------------------------------------------------------------------
    # 紧急越级：双人确认与到期回收
    # ------------------------------------------------------------------

    def _override_scenario(self):
        self.add_equipment("P2", org="orgB", site="sB")
        self.add_vehicle("V2", org="orgB", site="sB")
        self.add_team("T2", org="orgB", site="sB")
        self.add_routes()  # 无互助协议
        alert = self.raise_alert()
        self.assertTrue(alert["reserved"])
        self.assertTrue(any(r["requires_override"] for r in alert["resources"]))
        return alert

    def test_override_requires_two_person_confirmation(self):
        alert = self._override_scenario()
        denied, _ = self.service.dispatch_task(request_id="disp-1", actor_id="op1",
                                               task_id=alert["task_id"])
        self.assertFalse(denied["dispatched"])
        self.assertTrue(any("互助协议" in reason for reason in denied["reasons"]))
        # 核验失败后重新接受，再申请越级。
        self.service.accept_task(request_id="accept-1", actor_id="op1", task_id=alert["task_id"])
        override, _ = self.service.request_override(request_id="ovr-1", actor_id="op1",
                                                    task_id=alert["task_id"],
                                                    reason="乙市唯一可用大流量泵", ttl_minutes=60)
        with self.assertRaises(PermissionDenied):
            self.service.confirm_override(request_id="ovr-confirm-self", actor_id="op1",
                                          override_id=override["override_id"])
        confirmed, _ = self.service.confirm_override(request_id="ovr-confirm", actor_id="rev1",
                                                     override_id=override["override_id"])
        self.assertEqual(confirmed["state"], "confirmed")
        dispatched, _ = self.service.dispatch_task(request_id="disp-2", actor_id="op1",
                                                   task_id=alert["task_id"])
        self.assertTrue(dispatched["dispatched"])
        self.assertEqual(dispatched["eta_at"], ts(T0 + timedelta(minutes=60)))

    def test_expired_override_recalls_unexecuted_commitments(self):
        alert = self._override_scenario()
        override, _ = self.service.request_override(request_id="ovr-1", actor_id="op1",
                                                    task_id=alert["task_id"],
                                                    reason="紧急越级", ttl_minutes=1)
        self.service.confirm_override(request_id="ovr-confirm", actor_id="rev1",
                                      override_id=override["override_id"])
        self.clock.advance(minutes=2)
        summary = self.service.recover()
        self.assertEqual(summary["expired_overrides"], 1)
        task = self.service.get_task(alert["task_id"])
        self.assertEqual(task["status"], "waitlisted")
        self.assertIn("越级授权到期回收", task["reasons"][0])
        states = {c["state"] for c in task["commitments"]}
        self.assertEqual(states, {"released"})
        equipment = {item["equipment_id"]: item for item in self.service.list_equipment()}
        self.assertEqual(equipment["P2"]["allocation"], "idle")

    def test_expired_override_keeps_executed_dispatch(self):
        alert = self._override_scenario()
        override, _ = self.service.request_override(request_id="ovr-1", actor_id="op1",
                                                    task_id=alert["task_id"],
                                                    reason="紧急越级", ttl_minutes=30)
        self.service.confirm_override(request_id="ovr-confirm", actor_id="rev1",
                                      override_id=override["override_id"])
        dispatched, _ = self.service.dispatch_task(request_id="disp-1", actor_id="op1",
                                                   task_id=alert["task_id"])
        self.assertTrue(dispatched["dispatched"])
        self.clock.advance(minutes=31)
        self.service.recover()
        task = self.service.get_task(alert["task_id"])
        self.assertEqual(task["status"], "dispatched")  # 已执行的调度保留原记录
        dispatch = self.service.get_dispatch(dispatched["dispatch_id"])
        self.assertEqual(dispatch["state"], "en_route")

    # ------------------------------------------------------------------
    # 候补顺序
    # ------------------------------------------------------------------

    def test_waitlist_orders_by_priority_then_time(self):
        low = self.raise_alert(alert_id="AL-low", priority=1)
        high = self.raise_alert(alert_id="AL-high", priority=9)
        mid = self.raise_alert(alert_id="AL-mid", priority=5)
        for alert in (low, high, mid):
            self.assertEqual(alert["status"], "waitlisted")
        order = [item["task_id"] for item in self.service.waitlist(site_id="sA")]
        self.assertEqual(order, [high["task_id"], mid["task_id"], low["task_id"]])
        positions = [item["position"] for item in self.service.waitlist(site_id="sA")]
        self.assertEqual(positions, [1, 2, 3])

    # ------------------------------------------------------------------
    # 重启恢复
    # ------------------------------------------------------------------

    def test_restart_resumes_transport_and_recovery(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            service = EquipmentService(database, clock=self.clock)
            domain = service.domain
            domain.register_organization(request_id="orgA", actor_id="bootstrap",
                                         organization_id="orgA", name="甲市应急中心")
            domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                                  display_name="管理员", role="admin", organization_id="orgA")
            domain.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                                  display_name="调度员", role="operator", organization_id="orgA")
            domain.register_site(request_id="sA", actor_id="admin1", site_id="sA",
                                 organization_id="orgA", name="甲市",
                                 timezone_name="Asia/Shanghai")
            service.register_equipment(request_id="eq-P1", actor_id="admin1", equipment_id="P1",
                                       organization_id="orgA", site_id="sA", category="pump",
                                       name="泵P1", capability={"flow_rate_m3h": 3000},
                                       environments=["urban_flood"])
            service.record_maintenance(request_id="mt-P1", actor_id="admin1", equipment_id="P1",
                                       valid_until=ts(T0 + timedelta(days=30)), result="passed",
                                       inspector="检验员")
            service.register_team(request_id="team-T1", actor_id="admin1", team_id="T1",
                                  organization_id="orgA", site_id="sA", name="队伍T1",
                                  qualifications=[{"code": "pump_large",
                                                   "valid_until": ts(T0 + timedelta(days=365))}])
            alert, _ = service.raise_alert(request_id="alert-1", actor_id="op1", alert_id="AL-1",
                                           site_id="sA", severity="orange", priority=5,
                                           requirement=requirement())
            dispatched, _ = service.dispatch_task(request_id="disp-1", actor_id="op1",
                                                  task_id=alert["task_id"])
            database.close()
            # 重启：新服务实例在同一数据库上恢复，运输中的调度继续。
            self.clock.advance(minutes=10)
            database2 = Database(path)
            service2 = EquipmentService(database2, clock=self.clock)
            self.assertEqual(service2.recovery_summary["inflight_dispatches"], 1)
            arrived, _ = service2.confirm_arrival(request_id="arr-1", actor_id="op1",
                                                  dispatch_id=dispatched["dispatch_id"],
                                                  equipment_ids=["P1"])
            self.assertEqual(arrived["state"], "on_site")
            service2.finish_task(request_id="fin-1", actor_id="op1", task_id=alert["task_id"])
            returned, _ = service2.confirm_return(request_id="ret-1", actor_id="op1",
                                                  dispatch_id=dispatched["dispatch_id"],
                                                  items=[{"equipment_id": "P1", "passed": True}])
            self.assertEqual(returned["task_status"], "completed")
            valid, _ = service2.domain.verify_audit()
            self.assertTrue(valid)
            database2.close()

    # ------------------------------------------------------------------
    # 权限与审计
    # ------------------------------------------------------------------

    def test_auditor_cannot_dispatch(self):
        self.add_equipment("P1")
        self.add_team("T1")
        alert = self.raise_alert()
        with self.assertRaises(PermissionDenied):
            self.service.dispatch_task(request_id="disp-aud", actor_id="aud1",
                                       task_id=alert["task_id"])

    def test_audit_chain_remains_valid(self):
        self.add_equipment("P1")
        self.add_team("T1")
        task_id, dispatch_id, equipment_ids = self.full_mission()
        self.service.finish_task(request_id="fin-1", actor_id="op1", task_id=task_id)
        self.service.confirm_return(request_id="ret-1", actor_id="rev1", dispatch_id=dispatch_id,
                                    items=[{"equipment_id": item, "passed": True}
                                           for item in equipment_ids])
        valid, count = self.service.domain.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 10)


if __name__ == "__main__":
    unittest.main()
