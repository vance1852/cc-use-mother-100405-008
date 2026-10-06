"""灾害装备战备与调拨服务的领域服务。

在基础服务的组织、操作者、幂等回执和哈希审计边界之上，把装备组件、能力参数、
维护检验、操作队伍资格、运输时长、部署环境、互助协议、任务优先级和替代组合
汇总为随时间变化的能力承诺，并覆盖限时预留、原子核验出动、执行中事件调整、
紧急越级双人确认与到期回收、以及服务重启后的流程恢复。
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

from science_strategy_foundation.audit import append_event, canonical_json, digest
from science_strategy_foundation.clock import Clock, SystemClock
from science_strategy_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

from .storage import ensure_schema

TRANSPORT_CATEGORY = "transport_vehicle"
OPEN_COMMITMENT_STATES = ("reserved", "deployed", "arrived")
SEVERITIES = frozenset({"blue", "yellow", "orange", "red"})


def _uuid() -> str:
    return uuid.uuid4().hex


class EquipmentService:
    """协调装备台账、能力评估、限时预留、原子出动与执行恢复的领域服务。"""

    def __init__(self, database: Database, domain: DomainService | None = None,
                 clock: Clock | None = None, reservation_ttl_minutes: int = 30) -> None:
        ensure_schema(database)
        self.database = database
        self.clock = clock or SystemClock()
        self.domain = domain or DomainService(database, self.clock)
        if reservation_ttl_minutes <= 0:
            raise ValueError("reservation_ttl_minutes 必须为正数")
        self.reservation_ttl_minutes = reservation_ttl_minutes
        # 进程内串行化事务；跨进程由 BEGIN IMMEDIATE 与 busy_timeout 兜底。
        self._lock = threading.RLock()
        # 服务启动即恢复：清扫到期预留与越级授权，让运输、交接和复原流程继续。
        self.recovery_summary = self.recover()

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        """在进程锁保护下开启 IMMEDIATE 事务，保证并发接受只有一个胜者。"""

        with self._lock:
            with self.database.transaction(immediate=True) as connection:
                yield connection

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _ts(self, value: datetime | None = None) -> str:
        value = value or self._now()
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _parse(self, value: str, field: str = "时间") -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 格式无效") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc)

    def _actor(self, connection, actor_id: str):
        return self.domain._actor(connection, actor_id)

    def _require_operator(self, connection, actor_id: str):
        actor = self._actor(connection, actor_id)
        self.domain._require(actor, "admin", "operator")
        return actor

    def _require_admin(self, connection, actor_id: str):
        actor = self._actor(connection, actor_id)
        self.domain._require(actor, "admin")
        return actor

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> tuple[dict[str, Any], bool]:
        """与基础服务共用 request_receipts，重放时返回首次执行的完整响应。"""

        request_id = self.domain._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return json.loads(row["response_json"]), True
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._ts()),
        )
        return response, False

    def _site_org(self, connection, site_id: str) -> str:
        row = connection.execute("SELECT organization_id FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row["organization_id"]

    def _load_task(self, connection, task_id: str):
        row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务不存在")
        return row

    def _load_dispatch(self, connection, dispatch_id: str):
        row = connection.execute("SELECT * FROM dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise NotFoundError("调度单不存在")
        return row

    # ------------------------------------------------------------------
    # 战备就绪判定
    # ------------------------------------------------------------------

    def _cert_valid_until(self, connection, equipment_id: str) -> Optional[datetime]:
        row = connection.execute(
            "SELECT valid_until FROM maintenance_records WHERE equipment_id=? AND result='passed' "
            "ORDER BY valid_until DESC LIMIT 1",
            (equipment_id,),
        ).fetchone()
        return self._parse(row["valid_until"]) if row else None

    def _equipment_ready(self, connection, row, now: datetime) -> tuple[bool, Optional[str]]:
        if row["status"] == "maintenance":
            return False, "维修中"
        if row["status"] == "out_of_service":
            return False, "故障停用"
        if row["allocation"] == "reserved":
            return False, "已预留待出动"
        if row["allocation"] == "deployed":
            return False, "已出动"
        valid_until = self._cert_valid_until(connection, row["equipment_id"])
        if valid_until is None:
            return False, "无有效检验记录"
        if valid_until <= now:
            return False, "检验证书已过期"
        return True, None

    def _team_ready(self, connection, row, now: datetime,
                    qualification: str | None = None) -> tuple[bool, Optional[str]]:
        if not row["active"]:
            return False, "队伍已停用"
        if row["allocation"] == "reserved":
            return False, "已预留待出动"
        if row["allocation"] == "deployed":
            return False, "已出动"
        qualifications = json.loads(row["qualifications_json"])
        relevant = [q for q in qualifications if qualification is None or q.get("code") == qualification]
        if qualification and not relevant:
            return False, f"缺少资格 {qualification}"
        if not relevant:
            return False, "无有效资格"
        if all(self._parse(q["valid_until"], "valid_until") <= now for q in relevant):
            return False, "队伍资格已过期"
        return True, None

    def _route_minutes(self, connection, from_site_id: str, to_site_id: str) -> Optional[int]:
        row = connection.execute(
            "SELECT duration_minutes FROM transport_routes WHERE from_site_id=? AND to_site_id=?",
            (from_site_id, to_site_id),
        ).fetchone()
        return row["duration_minutes"] if row else None

    def _agreement_for(self, connection, provider_org: str, requester_org: str,
                       category: str, now: datetime) -> Optional[str]:
        rows = connection.execute(
            "SELECT * FROM mutual_aid_agreements WHERE provider_org_id=? AND requester_org_id=?",
            (provider_org, requester_org),
        ).fetchall()
        for row in rows:
            if category not in json.loads(row["categories_json"]):
                continue
            if self._parse(row["valid_from"]) <= now <= self._parse(row["valid_until"]):
                return row["agreement_id"]
        return None

    def _confirmed_override(self, connection, task_id: str, now: datetime):
        rows = connection.execute(
            "SELECT * FROM overrides WHERE task_id=? AND state='confirmed'", (task_id,)
        ).fetchall()
        for row in rows:
            if self._parse(row["expires_at"]) > now:
                return row
        return None

    # ------------------------------------------------------------------
    # 台账登记
    # ------------------------------------------------------------------

    def register_equipment(self, *, request_id: str, actor_id: str, equipment_id: str,
                           organization_id: str, site_id: str, category: str, name: str,
                           capability: dict[str, Any], environments: list[str]) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "equipment_id": equipment_id, "organization_id": organization_id,
                   "site_id": site_id, "category": category, "name": name,
                   "capability": capability, "environments": environments}
        with self._transaction() as connection:
            actor = self._require_operator(connection, actor_id)
            if actor.role != "admin" and actor.organization_id != organization_id:
                raise PermissionDenied("不能为其他组织登记装备")
            equipment_id = self.domain._identifier(equipment_id, "equipment_id")
            category = self.domain._identifier(category, "category")
            name = self.domain._text(name, "name")
            if not isinstance(capability, dict):
                raise ValidationError("capability 必须是对象")
            for key, value in capability.items():
                if not key or not isinstance(value, (int, float, str)):
                    raise ValidationError("capability 参数必须是数值或文本")
            if not isinstance(environments, list) or any(not str(item).strip() for item in environments):
                raise ValidationError("environments 必须是非空文本列表")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            site = connection.execute("SELECT organization_id FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if site["organization_id"] != organization_id:
                raise ValidationError("装备常驻场所必须属于所属组织")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO equipment_units(equipment_id,organization_id,home_site_id,current_site_id,"
                        "category,name,capability_json,environments_json,status,allocation,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,'available','idle',?)",
                        (equipment_id, organization_id, site_id, site_id, category, name,
                         canonical_json(capability), canonical_json([str(item) for item in environments]),
                         self._ts()),
                    )
                except Exception as exc:
                    raise ConflictError("装备编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="equipment.registered",
                             resource_type="equipment", resource_id=equipment_id,
                             detail={"organization_id": organization_id, "site_id": site_id,
                                     "category": category, "capability": capability},
                             occurred_at=self._ts())
                return "equipment", equipment_id, {"equipment_id": equipment_id, "status": "available",
                                                   "allocation": "idle"}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_equipment", payload=payload, create=create)

    def record_maintenance(self, *, request_id: str, actor_id: str, equipment_id: str,
                           valid_until: str, result: str = "passed", inspector: str = "",
                           inspected_at: str | None = None) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "equipment_id": equipment_id, "valid_until": valid_until,
                   "result": result, "inspector": inspector, "inspected_at": inspected_at}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            row = connection.execute("SELECT * FROM equipment_units WHERE equipment_id=?",
                                     (equipment_id,)).fetchone()
            if row is None:
                raise NotFoundError("装备不存在")
            if result not in ("passed", "failed"):
                raise ValidationError("result 必须是 passed 或 failed")
            valid_until_ts = self._ts(self._parse(valid_until, "valid_until"))
            inspected_ts = self._ts(self._parse(inspected_at, "inspected_at")) if inspected_at else self._ts()
            inspector = inspector.strip() if isinstance(inspector, str) else ""
            if not inspector:
                raise ValidationError("inspector 不能为空")

            def create() -> tuple[str, str, dict[str, Any]]:
                record_id = _uuid()
                connection.execute(
                    "INSERT INTO maintenance_records(record_id,equipment_id,inspected_at,valid_until,inspector,result,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (record_id, equipment_id, inspected_ts, valid_until_ts, inspector, result, self._ts()),
                )
                new_status = row["status"]
                if row["allocation"] == "idle":
                    if result == "failed" and row["status"] == "available":
                        new_status = "maintenance"
                    elif result == "passed" and row["status"] in ("maintenance", "out_of_service"):
                        new_status = "available"
                if new_status != row["status"]:
                    connection.execute("UPDATE equipment_units SET status=? WHERE equipment_id=?",
                                       (new_status, equipment_id))
                append_event(connection, actor_id=actor_id, action="equipment.maintained",
                             resource_type="equipment", resource_id=equipment_id,
                             detail={"record_id": record_id, "result": result,
                                     "valid_until": valid_until_ts, "inspector": inspector},
                             occurred_at=self._ts())
                return "maintenance_record", record_id, {"record_id": record_id, "equipment_id": equipment_id,
                                                         "result": result, "valid_until": valid_until_ts,
                                                         "status": new_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_maintenance", payload=payload, create=create)

    def register_team(self, *, request_id: str, actor_id: str, team_id: str,
                      organization_id: str, site_id: str, name: str,
                      qualifications: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "team_id": team_id, "organization_id": organization_id,
                   "site_id": site_id, "name": name, "qualifications": qualifications}
        with self._transaction() as connection:
            actor = self._require_operator(connection, actor_id)
            if actor.role != "admin" and actor.organization_id != organization_id:
                raise PermissionDenied("不能为其他组织登记队伍")
            team_id = self.domain._identifier(team_id, "team_id")
            name = self.domain._text(name, "name")
            if not isinstance(qualifications, list) or not qualifications:
                raise ValidationError("qualifications 必须是非空列表")
            normalized = []
            for item in qualifications:
                if not isinstance(item, dict) or not str(item.get("code", "")).strip():
                    raise ValidationError("资格必须包含 code")
                normalized.append({"code": str(item["code"]).strip(),
                                   "valid_until": self._ts(self._parse(item.get("valid_until", ""), "valid_until"))})
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            site = connection.execute("SELECT organization_id FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if site["organization_id"] != organization_id:
                raise ValidationError("队伍常驻场所必须属于所属组织")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO operator_teams(team_id,organization_id,home_site_id,current_site_id,name,"
                        "qualifications_json,allocation,active,created_at) VALUES(?,?,?,?,?,?,'idle',1,?)",
                        (team_id, organization_id, site_id, site_id, name,
                         canonical_json(normalized), self._ts()),
                    )
                except Exception as exc:
                    raise ConflictError("队伍编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="team.registered",
                             resource_type="team", resource_id=team_id,
                             detail={"organization_id": organization_id, "site_id": site_id,
                                     "qualifications": normalized},
                             occurred_at=self._ts())
                return "team", team_id, {"team_id": team_id, "allocation": "idle"}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_team", payload=payload, create=create)

    def register_route(self, *, request_id: str, actor_id: str, route_id: str,
                       from_site_id: str, to_site_id: str, duration_minutes: int) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "route_id": route_id, "from_site_id": from_site_id,
                   "to_site_id": to_site_id, "duration_minutes": duration_minutes}
        with self._transaction() as connection:
            self._require_admin(connection, actor_id)
            route_id = self.domain._identifier(route_id, "route_id")
            if from_site_id == to_site_id:
                raise ValidationError("运输路线起点和终点不能相同")
            if not isinstance(duration_minutes, int) or duration_minutes <= 0:
                raise ValidationError("duration_minutes 必须是正整数")
            for site_id in (from_site_id, to_site_id):
                if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                    raise NotFoundError(f"场所 {site_id} 不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO transport_routes(route_id,from_site_id,to_site_id,duration_minutes,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (route_id, from_site_id, to_site_id, duration_minutes, self._ts()),
                    )
                except Exception as exc:
                    raise ConflictError("运输路线编号已存在或起终点重复") from exc
                append_event(connection, actor_id=actor_id, action="route.registered",
                             resource_type="route", resource_id=route_id,
                             detail={"from_site_id": from_site_id, "to_site_id": to_site_id,
                                     "duration_minutes": duration_minutes},
                             occurred_at=self._ts())
                return "route", route_id, {"route_id": route_id, "duration_minutes": duration_minutes}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_route", payload=payload, create=create)

    def register_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                           provider_org_id: str, requester_org_id: str, categories: list[str],
                           valid_from: str, valid_until: str) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "provider_org_id": provider_org_id,
                   "requester_org_id": requester_org_id, "categories": categories,
                   "valid_from": valid_from, "valid_until": valid_until}
        with self._transaction() as connection:
            self._require_admin(connection, actor_id)
            agreement_id = self.domain._identifier(agreement_id, "agreement_id")
            if provider_org_id == requester_org_id:
                raise ValidationError("互助协议双方不能是同一组织")
            if not isinstance(categories, list) or not categories:
                raise ValidationError("categories 必须是非空列表")
            normalized_categories = sorted({self.domain._identifier(item, "category") for item in categories})
            from_ts = self._parse(valid_from, "valid_from")
            until_ts = self._parse(valid_until, "valid_until")
            if from_ts >= until_ts:
                raise ValidationError("valid_from 必须早于 valid_until")
            for org_id in (provider_org_id, requester_org_id):
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (org_id,)).fetchone() is None:
                    raise NotFoundError(f"组织 {org_id} 不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO mutual_aid_agreements(agreement_id,provider_org_id,requester_org_id,"
                        "categories_json,valid_from,valid_until,created_at) VALUES(?,?,?,?,?,?,?)",
                        (agreement_id, provider_org_id, requester_org_id,
                         canonical_json(normalized_categories), self._ts(from_ts), self._ts(until_ts), self._ts()),
                    )
                except Exception as exc:
                    raise ConflictError("互助协议编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="agreement.registered",
                             resource_type="agreement", resource_id=agreement_id,
                             detail={"provider_org_id": provider_org_id, "requester_org_id": requester_org_id,
                                     "categories": normalized_categories,
                                     "valid_until": self._ts(until_ts)},
                             occurred_at=self._ts())
                return "agreement", agreement_id, {"agreement_id": agreement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_agreement", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 能力评估（替代组合 + 限时预留）
    # ------------------------------------------------------------------

    def _validate_requirement(self, requirement: Any) -> dict[str, Any]:
        if not isinstance(requirement, dict):
            raise ValidationError("requirement 必须是对象")
        combinations = requirement.get("combinations")
        if not isinstance(combinations, list) or not combinations:
            raise ValidationError("requirement.combinations 必须是非空列表")
        for combo in combinations:
            if not isinstance(combo, dict):
                raise ValidationError("替代组合必须是对象")
            items = combo.get("items")
            if not isinstance(items, list) or not items:
                raise ValidationError("替代组合的 items 必须是非空列表")
            for item in items:
                self.domain._identifier(str(item.get("category", "")), "category")
                if not isinstance(item.get("count"), int) or item["count"] <= 0:
                    raise ValidationError("替代组合条目的 count 必须是正整数")
                min_capability = item.get("min_capability", {})
                if not isinstance(min_capability, dict):
                    raise ValidationError("min_capability 必须是对象")
            teams = combo.get("teams", 1)
            if not isinstance(teams, int) or teams < 0:
                raise ValidationError("替代组合的 teams 必须是非负整数")
        duration = requirement.get("duration_hours", 24)
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise ValidationError("duration_hours 必须是正数")
        environment = requirement.get("environment")
        if environment is not None and not str(environment).strip():
            raise ValidationError("environment 不能为空文本")
        return requirement

    def _select_equipment(self, connection, item: dict[str, Any], environment: str | None,
                          requester_org: str, task_site_id: str, now: datetime,
                          exclude_orgs: frozenset[str], already: set[str]) -> tuple[list[dict[str, Any]], Optional[str]]:
        category = item["category"]
        min_capability = item.get("min_capability", {})
        rows = connection.execute(
            "SELECT * FROM equipment_units WHERE category=? AND status='available' AND allocation='idle' "
            "ORDER BY equipment_id",
            (category,),
        ).fetchall()
        candidates = []
        for row in rows:
            if row["equipment_id"] in already or row["organization_id"] in exclude_orgs:
                continue
            capability = json.loads(row["capability_json"])
            if any(not isinstance(capability.get(key), (int, float)) or capability[key] < need
                   for key, need in min_capability.items()):
                continue
            environments = json.loads(row["environments_json"])
            if environment and environment not in environments:
                continue
            ready, _ = self._equipment_ready(connection, row, now)
            if not ready:
                continue
            if row["current_site_id"] != task_site_id and \
                    self._route_minutes(connection, row["current_site_id"], task_site_id) is None:
                continue
            if row["organization_id"] == requester_org:
                rank, requires_override = 0, 0
            elif self._agreement_for(connection, row["organization_id"], requester_org, category, now):
                rank, requires_override = 1, 0
            else:
                rank, requires_override = 2, 1
            candidates.append((rank, row["equipment_id"], row, requires_override))
        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        chosen = candidates[:item["count"]]
        if len(chosen) < item["count"]:
            return [], (f"类别 {category} 可兑现装备不足：需要 {item['count']} 台，"
                        f"当前可兑现 {len(chosen)} 台")
        selection = []
        for _, _, row, requires_override in chosen:
            selection.append({"resource_type": "equipment", "resource_id": row["equipment_id"],
                              "role": "primary", "provider_org_id": row["organization_id"],
                              "from_site_id": row["current_site_id"],
                              "requires_override": requires_override, "qualification": None})
        return selection, None

    def _select_vehicle(self, connection, requester_org: str, near_site_id: str, now: datetime,
                        exclude_orgs: frozenset[str], already: set[str]) -> Optional[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM equipment_units WHERE category=? AND status='available' AND allocation='idle' "
            "ORDER BY equipment_id",
            (TRANSPORT_CATEGORY,),
        ).fetchall()
        candidates = []
        for row in rows:
            if row["equipment_id"] in already or row["organization_id"] in exclude_orgs:
                continue
            ready, _ = self._equipment_ready(connection, row, now)
            if not ready:
                continue
            if row["organization_id"] == requester_org:
                org_rank, requires_override = 0, 0
            elif self._agreement_for(connection, row["organization_id"], requester_org,
                                     TRANSPORT_CATEGORY, now):
                org_rank, requires_override = 1, 0
            else:
                org_rank, requires_override = 2, 1
            site_rank = 0 if row["current_site_id"] == near_site_id else 1
            candidates.append((site_rank, org_rank, row["equipment_id"], row, requires_override))
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
        _, _, _, row, requires_override = candidates[0]
        return {"resource_type": "equipment", "resource_id": row["equipment_id"], "role": "transport",
                "provider_org_id": row["organization_id"], "from_site_id": row["current_site_id"],
                "requires_override": requires_override, "qualification": None}

    def _select_teams(self, connection, count: int, qualification: str | None,
                      requester_org: str, now: datetime,
                      exclude_orgs: frozenset[str]) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM operator_teams WHERE active=1 AND allocation='idle' ORDER BY team_id"
        ).fetchall()
        candidates = []
        for row in rows:
            if row["organization_id"] in exclude_orgs:
                continue
            ready, _ = self._team_ready(connection, row, now, qualification)
            if not ready:
                continue
            if row["organization_id"] == requester_org:
                rank, requires_override = 0, 0
            elif self._agreement_for(connection, row["organization_id"], requester_org,
                                     "operator_team", now):
                rank, requires_override = 1, 0
            else:
                rank, requires_override = 2, 1
            candidates.append((rank, row["team_id"], row, requires_override))
        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        selection = []
        for _, _, row, requires_override in candidates[:count]:
            selection.append({"resource_type": "team", "resource_id": row["team_id"], "role": "operator",
                              "provider_org_id": row["organization_id"],
                              "from_site_id": row["current_site_id"],
                              "requires_override": requires_override, "qualification": qualification})
        return selection

    def _select_resources(self, connection, combo: dict[str, Any], environment: str | None,
                          requester_org: str, task_site_id: str, now: datetime,
                          exclude_orgs: frozenset[str]) -> tuple[Optional[list[dict[str, Any]]], list[str]]:
        selection: list[dict[str, Any]] = []
        already: set[str] = set()
        for item in combo["items"]:
            chosen, reason = self._select_equipment(connection, item, environment, requester_org,
                                                    task_site_id, now, exclude_orgs, already)
            if reason:
                return None, [reason]
            selection.extend(chosen)
            already.update(entry["resource_id"] for entry in chosen)
        for entry in [s for s in selection if s["from_site_id"] != task_site_id]:
            vehicle = self._select_vehicle(connection, requester_org, entry["from_site_id"],
                                           now, exclude_orgs, already)
            if vehicle is None:
                return None, [f"装备 {entry['resource_id']} 需要跨区运输，但缺少可用运输车辆"]
            selection.append(vehicle)
            already.add(vehicle["resource_id"])
        teams_needed = combo.get("teams", 1)
        if teams_needed:
            qualification = combo.get("qualification")
            teams = self._select_teams(connection, teams_needed, qualification, requester_org,
                                       now, exclude_orgs)
            if len(teams) < teams_needed:
                label = qualification or "任意"
                return None, [f"具备资格 {label} 的操作队伍不足：需要 {teams_needed} 支，可用 {len(teams)} 支"]
            selection.extend(teams)
        return selection, []

    def _create_commitments(self, connection, task_id: str, selection: list[dict[str, Any]],
                            now: datetime, ttl_minutes: int, duration_hours: float) -> str:
        expires_at = now + timedelta(minutes=ttl_minutes)
        end_at = expires_at + timedelta(hours=duration_hours)
        for entry in selection:
            connection.execute(
                "INSERT INTO commitments(commitment_id,task_id,dispatch_id,resource_type,resource_id,role,"
                "provider_org_id,from_site_id,requires_override,qualification,state,start_at,end_at,expires_at,"
                "created_at,updated_at) VALUES(?,?,NULL,?,?,?,?,?,?,?,'reserved',?,?,?,?,?)",
                (_uuid(), task_id, entry["resource_type"], entry["resource_id"], entry["role"],
                 entry["provider_org_id"], entry["from_site_id"], entry["requires_override"],
                 entry["qualification"], self._ts(now), self._ts(end_at), self._ts(expires_at),
                 self._ts(), self._ts()),
            )
            table = "equipment_units" if entry["resource_type"] == "equipment" else "operator_teams"
            key = "equipment_id" if entry["resource_type"] == "equipment" else "team_id"
            cursor = connection.execute(
                f"UPDATE {table} SET allocation='reserved' WHERE {key}=? AND allocation='idle'",
                (entry["resource_id"],),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"资源 {entry['resource_id']} 已被其他任务占用")
        return self._ts(expires_at)

    def _evaluate_and_mark(self, connection, task_id: str, now: datetime, ttl_minutes: int,
                           exclude_orgs: frozenset[str] = frozenset()) -> dict[str, Any]:
        task = self._load_task(connection, task_id)
        alert = connection.execute("SELECT * FROM alerts WHERE alert_id=?", (task["alert_id"],)).fetchone()
        requirement = json.loads(alert["requirement_json"])
        environment = requirement.get("environment")
        duration_hours = requirement.get("duration_hours", 24)
        requester_org = self._site_org(connection, task["site_id"])
        reasons: list[str] = []
        chosen: Optional[tuple[dict[str, Any], list[dict[str, Any]]]] = None
        for combo in requirement["combinations"]:
            selection, combo_reasons = self._select_resources(
                connection, combo, environment, requester_org, task["site_id"], now, exclude_orgs)
            if selection is not None:
                chosen = (combo, selection)
                break
            reasons.extend(combo_reasons)
        if chosen is None:
            connection.execute(
                "UPDATE tasks SET status='waitlisted', reasons_json=?, updated_at=?, version=version+1 "
                "WHERE task_id=?",
                (canonical_json(reasons), self._ts(), task_id),
            )
            append_event(connection, actor_id="system", action="task.waitlisted",
                         resource_type="task", resource_id=task_id,
                         detail={"reasons": reasons}, occurred_at=self._ts())
            return {"reserved": False, "status": "waitlisted", "reasons": reasons}
        combo, selection = chosen
        expires_at = self._create_commitments(connection, task_id, selection, now,
                                              ttl_minutes, duration_hours)
        expected_end = self._ts(self._parse(expires_at) + timedelta(hours=duration_hours))
        connection.execute(
            "UPDATE tasks SET status='reserved', reasons_json='[]', expected_end_at=?, updated_at=?, "
            "version=version+1 WHERE task_id=?",
            (expected_end, self._ts(), task_id),
        )
        resources = [{"resource_type": entry["resource_type"], "resource_id": entry["resource_id"],
                      "role": entry["role"], "provider_org_id": entry["provider_org_id"],
                      "requires_override": bool(entry["requires_override"])} for entry in selection]
        append_event(connection, actor_id="system", action="task.reserved",
                     resource_type="task", resource_id=task_id,
                     detail={"combination": combo.get("label"), "resources": resources,
                             "expires_at": expires_at},
                     occurred_at=self._ts())
        return {"reserved": True, "status": "reserved", "combination": combo.get("label"),
                "resources": resources, "expires_at": expires_at, "reasons": []}

    # ------------------------------------------------------------------
    # 告警接入与任务接受
    # ------------------------------------------------------------------

    def raise_alert(self, *, request_id: str, actor_id: str, alert_id: str, site_id: str,
                    severity: str, priority: int, requirement: dict[str, Any],
                    reservation_ttl_minutes: int | None = None) -> tuple[dict[str, Any], bool]:
        requirement = self._validate_requirement(requirement)
        payload = {"actor_id": actor_id, "alert_id": alert_id, "site_id": site_id, "severity": severity,
                   "priority": priority, "requirement": requirement,
                   "reservation_ttl_minutes": reservation_ttl_minutes}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            alert_id = self.domain._identifier(alert_id, "alert_id")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            if severity not in SEVERITIES:
                raise ValidationError("severity 必须是 blue/yellow/orange/red 之一")
            if not isinstance(priority, int) or priority < 0:
                raise ValidationError("priority 必须是非负整数")
            ttl = reservation_ttl_minutes or self.reservation_ttl_minutes
            if ttl <= 0:
                raise ValidationError("reservation_ttl_minutes 必须为正数")
            now = self._now()
            self._sweep(connection, now)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT task_id FROM tasks WHERE alert_id=?", (alert_id,)
                ).fetchone()
                if existing:
                    task = self._load_task(connection, existing["task_id"])
                    # 重复告警不重复占用装备，直接回执既有任务。
                    return "task", existing["task_id"], {
                        "task_id": existing["task_id"], "alert_id": alert_id,
                        "status": task["status"], "duplicate_alert": True,
                        "reasons": json.loads(task["reasons_json"]),
                    }
                connection.execute(
                    "INSERT INTO alerts(alert_id,site_id,severity,priority,requirement_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (alert_id, site_id, severity, priority, canonical_json(requirement), self._ts()),
                )
                task_id = _uuid()
                connection.execute(
                    "INSERT INTO tasks(task_id,alert_id,site_id,priority,status,reasons_json,expected_end_at,"
                    "created_at,updated_at,version) VALUES(?,?,?,?,'pending','[]',NULL,?,?,1)",
                    (task_id, alert_id, site_id, priority, self._ts(), self._ts()),
                )
                append_event(connection, actor_id=actor_id, action="alert.raised",
                             resource_type="alert", resource_id=alert_id,
                             detail={"task_id": task_id, "site_id": site_id, "severity": severity,
                                     "priority": priority},
                             occurred_at=self._ts())
                evaluation = self._evaluate_and_mark(connection, task_id, now, ttl)
                return "task", task_id, {"task_id": task_id, "alert_id": alert_id,
                                         "duplicate_alert": False, **evaluation}

            return self._idempotent(connection, request_id=request_id,
                                    action="raise_alert", payload=payload, create=create)

    def accept_task(self, *, request_id: str, actor_id: str, task_id: str,
                    reservation_ttl_minutes: int | None = None) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "task_id": task_id,
                   "reservation_ttl_minutes": reservation_ttl_minutes}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            self._load_task(connection, task_id)
            ttl = reservation_ttl_minutes or self.reservation_ttl_minutes
            # 状态守卫配合 BEGIN IMMEDIATE：并发接受只有一个胜者。
            cursor = connection.execute(
                "UPDATE tasks SET updated_at=?, version=version+1 WHERE task_id=? AND status IN ('pending','waitlisted')",
                (self._ts(), task_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("任务已被其他调度处理，当前状态不能接受")

            def create() -> tuple[str, str, dict[str, Any]]:
                evaluation = self._evaluate_and_mark(connection, task_id, now, ttl)
                return "task", task_id, {"task_id": task_id, **evaluation}

            return self._idempotent(connection, request_id=request_id,
                                    action="accept_task", payload=payload, create=create)

    def cancel_task(self, *, request_id: str, actor_id: str, task_id: str,
                    reason: str = "") -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            task = self._load_task(connection, task_id)
            if task["status"] not in ("pending", "reserved", "waitlisted"):
                raise ConflictError("任务已出动，不能取消，请使用接管或收尾流程")

            def create() -> tuple[str, str, dict[str, Any]]:
                released = self._release_open_commitments(connection, task_id, now,
                                                          states=("reserved",))
                connection.execute(
                    "UPDATE tasks SET status='cancelled', updated_at=?, version=version+1 WHERE task_id=?",
                    (self._ts(), task_id),
                )
                append_event(connection, actor_id=actor_id, action="task.cancelled",
                             resource_type="task", resource_id=task_id,
                             detail={"reason": reason, "released_commitments": released},
                             occurred_at=self._ts())
                promoted = self._reevaluate_waitlist(connection, now)
                return "task", task_id, {"task_id": task_id, "status": "cancelled",
                                         "released_commitments": released,
                                         "promoted_tasks": promoted}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_task", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 原子核验出动
    # ------------------------------------------------------------------

    def _verify_commitment(self, connection, commitment, task, requester_org: str,
                           now: datetime) -> tuple[list[str], list[str]]:
        checks: list[str] = []
        failures: list[str] = []
        resource_id = commitment["resource_id"]
        if commitment["expires_at"] and self._parse(commitment["expires_at"]) <= now:
            failures.append(f"资源 {resource_id} 的预留已到期")
            return checks, failures
        if commitment["resource_type"] == "equipment":
            row = connection.execute("SELECT * FROM equipment_units WHERE equipment_id=?",
                                     (resource_id,)).fetchone()
            if row is None:
                return checks, [f"装备 {resource_id} 不存在"]
            if row["status"] != "available":
                failures.append(f"装备 {resource_id} 当前物理状态不可用（{row['status']}）")
            if row["allocation"] != "reserved":
                failures.append(f"装备 {resource_id} 的预留占用状态已被破坏")
            valid_until = self._cert_valid_until(connection, resource_id)
            if valid_until is None or valid_until <= now:
                failures.append(f"装备 {resource_id} 的检验证书已过期或缺失")
            else:
                checks.append(f"装备 {resource_id} 检验证书有效至 {self._ts(valid_until)}")
            alert = connection.execute("SELECT requirement_json FROM alerts WHERE alert_id=?",
                                       (task["alert_id"],)).fetchone()
            environment = json.loads(alert["requirement_json"]).get("environment")
            if environment and environment not in json.loads(row["environments_json"]):
                failures.append(f"装备 {resource_id} 不支持部署环境 {environment}")
            if commitment["from_site_id"] != task["site_id"]:
                minutes = self._route_minutes(connection, commitment["from_site_id"], task["site_id"])
                if minutes is None:
                    failures.append(f"装备 {resource_id} 缺少到任务区域的运输路线")
                else:
                    checks.append(f"装备 {resource_id} 运输路线约 {minutes} 分钟")
        else:
            row = connection.execute("SELECT * FROM operator_teams WHERE team_id=?",
                                     (resource_id,)).fetchone()
            if row is None:
                return checks, [f"队伍 {resource_id} 不存在"]
            if row["allocation"] != "reserved":
                failures.append(f"队伍 {resource_id} 的预留占用状态已被破坏")
            ready, reason = self._team_ready(connection, {**row, "allocation": "idle"}, now,
                                             commitment["qualification"])
            if not ready:
                failures.append(f"队伍 {resource_id} {reason}")
            else:
                checks.append(f"队伍 {resource_id} 资格有效")
        if commitment["provider_org_id"] != requester_org:
            category = TRANSPORT_CATEGORY if commitment["role"] == "transport" else (
                "operator_team" if commitment["resource_type"] == "team" else
                self._equipment_category(connection, resource_id))
            agreement_id = self._agreement_for(connection, commitment["provider_org_id"],
                                               requester_org, category, now)
            if agreement_id:
                checks.append(f"资源 {resource_id} 由互助协议 {agreement_id} 覆盖")
            else:
                override = self._confirmed_override(connection, task["task_id"], now)
                if override is None:
                    failures.append(f"资源 {resource_id} 缺少有效互助协议且越级授权不可用")
                else:
                    checks.append(f"资源 {resource_id} 由越级授权 {override['override_id']} 覆盖")
        return checks, failures

    def _equipment_category(self, connection, equipment_id: str) -> str:
        row = connection.execute("SELECT category FROM equipment_units WHERE equipment_id=?",
                                 (equipment_id,)).fetchone()
        return row["category"] if row else ""

    def dispatch_task(self, *, request_id: str, actor_id: str,
                      task_id: str) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "task_id": task_id}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            task = self._load_task(connection, task_id)
            if task["status"] != "reserved":
                raise ConflictError("任务不在可出动状态（需要 reserved）")
            requester_org = self._site_org(connection, task["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                commitments = connection.execute(
                    "SELECT * FROM commitments WHERE task_id=? AND state='reserved' ORDER BY commitment_id",
                    (task_id,),
                ).fetchall()
                if not commitments:
                    raise ConflictError("任务没有可出动的预留资源")
                all_checks: list[dict[str, Any]] = []
                failures: list[str] = []
                for commitment in commitments:
                    checks, problems = self._verify_commitment(connection, commitment, task,
                                                               requester_org, now)
                    all_checks.append({"resource_id": commitment["resource_id"],
                                       "resource_type": commitment["resource_type"],
                                       "checks": checks,
                                       "result": "passed" if not problems else "failed"})
                    failures.extend(problems)
                if failures:
                    # 原子核验失败：不留下任何部分占用，整套前置条件要么全过要么全退。
                    released = self._release_open_commitments(connection, task_id, now,
                                                              states=("reserved",))
                    connection.execute(
                        "UPDATE tasks SET status='waitlisted', reasons_json=?, updated_at=?, "
                        "version=version+1 WHERE task_id=?",
                        (canonical_json(failures), self._ts(), task_id),
                    )
                    append_event(connection, actor_id=actor_id, action="task.dispatch_failed",
                                 resource_type="task", resource_id=task_id,
                                 detail={"reasons": failures, "released_commitments": released},
                                 occurred_at=self._ts())
                    return "task", task_id, {"task_id": task_id, "dispatched": False,
                                             "status": "waitlisted", "reasons": failures}
                alert = connection.execute("SELECT requirement_json FROM alerts WHERE alert_id=?",
                                           (task["alert_id"],)).fetchone()
                duration_hours = json.loads(alert["requirement_json"]).get("duration_hours", 24)
                eta_minutes = 0
                for commitment in commitments:
                    if commitment["resource_type"] != "equipment":
                        continue
                    if commitment["from_site_id"] == task["site_id"]:
                        continue
                    minutes = self._route_minutes(connection, commitment["from_site_id"],
                                                  task["site_id"]) or 0
                    eta_minutes = max(eta_minutes, minutes)
                eta_at = now + timedelta(minutes=eta_minutes)
                expected_end = eta_at + timedelta(hours=duration_hours)
                dispatch_id = _uuid()
                override = self._confirmed_override(connection, task_id, now)
                items = [{"commitment_id": c["commitment_id"], "resource_type": c["resource_type"],
                          "resource_id": c["resource_id"], "role": c["role"],
                          "from_site_id": c["from_site_id"]} for c in commitments]
                connection.execute(
                    "INSERT INTO dispatches(dispatch_id,task_id,override_id,state,items_json,checks_json,"
                    "dispatched_at,eta_at,arrived_at,finished_at,closed_at,created_at) "
                    "VALUES(?,?,?,'en_route',?,?,?,?,NULL,NULL,NULL,?)",
                    (dispatch_id, task_id, override["override_id"] if override else None,
                     canonical_json(items), canonical_json(all_checks), self._ts(now),
                     self._ts(eta_at), self._ts()),
                )
                for commitment in commitments:
                    connection.execute(
                        "UPDATE commitments SET state='deployed', dispatch_id=?, end_at=?, expires_at=NULL, "
                        "updated_at=? WHERE commitment_id=?",
                        (dispatch_id, self._ts(expected_end), self._ts(), commitment["commitment_id"]),
                    )
                    self._set_allocation(connection, commitment["resource_type"],
                                         commitment["resource_id"], "deployed")
                connection.execute(
                    "UPDATE tasks SET status='dispatched', expected_end_at=?, updated_at=?, "
                    "version=version+1 WHERE task_id=?",
                    (self._ts(expected_end), self._ts(), task_id),
                )
                append_event(connection, actor_id=actor_id, action="dispatch.executed",
                             resource_type="dispatch", resource_id=dispatch_id,
                             detail={"task_id": task_id, "items": items, "checks": all_checks,
                                     "eta_at": self._ts(eta_at)},
                             occurred_at=self._ts())
                return "dispatch", dispatch_id, {
                    "task_id": task_id, "dispatch_id": dispatch_id, "dispatched": True,
                    "state": "en_route", "eta_at": self._ts(eta_at),
                    "expected_end_at": self._ts(expected_end),
                    "items": items, "checks": all_checks,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="dispatch_task", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 执行中事件（只调整尚未完成的承诺）
    # ------------------------------------------------------------------

    def _set_allocation(self, connection, resource_type: str, resource_id: str, allocation: str) -> None:
        if resource_type == "equipment":
            connection.execute("UPDATE equipment_units SET allocation=? WHERE equipment_id=?",
                               (allocation, resource_id))
        else:
            connection.execute("UPDATE operator_teams SET allocation=? WHERE team_id=?",
                               (allocation, resource_id))

    def _free_allocation(self, connection, resource_type: str, resource_id: str) -> None:
        if resource_type == "equipment":
            connection.execute(
                "UPDATE equipment_units SET allocation='idle' WHERE equipment_id=? AND allocation IN ('reserved','deployed')",
                (resource_id,),
            )
        else:
            connection.execute(
                "UPDATE operator_teams SET allocation='idle' WHERE team_id=? AND allocation IN ('reserved','deployed')",
                (resource_id,),
            )

    def _release_open_commitments(self, connection, task_id: str, now: datetime,
                                  states: tuple[str, ...] = OPEN_COMMITMENT_STATES) -> list[str]:
        placeholders = ",".join("?" for _ in states)
        rows = connection.execute(
            f"SELECT * FROM commitments WHERE task_id=? AND state IN ({placeholders})",
            (task_id, *states),
        ).fetchall()
        released = []
        for row in rows:
            connection.execute(
                "UPDATE commitments SET state='released', updated_at=? WHERE commitment_id=?",
                (self._ts(), row["commitment_id"]),
            )
            self._free_allocation(connection, row["resource_type"], row["resource_id"])
            released.append(row["commitment_id"])
        return released

    def _record_dispatch_event(self, connection, dispatch_id: str, kind: str,
                               detail: dict[str, Any], actor_id: str) -> None:
        connection.execute(
            "INSERT INTO dispatch_events(event_id,dispatch_id,kind,detail_json,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (_uuid(), dispatch_id, kind, canonical_json(detail), actor_id, self._ts()),
        )

    def confirm_arrival(self, *, request_id: str, actor_id: str, dispatch_id: str,
                        equipment_ids: list[str]) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "dispatch_id": dispatch_id, "equipment_ids": equipment_ids}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            dispatch = self._load_dispatch(connection, dispatch_id)
            if dispatch["state"] != "en_route":
                raise ConflictError("调度单不在运输途中状态")
            if not isinstance(equipment_ids, list) or not equipment_ids:
                raise ValidationError("equipment_ids 必须是非空列表")
            task = self._load_task(connection, dispatch["task_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                arrived: list[str] = []
                for equipment_id in dict.fromkeys(equipment_ids):
                    commitment = connection.execute(
                        "SELECT * FROM commitments WHERE dispatch_id=? AND resource_type='equipment' "
                        "AND resource_id=? AND state='deployed'",
                        (dispatch_id, equipment_id),
                    ).fetchone()
                    if commitment is None:
                        raise ConflictError(f"装备 {equipment_id} 不在该调度单的待到场列表中")
                    connection.execute(
                        "UPDATE commitments SET state='arrived', updated_at=? WHERE commitment_id=?",
                        (self._ts(), commitment["commitment_id"]),
                    )
                    connection.execute("UPDATE equipment_units SET current_site_id=? WHERE equipment_id=?",
                                       (task["site_id"], equipment_id))
                    arrived.append(equipment_id)
                pending_rows = connection.execute(
                    "SELECT resource_id FROM commitments WHERE dispatch_id=? AND resource_type='equipment' "
                    "AND state='deployed'",
                    (dispatch_id,),
                ).fetchall()
                pending = [row["resource_id"] for row in pending_rows]
                kind = "arrival" if not pending else "partial_arrival"
                self._record_dispatch_event(connection, dispatch_id, kind,
                                            {"arrived": arrived, "pending": pending}, actor_id)
                state = dispatch["state"]
                if not pending:
                    # 全部到场：队伍随队到达，调度单转入现场作业。
                    team_rows = connection.execute(
                        "SELECT * FROM commitments WHERE dispatch_id=? AND resource_type='team' AND state='deployed'",
                        (dispatch_id,),
                    ).fetchall()
                    for team_commitment in team_rows:
                        connection.execute(
                            "UPDATE commitments SET state='arrived', updated_at=? WHERE commitment_id=?",
                            (self._ts(), team_commitment["commitment_id"]),
                        )
                        connection.execute("UPDATE operator_teams SET current_site_id=? WHERE team_id=?",
                                           (task["site_id"], team_commitment["resource_id"]))
                    connection.execute(
                        "UPDATE dispatches SET state='on_site', arrived_at=? WHERE dispatch_id=?",
                        (self._ts(), dispatch_id),
                    )
                    if task["status"] == "dispatched":
                        connection.execute(
                            "UPDATE tasks SET status='active', updated_at=?, version=version+1 WHERE task_id=?",
                            (self._ts(), task["task_id"]),
                        )
                    state = "on_site"
                append_event(connection, actor_id=actor_id, action="dispatch.arrival",
                             resource_type="dispatch", resource_id=dispatch_id,
                             detail={"arrived": arrived, "pending": pending, "partial": bool(pending)},
                             occurred_at=self._ts())
                task_status = connection.execute("SELECT status FROM tasks WHERE task_id=?",
                                                 (task["task_id"],)).fetchone()["status"]
                return "dispatch", dispatch_id, {"dispatch_id": dispatch_id, "state": state,
                                                 "arrived": arrived, "pending": pending,
                                                 "task_status": task_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_arrival", payload=payload, create=create)

    def report_breakdown(self, *, request_id: str, actor_id: str, dispatch_id: str,
                         equipment_id: str, reason: str) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "dispatch_id": dispatch_id,
                   "equipment_id": equipment_id, "reason": reason}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            dispatch = self._load_dispatch(connection, dispatch_id)
            if dispatch["state"] not in ("en_route", "on_site"):
                raise ConflictError("调度单当前状态不能登记故障")
            reason = self.domain._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment = connection.execute(
                    "SELECT * FROM commitments WHERE dispatch_id=? AND resource_type='equipment' "
                    "AND resource_id=? AND state IN ('deployed','arrived')",
                    (dispatch_id, equipment_id),
                ).fetchone()
                if commitment is None:
                    raise ConflictError("装备不在该调度单的可故障状态中")
                connection.execute(
                    "UPDATE commitments SET state='broken', updated_at=? WHERE commitment_id=?",
                    (self._ts(), commitment["commitment_id"]),
                )
                connection.execute(
                    "UPDATE equipment_units SET status='out_of_service', allocation='idle' WHERE equipment_id=?",
                    (equipment_id,),
                )
                self._record_dispatch_event(connection, dispatch_id, "breakdown",
                                            {"equipment_id": equipment_id, "reason": reason}, actor_id)
                append_event(connection, actor_id=actor_id, action="dispatch.breakdown",
                             resource_type="dispatch", resource_id=dispatch_id,
                             detail={"equipment_id": equipment_id, "reason": reason},
                             occurred_at=self._ts())
                task = self._load_task(connection, dispatch["task_id"])
                replacement = self._try_replacement(connection, task, equipment_id, now)
                reasons: list[str] = []
                if replacement is None:
                    reasons.append(f"装备 {equipment_id} 故障，暂无可用替代组合")
                    current = json.loads(task["reasons_json"])
                    connection.execute(
                        "UPDATE tasks SET reasons_json=?, updated_at=?, version=version+1 WHERE task_id=?",
                        (canonical_json(current + reasons), self._ts(), task["task_id"]),
                    )
                open_rows = connection.execute(
                    "SELECT COUNT(*) AS count FROM commitments WHERE task_id=? AND state IN "
                    "('reserved','deployed','arrived')",
                    (task["task_id"],),
                ).fetchone()
                task_status = task["status"]
                if open_rows["count"] == 0 and task_status in ("dispatched", "active"):
                    connection.execute(
                        "UPDATE tasks SET status='waitlisted', updated_at=?, version=version+1 WHERE task_id=?",
                        (self._ts(), task_id),
                    )
                    task_status = "waitlisted"
                return "dispatch", dispatch_id, {
                    "dispatch_id": dispatch_id, "equipment_id": equipment_id, "broken": True,
                    "replacement": replacement, "reasons": reasons, "task_status": task_status,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="report_breakdown", payload=payload, create=create)

    def _try_replacement(self, connection, task, broken_equipment_id: str,
                         now: datetime) -> Optional[dict[str, Any]]:
        broken = connection.execute("SELECT * FROM equipment_units WHERE equipment_id=?",
                                    (broken_equipment_id,)).fetchone()
        if broken is None:
            return None
        alert = connection.execute("SELECT requirement_json FROM alerts WHERE alert_id=?",
                                   (task["alert_id"],)).fetchone()
        requirement = json.loads(alert["requirement_json"])
        environment = requirement.get("environment")
        requester_org = self._site_org(connection, task["site_id"])
        min_capability = {key: value for key, value in json.loads(broken["capability_json"]).items()
                          if isinstance(value, (int, float))}
        busy = {row["resource_id"] for row in connection.execute(
            "SELECT resource_id FROM commitments WHERE task_id=? AND state IN ('reserved','deployed','arrived')",
            (task["task_id"],),
        ).fetchall()}
        override = self._confirmed_override(connection, task["task_id"], now)
        item = {"category": broken["category"], "count": 1, "min_capability": min_capability}
        selection, _ = self._select_equipment(connection, item, environment, requester_org,
                                              task["site_id"], now, frozenset(), busy)
        if not selection:
            return None
        entry = selection[0]
        if entry["requires_override"] and override is None:
            return None
        entries = [entry]
        if entry["from_site_id"] != task["site_id"]:
            vehicle = self._select_vehicle(connection, requester_org, entry["from_site_id"],
                                           now, frozenset(), busy | {entry["resource_id"]})
            if vehicle is None or (vehicle["requires_override"] and override is None):
                return None
            entries.append(vehicle)
        duration_hours = requirement.get("duration_hours", 24)
        eta_minutes = 0
        if entry["from_site_id"] != task["site_id"]:
            eta_minutes = self._route_minutes(connection, entry["from_site_id"], task["site_id"]) or 0
        eta_at = now + timedelta(minutes=eta_minutes)
        end_at = eta_at + timedelta(hours=duration_hours)
        dispatch_id = _uuid()
        items = []
        pending_rows = []
        for selected in entries:
            commitment_id = _uuid()
            role = "replacement" if selected is entry else "transport"
            pending_rows.append((commitment_id, selected, role))
            items.append({"commitment_id": commitment_id, "resource_type": selected["resource_type"],
                          "resource_id": selected["resource_id"], "role": role,
                          "from_site_id": selected["from_site_id"]})
        connection.execute(
            "INSERT INTO dispatches(dispatch_id,task_id,override_id,state,items_json,checks_json,"
            "dispatched_at,eta_at,arrived_at,finished_at,closed_at,created_at) "
            "VALUES(?,?,?,'en_route',?,?,?,?,NULL,NULL,NULL,?)",
            (dispatch_id, task["task_id"], override["override_id"] if override else None,
             canonical_json(items), canonical_json([{"resource_id": entry["resource_id"],
                                                     "checks": ["故障替代自动核验通过"],
                                                     "result": "passed"}]),
             self._ts(now), self._ts(eta_at), self._ts()),
        )
        for commitment_id, selected, role in pending_rows:
            connection.execute(
                "INSERT INTO commitments(commitment_id,task_id,dispatch_id,resource_type,resource_id,role,"
                "provider_org_id,from_site_id,requires_override,qualification,state,start_at,end_at,expires_at,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,NULL,'deployed',?,?,NULL,?,?)",
                (commitment_id, task["task_id"], dispatch_id, selected["resource_type"],
                 selected["resource_id"], role, selected["provider_org_id"], selected["from_site_id"],
                 selected["requires_override"], self._ts(now), self._ts(end_at), self._ts(), self._ts()),
            )
            self._set_allocation(connection, selected["resource_type"], selected["resource_id"], "deployed")
        append_event(connection, actor_id="system", action="dispatch.replacement",
                     resource_type="dispatch", resource_id=dispatch_id,
                     detail={"task_id": task["task_id"], "replaces": broken_equipment_id,
                             "items": items},
                     occurred_at=self._ts())
        return {"dispatch_id": dispatch_id, "equipment_id": entry["resource_id"],
                "eta_at": self._ts(eta_at)}

    def extend_task(self, *, request_id: str, actor_id: str, task_id: str,
                    new_end_at: str) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "task_id": task_id, "new_end_at": new_end_at}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            task = self._load_task(connection, task_id)
            if task["status"] not in ("reserved", "dispatched", "active", "returning"):
                raise ConflictError("任务当前状态不能延长")
            new_end = self._parse(new_end_at, "new_end_at")
            if new_end <= now:
                raise ValidationError("new_end_at 必须晚于当前时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                # 任务延长只调整尚未完成的承诺，已终结的承诺保持原记录。
                cursor = connection.execute(
                    "UPDATE commitments SET end_at=?, updated_at=? WHERE task_id=? AND state IN "
                    "('reserved','deployed','arrived')",
                    (self._ts(new_end), self._ts(), task_id),
                )
                connection.execute(
                    "UPDATE tasks SET expected_end_at=?, updated_at=?, version=version+1 WHERE task_id=?",
                    (self._ts(new_end), self._ts(), task_id),
                )
                append_event(connection, actor_id=actor_id, action="task.extended",
                             resource_type="task", resource_id=task_id,
                             detail={"new_end_at": self._ts(new_end),
                                     "extended_commitments": cursor.rowcount},
                             occurred_at=self._ts())
                return "task", task_id, {"task_id": task_id,
                                         "extended_commitments": cursor.rowcount,
                                         "new_end_at": self._ts(new_end)}

            return self._idempotent(connection, request_id=request_id,
                                    action="extend_task", payload=payload, create=create)

    def takeover_task(self, *, request_id: str, actor_id: str, task_id: str,
                      note: str = "") -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "task_id": task_id, "note": note}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            task = self._load_task(connection, task_id)
            if task["status"] not in ("reserved", "dispatched", "active"):
                raise ConflictError("任务当前状态不能接管")

            def create() -> tuple[str, str, dict[str, Any]]:
                open_rows = connection.execute(
                    "SELECT * FROM commitments WHERE task_id=? AND state IN ('reserved','deployed','arrived')",
                    (task_id,),
                ).fetchall()
                providers = {row["provider_org_id"] for row in open_rows}
                released = self._release_open_commitments(connection, task_id, now)
                # 已执行的调度保留原记录，仅标记为被接管。
                connection.execute(
                    "UPDATE dispatches SET state='superseded' WHERE task_id=? AND state IN ('en_route','on_site')",
                    (task_id,),
                )
                connection.execute(
                    "UPDATE tasks SET status='pending', updated_at=?, version=version+1 WHERE task_id=?",
                    (self._ts(), task_id),
                )
                append_event(connection, actor_id=actor_id, action="task.taken_over",
                             resource_type="task", resource_id=task_id,
                             detail={"released_commitments": released,
                                     "previous_providers": sorted(providers), "note": note},
                             occurred_at=self._ts())
                evaluation = self._evaluate_and_mark(connection, task_id, now,
                                                     self.reservation_ttl_minutes,
                                                     exclude_orgs=frozenset(providers))
                return "task", task_id, {"task_id": task_id, "released_commitments": len(released),
                                         "previous_providers": sorted(providers), **evaluation}

            return self._idempotent(connection, request_id=request_id,
                                    action="takeover_task", payload=payload, create=create)

    def finish_task(self, *, request_id: str, actor_id: str,
                    task_id: str) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "task_id": task_id}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            task = self._load_task(connection, task_id)
            if task["status"] != "active":
                raise ConflictError("任务尚未进入现场作业状态，不能收尾")

            def create() -> tuple[str, str, dict[str, Any]]:
                cursor = connection.execute(
                    "UPDATE dispatches SET state='returning', finished_at=? WHERE task_id=? AND state='on_site'",
                    (self._ts(), task_id),
                )
                connection.execute(
                    "UPDATE tasks SET status='returning', updated_at=?, version=version+1 WHERE task_id=?",
                    (self._ts(), task_id),
                )
                append_event(connection, actor_id=actor_id, action="task.finishing",
                             resource_type="task", resource_id=task_id,
                             detail={"returning_dispatches": cursor.rowcount},
                             occurred_at=self._ts())
                return "task", task_id, {"task_id": task_id, "status": "returning",
                                         "returning_dispatches": cursor.rowcount}

            return self._idempotent(connection, request_id=request_id,
                                    action="finish_task", payload=payload, create=create)

    def confirm_return(self, *, request_id: str, actor_id: str, dispatch_id: str,
                       items: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "dispatch_id": dispatch_id, "items": items}
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator", "reviewer")
            now = self._now()
            self._sweep(connection, now)
            dispatch = self._load_dispatch(connection, dispatch_id)
            if dispatch["state"] not in ("on_site", "returning"):
                raise ConflictError("调度单当前状态不能归还验收")
            if not isinstance(items, list):
                raise ValidationError("items 必须是列表")
            task = self._load_task(connection, dispatch["task_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                accepted = []
                for item in items:
                    if not isinstance(item, dict) or not item.get("equipment_id"):
                        raise ValidationError("归还验收条目必须包含 equipment_id")
                    equipment_id = item["equipment_id"]
                    passed = bool(item.get("passed", True))
                    commitment = connection.execute(
                        "SELECT * FROM commitments WHERE dispatch_id=? AND resource_type='equipment' "
                        "AND resource_id=? AND state='arrived'",
                        (dispatch_id, equipment_id),
                    ).fetchone()
                    if commitment is None:
                        raise ConflictError(f"装备 {equipment_id} 不在该调度单的待归还列表中")
                    connection.execute(
                        "UPDATE commitments SET state='fulfilled', updated_at=? WHERE commitment_id=?",
                        (self._ts(), commitment["commitment_id"]),
                    )
                    equipment = connection.execute(
                        "SELECT home_site_id FROM equipment_units WHERE equipment_id=?", (equipment_id,)
                    ).fetchone()
                    connection.execute(
                        "UPDATE equipment_units SET allocation='idle', status=?, current_site_id=? "
                        "WHERE equipment_id=?",
                        ("available" if passed else "maintenance",
                         equipment["home_site_id"], equipment_id),
                    )
                    accepted.append({"equipment_id": equipment_id, "passed": passed,
                                     "notes": str(item.get("notes", ""))})
                self._record_dispatch_event(connection, dispatch_id, "return_acceptance",
                                            {"items": accepted}, actor_id)
                open_equipment = connection.execute(
                    "SELECT COUNT(*) AS count FROM commitments WHERE dispatch_id=? AND "
                    "resource_type='equipment' AND state IN ('reserved','deployed','arrived')",
                    (dispatch_id,),
                ).fetchone()["count"]
                state = dispatch["state"]
                if open_equipment == 0:
                    team_rows = connection.execute(
                        "SELECT * FROM commitments WHERE dispatch_id=? AND resource_type='team' "
                        "AND state='arrived'",
                        (dispatch_id,),
                    ).fetchall()
                    for team_commitment in team_rows:
                        connection.execute(
                            "UPDATE commitments SET state='fulfilled', updated_at=? WHERE commitment_id=?",
                            (self._ts(), team_commitment["commitment_id"]),
                        )
                        team = connection.execute("SELECT home_site_id FROM operator_teams WHERE team_id=?",
                                                  (team_commitment["resource_id"],)).fetchone()
                        connection.execute(
                            "UPDATE operator_teams SET allocation='idle', current_site_id=? WHERE team_id=?",
                            (team["home_site_id"], team_commitment["resource_id"]),
                        )
                    connection.execute(
                        "UPDATE dispatches SET state='closed', closed_at=? WHERE dispatch_id=?",
                        (self._ts(), dispatch_id),
                    )
                    state = "closed"
                append_event(connection, actor_id=actor_id, action="dispatch.returned",
                             resource_type="dispatch", resource_id=dispatch_id,
                             detail={"accepted": accepted, "state": state},
                             occurred_at=self._ts())
                open_any = connection.execute(
                    "SELECT COUNT(*) AS count FROM commitments WHERE task_id=? AND state IN "
                    "('reserved','deployed','arrived')",
                    (task["task_id"],),
                ).fetchone()["count"]
                open_dispatches = connection.execute(
                    "SELECT COUNT(*) AS count FROM dispatches WHERE task_id=? AND state IN "
                    "('en_route','on_site','returning')",
                    (task["task_id"],),
                ).fetchone()["count"]
                task_status = connection.execute("SELECT status FROM tasks WHERE task_id=?",
                                                 (task["task_id"],)).fetchone()["status"]
                if open_any == 0 and open_dispatches == 0 and task_status == "returning":
                    connection.execute(
                        "UPDATE tasks SET status='completed', updated_at=?, version=version+1 WHERE task_id=?",
                        (self._ts(), task["task_id"]),
                    )
                    append_event(connection, actor_id=actor_id, action="task.completed",
                                 resource_type="task", resource_id=task["task_id"],
                                 detail={}, occurred_at=self._ts())
                    task_status = "completed"
                promoted = self._reevaluate_waitlist(connection, now)
                return "dispatch", dispatch_id, {"dispatch_id": dispatch_id, "state": state,
                                                 "accepted": accepted, "task_status": task_status,
                                                 "promoted_tasks": promoted}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_return", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 紧急越级（双人确认 + 到期回收）
    # ------------------------------------------------------------------

    def request_override(self, *, request_id: str, actor_id: str, task_id: str,
                         reason: str, ttl_minutes: int = 60) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason,
                   "ttl_minutes": ttl_minutes}
        with self._transaction() as connection:
            self._require_operator(connection, actor_id)
            now = self._now()
            self._sweep(connection, now)
            task = self._load_task(connection, task_id)
            if task["status"] not in ("pending", "reserved", "waitlisted"):
                raise ConflictError("任务当前状态不需要越级授权")
            reason = self.domain._text(reason, "reason")
            if not isinstance(ttl_minutes, int) or ttl_minutes <= 0 or ttl_minutes > 24 * 60:
                raise ValidationError("ttl_minutes 必须是 1 到 1440 之间的整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                override_id = _uuid()
                expires_at = now + timedelta(minutes=ttl_minutes)
                connection.execute(
                    "INSERT INTO overrides(override_id,task_id,reason,initiator_id,confirmer_id,state,"
                    "expires_at,created_at,confirmed_at) VALUES(?,?,?,?,NULL,'pending',?,?,NULL)",
                    (override_id, task_id, reason, actor_id, self._ts(expires_at), self._ts()),
                )
                append_event(connection, actor_id=actor_id, action="override.requested",
                             resource_type="override", resource_id=override_id,
                             detail={"task_id": task_id, "reason": reason,
                                     "expires_at": self._ts(expires_at)},
                             occurred_at=self._ts())
                return "override", override_id, {"override_id": override_id, "task_id": task_id,
                                                 "state": "pending",
                                                 "expires_at": self._ts(expires_at)}

            return self._idempotent(connection, request_id=request_id,
                                    action="request_override", payload=payload, create=create)

    def confirm_override(self, *, request_id: str, actor_id: str,
                         override_id: str) -> tuple[dict[str, Any], bool]:
        payload = {"actor_id": actor_id, "override_id": override_id}
        with self._transaction() as connection:
            actor = self._actor(connection, actor_id)
            self.domain._require(actor, "admin", "reviewer")
            now = self._now()
            self._sweep(connection, now)
            row = connection.execute("SELECT * FROM overrides WHERE override_id=?",
                                     (override_id,)).fetchone()
            if row is None:
                raise NotFoundError("越级授权不存在")
            if row["state"] != "pending":
                raise ConflictError("越级授权不在待确认状态")
            if self._parse(row["expires_at"]) <= now:
                raise ConflictError("越级授权已过期，无法确认")
            if row["initiator_id"] == actor_id:
                raise PermissionDenied("紧急越级必须由不同操作者双人确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE overrides SET state='confirmed', confirmer_id=?, confirmed_at=? WHERE override_id=?",
                    (actor_id, self._ts(), override_id),
                )
                append_event(connection, actor_id=actor_id, action="override.confirmed",
                             resource_type="override", resource_id=override_id,
                             detail={"task_id": row["task_id"], "initiator_id": row["initiator_id"],
                                     "confirmer_id": actor_id},
                             occurred_at=self._ts())
                return "override", override_id, {"override_id": override_id, "state": "confirmed",
                                                 "confirmer_id": actor_id,
                                                 "confirmed_at": self._ts()}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_override", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 到期回收与重启恢复
    # ------------------------------------------------------------------

    def _sweep(self, connection, now: datetime) -> dict[str, Any]:
        """惰性回收：过期预留与过期越级授权只影响尚未完成的承诺。"""

        expired_reservations = 0
        affected_tasks: set[str] = set()
        rows = connection.execute(
            "SELECT * FROM commitments WHERE state='reserved' AND expires_at IS NOT NULL"
        ).fetchall()
        for row in rows:
            if self._parse(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE commitments SET state='expired', updated_at=? WHERE commitment_id=?",
                    (self._ts(), row["commitment_id"]),
                )
                self._free_allocation(connection, row["resource_type"], row["resource_id"])
                append_event(connection, actor_id="system", action="commitment.expired",
                             resource_type="commitment", resource_id=row["commitment_id"],
                             detail={"task_id": row["task_id"], "resource_id": row["resource_id"]},
                             occurred_at=self._ts())
                expired_reservations += 1
                affected_tasks.add(row["task_id"])
        recalled: list[str] = []
        for task_id in sorted(affected_tasks):
            remaining = connection.execute(
                "SELECT COUNT(*) AS count FROM commitments WHERE task_id=? AND state='reserved'",
                (task_id,),
            ).fetchone()["count"]
            task = connection.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if remaining == 0 and task and task["status"] == "reserved":
                reasons = ["预留到期未出动，资源已回收"]
                connection.execute(
                    "UPDATE tasks SET status='waitlisted', reasons_json=?, updated_at=?, version=version+1 "
                    "WHERE task_id=?",
                    (canonical_json(reasons), self._ts(), task_id),
                )
                append_event(connection, actor_id="system", action="task.waitlisted",
                             resource_type="task", resource_id=task_id,
                             detail={"reasons": reasons}, occurred_at=self._ts())
                recalled.append(task_id)
        expired_overrides = 0
        override_rows = connection.execute(
            "SELECT * FROM overrides WHERE state IN ('pending','confirmed')"
        ).fetchall()
        for row in override_rows:
            if self._parse(row["expires_at"]) <= now:
                connection.execute("UPDATE overrides SET state='expired' WHERE override_id=?",
                                   (row["override_id"],))
                append_event(connection, actor_id="system", action="override.expired",
                             resource_type="override", resource_id=row["override_id"],
                             detail={"task_id": row["task_id"], "previous_state": row["state"]},
                             occurred_at=self._ts())
                expired_overrides += 1
                if row["state"] == "confirmed":
                    task = connection.execute("SELECT * FROM tasks WHERE task_id=?",
                                              (row["task_id"],)).fetchone()
                    if task and task["status"] == "reserved":
                        reserved = connection.execute(
                            "SELECT * FROM commitments WHERE task_id=? AND state='reserved'",
                            (row["task_id"],),
                        ).fetchall()
                        if any(item["requires_override"] for item in reserved):
                            self._release_open_commitments(connection, row["task_id"], now,
                                                           states=("reserved",))
                            reasons = ["越级授权到期回收，预留资源已释放"]
                            connection.execute(
                                "UPDATE tasks SET status='waitlisted', reasons_json=?, updated_at=?, "
                                "version=version+1 WHERE task_id=?",
                                (canonical_json(reasons), self._ts(), row["task_id"]),
                            )
                            append_event(connection, actor_id="system", action="task.waitlisted",
                                         resource_type="task", resource_id=row["task_id"],
                                         detail={"reasons": reasons}, occurred_at=self._ts())
                            recalled.append(row["task_id"])
        return {"expired_reservations": expired_reservations,
                "expired_overrides": expired_overrides,
                "recalled_tasks": sorted(set(recalled))}

    def _reevaluate_waitlist(self, connection, now: datetime) -> list[str]:
        """资源释放后按候补顺序自动重估等待中的任务。"""

        rows = connection.execute(
            "SELECT task_id FROM tasks WHERE status='waitlisted' "
            "ORDER BY priority DESC, created_at ASC, task_id ASC"
        ).fetchall()
        promoted = []
        for row in rows:
            result = self._evaluate_and_mark(connection, row["task_id"], now,
                                             self.reservation_ttl_minutes)
            if result["reserved"]:
                promoted.append(row["task_id"])
        return promoted

    def recover(self) -> dict[str, Any]:
        """服务启动或人工触发时恢复：清扫到期承诺，在途调度随状态机继续。"""

        now = self._now()
        with self._transaction() as connection:
            summary = self._sweep(connection, now)
            inflight = connection.execute(
                "SELECT COUNT(*) AS count FROM dispatches WHERE state IN ('en_route','on_site','returning')"
            ).fetchone()["count"]
            summary["inflight_dispatches"] = inflight
            append_event(connection, actor_id="system", action="system.recovered",
                         resource_type="service", resource_id="disaster_equipment",
                         detail=summary, occurred_at=self._ts())
            return summary

    # ------------------------------------------------------------------
    # 指挥查询
    # ------------------------------------------------------------------

    def region_capability(self, site_id: str, at: str | None = None) -> dict[str, Any]:
        """汇总某地区当前可兑现的能力：就绪装备、队伍、路线与协议。"""

        now = self._parse(at, "at") if at else self._now()
        with self._transaction() as connection:
            self._sweep(connection, now)
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            equipment_rows = connection.execute(
                "SELECT * FROM equipment_units WHERE current_site_id=? ORDER BY equipment_id",
                (site_id,),
            ).fetchall()
            equipment_view = []
            categories: dict[str, dict[str, Any]] = {}
            for row in equipment_rows:
                ready, reason = self._equipment_ready(connection, row, now)
                capability = json.loads(row["capability_json"])
                equipment_view.append({
                    "equipment_id": row["equipment_id"], "category": row["category"],
                    "name": row["name"], "status": row["status"], "allocation": row["allocation"],
                    "capability": capability, "ready": ready, "reason": reason,
                })
                if ready:
                    bucket = categories.setdefault(row["category"], {"count": 0})
                    bucket["count"] += 1
                    for key, value in capability.items():
                        if isinstance(value, (int, float)):
                            bucket[key] = bucket.get(key, 0) + value
            team_rows = connection.execute(
                "SELECT * FROM operator_teams WHERE current_site_id=? ORDER BY team_id",
                (site_id,),
            ).fetchall()
            teams_view = []
            teams_available = 0
            for row in team_rows:
                ready, reason = self._team_ready(connection, row, now)
                teams_view.append({"team_id": row["team_id"], "name": row["name"],
                                   "allocation": row["allocation"], "ready": ready, "reason": reason})
                if ready:
                    teams_available += 1
            routes = [{"to_site_id": row["to_site_id"], "duration_minutes": row["duration_minutes"]}
                      for row in connection.execute(
                          "SELECT * FROM transport_routes WHERE from_site_id=? ORDER BY to_site_id",
                          (site_id,)).fetchall()]
            org_id = self._site_org(connection, site_id)
            agreements = []
            for row in connection.execute(
                    "SELECT * FROM mutual_aid_agreements WHERE requester_org_id=?", (org_id,)).fetchall():
                if self._parse(row["valid_from"]) <= now <= self._parse(row["valid_until"]):
                    agreements.append({"agreement_id": row["agreement_id"],
                                       "provider_org_id": row["provider_org_id"],
                                       "categories": json.loads(row["categories_json"]),
                                       "valid_until": row["valid_until"]})
            open_commitments = connection.execute(
                "SELECT COUNT(*) AS count FROM commitments WHERE from_site_id=? AND state IN "
                "('reserved','deployed','arrived')",
                (site_id,),
            ).fetchone()["count"]
            return {
                "site_id": site_id, "at": self._ts(now),
                "deliverable": {"categories": categories, "teams_available": teams_available,
                                "teams_total": len(team_rows)},
                "equipment": equipment_view,
                "teams": teams_view,
                "routes": routes,
                "agreements": agreements,
                "open_commitments": open_commitments,
            }

    def list_equipment(self, site_id: str | None = None,
                       category: str | None = None) -> list[dict[str, Any]]:
        """列出装备及单台就绪结论，用于对照纸面能力与可出动能力。"""

        now = self._now()
        with self._transaction() as connection:
            self._sweep(connection, now)
            query = "SELECT * FROM equipment_units"
            conditions: list[str] = []
            parameters: list[Any] = []
            if site_id:
                conditions.append("current_site_id=?")
                parameters.append(site_id)
            if category:
                conditions.append("category=?")
                parameters.append(category)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY equipment_id"
            items = []
            for row in connection.execute(query, parameters).fetchall():
                ready, reason = self._equipment_ready(connection, row, now)
                items.append({
                    "equipment_id": row["equipment_id"], "organization_id": row["organization_id"],
                    "home_site_id": row["home_site_id"], "current_site_id": row["current_site_id"],
                    "category": row["category"], "name": row["name"],
                    "capability": json.loads(row["capability_json"]),
                    "environments": json.loads(row["environments_json"]),
                    "status": row["status"], "allocation": row["allocation"],
                    "ready": ready, "reason": reason,
                })
            return items

    def _task_view(self, connection, row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"], "alert_id": row["alert_id"], "site_id": row["site_id"],
            "priority": row["priority"], "status": row["status"],
            "reasons": json.loads(row["reasons_json"]),
            "expected_end_at": row["expected_end_at"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "version": row["version"],
        }

    def get_task(self, task_id: str) -> dict[str, Any]:
        now = self._now()
        with self._transaction() as connection:
            self._sweep(connection, now)
            task = self._load_task(connection, task_id)
            alert = connection.execute("SELECT * FROM alerts WHERE alert_id=?",
                                       (task["alert_id"],)).fetchone()
            commitments = [dict(row) for row in connection.execute(
                "SELECT * FROM commitments WHERE task_id=? ORDER BY created_at, commitment_id",
                (task_id,)).fetchall()]
            dispatches = []
            for row in connection.execute(
                    "SELECT * FROM dispatches WHERE task_id=? ORDER BY created_at, dispatch_id",
                    (task_id,)).fetchall():
                dispatches.append({
                    "dispatch_id": row["dispatch_id"], "state": row["state"],
                    "items": json.loads(row["items_json"]), "checks": json.loads(row["checks_json"]),
                    "dispatched_at": row["dispatched_at"], "eta_at": row["eta_at"],
                    "arrived_at": row["arrived_at"], "finished_at": row["finished_at"],
                    "closed_at": row["closed_at"], "override_id": row["override_id"],
                })
            overrides = [dict(row) for row in connection.execute(
                "SELECT * FROM overrides WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()]
            view = self._task_view(connection, task)
            view.update({
                "severity": alert["severity"],
                "requirement": json.loads(alert["requirement_json"]),
                "commitments": commitments,
                "dispatches": dispatches,
                "overrides": overrides,
            })
            return view

    def list_tasks(self, site_id: str | None = None,
                   status: str | None = None) -> list[dict[str, Any]]:
        now = self._now()
        with self._transaction() as connection:
            self._sweep(connection, now)
            query = "SELECT * FROM tasks"
            conditions: list[str] = []
            parameters: list[Any] = []
            if site_id:
                conditions.append("site_id=?")
                parameters.append(site_id)
            if status:
                conditions.append("status=?")
                parameters.append(status)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY created_at, task_id"
            return [self._task_view(connection, row)
                    for row in connection.execute(query, parameters).fetchall()]

    def waitlist(self, site_id: str | None = None) -> list[dict[str, Any]]:
        """按优先级与到达时间给出落选任务的候补顺序。"""

        now = self._now()
        with self._transaction() as connection:
            self._sweep(connection, now)
            query = ("SELECT * FROM tasks WHERE status='waitlisted'")
            parameters: list[Any] = []
            if site_id:
                query += " AND site_id=?"
                parameters.append(site_id)
            query += " ORDER BY priority DESC, created_at ASC, task_id ASC"
            items = []
            for position, row in enumerate(connection.execute(query, parameters).fetchall(), start=1):
                view = self._task_view(connection, row)
                view["position"] = position
                items.append(view)
            return items

    def get_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        now = self._now()
        with self._transaction() as connection:
            self._sweep(connection, now)
            row = self._load_dispatch(connection, dispatch_id)
            events = [{"event_id": event["event_id"], "kind": event["kind"],
                       "detail": json.loads(event["detail_json"]), "actor_id": event["actor_id"],
                       "created_at": event["created_at"]}
                      for event in connection.execute(
                          "SELECT * FROM dispatch_events WHERE dispatch_id=? ORDER BY rowid",
                          (dispatch_id,)).fetchall()]
            return {
                "dispatch_id": row["dispatch_id"], "task_id": row["task_id"], "state": row["state"],
                "items": json.loads(row["items_json"]), "checks": json.loads(row["checks_json"]),
                "dispatched_at": row["dispatched_at"], "eta_at": row["eta_at"],
                "arrived_at": row["arrived_at"], "finished_at": row["finished_at"],
                "closed_at": row["closed_at"], "override_id": row["override_id"],
                "events": events,
            }
