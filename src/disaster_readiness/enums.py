"""灾害装备战备与调拨领域使用的固定取值。"""

from __future__ import annotations

#: 承诺内每条装备明细的状态。已经执行的明细只允许向前迁移，不删除、不覆写。
ITEM_PLANNED = "planned"        # 随限时预留一起占位，尚未出动
ITEM_IN_TRANSIT = "in_transit"  # 已正式出动，运输中
ITEM_ARRIVED = "arrived"        # 已经到场
ITEM_FAULTY = "faulty"          # 故障或归还验收不合格，记录保留但释放占用
ITEM_RETURNED = "returned"      # 归还验收合格
ITEM_PREEMPTED = "preempted"    # 被紧急越级暂时征用，越级到期后恢复
ITEM_CANCELLED = "cancelled"    # 预留到期或任务取消，未执行即释放

#: 仍然独占装备/车辆/队伍的明细状态。
ITEM_BUSY_STATUSES = frozenset({ITEM_PLANNED, ITEM_IN_TRANSIT, ITEM_ARRIVED})
#: 不再占用任何资源的明细状态。
ITEM_RELEASED_STATUSES = frozenset(
    {ITEM_FAULTY, ITEM_RETURNED, ITEM_PREEMPTED, ITEM_CANCELLED}
)
#: 物理运输尚未完成的明细状态（重启后需要继续运输）。
ITEM_TRAVEL_STATUSES = frozenset({ITEM_PLANNED, ITEM_IN_TRANSIT})
#: 已进入终态、后续调整不得再改写的明细状态。
ITEM_TERMINAL_STATUSES = frozenset({ITEM_RETURNED, ITEM_FAULTY})

#: 承诺生命周期。
COMMITMENT_HELD = "held"                # 预警阶段的限时预留
COMMITMENT_DISPATCHED = "dispatched"    # 正式出动，运输中
COMMITMENT_OPERATING = "operating"      # 至少部分到场并投入
COMMITMENT_RETURNING = "returning"      # 撤收运输，等待归还验收
COMMITMENT_COMPLETED = "completed"      # 归还验收完成
COMMITMENT_CANCELLED = "cancelled"
COMMITMENT_EXPIRED = "expired"          # 预留到期未确认
COMMITMENT_PREEMPTED = "preempted"      # 整套软预留被紧急越级征用

#: 仍然占用能力的承诺状态。
ACTIVE_COMMITMENT_STATUSES = frozenset({
    COMMITMENT_HELD,
    COMMITMENT_DISPATCHED,
    COMMITMENT_OPERATING,
    COMMITMENT_RETURNING,
})
#: 已经执行、不可取消只可继续走交接/归还流程的状态。
EXECUTED_COMMITMENT_STATUSES = frozenset({
    COMMITMENT_DISPATCHED,
    COMMITMENT_OPERATING,
    COMMITMENT_RETURNING,
})

MISSION_OPEN = "open"
MISSION_RESERVED = "reserved"
MISSION_DISPATCHED = "dispatched"
MISSION_COMPLETED = "completed"
MISSION_CANCELLED = "cancelled"

WAITLIST_WAITING = "waiting"
WAITLIST_PROMOTED = "promoted"
WAITLIST_CANCELLED = "cancelled"

#: 落选原因：容量竞争类（可候补、低优先级软预留可被越级）。
CAPACITY_REASONS = frozenset({"equipment_busy", "vehicle_busy", "crew_busy"})
#: 落选原因：硬性不合格类。
HARD_REASONS = frozenset({
    "equipment_inactive",
    "maintenance_open",
    "cert_missing",
    "cert_expired",
    "cert_revoked",
    "env_unsuitable",
    "crew_not_qualified",
    "crew_inactive",
    "vehicle_missing",
    "vehicle_inactive",
    "travel_unknown",
    "agreement_missing",
    "agreement_expired",
})

KIND_RESERVE = "reserve"
KIND_OVERRIDE = "override"

#: 候补自动转正时给予的预留有效期（分钟）。
DEFAULT_PROMOTION_TTL_MINUTES = 30
