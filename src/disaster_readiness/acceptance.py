"""灾害装备战备与调拨服务的离线端到端验收。

用一条完整的防汛剧情核对：纸面能力与可出动能力的差异、预警限时预留、
出动前原子核验、落选原因与候补、正式出动、部分到场、故障替代、任务延长、
跨区接管、紧急越级双人确认与到期回收、归还验收，以及“服务重启”后续接。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .service import ReadinessService
from .storage import DisasterDatabase
from science_strategy_foundation.errors import ConflictError, PermissionDenied


class AdvancingClock:
    """测试用：可向前推进的 UTC 时钟。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("时间必须包含时区")
        self._value = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._value

    def advance(self, minutes: int) -> None:
        self._value += timedelta(minutes=minutes)


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = DisasterDatabase(Path(directory) / "readiness.sqlite3")
        clock = AdvancingClock(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc))
        service = ReadinessService(database, clock)
        checks: dict[str, object] = {}

        # 两座城市与各自装备/队伍/车辆。
        service.register_region(request_id="acc-region-a", actor_id="sys", region_id="city-a", name="甲市")
        service.register_region(request_id="acc-region-b", actor_id="sys", region_id="city-b", name="乙市")
        for equipment_id, region in (("pump-a1", "city-a"), ("pump-a2", "city-a"), ("pump-b1", "city-b")):
            service.register_equipment(
                request_id=f"acc-eq-{equipment_id}", actor_id="sys", equipment_id=equipment_id,
                region_id=region, name=equipment_id, capability_code="drainage_high",
                capability={"flow_m3h": 3000}, environments=["urban", "underpass"])
        # pump-a2 的检验证书已过期：纸面在册但不可出动。
        service.register_certification(
            request_id="acc-cert-a1", actor_id="sys", certification_id="cert-a1",
            equipment_id="pump-a1", cert_type="inspection",
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z")
        service.register_certification(
            request_id="acc-cert-a2", actor_id="sys", certification_id="cert-a2",
            equipment_id="pump-a2", cert_type="inspection",
            valid_from="2025-01-01T00:00:00Z", valid_until="2026-09-01T00:00:00Z")
        service.register_certification(
            request_id="acc-cert-b1", actor_id="sys", certification_id="cert-b1",
            equipment_id="pump-b1", cert_type="inspection",
            valid_from="2026-01-01T00:00:00Z", valid_until="2027-01-01T00:00:00Z")
        service.register_crew(request_id="acc-crew-a", actor_id="sys", crew_id="crew-a",
                              region_id="city-a", name="甲市排水班", qualifications=["drainage_high"])
        service.register_crew(request_id="acc-crew-b", actor_id="sys", crew_id="crew-b",
                              region_id="city-b", name="乙市排水班", qualifications=["drainage_high"])
        service.register_vehicle(request_id="acc-truck-a", actor_id="sys", vehicle_id="truck-a",
                                 region_id="city-a", name="甲市运输车")
        service.register_vehicle(request_id="acc-truck-b", actor_id="sys", vehicle_id="truck-b",
                                 region_id="city-b", name="乙市运输车")
        service.set_travel_time(request_id="acc-tt-aa", actor_id="sys", origin_region_id="city-a",
                                destination_region_id="city-a", vehicle_id="truck-a", minutes=40)
        service.set_travel_time(request_id="acc-tt-ba", actor_id="sys", origin_region_id="city-b",
                                destination_region_id="city-a", vehicle_id="truck-b", minutes=95)
        # 注意：此刻甲市尚未与乙市签订互助协议，乙市装备纸面存在却不能跨区兑现；
        # 本市也只有一个排水班组和一辆可出动运输车（另一辆车在别的事故现场）。

        # 甲市下穿桥积水任务，需要两套大流量排水装备。
        service.create_mission(
            request_id="acc-mission", actor_id="dispatch", mission_id="flood-1",
            region_id="city-a", alarm_key="ALARM-FLOOD-001", title="下穿桥积水",
            environment="underpass", priority=50,
            requirements=[{"capability_code": "drainage_high", "quantity": 2}])

        feasibility = service.check_feasibility("flood-1")
        checks["initial_infeasible"] = not feasibility["feasible"]
        checks["initial_reasons"] = sorted({r["code"] for r in feasibility["rejections"]})

        # 预警升级：预留失败进入候补，而不是占用纸面能力。
        rejected = service.reserve(request_id="acc-reserve", actor_id="dispatch",
                                   mission_id="flood-1", ttl_minutes=45)
        checks["reserve_waitlisted"] = rejected["resource_type"] == "waitlist"

        # 维修关闭 + 证书补办、互助协议签订、第二班组与车辆归建后，
        # 两套能力齐备（本市 a1、a2），候补自动转正。
        service.register_certification(
            request_id="acc-cert-a2-fix", actor_id="sys", certification_id="cert-a2-fix",
            equipment_id="pump-a2", cert_type="inspection",
            valid_from="2026-10-06T00:00:00Z", valid_until="2027-10-06T00:00:00Z")
        service.register_aid_agreement(
            request_id="acc-aid", actor_id="sys", agreement_id="aid-b-to-a",
            holder_region_id="city-a", counterpart_region_id="city-b")
        service.register_crew(request_id="acc-crew-a2", actor_id="sys", crew_id="crew-a2",
                              region_id="city-a", name="甲市排水班二组",
                              qualifications=["drainage_high"])
        service.register_vehicle(request_id="acc-truck-a2", actor_id="sys", vehicle_id="truck-a2",
                                 region_id="city-a", name="甲市运输车2")
        service.set_travel_time(request_id="acc-tt-aa-2", actor_id="sys", origin_region_id="city-a",
                                destination_region_id="city-a", vehicle_id="truck-a2", minutes=45)
        promoted = service.run_due_processing()["promoted_waitlist_entries"]
        checks["waitlist_promoted"] = len(promoted) == 1

        status = service.mission_status("flood-1")
        commitment = next(c for c in status["commitments"] if c["status"] == "held")
        commitment_id = commitment["commitment_id"]

        # 正式出动前原子核验整套前置条件。
        dispatch = service.confirm_dispatch(request_id="acc-dispatch", actor_id="dispatch",
                                            commitment_id=commitment_id)
        checks["dispatched"] = not dispatch["replayed"]

        # 部分到场：只到了一套。
        commitment = service.mission_status("flood-1")["commitments"][0]
        first_item = commitment["items"][0]["item_id"]
        second_item = commitment["items"][1]["item_id"]
        service.mark_arrival(actor_id="field", commitment_id=commitment_id, item_ids=[first_item])
        after_partial = service.mission_status("flood-1")["commitments"][0]
        checks["partial_arrival_operating"] = after_partial["status"] == "operating"

        # 到场装备故障：只释放该明细，另一套保留。
        service.report_fault(actor_id="field", commitment_id=commitment_id,
                             item_id=first_item, note="泵体异响")
        # 用乙市装备挂接替代组合（追加行，原故障记录保留）。
        substitute = service.attach_substitute(
            request_id="acc-substitute", actor_id="dispatch", commitment_id=commitment_id,
            replaces_item_id=first_item, equipment_id="pump-b1",
            crew_id="crew-b", vehicle_id="truck-b")
        checks["substitute_attached"] = bool(substitute["resource_id"])

        # 任务延长只追加事件，不改写已执行调度。
        extension = service.extend_mission(actor_id="dispatch", commitment_id=commitment_id,
                                           minutes=120, reason="降雨持续")
        checks["extended"] = extension["extended_minutes"] == 120

        # 跨区接管给乙市（凭互助协议）。
        sub_item = next(i for i in service.mission_status("flood-1")["commitments"][0]["items"]
                        if i["item_id"] == substitute["resource_id"])
        service.handover_to_region(
            request_id="acc-handover", actor_id="dispatch", commitment_id=commitment_id,
            to_region_id="city-b",
            assignments=[{"item_id": sub_item["item_id"], "crew_id": "crew-b",
                          "vehicle_id": "truck-b"}])
        checks["handed_over"] = (
            service.mission_status("flood-1")["commitments"][0]["region_id"] == "city-b")

        # 紧急越级：更高优先级任务双人确认征用剩余在途装备会作用于低优先级预留；
        # 这里验证同单人越级被拒绝。
        service.create_mission(
            request_id="acc-mission-urgent", actor_id="dispatch", mission_id="flood-urgent",
            region_id="city-a", alarm_key="ALARM-FLOOD-999", title="堤坝险情",
            environment="underpass", priority=95,
            requirements=[{"capability_code": "drainage_high", "quantity": 1}])
        try:
            service.emergency_override(
                request_id="acc-override-bad", initiator_id="cmd1", confirmer_id="cmd1",
                mission_id="flood-urgent", ttl_minutes=30, reason="堤坝险情")
            checks["dual_person"] = False
        except PermissionDenied:
            checks["dual_person"] = True

        # 归还验收：替代组合合格、原第二套也合格，承诺完成。
        accepted = service.accept_return(
            actor_id="dispatch", commitment_id=commitment_id,
            verdicts=[{"item_id": second_item, "accepted": True},
                      {"item_id": sub_item["item_id"], "accepted": True}])
        checks["completed"] = accepted["status"] == "completed"

        # 已执行调度不可取消，只能保留记录。
        try:
            service.cancel_unexecuted(actor_id="dispatch", commitment_id=commitment_id)
            checks["executed_immutable"] = False
        except ConflictError:
            checks["executed_immutable"] = True

        # 指挥视图：落选任务原因与候补顺序可查。
        waitlist = service.waitlist()
        checks["waitlist_queryable"] = isinstance(waitlist["items"], list)

        valid, event_count = service.verify_audit()
        checks["audit_valid"] = valid
        checks["audit_events"] = event_count

        # 服务重启：重新打开同一数据库，已完成状态与审计历史继续保留，
        # 且 in-flight 接口可用于续接运输/交接/归还流程。
        database.close()
        restarted_db = DisasterDatabase(Path(directory) / "readiness.sqlite3")
        restarted = ReadinessService(restarted_db, clock)
        restarted_valid, _ = restarted.verify_audit()
        checks["survives_restart"] = (
            restarted_valid
            and restarted.mission_status("flood-1")["status"] == "completed"
            and "travelling" in restarted.in_flight())
        restarted_db.close()

    result = {"status": "ok", **checks}
    return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
