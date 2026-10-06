"""运行灾害装备战备与调拨服务的离线端到端验收。

场景：甲市（orgA）与乙市（orgB）签订互助协议。甲市先完成一次本市排水任务
（预留→原子核验出动→到场→收尾→归还验收），随后发起需要两市泵组协同的
跨区任务，在运输途中模拟服务重启，验证重启后运输、交接和复原流程继续。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from science_strategy_foundation.storage import Database

from .clock import MutableClock
from .service import EquipmentService

T0 = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _requirement(flow: int, count: int) -> dict:
    return {
        "environment": "urban_flood",
        "duration_hours": 6,
        "combinations": [
            {"label": "大流量排水组合",
             "items": [{"category": "pump", "min_capability": {"flow_rate_m3h": flow},
                        "count": count}],
             "teams": 1, "qualification": "pump_large"},
            {"label": "中型泵备援组合",
             "items": [{"category": "pump", "min_capability": {"flow_rate_m3h": flow // 2},
                        "count": count * 2}],
             "teams": 1, "qualification": "pump_large"},
        ],
    }


def _register_world(service: EquipmentService) -> None:
    domain = service.domain
    domain.register_organization(request_id="org-a", actor_id="bootstrap",
                                 organization_id="orgA", name="甲市应急保障中心")
    domain.register_organization(request_id="org-b", actor_id="bootstrap",
                                 organization_id="orgB", name="乙市应急保障中心")
    domain.register_actor(request_id="actor-admin", actor_id="bootstrap", new_actor_id="admin1",
                          display_name="值班管理员", role="admin", organization_id="orgA")
    domain.register_actor(request_id="actor-op", actor_id="admin1", new_actor_id="op1",
                          display_name="调度员", role="operator", organization_id="orgA")
    domain.register_actor(request_id="actor-rev", actor_id="admin1", new_actor_id="rev1",
                          display_name="复核员", role="reviewer", organization_id="orgA")
    domain.register_site(request_id="site-a", actor_id="admin1", site_id="sA",
                         organization_id="orgA", name="甲市", timezone_name="Asia/Shanghai")
    domain.register_site(request_id="site-b", actor_id="admin1", site_id="sB",
                         organization_id="orgB", name="乙市", timezone_name="Asia/Shanghai")
    valid_until = _ts(T0 + timedelta(days=30))
    for equipment_id, org, site, category, capability in [
        ("P1", "orgA", "sA", "pump", {"flow_rate_m3h": 3000}),
        ("V1", "orgA", "sA", "transport_vehicle", {"load_tonnes": 30}),
        ("P2", "orgB", "sB", "pump", {"flow_rate_m3h": 3000}),
        ("V2", "orgB", "sB", "transport_vehicle", {"load_tonnes": 30}),
    ]:
        service.register_equipment(request_id=f"eq-{equipment_id}", actor_id="admin1",
                                   equipment_id=equipment_id, organization_id=org, site_id=site,
                                   category=category, name=f"装备{equipment_id}",
                                   capability=capability, environments=["urban_flood"])
        service.record_maintenance(request_id=f"mt-{equipment_id}", actor_id="admin1",
                                   equipment_id=equipment_id, valid_until=valid_until,
                                   result="passed", inspector="检验员甲")
    for team_id, org, site in [("T1", "orgA", "sA"), ("T2", "orgB", "sB")]:
        service.register_team(request_id=f"team-{team_id}", actor_id="admin1", team_id=team_id,
                              organization_id=org, site_id=site, name=f"队伍{team_id}",
                              qualifications=[{"code": "pump_large",
                                               "valid_until": _ts(T0 + timedelta(days=365))}])
    service.register_route(request_id="route-ab", actor_id="admin1", route_id="rAB",
                           from_site_id="sA", to_site_id="sB", duration_minutes=60)
    service.register_route(request_id="route-ba", actor_id="admin1", route_id="rBA",
                           from_site_id="sB", to_site_id="sA", duration_minutes=60)
    service.register_agreement(request_id="agr-ba", actor_id="admin1", agreement_id="agrBA",
                               provider_org_id="orgB", requester_org_id="orgA",
                               categories=["pump", "transport_vehicle", "operator_team"],
                               valid_from=_ts(T0 - timedelta(days=1)),
                               valid_until=_ts(T0 + timedelta(days=30)))


def _complete_mission(service: EquipmentService, task_id: str, dispatch_id: str,
                      equipment_ids: list[str]) -> None:
    service.confirm_arrival(request_id=f"arr-{dispatch_id}", actor_id="op1",
                            dispatch_id=dispatch_id, equipment_ids=equipment_ids)
    service.finish_task(request_id=f"fin-{task_id}", actor_id="op1", task_id=task_id)
    service.confirm_return(request_id=f"ret-{dispatch_id}", actor_id="rev1",
                           dispatch_id=dispatch_id,
                           items=[{"equipment_id": item, "passed": True}
                                  for item in equipment_ids])


def run() -> dict[str, object]:
    """执行完整战备调拨链并返回验收结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "equipment.sqlite3"
        clock = MutableClock(T0)
        database = Database(path)
        service = EquipmentService(database, clock=clock)
        _register_world(service)

        # 第一阶段：甲市本市任务，走完整个生命周期。
        first, _ = service.raise_alert(request_id="alert-1", actor_id="op1", alert_id="AL-1",
                                       site_id="sA", severity="orange", priority=5,
                                       requirement=_requirement(3000, 1))
        assert first["reserved"], first
        dispatched, _ = service.dispatch_task(request_id="disp-1", actor_id="op1",
                                              task_id=first["task_id"])
        assert dispatched["dispatched"], dispatched
        _complete_mission(service, first["task_id"], dispatched["dispatch_id"], ["P1"])

        # 重复告警不得反复占用装备。
        duplicate, _ = service.raise_alert(request_id="alert-1b", actor_id="op1", alert_id="AL-1",
                                           site_id="sA", severity="orange", priority=5,
                                           requirement=_requirement(3000, 1))
        assert duplicate["duplicate_alert"] and duplicate["task_id"] == first["task_id"]

        # 第二阶段：跨区协同任务，乙市泵组经互助协议驰援。
        second, _ = service.raise_alert(request_id="alert-2", actor_id="op1", alert_id="AL-2",
                                        site_id="sA", severity="red", priority=9,
                                        requirement=_requirement(3000, 2))
        assert second["reserved"], second
        dispatched2, _ = service.dispatch_task(request_id="disp-2", actor_id="op1",
                                               task_id=second["task_id"])
        assert dispatched2["dispatched"], dispatched2
        inflight_before = service.region_capability("sA")["open_commitments"]
        database.close()

        # 模拟服务重启：运输途中恢复，继续到场、收尾与归还复原流程。
        clock.advance(minutes=30)
        database2 = Database(path)
        service2 = EquipmentService(database2, clock=clock)
        recovery = service2.recovery_summary
        assert recovery["inflight_dispatches"] >= 1, recovery
        _complete_mission(service2, second["task_id"], dispatched2["dispatch_id"],
                          ["P1", "P2", "V2"])
        capability = service2.region_capability("sA")
        valid, event_count = service2.domain.verify_audit()
        result = {
            "status": "ok",
            "first_task": service2.get_task(first["task_id"])["status"],
            "second_task": service2.get_task(second["task_id"])["status"],
            "duplicate_alert": duplicate["duplicate_alert"],
            "inflight_before_restart": inflight_before,
            "recovery": recovery,
            "pump_ready_after_return": capability["deliverable"]["categories"]
            .get("pump", {}).get("count", 0),
            "audit_events": event_count,
            "audit_valid": valid,
        }
        database2.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and result["audit_valid"] \
        and result["first_task"] == "completed" and result["second_task"] == "completed"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
