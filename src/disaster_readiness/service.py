"""灾害装备战备与调拨领域服务。

在基础服务的 SQLite 事务、哈希审计链、可替换时钟和业务异常之上，实现
装备登记、随时间变化的能力核验、限时预留、原子出动核验、故障/部分到场/
任务延长/跨区接管/归还验收的增量调整，以及紧急越级、双人确认、到期回收、
竞争候补和服务重启后的流程续接。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from science_strategy_foundation.audit import append_event, canonical_json, digest
from science_strategy_foundation.clock import Clock, SystemClock
from science_strategy_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError

from . import enums as E

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")


class ReadinessService:
    """协调装备战备数据、承诺状态机、资源竞争和审计规则。"""

    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _get(self, connection, table: str, key_field: str, key_value: str):
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {key_field}=?", (key_value,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{table} 中不存在 {key_field}={key_value}")
        return row

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    def _commitment_event(self, connection, *, commitment_id: str, event_type: str,
                          actor_id: str, detail: dict[str, Any]) -> None:
        seq_row = connection.execute(
            "SELECT COALESCE(MAX(seq),0) AS s FROM dr_commitment_events WHERE commitment_id=?",
            (commitment_id,),
        ).fetchone()
        connection.execute(
            "INSERT INTO dr_commitment_events(event_id,commitment_id,event_type,actor_id,detail_json,occurred_at,seq) "
            "VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, commitment_id, event_type, actor_id,
             canonical_json(detail), self._now(), seq_row["s"] + 1),
        )

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM dr_request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            stored = json.loads(row["response_json"])
            return {"replayed": True, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], **stored}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO dr_request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"replayed": False, "resource_type": resource_type,
                "resource_id": resource_id, **response}

    def _txn(self):
        return self.database.transaction(immediate=True)

    def _replay_receipt(self, connection, request_id: str, action: str,
                        payload: dict[str, Any]) -> dict[str, Any] | None:
        """若请求已处理则返回重放响应，否则返回 None（供状态守卫之前调用）。"""
        request_id = self._id(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM dr_request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {"replayed": True, "resource_type": row["resource_type"],
                "resource_id": row["resource_id"], **json.loads(row["response_json"])}

    # ------------------------------------------------------------------ 基础登记

    def register_region(self, *, request_id: str, actor_id: str, region_id: str, name: str) -> dict[str, Any]:
        payload = {"region_id": region_id, "name": name}
        with self._txn() as connection:
            region_id = self._id(region_id, "region_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO dr_regions(region_id,name,created_at) VALUES(?,?,?)",
                        (region_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("地区编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="region.registered",
                            resource_type="region", resource_id=region_id, detail={"name": name})
                return "region", region_id, {"region_id": region_id}

            return self._idempotent(connection, request_id=request_id, action="register_region",
                                    payload=payload, create=create)

    def register_equipment(self, *, request_id: str, actor_id: str, equipment_id: str,
                           region_id: str, name: str, capability_code: str,
                           capability: dict[str, Any], environments: list[str],
                           active: bool = True) -> dict[str, Any]:
        if not isinstance(capability, dict) or not capability:
            raise ValidationError("capability 必须是非空对象")
        environments = [self._text(e, "environments", 80) for e in environments]
        payload = {"equipment_id": equipment_id, "region_id": region_id, "name": name,
                   "capability_code": capability_code, "capability": capability,
                   "environments": environments}
        with self._txn() as connection:
            self._get(connection, "dr_regions", "region_id", region_id)
            equipment_id = self._id(equipment_id, "equipment_id")
            capability_code = self._id(capability_code, "capability_code")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO dr_equipment(equipment_id,region_id,name,capability_code,"
                        "capability_json,environments_json,active,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (equipment_id, region_id, name, capability_code, canonical_json(capability),
                         canonical_json(environments), 1 if active else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("装备编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="equipment.registered",
                            resource_type="equipment", resource_id=equipment_id,
                            detail={"region_id": region_id, "capability_code": capability_code})
                return "equipment", equipment_id, {"equipment_id": equipment_id}

            return self._idempotent(connection, request_id=request_id, action="register_equipment",
                                    payload=payload, create=create)

    def register_certification(self, *, request_id: str, actor_id: str, certification_id: str,
                               equipment_id: str, cert_type: str, valid_from: str,
                               valid_until: str, revoked: bool = False) -> dict[str, Any]:
        payload = {"certification_id": certification_id, "equipment_id": equipment_id,
                   "cert_type": cert_type, "valid_from": valid_from, "valid_until": valid_until}
        with self._txn() as connection:
            self._get(connection, "dr_equipment", "equipment_id", equipment_id)
            certification_id = self._id(certification_id, "certification_id")
            cert_type = self._text(cert_type, "cert_type", 80)
            valid_from = self._text(valid_from, "valid_from", 40)
            valid_until = self._text(valid_until, "valid_until", 40)
            if valid_until <= valid_from:
                raise ValidationError("证书有效期截止时间必须晚于生效时间")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO dr_certifications(certification_id,equipment_id,cert_type,"
                        "valid_from,valid_until,revoked,created_at) VALUES(?,?,?,?,?,?,?)",
                        (certification_id, equipment_id, cert_type, valid_from, valid_until,
                         1 if revoked else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("证书编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="certification.registered",
                            resource_type="certification", resource_id=certification_id,
                            detail={"equipment_id": equipment_id, "valid_until": valid_until})
                return "certification", certification_id, {"certification_id": certification_id}

            return self._idempotent(connection, request_id=request_id, action="register_certification",
                                    payload=payload, create=create)

    def open_maintenance(self, *, request_id: str, actor_id: str, maintenance_id: str,
                         equipment_id: str, title: str, start_at: str, end_at: str | None = None,
                         closed: bool = False) -> dict[str, Any]:
        payload = {"maintenance_id": maintenance_id, "equipment_id": equipment_id,
                   "title": title, "start_at": start_at, "end_at": end_at}
        with self._txn() as connection:
            self._get(connection, "dr_equipment", "equipment_id", equipment_id)
            maintenance_id = self._id(maintenance_id, "maintenance_id")
            title = self._text(title, "title")
            start_at = self._text(start_at, "start_at", 40)
            if end_at is not None and end_at <= start_at:
                raise ValidationError("维修结束时间必须晚于开始时间")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO dr_maintenance(maintenance_id,equipment_id,title,start_at,end_at,closed,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (maintenance_id, equipment_id, title, start_at, end_at,
                         1 if closed else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("维修单编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="maintenance.opened",
                            resource_type="maintenance", resource_id=maintenance_id,
                            detail={"equipment_id": equipment_id, "closed": closed})
                return "maintenance", maintenance_id, {"maintenance_id": maintenance_id}

            return self._idempotent(connection, request_id=request_id, action="open_maintenance",
                                    payload=payload, create=create)

    def close_maintenance(self, *, actor_id: str, maintenance_id: str) -> dict[str, Any]:
        with self._txn() as connection:
            self._get(connection, "dr_maintenance", "maintenance_id", maintenance_id)
            connection.execute("UPDATE dr_maintenance SET closed=1, end_at=COALESCE(end_at,?) WHERE maintenance_id=?",
                               (self._now(), maintenance_id))
            self._audit(connection, actor_id=actor_id, action="maintenance.closed",
                        resource_type="maintenance", resource_id=maintenance_id, detail={})
            return {"maintenance_id": maintenance_id, "closed": True}

    def register_crew(self, *, request_id: str, actor_id: str, crew_id: str, region_id: str,
                      name: str, qualifications: list[str], active: bool = True) -> dict[str, Any]:
        qualifications = sorted({self._id(q, "qualifications") for q in qualifications})
        payload = {"crew_id": crew_id, "region_id": region_id, "name": name,
                   "qualifications": qualifications}
        with self._txn() as connection:
            self._get(connection, "dr_regions", "region_id", region_id)
            crew_id = self._id(crew_id, "crew_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO dr_crews(crew_id,region_id,name,active,created_at) VALUES(?,?,?,?,?)",
                        (crew_id, region_id, name, 1 if active else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("队伍编号已经存在") from exc
                for code in qualifications:
                    connection.execute(
                        "INSERT INTO dr_crew_qualifications(crew_id,capability_code) VALUES(?,?)",
                        (crew_id, code),
                    )
                self._audit(connection, actor_id=actor_id, action="crew.registered",
                            resource_type="crew", resource_id=crew_id,
                            detail={"region_id": region_id, "qualifications": qualifications})
                return "crew", crew_id, {"crew_id": crew_id}

            return self._idempotent(connection, request_id=request_id, action="register_crew",
                                    payload=payload, create=create)

    def register_vehicle(self, *, request_id: str, actor_id: str, vehicle_id: str, region_id: str,
                         name: str, active: bool = True) -> dict[str, Any]:
        payload = {"vehicle_id": vehicle_id, "region_id": region_id, "name": name}
        with self._txn() as connection:
            self._get(connection, "dr_regions", "region_id", region_id)
            vehicle_id = self._id(vehicle_id, "vehicle_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO dr_vehicles(vehicle_id,region_id,name,active,created_at) VALUES(?,?,?,?,?)",
                        (vehicle_id, region_id, name, 1 if active else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运输车辆编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="vehicle.registered",
                            resource_type="vehicle", resource_id=vehicle_id,
                            detail={"region_id": region_id})
                return "vehicle", vehicle_id, {"vehicle_id": vehicle_id}

            return self._idempotent(connection, request_id=request_id, action="register_vehicle",
                                    payload=payload, create=create)

    def set_travel_time(self, *, request_id: str, actor_id: str, origin_region_id: str,
                        destination_region_id: str, vehicle_id: str, minutes: int) -> dict[str, Any]:
        minutes = int(minutes)
        if minutes < 0:
            raise ValidationError("运输时长不能为负")
        payload = {"origin_region_id": origin_region_id,
                   "destination_region_id": destination_region_id,
                   "vehicle_id": vehicle_id, "minutes": minutes}
        with self._txn() as connection:
            self._get(connection, "dr_regions", "region_id", origin_region_id)
            self._get(connection, "dr_regions", "region_id", destination_region_id)
            self._get(connection, "dr_vehicles", "vehicle_id", vehicle_id)
            connection.execute(
                "INSERT INTO dr_travel_times(origin_region_id,destination_region_id,vehicle_id,minutes) "
                "VALUES(?,?,?,?) ON CONFLICT(origin_region_id,destination_region_id,vehicle_id) "
                "DO UPDATE SET minutes=excluded.minutes",
                (origin_region_id, destination_region_id, vehicle_id, minutes),
            )
            self._audit(connection, actor_id=actor_id, action="travel_time.set",
                        resource_type="travel_time",
                        resource_id=f"{origin_region_id}:{destination_region_id}:{vehicle_id}",
                        detail={"minutes": minutes})

            def create():
                return ("travel_time",
                        f"{origin_region_id}:{destination_region_id}:{vehicle_id}",
                        {"minutes": minutes})

            return self._idempotent(connection, request_id=request_id, action="set_travel_time",
                                    payload=payload, create=create)

    def register_aid_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                               holder_region_id: str, counterpart_region_id: str,
                               valid_until: str | None = None) -> dict[str, Any]:
        payload = {"agreement_id": agreement_id, "holder_region_id": holder_region_id,
                   "counterpart_region_id": counterpart_region_id, "valid_until": valid_until}
        with self._txn() as connection:
            self._get(connection, "dr_regions", "region_id", holder_region_id)
            self._get(connection, "dr_regions", "region_id", counterpart_region_id)
            if holder_region_id == counterpart_region_id:
                raise ValidationError("互助协议双方不能是同一地区")
            agreement_id = self._id(agreement_id, "agreement_id")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO dr_mutual_aid(agreement_id,holder_region_id,counterpart_region_id,"
                        "valid_until,created_at) VALUES(?,?,?,?,?)",
                        (agreement_id, holder_region_id, counterpart_region_id, valid_until, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("互助协议已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="aid_agreement.registered",
                            resource_type="aid_agreement", resource_id=agreement_id,
                            detail={"holder_region_id": holder_region_id,
                                    "counterpart_region_id": counterpart_region_id,
                                    "valid_until": valid_until})
                return "aid_agreement", agreement_id, {"agreement_id": agreement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_aid_agreement", payload=payload, create=create)

    # ------------------------------------------------------------------ 任务建档

    def create_mission(self, *, request_id: str, actor_id: str, mission_id: str, region_id: str,
                       alarm_key: str, title: str, environment: str, priority: int,
                       requirements: list[dict[str, Any]]) -> dict[str, Any]:
        priority = int(priority)
        if not 1 <= priority <= 100:
            raise ValidationError("任务优先级必须在 1 到 100 之间")
        clean_requirements = []
        for index, requirement in enumerate(requirements):
            code = self._id(requirement["capability_code"], "requirements.capability_code")
            quantity = int(requirement["quantity"])
            if quantity < 1:
                raise ValidationError("需求数量至少为 1")
            clean_requirements.append((code, quantity, index))
        payload = {"mission_id": mission_id, "region_id": region_id, "alarm_key": alarm_key,
                   "title": title, "environment": environment, "priority": priority,
                   "requirements": [{"capability_code": c, "quantity": q} for c, q, _ in clean_requirements]}
        with self._txn() as connection:
            self._get(connection, "dr_regions", "region_id", region_id)
            mission_id = self._id(mission_id, "mission_id")
            alarm_key = self._id(alarm_key, "alarm_key")
            title = self._text(title, "title")
            environment = self._text(environment, "environment", 80)
            existing = connection.execute(
                "SELECT mission_id FROM dr_missions WHERE alarm_key=?", (alarm_key,)
            ).fetchone()
            if existing:
                raise ConflictError(f"同一告警已经关联任务 {existing['mission_id']}，不得重复占用装备")

            def create():
                connection.execute(
                    "INSERT INTO dr_missions(mission_id,region_id,alarm_key,title,environment,priority,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (mission_id, region_id, alarm_key, title, environment, priority,
                     E.MISSION_OPEN, actor_id, self._now()),
                )
                for code, quantity, index in clean_requirements:
                    connection.execute(
                        "INSERT INTO dr_mission_requirements(requirement_id,mission_id,capability_code,"
                        "quantity,seq) VALUES(?,?,?,?,?)",
                        (uuid.uuid4().hex, mission_id, code, quantity, index),
                    )
                self._audit(connection, actor_id=actor_id, action="mission.created",
                            resource_type="mission", resource_id=mission_id,
                            detail={"alarm_key": alarm_key, "priority": priority,
                                    "region_id": region_id})
                return "mission", mission_id, {"mission_id": mission_id}

            return self._idempotent(connection, request_id=request_id, action="create_mission",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 能力核验

    def _cert_ok(self, connection, equipment_id: str, at: str) -> tuple[bool, str | None]:
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM dr_certifications WHERE equipment_id=? AND revoked=0 "
            "AND valid_from<=? AND valid_until>=?",
            (equipment_id, at, at),
        ).fetchone()
        if row["c"] == 0:
            exists = connection.execute(
                "SELECT 1 FROM dr_certifications WHERE equipment_id=?", (equipment_id,)
            ).fetchone()
            return False, "cert_missing" if not exists else "cert_expired"
        return True, None

    def _maintenance_open(self, connection, equipment_id: str, at: str) -> bool:
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM dr_maintenance WHERE equipment_id=? AND closed=0 "
            "AND start_at<=? AND (end_at IS NULL OR end_at>=?)",
            (equipment_id, at, at),
        ).fetchone()
        return row["c"] > 0

    def _agreement_ok(self, connection, holder_region: str, equipment_region: str,
                      at: str) -> tuple[bool, str | None]:
        if holder_region == equipment_region:
            return True, None
        row = connection.execute(
            "SELECT * FROM dr_mutual_aid WHERE holder_region_id=? AND counterpart_region_id=?",
            (holder_region, equipment_region),
        ).fetchone()
        if row is None:
            return False, "agreement_missing"
        if row["valid_until"] is not None and row["valid_until"] < at:
            return False, "agreement_expired"
        return True, None

    def _lock_owner(self, connection, resource_type: str, resource_id: str):
        return connection.execute(
            "SELECT * FROM dr_resource_locks WHERE resource_type=? AND resource_id=?",
            (resource_type, resource_id),
        ).fetchone()

    def _evaluate(self, connection, *, mission_row, at: str,
                  hold_commitment_id: str | None = None,
                  preempt_below_priority: int | None = None) -> dict[str, Any]:
        """在即时事务内原子评估整套需求。

        评估过程只在内存中簿记“本方案拟占用”的资源，不写锁表；只有评估整体
        可行后，由持久化步骤一次性写入，因此不可行时不会影响任何既有承诺。
        """

        region_id = mission_row["region_id"]
        environment = mission_row["environment"]
        requirements = connection.execute(
            "SELECT * FROM dr_mission_requirements WHERE mission_id=? ORDER BY seq",
            (mission_row["mission_id"],),
        ).fetchall()

        chosen: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        stolen_items: dict[str, str] = {}  # item_id -> 被越级承诺 id
        taken: set[tuple[str, str]] = set()

        def owner_commitment(lock):
            return connection.execute(
                "SELECT * FROM dr_commitments WHERE commitment_id=?",
                (lock["commitment_id"],),
            ).fetchone()

        def probe(resource_type: str, resource_id: str) -> tuple[bool, object, bool]:
            """只读探测：(可否占用, 当前锁, 是否来自可越级软预留)，不改簿记。"""
            if (resource_type, resource_id) in taken:
                return False, None, False
            lock = self._lock_owner(connection, resource_type, resource_id)
            if lock is None:
                return True, None, False
            if hold_commitment_id and lock["commitment_id"] == hold_commitment_id:
                return True, lock, False
            if preempt_below_priority is not None and lock["item_id"]:
                owner = owner_commitment(lock)
                if (owner is not None and owner["status"] == E.COMMITMENT_HELD
                        and owner["kind"] == E.KIND_RESERVE
                        and owner["priority"] < preempt_below_priority):
                    return True, lock, True
            return False, lock, False

        def commit_take(resource_type: str, resource_id: str, lock, stolen: bool) -> None:
            taken.add((resource_type, resource_id))
            if stolen and lock is not None and lock["item_id"]:
                stolen_items[lock["item_id"]] = lock["commitment_id"]

        for requirement in requirements:
            code = requirement["capability_code"]
            need = requirement["quantity"]
            candidates = connection.execute(
                "SELECT * FROM dr_equipment WHERE capability_code=? ORDER BY "
                "(region_id<>?) ASC, equipment_id ASC",
                (code, region_id),
            ).fetchall()
            assigned = 0
            for equipment in candidates:
                if assigned >= need:
                    break
                eq_id = equipment["equipment_id"]
                eq_ok, eq_lock, eq_stolen = probe("equipment", eq_id)
                if not eq_ok:
                    rejections.append({"subject": "equipment", "code": "equipment_busy",
                                       "capability_code": code, "resource_id": eq_id,
                                       "message": "装备已被其他承诺占用", "capacity": True})
                    continue
                if not equipment["active"]:
                    rejections.append({"subject": "equipment", "code": "equipment_inactive",
                                       "capability_code": code, "resource_id": eq_id,
                                       "message": "装备已停用", "capacity": False})
                    continue
                if environment not in json.loads(equipment["environments_json"]):
                    rejections.append({"subject": "equipment", "code": "env_unsuitable",
                                       "capability_code": code, "resource_id": eq_id,
                                       "message": f"装备不适合部署环境 {environment}", "capacity": False})
                    continue
                if self._maintenance_open(connection, eq_id, at):
                    rejections.append({"subject": "equipment", "code": "maintenance_open",
                                       "capability_code": code, "resource_id": eq_id,
                                       "message": "装备存在未结束的维修", "capacity": False})
                    continue
                cert_ok, cert_code = self._cert_ok(connection, eq_id, at)
                if not cert_ok:
                    rejections.append({"subject": "equipment", "code": cert_code,
                                       "capability_code": code, "resource_id": eq_id,
                                       "message": "检验证书缺失或已过期", "capacity": False})
                    continue
                agreement_ok, agreement_code = self._agreement_ok(
                    connection, region_id, equipment["region_id"], at)
                if not agreement_ok:
                    rejections.append({"subject": "equipment", "code": agreement_code,
                                       "capability_code": code, "resource_id": eq_id,
                                       "message": "跨区调用缺少有效互助协议", "capacity": False})
                    continue

                crew_row = crew_lock = crew_stolen = None
                crew_candidates = connection.execute(
                    "SELECT c.* FROM dr_crews c JOIN dr_crew_qualifications q ON c.crew_id=q.crew_id "
                    "WHERE q.capability_code=? AND c.region_id=? AND c.active=1 ORDER BY c.crew_id",
                    (code, equipment["region_id"]),
                ).fetchall()
                saw_qualified = len(crew_candidates) > 0
                for crew in crew_candidates:
                    ok, clock_, stolen = probe("crew", crew["crew_id"])
                    if ok:
                        crew_row, crew_lock, crew_stolen = crew, clock_, stolen
                        break
                    rejections.append({"subject": "crew", "code": "crew_busy",
                                       "capability_code": code, "resource_id": crew["crew_id"],
                                       "message": "合格操作队伍已被占用", "capacity": True})
                if crew_row is None:
                    rejections.append({"subject": "crew",
                                       "code": "crew_busy" if saw_qualified else "crew_not_qualified",
                                       "capability_code": code, "resource_id": eq_id,
                                       "message": "没有空闲且具备资格的操作队伍",
                                       "capacity": saw_qualified})
                    continue

                vehicle_row = vehicle_lock = vehicle_stolen = None
                travel = connection.execute(
                    "SELECT v.*, t.minutes FROM dr_vehicles v JOIN dr_travel_times t "
                    "ON v.vehicle_id=t.vehicle_id WHERE v.active=1 AND v.region_id=? "
                    "AND t.origin_region_id=? AND t.destination_region_id=? "
                    "ORDER BY t.minutes ASC, v.vehicle_id ASC",
                    (equipment["region_id"], equipment["region_id"], region_id),
                ).fetchall()
                for option in travel:
                    ok, vlock, stolen = probe("vehicle", option["vehicle_id"])
                    if ok:
                        vehicle_row, vehicle_lock, vehicle_stolen = option, vlock, stolen
                        break
                    rejections.append({"subject": "vehicle", "code": "vehicle_busy",
                                       "capability_code": code,
                                       "resource_id": option["vehicle_id"],
                                       "message": "运输车辆已被占用", "capacity": True})
                if vehicle_row is None:
                    vehicle_exists = connection.execute(
                        "SELECT COUNT(*) AS c FROM dr_vehicles WHERE region_id=? AND active=1",
                        (equipment["region_id"],),
                    ).fetchone()["c"]
                    rejections.append({
                        "subject": "vehicle",
                        "code": "vehicle_missing" if vehicle_exists == 0 else "travel_unknown",
                        "capability_code": code, "resource_id": eq_id,
                        "message": "缺少可用车辆或没有运输时长数据", "capacity": False})
                    continue

                commit_take("equipment", eq_id, eq_lock, eq_stolen)
                commit_take("crew", crew_row["crew_id"], crew_lock, crew_stolen)
                commit_take("vehicle", vehicle_row["vehicle_id"], vehicle_lock, vehicle_stolen)
                assigned += 1
                chosen.append({
                    "requirement_id": requirement["requirement_id"],
                    "capability_code": code,
                    "equipment_id": eq_id,
                    "equipment_region_id": equipment["region_id"],
                    "crew_id": crew_row["crew_id"],
                    "vehicle_id": vehicle_row["vehicle_id"],
                    "eta_minutes": vehicle_row["minutes"],
                })

            if assigned < need:
                rejections.append({"subject": "requirement",
                                   "code": "capacity_shortfall",
                                   "capability_code": code,
                                   "resource_id": requirement["requirement_id"],
                                   "message": f"能力 {code} 需要 {need} 套，只能满足 {assigned} 套",
                                   "capacity": True, "required": need, "satisfied": assigned})

        feasible = all(
            not (r["subject"] == "requirement" and r["code"] == "capacity_shortfall")
            for r in rejections
        )
        return {"feasible": feasible, "items": chosen, "rejections": rejections,
                "stolen_items": stolen_items}


    def check_feasibility(self, mission_id: str, *, at: str | None = None) -> dict[str, Any]:
        """只做可行性核验，不写入任何占用。"""
        at = at or self._now()
        connection = self.database.connection
        mission = self._get(connection, "dr_missions", "mission_id", mission_id)
        result = self._evaluate(connection, mission_row=mission, at=at)
        return {"mission_id": mission_id, "feasible": result["feasible"],
                "items": result["items"], "rejections": result["rejections"]}

    # ------------------------------------------------------------------ 预留与出动

    def _persist_commitment(self, connection, *, mission, kind: str, status: str, actor_id: str,
                            expires_at: str | None, items: list[dict[str, Any]],
                            source_commitment_id: str | None = None,
                            waitlist_entry_id: str | None = None) -> str:
        commitment_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO dr_commitments(commitment_id,mission_id,region_id,kind,status,priority,"
            "environment,created_by,created_at,expires_at,source_commitment_id,waitlist_entry_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (commitment_id, mission["mission_id"], mission["region_id"], kind, status,
             mission["priority"], mission["environment"], actor_id, self._now(), expires_at,
             source_commitment_id, waitlist_entry_id),
        )
        for seq, item in enumerate(items):
            item_id = uuid.uuid4().hex
            initial = E.ITEM_PLANNED
            connection.execute(
                "INSERT INTO dr_commitment_items(item_id,commitment_id,requirement_id,equipment_id,"
                "vehicle_id,crew_id,origin_region_id,eta_minutes,status,seq) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (item_id, commitment_id, item["requirement_id"], item["equipment_id"],
                 item["vehicle_id"], item["crew_id"], item["equipment_region_id"],
                 item["eta_minutes"], initial, seq),
            )
            for resource_type, resource_id in (
                    ("equipment", item["equipment_id"]),
                    ("vehicle", item["vehicle_id"]),
                    ("crew", item["crew_id"])):
                connection.execute(
                    "INSERT INTO dr_resource_locks(resource_type,resource_id,commitment_id,item_id,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(resource_type,resource_id) DO UPDATE SET "
                    "commitment_id=excluded.commitment_id,item_id=excluded.item_id,"
                    "updated_at=excluded.updated_at",
                    (resource_type, resource_id, commitment_id, item_id, self._now()),
                )
        return commitment_id

    def reserve(self, *, request_id: str, actor_id: str, mission_id: str, ttl_minutes: int,
                join_waitlist_on_reject: bool = True) -> dict[str, Any]:
        """预警升级时限时预留资源。重复告警由任务 alarm_key 唯一约束拦截。"""
        ttl_minutes = int(ttl_minutes)
        if ttl_minutes <= 0:
            raise ValidationError("预留有效期必须为正数")
        payload = {"mission_id": mission_id, "ttl_minutes": ttl_minutes}
        with self._txn() as connection:
            replay = self._replay_receipt(connection, request_id, "reserve", payload)
            if replay is not None:
                return replay
            mission = self._get(connection, "dr_missions", "mission_id", mission_id)
            if mission["status"] != E.MISSION_OPEN:
                raise ConflictError("任务已经存在预留或已结束，重复告警不得再次占用装备")

            def create():
                at = self._now()
                result = self._evaluate(connection, mission_row=mission, at=at)
                if not result["feasible"]:
                    entry_id = None
                    if join_waitlist_on_reject:
                        entry_id = self._append_waitlist(connection, mission=mission,
                                                        payload={"ttl_minutes": ttl_minutes},
                                                        rejections=result["rejections"],
                                                        actor_id=actor_id, at=at)
                    return "waitlist" if entry_id else "rejection", entry_id or mission_id, {
                        "mission_id": mission_id,
                        "feasible": False,
                        "rejections": self._dedup_rejections(result["rejections"]),
                        "waitlist_entry_id": entry_id,
                    }
                expires_at = self._iso_after(ttl_minutes)
                commitment_id = self._persist_commitment(
                    connection, mission=mission, kind=E.KIND_RESERVE, status=E.COMMITMENT_HELD,
                    actor_id=actor_id, expires_at=expires_at, items=result["items"])
                connection.execute(
                    "UPDATE dr_missions SET status=? WHERE mission_id=?",
                    (E.MISSION_RESERVED, mission_id),
                )
                self._commitment_event(connection, commitment_id=commitment_id,
                                       event_type="reserved", actor_id=actor_id,
                                       detail={"expires_at": expires_at, "items": result["items"]})
                self._audit(connection, actor_id=actor_id, action="commitment.reserved",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"mission_id": mission_id, "expires_at": expires_at})
                return "commitment", commitment_id, {
                    "commitment_id": commitment_id, "feasible": True,
                    "expires_at": expires_at,
                    "eta_minutes": max((i["eta_minutes"] for i in result["items"]), default=0)}

            return self._idempotent(connection, request_id=request_id, action="reserve",
                                    payload=payload, create=create)

    def _iso_after(self, minutes: int) -> str:
        from datetime import timedelta
        return (self.clock.now() + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

    def _dedup_rejections(self, rejections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str | None, str]] = set()
        out = []
        for rejection in rejections:
            key = (rejection["subject"], rejection["code"], rejection.get("resource_id"))
            if key in seen:
                continue
            seen.add(key)
            out.append(rejection)
        return out

    def confirm_dispatch(self, *, request_id: str, actor_id: str, commitment_id: str) -> dict[str, Any]:
        """正式出动前重新原子核验整套前置条件，任一不满足则拒绝出动。"""
        payload = {"commitment_id": commitment_id}
        with self._txn() as connection:
            commitment = self._get(connection, "dr_commitments", "commitment_id", commitment_id)
            if commitment["status"] != E.COMMITMENT_HELD:
                raise ConflictError("只有限时预留状态可以确认出动")
            if commitment["expires_at"] is not None and commitment["expires_at"] < self._now():
                raise ConflictError("预留已经到期，请重新申请")

            def create():
                at = self._now()
                mission = self._get(connection, "dr_missions", "mission_id", commitment["mission_id"])
                result = self._evaluate(connection, mission_row=mission, at=at,
                                        hold_commitment_id=commitment_id)
                if not result["feasible"]:
                    raise ConflictError(json.dumps(
                        {"message": "出动前置条件核验失败",
                         "rejections": self._dedup_rejections(result["rejections"])},
                        ensure_ascii=False))
                item_rows = connection.execute(
                    "SELECT * FROM dr_commitment_items WHERE commitment_id=? ORDER BY seq",
                    (commitment_id,),
                ).fetchall()
                current = {(i["equipment_id"], i["crew_id"], i["vehicle_id"]) for i in item_rows}
                proposed = {(i["equipment_id"], i["crew_id"], i["vehicle_id"]) for i in result["items"]}
                if current != proposed:
                    raise ConflictError(json.dumps(
                        {"message": "出动前置条件核验失败：可兑现组合发生变化",
                         "rejections": self._dedup_rejections(result["rejections"])},
                        ensure_ascii=False))
                connection.execute(
                    "UPDATE dr_commitment_items SET status=? WHERE commitment_id=? AND status=?",
                    (E.ITEM_IN_TRANSIT, commitment_id, E.ITEM_PLANNED),
                )
                connection.execute(
                    "UPDATE dr_commitments SET status=?,confirmed_by=?,confirmed_at=?,version=version+1 "
                    "WHERE commitment_id=?",
                    (E.COMMITMENT_DISPATCHED, actor_id, at, commitment_id),
                )
                connection.execute("UPDATE dr_missions SET status=? WHERE mission_id=?",
                                   (E.MISSION_DISPATCHED, commitment["mission_id"]))
                self._commitment_event(connection, commitment_id=commitment_id,
                                       event_type="dispatched", actor_id=actor_id,
                                       detail={"confirmed_at": at})
                self._audit(connection, actor_id=actor_id, action="commitment.dispatched",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"mission_id": commitment["mission_id"]})
                return "commitment", commitment_id, {"commitment_id": commitment_id, "dispatched": True}

            return self._idempotent(connection, request_id=request_id, action="confirm_dispatch",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 执行期调整

    def _release_lock(self, connection, *, item_row) -> None:
        for resource_type in ("equipment", "vehicle", "crew"):
            resource_id = item_row[f"{resource_type}_id"]
            connection.execute(
                "DELETE FROM dr_resource_locks WHERE resource_type=? AND resource_id=? AND item_id=?",
                (resource_type, resource_id, item_row["item_id"]),
            )

    def mark_arrival(self, *, actor_id: str, commitment_id: str, item_ids: list[str]) -> dict[str, Any]:
        """登记到场，允许只登记部分到场。"""
        with self._txn() as connection:
            commitment = self._get(connection, "dr_commitments", "commitment_id", commitment_id)
            if commitment["status"] not in (E.COMMITMENT_DISPATCHED, E.COMMITMENT_OPERATING):
                raise ConflictError("只有出动中的承诺可以登记到场")
            arrived = []
            for item_id in item_ids:
                item = self._get(connection, "dr_commitment_items", "item_id", item_id)
                if item["commitment_id"] != commitment_id:
                    raise ValidationError("明细不属于该承诺")
                if item["status"] != E.ITEM_IN_TRANSIT:
                    raise ConflictError(f"明细 {item_id} 当前状态 {item['status']}，不能登记到场")
                connection.execute(
                    "UPDATE dr_commitment_items SET status=?,arrived_at=? WHERE item_id=?",
                    (E.ITEM_ARRIVED, self._now(), item_id),
                )
                arrived.append(item_id)
            connection.execute(
                "UPDATE dr_commitments SET status=?,version=version+1 WHERE commitment_id=? AND status=?",
                (E.COMMITMENT_OPERATING, commitment_id, E.COMMITMENT_DISPATCHED),
            )
            self._commitment_event(connection, commitment_id=commitment_id, event_type="arrived",
                                   actor_id=actor_id, detail={"item_ids": arrived, "partial": True})
            self._audit(connection, actor_id=actor_id, action="commitment.arrived",
                        resource_type="commitment", resource_id=commitment_id,
                        detail={"item_ids": arrived})
            return {"commitment_id": commitment_id, "arrived_item_ids": arrived}

    def report_fault(self, *, actor_id: str, commitment_id: str, item_id: str,
                    note: str = "") -> dict[str, Any]:
        """登记故障。只释放该明细占用，承诺内其他明细和已执行记录不变。"""
        with self._txn() as connection:
            commitment = self._get(connection, "dr_commitments", "commitment_id", commitment_id)
            if commitment["status"] not in E.ACTIVE_COMMITMENT_STATUSES:
                raise ConflictError("承诺已结束，不能再登记故障")
            item = self._get(connection, "dr_commitment_items", "item_id", item_id)
            if item["commitment_id"] != commitment_id:
                raise ValidationError("明细不属于该承诺")
            if item["status"] in E.ITEM_TERMINAL_STATUSES:
                raise ConflictError("该明细已经终态，保留原记录")
            self._release_lock(connection, item_row=item)
            connection.execute(
                "UPDATE dr_commitment_items SET status=?,note=? WHERE item_id=?",
                (E.ITEM_FAULTY, note, item_id),
            )
            self._commitment_event(connection, commitment_id=commitment_id, event_type="item_faulted",
                                   actor_id=actor_id,
                                   detail={"item_id": item_id, "equipment_id": item["equipment_id"],
                                           "note": note})
            self._audit(connection, actor_id=actor_id, action="commitment.item_faulted",
                        resource_type="commitment_item", resource_id=item_id, detail={})
            return {"commitment_id": commitment_id, "item_id": item_id, "status": E.ITEM_FAULTY}

    def attach_substitute(self, *, request_id: str, actor_id: str, commitment_id: str,
                          replaces_item_id: str, equipment_id: str, crew_id: str,
                          vehicle_id: str) -> dict[str, Any]:
        """为故障/未到位明细挂接替代组合，作为追加行而非改写原记录。"""
        payload = {"commitment_id": commitment_id, "replaces_item_id": replaces_item_id,
                   "equipment_id": equipment_id, "crew_id": crew_id, "vehicle_id": vehicle_id}
        with self._txn() as connection:
            commitment = self._get(connection, "dr_commitments", "commitment_id", commitment_id)
            if commitment["status"] not in (E.COMMITMENT_HELD, E.COMMITMENT_DISPATCHED,
                                            E.COMMITMENT_OPERATING):
                raise ConflictError("当前承诺状态不能追加替代组合")
            old_item = self._get(connection, "dr_commitment_items", "item_id", replaces_item_id)
            if old_item["commitment_id"] != commitment_id:
                raise ValidationError("被替代明细不属于该承诺")
            if old_item["status"] not in (E.ITEM_FAULTY, E.ITEM_CANCELLED):
                raise ConflictError("只能替代故障或已取消的明细")

            def create():
                at = self._now()
                equipment = self._get(connection, "dr_equipment", "equipment_id", equipment_id)
                crew = self._get(connection, "dr_crews", "crew_id", crew_id)
                vehicle = self._get(connection, "dr_vehicles", "vehicle_id", vehicle_id)
                if not equipment["active"] or not crew["active"] or not vehicle["active"]:
                    raise ConflictError("替代资源包含已停用对象")
                cert_ok, cert_code = self._cert_ok(connection, equipment_id, at)
                if not cert_ok:
                    raise ConflictError(f"替代装备核验失败: {cert_code}")
                if self._maintenance_open(connection, equipment_id, at):
                    raise ConflictError("替代装备存在未结束维修")
                qualified = connection.execute(
                    "SELECT 1 FROM dr_crew_qualifications WHERE crew_id=? AND capability_code=?",
                    (crew_id, equipment["capability_code"]),
                ).fetchone()
                if not qualified:
                    raise ConflictError("替代队伍不具备该能力资格")
                travel = connection.execute(
                    "SELECT minutes FROM dr_travel_times WHERE origin_region_id=? "
                    "AND destination_region_id=? AND vehicle_id=?",
                    (equipment["region_id"], commitment["region_id"], vehicle_id),
                ).fetchone()
                if travel is None:
                    raise ConflictError("替代组合缺少运输时长数据")
                agreement_ok, agreement_code = self._agreement_ok(
                    connection, commitment["region_id"], equipment["region_id"], at)
                if not agreement_ok:
                    raise ConflictError(f"替代装备跨区协议无效: {agreement_code}")
                for resource_type, resource_id in (("equipment", equipment_id),
                                                   ("crew", crew_id), ("vehicle", vehicle_id)):
                    lock = self._lock_owner(connection, resource_type, resource_id)
                    if lock is not None and lock["commitment_id"] != commitment_id:
                        raise ConflictError(f"替代资源 {resource_type}:{resource_id} 已被占用")
                seq_row = connection.execute(
                    "SELECT COALESCE(MAX(seq),-1)+1 AS s FROM dr_commitment_items WHERE commitment_id=?",
                    (commitment_id,),
                ).fetchone()
                new_item_id = uuid.uuid4().hex
                initial_status = (E.ITEM_PLANNED if commitment["status"] == E.COMMITMENT_HELD
                                  else E.ITEM_IN_TRANSIT)
                connection.execute(
                    "INSERT INTO dr_commitment_items(item_id,commitment_id,requirement_id,equipment_id,"
                    "vehicle_id,crew_id,origin_region_id,eta_minutes,status,seq,replaces_item_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (new_item_id, commitment_id, old_item["requirement_id"], equipment_id,
                     vehicle_id, crew_id, equipment["region_id"], travel["minutes"],
                     initial_status, seq_row["s"], replaces_item_id),
                )
                for resource_type, resource_id in (("equipment", equipment_id),
                                                   ("crew", crew_id), ("vehicle", vehicle_id)):
                    connection.execute(
                        "INSERT INTO dr_resource_locks(resource_type,resource_id,commitment_id,item_id,updated_at) "
                        "VALUES(?,?,?,?,?)",
                        (resource_type, resource_id, commitment_id, new_item_id, at),
                    )
                connection.execute(
                    "UPDATE dr_commitments SET version=version+1 WHERE commitment_id=?",
                    (commitment_id,),
                )
                self._commitment_event(connection, commitment_id=commitment_id,
                                       event_type="substitute_attached", actor_id=actor_id,
                                       detail={"new_item_id": new_item_id,
                                               "replaces_item_id": replaces_item_id,
                                               "equipment_id": equipment_id, "crew_id": crew_id,
                                               "vehicle_id": vehicle_id})
                self._audit(connection, actor_id=actor_id, action="commitment.substitute_attached",
                            resource_type="commitment_item", resource_id=new_item_id,
                            detail={"commitment_id": commitment_id})
                return "commitment_item", new_item_id, {"item_id": new_item_id}

            return self._idempotent(connection, request_id=request_id, action="attach_substitute",
                                    payload=payload, create=create)

    def extend_mission(self, *, actor_id: str, commitment_id: str, minutes: int,
                       reason: str = "") -> dict[str, Any]:
        """任务延长仅对在执行承诺追加延长记录，不改变已经执行的调度事实。"""
        minutes = int(minutes)
        if minutes <= 0:
            raise ValidationError("延长分钟数必须为正数")
        with self._txn() as connection:
            commitment = self._get(connection, "dr_commitments", "commitment_id", commitment_id)
            if commitment["status"] not in (E.COMMITMENT_DISPATCHED, E.COMMITMENT_OPERATING):
                raise ConflictError("只有执行中的承诺可以延长")
            connection.execute(
                "UPDATE dr_commitments SET version=version+1 WHERE commitment_id=?", (commitment_id,))
            self._commitment_event(connection, commitment_id=commitment_id,
                                   event_type="extended", actor_id=actor_id,
                                   detail={"minutes": minutes, "reason": reason, "at": self._now()})
            self._audit(connection, actor_id=actor_id, action="commitment.extended",
                        resource_type="commitment", resource_id=commitment_id,
                        detail={"minutes": minutes})
            return {"commitment_id": commitment_id, "extended_minutes": minutes}

    def handover_to_region(self, *, request_id: str, actor_id: str, commitment_id: str,
                           to_region_id: str, assignments: list[dict[str, str]]) -> dict[str, Any]:
        """跨区接管：凭有效互助协议把尚未完成明细的运输车辆/操作队伍交给接管地区。

        已经归还验收或故障终态的明细不调整；原调度事实通过承诺事件永久保留。
        """
        payload = {"commitment_id": commitment_id, "to_region_id": to_region_id,
                   "assignments": assignments}
        with self._txn() as connection:
            commitment = self._get(connection, "dr_commitments", "commitment_id", commitment_id)
            if commitment["status"] not in E.ACTIVE_COMMITMENT_STATUSES:
                raise ConflictError("承诺已结束，不能跨区接管")
            self._get(connection, "dr_regions", "region_id", to_region_id)
            at = self._now()
            agreement_ok, agreement_code = self._agreement_ok(
                connection, to_region_id, commitment["region_id"], at)
            reverse_ok, _ = self._agreement_ok(
                connection, commitment["region_id"], to_region_id, at)
            if not (agreement_ok or reverse_ok):
                raise ConflictError(f"跨区接管缺少有效互助协议: {agreement_code}")

            def create():
                changes = []
                for assignment in assignments:
                    item_id = assignment["item_id"]
                    item = self._get(connection, "dr_commitment_items", "item_id", item_id)
                    if item["commitment_id"] != commitment_id:
                        raise ValidationError("明细不属于该承诺")
                    if item["status"] in E.ITEM_TERMINAL_STATUSES:
                        raise ConflictError(f"明细 {item_id} 已终态，接管不调整已完成部分")
                    new_crew_id = assignment.get("crew_id", item["crew_id"])
                    new_vehicle_id = assignment.get("vehicle_id", item["vehicle_id"])
                    crew = self._get(connection, "dr_crews", "crew_id", new_crew_id)
                    vehicle = self._get(connection, "dr_vehicles", "vehicle_id", new_vehicle_id)
                    equipment = self._get(connection, "dr_equipment", "equipment_id",
                                          item["equipment_id"])
                    if not crew["active"] or not vehicle["active"]:
                        raise ConflictError("接管资源包含已停用对象")
                    qualified = connection.execute(
                        "SELECT 1 FROM dr_crew_qualifications WHERE crew_id=? AND capability_code=?",
                        (new_crew_id, equipment["capability_code"]),
                    ).fetchone()
                    if not qualified:
                        raise ConflictError("接管队伍不具备该能力资格")
                    travel = connection.execute(
                        "SELECT minutes FROM dr_travel_times WHERE origin_region_id=? "
                        "AND destination_region_id=? AND vehicle_id=?",
                        (vehicle["region_id"], commitment["region_id"], new_vehicle_id),
                    ).fetchone()
                    if travel is None and item["status"] in E.ITEM_TRAVEL_STATUSES:
                        raise ConflictError("接管运输缺少运输时长数据")
                    for resource_type, new_id, old_id in (
                            ("crew", new_crew_id, item["crew_id"]),
                            ("vehicle", new_vehicle_id, item["vehicle_id"])):
                        if new_id == old_id:
                            continue
                        lock = self._lock_owner(connection, resource_type, new_id)
                        if lock is not None and lock["commitment_id"] != commitment_id:
                            raise ConflictError(f"接管资源 {resource_type}:{new_id} 已被占用")
                        connection.execute(
                            "DELETE FROM dr_resource_locks WHERE resource_type=? AND resource_id=? "
                            "AND commitment_id=?",
                            (resource_type, old_id, commitment_id),
                        )
                        connection.execute(
                            "INSERT INTO dr_resource_locks(resource_type,resource_id,commitment_id,"
                            "item_id,updated_at) VALUES(?,?,?,?,?)",
                            (resource_type, new_id, commitment_id, item_id, at),
                        )
                    connection.execute(
                        "UPDATE dr_commitment_items SET crew_id=?,vehicle_id=? WHERE item_id=?",
                        (new_crew_id, new_vehicle_id, item_id),
                    )
                    changes.append({"item_id": item_id, "crew_id": new_crew_id,
                                    "vehicle_id": new_vehicle_id})
                connection.execute(
                    "UPDATE dr_commitments SET region_id=?,version=version+1 WHERE commitment_id=?",
                    (to_region_id, commitment_id),
                )
                self._commitment_event(connection, commitment_id=commitment_id,
                                       event_type="handed_over", actor_id=actor_id,
                                       detail={"from_region_id": commitment["region_id"],
                                               "to_region_id": to_region_id, "changes": changes})
                self._audit(connection, actor_id=actor_id, action="commitment.handed_over",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"to_region_id": to_region_id})
                return "commitment", commitment_id, {"commitment_id": commitment_id, "changes": changes}

            return self._idempotent(connection, request_id=request_id, action="handover_to_region",
                                    payload=payload, create=create)

    def accept_return(self, *, actor_id: str, commitment_id: str,
                      verdicts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """归还验收：逐条给出合格/不合格，全部明细离开占用状态后承诺完成。"""
        verdicts = verdicts or []
        verdict_map = {v["item_id"]: v for v in verdicts}
        with self._txn() as connection:
            commitment = self._get(connection, "dr_commitments", "commitment_id", commitment_id)
            if commitment["status"] not in (E.COMMITMENT_OPERATING, E.COMMITMENT_DISPATCHED,
                                            E.COMMITMENT_RETURNING):
                raise ConflictError("当前承诺状态不能进行归还验收")
            items = connection.execute(
                "SELECT * FROM dr_commitment_items WHERE commitment_id=? ORDER BY seq",
                (commitment_id,),
            ).fetchall()
            known = {item["item_id"] for item in items}
            for item_id in verdict_map:
                if item_id not in known:
                    raise ValidationError(f"验收明细 {item_id} 不属于该承诺")
            accepted, rejected = [], []
            for item in items:
                if item["status"] in E.ITEM_TERMINAL_STATUSES:
                    continue
                verdict = verdict_map.get(item["item_id"], {"accepted": True})
                if item["status"] == E.ITEM_PLANNED:
                    # 预留未出动的装备不经过归还，需走取消路径。
                    raise ConflictError("仍处于预留状态的明细请先取消或出动")
                if verdict.get("accepted", True):
                    connection.execute(
                        "UPDATE dr_commitment_items SET status=?,returned_at=?,note=? WHERE item_id=?",
                        (E.ITEM_RETURNED, self._now(), verdict.get("note", ""), item["item_id"]),
                    )
                    accepted.append(item["item_id"])
                else:
                    connection.execute(
                        "UPDATE dr_commitment_items SET status=?,note=? WHERE item_id=?",
                        (E.ITEM_FAULTY, verdict.get("note", "验收不合格"), item["item_id"]),
                    )
                    rejected.append(item["item_id"])
                self._release_lock(connection, item_row=item)
            busy = connection.execute(
                "SELECT COUNT(*) AS c FROM dr_commitment_items WHERE commitment_id=? AND status IN (?,?,?,?)",
                (commitment_id, E.ITEM_PLANNED, E.ITEM_IN_TRANSIT, E.ITEM_ARRIVED, E.ITEM_PREEMPTED),
            ).fetchone()["c"]
            new_status = E.COMMITMENT_RETURNING if busy else E.COMMITMENT_COMPLETED
            connection.execute(
                "UPDATE dr_commitments SET status=?,version=version+1 WHERE commitment_id=?",
                (new_status, commitment_id),
            )
            self._commitment_event(connection, commitment_id=commitment_id,
                                   event_type="return_accepted" if not busy else "return_partial",
                                   actor_id=actor_id,
                                   detail={"accepted": accepted, "rejected": rejected,
                                           "completed": not busy})
            self._audit(connection, actor_id=actor_id,
                        action="commitment.completed" if not busy else "commitment.return_partial",
                        resource_type="commitment", resource_id=commitment_id,
                        detail={"accepted": accepted, "rejected": rejected})
            if not busy:
                connection.execute("UPDATE dr_missions SET status=? WHERE mission_id=?",
                                   (E.MISSION_COMPLETED, commitment["mission_id"]))
                self._try_restore_preempted(connection, commitment=commitment, at=self._now())
            return {"commitment_id": commitment_id, "status": new_status,
                    "accepted": accepted, "rejected": rejected}

    def cancel_unexecuted(self, *, actor_id: str, commitment_id: str, reason: str = "") -> dict[str, Any]:
        """取消只能作用于尚未出动的预留；已执行调度保留原记录。"""
        with self._txn() as connection:
            commitment = self._get(connection, "dr_commitments", "commitment_id", commitment_id)
            if commitment["status"] != E.COMMITMENT_HELD:
                raise ConflictError("已经出动的承诺不能取消，只能走归还验收")
            items = connection.execute(
                "SELECT * FROM dr_commitment_items WHERE commitment_id=?", (commitment_id,),
            ).fetchall()
            for item in items:
                self._release_lock(connection, item_row=item)
                connection.execute("UPDATE dr_commitment_items SET status=? WHERE item_id=?",
                                   (E.ITEM_CANCELLED, item["item_id"]))
            connection.execute(
                "UPDATE dr_commitments SET status=?,version=version+1 WHERE commitment_id=?",
                (E.COMMITMENT_CANCELLED, commitment_id),
            )
            connection.execute("UPDATE dr_missions SET status=? WHERE mission_id=?",
                               (E.MISSION_OPEN, commitment["mission_id"]))
            self._commitment_event(connection, commitment_id=commitment_id, event_type="cancelled",
                                   actor_id=actor_id, detail={"reason": reason})
            self._audit(connection, actor_id=actor_id, action="commitment.cancelled",
                        resource_type="commitment", resource_id=commitment_id,
                        detail={"reason": reason})
            self._promote_waitlist(connection, mission_id=None, at=self._now())
            return {"commitment_id": commitment_id, "status": E.COMMITMENT_CANCELLED}

    # ------------------------------------------------------------------ 紧急越级

    def emergency_override(self, *, request_id: str, initiator_id: str, confirmer_id: str,
                           mission_id: str, ttl_minutes: int, reason: str) -> dict[str, Any]:
        """紧急越级征用：双人确认（发起人与复核人不得相同），并在到期后回收恢复。"""
        ttl_minutes = int(ttl_minutes)
        if ttl_minutes <= 0:
            raise ValidationError("越级有效期必须为正数")
        if not initiator_id or not confirmer_id:
            raise ValidationError("越级必须同时填写发起人和复核人")
        if initiator_id == confirmer_id:
            raise PermissionDenied("紧急越级必须双人确认，发起人与复核人不能相同")
        reason = self._text(reason, "reason")
        payload = {"mission_id": mission_id, "ttl_minutes": ttl_minutes, "reason": reason,
                   "initiator_id": initiator_id, "confirmer_id": confirmer_id}
        with self._txn() as connection:
            mission = self._get(connection, "dr_missions", "mission_id", mission_id)

            def create():
                at = self._now()
                result = self._evaluate(connection, mission_row=mission, at=at,
                                        preempt_below_priority=mission["priority"])
                if not result["feasible"]:
                    raise ConflictError(json.dumps(
                        {"message": "即使越级也无法满足任务",
                         "rejections": self._dedup_rejections(result["rejections"])},
                        ensure_ascii=False))
                displaced: dict[str, list[str]] = {}
                # 越级按“整项”抢占：只要该低优先级预留明细的任一资源被征用，
                # 就把整条明细置为被抢占并释放其装备/车辆/队伍占用。
                for item_id, owner_id in result["stolen_items"].items():
                    old_item = connection.execute(
                        "SELECT * FROM dr_commitment_items WHERE item_id=?", (item_id,)
                    ).fetchone()
                    if old_item is None or old_item["status"] == E.ITEM_PREEMPTED:
                        continue
                    for resource_type, resource_id in (
                            ("equipment", old_item["equipment_id"]),
                            ("crew", old_item["crew_id"]),
                            ("vehicle", old_item["vehicle_id"])):
                        connection.execute(
                            "DELETE FROM dr_resource_locks WHERE resource_type=? AND resource_id=?",
                            (resource_type, resource_id),
                        )
                    connection.execute(
                        "UPDATE dr_commitment_items SET status=? WHERE item_id=?",
                        (E.ITEM_PREEMPTED, item_id),
                    )
                    displaced.setdefault(owner_id, []).append(item_id)
                for owner_id, item_ids in displaced.items():
                    remaining = connection.execute(
                        "SELECT COUNT(*) AS c FROM dr_commitment_items WHERE commitment_id=? "
                        "AND status IN (?,?,?,?)",
                        (owner_id, E.ITEM_PLANNED, E.ITEM_IN_TRANSIT, E.ITEM_ARRIVED,
                         E.ITEM_PREEMPTED),
                    ).fetchone()["c"]
                    preempted_only = connection.execute(
                        "SELECT COUNT(*) AS c FROM dr_commitment_items WHERE commitment_id=? "
                        "AND status=?",
                        (owner_id, E.ITEM_PREEMPTED),
                    ).fetchone()["c"]
                    if remaining == preempted_only and preempted_only:
                        connection.execute(
                            "UPDATE dr_commitments SET status=?,version=version+1 WHERE commitment_id=?",
                            (E.COMMITMENT_PREEMPTED, owner_id),
                        )
                    self._commitment_event(connection, commitment_id=owner_id,
                                           event_type="preempted", actor_id=initiator_id,
                                           detail={"item_ids": item_ids, "reason": reason})
                expires_at = self._iso_after(ttl_minutes)
                commitment_id = self._persist_commitment(
                    connection, mission=mission, kind=E.KIND_OVERRIDE, status=E.COMMITMENT_HELD,
                    actor_id=initiator_id, expires_at=expires_at, items=result["items"])
                override_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO dr_overrides(override_id,commitment_id,initiator_id,confirmer_id,"
                    "reason,expires_at,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (override_id, commitment_id, initiator_id, confirmer_id, reason,
                     expires_at, "active", at),
                )
                for owner_id, item_ids in displaced.items():
                    for item_id in item_ids:
                        connection.execute(
                            "INSERT INTO dr_override_displacements(displacement_id,override_id,"
                            "commitment_id,item_id,restored) VALUES(?,?,?,?,0)",
                            (uuid.uuid4().hex, override_id, owner_id, item_id),
                        )
                if mission["status"] == E.MISSION_OPEN:
                    connection.execute("UPDATE dr_missions SET status=? WHERE mission_id=?",
                                       (E.MISSION_RESERVED, mission_id))
                self._commitment_event(connection, commitment_id=commitment_id,
                                       event_type="override_reserved", actor_id=initiator_id,
                                       detail={"confirmer_id": confirmer_id, "reason": reason,
                                               "expires_at": expires_at,
                                               "displaced_commitment_ids": sorted(displaced)})
                self._audit(connection, actor_id=initiator_id, action="commitment.override_reserved",
                            resource_type="commitment", resource_id=commitment_id,
                            detail={"confirmer_id": confirmer_id, "reason": reason,
                                    "displaced": displaced})
                return "commitment", commitment_id, {
                    "commitment_id": commitment_id, "override_id": override_id,
                    "displaced_commitment_ids": sorted(displaced), "expires_at": expires_at}

            return self._idempotent(connection, request_id=request_id, action="emergency_override",
                                    payload=payload, create=create)

    def _try_restore_preempted(self, connection, *, commitment, at: str) -> None:
        """越级承诺结束时，把仍处于被抢占状态的明细恢复给原承诺（资源空闲才恢复）。"""
        rows = connection.execute(
            "SELECT d.* FROM dr_override_displacements d JOIN dr_overrides o ON d.override_id=o.override_id "
            "WHERE o.commitment_id=? AND d.restored=0",
            (commitment["commitment_id"],),
        ).fetchall()
        restored_any = False
        for row in rows:
            item = connection.execute(
                "SELECT * FROM dr_commitment_items WHERE item_id=?", (row["item_id"],)
            ).fetchone()
            if item is None or item["status"] != E.ITEM_PREEMPTED:
                connection.execute(
                    "UPDATE dr_override_displacements SET restored=1 WHERE displacement_id=?", (row["displacement_id"],))
                continue
            blockers = []
            for resource_type, resource_id in (("equipment", item["equipment_id"]),
                                               ("crew", item["crew_id"]),
                                               ("vehicle", item["vehicle_id"])):
                lock = self._lock_owner(connection, resource_type, resource_id)
                if lock is not None:
                    blockers.append((resource_type, resource_id))
            if blockers:
                continue
            connection.execute(
                "UPDATE dr_commitment_items SET status=? WHERE item_id=?",
                (E.ITEM_PLANNED, row["item_id"]),
            )
            for resource_type, resource_id in (("equipment", item["equipment_id"]),
                                               ("crew", item["crew_id"]),
                                               ("vehicle", item["vehicle_id"])):
                connection.execute(
                    "INSERT INTO dr_resource_locks(resource_type,resource_id,commitment_id,item_id,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (resource_type, resource_id, row["commitment_id"], row["item_id"], at),
                )
            connection.execute(
                "UPDATE dr_override_displacements SET restored=1 WHERE displacement_id=?", (row["displacement_id"],))
            restored_any = True
            self._commitment_event(connection, commitment_id=row["commitment_id"],
                                   event_type="preemption_restored", actor_id="system",
                                   detail={"item_id": row["item_id"]})
        if rows and all(
                connection.execute("SELECT restored FROM dr_override_displacements WHERE displacement_id=?",
                                   (r["displacement_id"],)).fetchone()["restored"] for r in rows):
            connection.execute(
                "UPDATE dr_overrides SET status='reverted',reverted_at=? WHERE commitment_id=?",
                (at, commitment["commitment_id"]),
            )
        if restored_any:
            self._revive_preempted_commitments(connection, at=at)

    def _revive_preempted_commitments(self, connection, *, at: str) -> None:
        owners = connection.execute(
            "SELECT DISTINCT commitment_id FROM dr_override_displacements d WHERE restored=1"
        ).fetchall()
        for owner in owners:
            commitment_id = owner["commitment_id"]
            status = connection.execute(
                "SELECT status FROM dr_commitments WHERE commitment_id=?", (commitment_id,)
            ).fetchone()["status"]
            if status != E.COMMITMENT_PREEMPTED:
                continue
            waiting_restore = connection.execute(
                "SELECT COUNT(*) AS c FROM dr_override_displacements WHERE commitment_id=? AND restored=0",
                (commitment_id,),
            ).fetchone()["c"]
            if waiting_restore == 0:
                connection.execute(
                    "UPDATE dr_commitments SET status=?,expires_at=?,version=version+1 WHERE commitment_id=?",
                    (E.COMMITMENT_HELD, self._iso_after(DEFAULT_REHOLD_MINUTES), commitment_id),
                )
                self._commitment_event(connection, commitment_id=commitment_id,
                                       event_type="reinstated", actor_id="system",
                                       detail={"at": at})

    # ------------------------------------------------------------------ 候补与到期

    def _append_waitlist(self, connection, *, mission, payload: dict[str, Any],
                         rejections: list[dict[str, Any]], actor_id: str, at: str) -> str:
        entry_id = uuid.uuid4().hex
        rank_row = connection.execute("SELECT COALESCE(MAX(rank),0)+1 AS r FROM dr_waitlist").fetchone()
        connection.execute(
            "INSERT INTO dr_waitlist(entry_id,mission_id,request_json,rank,status,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (entry_id, mission["mission_id"],
             canonical_json({"payload": payload,
                             "rejections": self._dedup_rejections(rejections)}),
             rank_row["r"], E.WAITLIST_WAITING, actor_id, at),
        )
        self._audit(connection, actor_id=actor_id, action="waitlist.joined",
                    resource_type="waitlist_entry", resource_id=entry_id,
                    detail={"mission_id": mission["mission_id"]})
        return entry_id

    def _promote_waitlist(self, connection, *, mission_id: str | None, at: str) -> list[str]:
        """按优先级降序、登记时间升序尝试候补转正；仍然不可行的保持候补。"""
        promoted = []
        query = ("SELECT w.*, m.priority AS mission_priority FROM dr_waitlist w "
                 "JOIN dr_missions m ON w.mission_id=m.mission_id WHERE w.status=?")
        parameters = [E.WAITLIST_WAITING]
        if mission_id is not None:
            query += " AND w.mission_id=?"
            parameters.append(mission_id)
        query += " ORDER BY m.priority DESC, w.rank ASC"
        for entry in connection.execute(query, parameters).fetchall():
            mission = connection.execute(
                "SELECT * FROM dr_missions WHERE mission_id=?", (entry["mission_id"],)
            ).fetchone()
            if mission["status"] != E.MISSION_OPEN:
                continue
            result = self._evaluate(connection, mission_row=mission, at=at)
            if not result["feasible"]:
                continue
            request = json.loads(entry["request_json"])
            ttl = int(request.get("payload", {}).get("ttl_minutes", E.DEFAULT_PROMOTION_TTL_MINUTES))
            expires_at = self._iso_after(ttl)
            commitment_id = self._persist_commitment(
                connection, mission=mission, kind=E.KIND_RESERVE, status=E.COMMITMENT_HELD,
                actor_id=entry["created_by"], expires_at=expires_at, items=result["items"],
                waitlist_entry_id=entry["entry_id"])
            connection.execute("UPDATE dr_waitlist SET status=?,commitment_id=? WHERE entry_id=?",
                               (E.WAITLIST_PROMOTED, commitment_id, entry["entry_id"]))
            connection.execute("UPDATE dr_missions SET status=? WHERE mission_id=?",
                               (E.MISSION_RESERVED, entry["mission_id"]))
            self._commitment_event(connection, commitment_id=commitment_id,
                                   event_type="reserved_from_waitlist", actor_id="system",
                                   detail={"waitlist_entry_id": entry["entry_id"],
                                           "expires_at": expires_at})
            promoted.append(entry["entry_id"])
        return promoted

    def run_due_processing(self, *, actor_id: str = "system") -> dict[str, Any]:
        """处理预留到期、越级到期回收和候补自动转正。重启后调用即可续接流程。"""
        with self._txn() as connection:
            at = self._now()
            expired = []
            for commitment in connection.execute(
                    "SELECT * FROM dr_commitments WHERE status=? AND expires_at IS NOT NULL "
                    "AND expires_at<?",
                    (E.COMMITMENT_HELD, at)).fetchall():
                if commitment["kind"] == E.KIND_OVERRIDE:
                    # 越级预留到期但尚未出动：直接回收并恢复被抢占者。
                    self._expire_override(connection, commitment=commitment, at=at, actor_id=actor_id)
                    expired.append(commitment["commitment_id"])
                    continue
                items = connection.execute(
                    "SELECT * FROM dr_commitment_items WHERE commitment_id=?",
                    (commitment["commitment_id"],),
                ).fetchall()
                for item in items:
                    if item["status"] == E.ITEM_PLANNED:
                        self._release_lock(connection, item_row=item)
                        connection.execute(
                            "UPDATE dr_commitment_items SET status=? WHERE item_id=?",
                            (E.ITEM_CANCELLED, item["item_id"]),
                        )
                connection.execute(
                    "UPDATE dr_commitments SET status=?,version=version+1 WHERE commitment_id=?",
                    (E.COMMITMENT_EXPIRED, commitment["commitment_id"]),
                )
                connection.execute("UPDATE dr_missions SET status=? WHERE mission_id=?",
                                   (E.MISSION_OPEN, commitment["mission_id"]))
                self._commitment_event(connection, commitment_id=commitment["commitment_id"],
                                       event_type="expired", actor_id=actor_id, detail={"at": at})
                self._audit(connection, actor_id=actor_id, action="commitment.expired",
                            resource_type="commitment", resource_id=commitment["commitment_id"],
                            detail={})
                expired.append(commitment["commitment_id"])
                self._promote_waitlist(connection, mission_id=None, at=at)

            # 越级已经出动的，到期后尝试恢复仍被抢占的软预留。
            for commitment in connection.execute(
                    "SELECT * FROM dr_commitments WHERE kind=? AND status<>? AND status<>?",
                    (E.KIND_OVERRIDE, E.COMMITMENT_COMPLETED, E.COMMITMENT_CANCELLED)).fetchall():
                if commitment["expires_at"] and commitment["expires_at"] < at:
                    self._try_restore_preempted(connection, commitment=commitment, at=at)

            promoted = self._promote_waitlist(connection, mission_id=None, at=at)
            return {"at": at, "expired_commitment_ids": expired, "promoted_waitlist_entries": promoted}

    def _expire_override(self, connection, *, commitment, at: str, actor_id: str) -> None:
        items = connection.execute(
            "SELECT * FROM dr_commitment_items WHERE commitment_id=?", (commitment["commitment_id"],),
        ).fetchall()
        for item in items:
            if item["status"] == E.ITEM_PLANNED:
                self._release_lock(connection, item_row=item)
                connection.execute("UPDATE dr_commitment_items SET status=? WHERE item_id=?",
                                   (E.ITEM_CANCELLED, item["item_id"]))
        connection.execute(
            "UPDATE dr_commitments SET status=?,version=version+1 WHERE commitment_id=?",
            (E.COMMITMENT_EXPIRED, commitment["commitment_id"]),
        )
        mission = connection.execute("SELECT * FROM dr_missions WHERE mission_id=?",
                                     (commitment["mission_id"],)).fetchone()
        if mission["status"] == E.MISSION_RESERVED:
            connection.execute("UPDATE dr_missions SET status=? WHERE mission_id=?",
                               (E.MISSION_OPEN, commitment["mission_id"]))
        connection.execute(
            "UPDATE dr_overrides SET status='reverted',reverted_at=? WHERE commitment_id=? AND status='active'",
            (at, commitment["commitment_id"]),
        )
        # 资源已全部释放，直接恢复被抢占明细。
        self._try_restore_preempted(connection, commitment=commitment, at=at)
        self._commitment_event(connection, commitment_id=commitment["commitment_id"],
                               event_type="override_expired", actor_id=actor_id, detail={"at": at})
        self._audit(connection, actor_id=actor_id, action="commitment.override_expired",
                    resource_type="commitment", resource_id=commitment["commitment_id"], detail={})
        self._promote_waitlist(connection, mission_id=None, at=at)

    # ------------------------------------------------------------------ 查询

    def region_readiness(self, region_id: str, *, environment: str | None = None,
                         at: str | None = None) -> dict[str, Any]:
        """给出某地区当前可兑现的能力清单与占用明细。"""
        at = at or self._now()
        connection = self.database.connection
        self._get(connection, "dr_regions", "region_id", region_id)
        capabilities: dict[str, dict[str, Any]] = {}
        equipment_rows = connection.execute(
            "SELECT * FROM dr_equipment ORDER BY capability_code, equipment_id"
        ).fetchall()
        for equipment in equipment_rows:
            code = equipment["capability_code"]
            bucket = capabilities.setdefault(code, {"capability_code": code, "total": 0,
                                                    "deliverable_to_region": 0, "items": []})
            env_ok = environment is None or environment in json.loads(equipment["environments_json"])
            agreement_ok, _ = self._agreement_ok(connection, region_id, equipment["region_id"], at)
            cert_ok, cert_code = self._cert_ok(connection, equipment["equipment_id"], at)
            maintained = self._maintenance_open(connection, equipment["equipment_id"], at)
            lock = self._lock_owner(connection, "equipment", equipment["equipment_id"])
            ready = (equipment["active"] and env_ok and agreement_ok and cert_ok
                     and not maintained and lock is None)
            bucket["total"] += 1
            if ready:
                bucket["deliverable_to_region"] += 1
            bucket["items"].append({
                "equipment_id": equipment["equipment_id"],
                "region_id": equipment["region_id"],
                "active": bool(equipment["active"]),
                "environment_suitable": env_ok,
                "agreement_valid": agreement_ok,
                "cert_status": "valid" if cert_ok else cert_code,
                "maintenance_open": maintained,
                "locked_by_commitment": lock["commitment_id"] if lock else None,
                "deliverable": ready,
            })
        missions = connection.execute(
            "SELECT * FROM dr_missions WHERE region_id=? ORDER BY priority DESC, created_at",
            (region_id,),
        ).fetchall()
        return {
            "region_id": region_id,
            "environment": environment,
            "at": at,
            "capabilities": sorted(capabilities.values(), key=lambda x: x["capability_code"]),
            "missions": [{"mission_id": m["mission_id"], "title": m["title"],
                          "priority": m["priority"], "status": m["status"],
                          "alarm_key": m["alarm_key"]} for m in missions],
        }

    def mission_status(self, mission_id: str) -> dict[str, Any]:
        connection = self.database.connection
        mission = self._get(connection, "dr_missions", "mission_id", mission_id)
        commitments = []
        for commitment in connection.execute(
                "SELECT * FROM dr_commitments WHERE mission_id=? ORDER BY created_at",
                (mission_id,)).fetchall():
            items = connection.execute(
                "SELECT * FROM dr_commitment_items WHERE commitment_id=? ORDER BY seq",
                (commitment["commitment_id"],),
            ).fetchall()
            events = connection.execute(
                "SELECT event_type,actor_id,detail_json,occurred_at,seq FROM dr_commitment_events "
                "WHERE commitment_id=? ORDER BY seq",
                (commitment["commitment_id"],),
            ).fetchall()
            commitments.append({
                "commitment_id": commitment["commitment_id"],
                "mission_id": commitment["mission_id"],
                "region_id": commitment["region_id"],
                "kind": commitment["kind"],
                "status": commitment["status"],
                "priority": commitment["priority"],
                "expires_at": commitment["expires_at"],
                "version": commitment["version"],
                "items": [{"item_id": i["item_id"], "equipment_id": i["equipment_id"],
                           "vehicle_id": i["vehicle_id"], "crew_id": i["crew_id"],
                           "eta_minutes": i["eta_minutes"], "status": i["status"],
                           "replaces_item_id": i["replaces_item_id"],
                           "arrived_at": i["arrived_at"], "returned_at": i["returned_at"],
                           "note": i["note"]} for i in items],
                "events": [{"seq": e["seq"], "event_type": e["event_type"], "actor_id": e["actor_id"],
                            "detail": json.loads(e["detail_json"]), "occurred_at": e["occurred_at"]}
                           for e in events],
            })
        return {"mission_id": mission_id, "status": mission["status"],
                "priority": mission["priority"], "commitments": commitments}

    def waitlist(self, *, mission_id: str | None = None) -> dict[str, Any]:
        connection = self.database.connection
        query = ("SELECT w.*, m.priority AS mission_priority, m.title AS mission_title "
                 "FROM dr_waitlist w JOIN dr_missions m ON w.mission_id=m.mission_id")
        parameters: list[Any] = []
        if mission_id:
            query += " WHERE w.mission_id=?"
            parameters.append(mission_id)
        query += " ORDER BY w.status, m.priority DESC, w.rank ASC"
        items = []
        for row in connection.execute(query, parameters).fetchall():
            stored = json.loads(row["request_json"])
            items.append({"entry_id": row["entry_id"], "mission_id": row["mission_id"],
                          "mission_title": row["mission_title"], "rank": row["rank"],
                          "status": row["status"], "commitment_id": row["commitment_id"],
                          "rejections": stored.get("rejections", []),
                          "created_at": row["created_at"]})
        return {"items": items}

    def in_flight(self) -> dict[str, Any]:
        """服务重启后用于续接：仍在运输、等待交接/归还，以及等待越级回收的承诺。"""
        connection = self.database.connection
        travelling, returning, pending_override_revert = [], [], []
        for commitment in connection.execute("SELECT * FROM dr_commitments").fetchall():
            items = connection.execute(
                "SELECT * FROM dr_commitment_items WHERE commitment_id=? AND status IN (?,?)",
                (commitment["commitment_id"], E.ITEM_PLANNED, E.ITEM_IN_TRANSIT),
            ).fetchall()
            for item in items:
                travelling.append({"commitment_id": commitment["commitment_id"],
                                   "mission_id": commitment["mission_id"],
                                   "item_id": item["item_id"], "status": item["status"],
                                   "eta_minutes": item["eta_minutes"],
                                   "confirmed_at": commitment["confirmed_at"]})
            if commitment["status"] == E.COMMITMENT_RETURNING:
                returning.append(commitment["commitment_id"])
        for override in connection.execute(
                "SELECT * FROM dr_overrides WHERE status='active'").fetchall():
            pending_override_revert.append({"override_id": override["override_id"],
                                            "commitment_id": override["commitment_id"],
                                            "expires_at": override["expires_at"]})
        return {"travelling": travelling, "returning": returning,
                "pending_override_revert": pending_override_revert}

    def verify_audit(self) -> tuple[bool, int]:
        from science_strategy_foundation.audit import verify_chain
        return verify_chain(self.database.connection)


DEFAULT_REHOLD_MINUTES = 30
