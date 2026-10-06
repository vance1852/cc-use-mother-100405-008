"""灾害装备战备与调拨领域在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Equipment:
    equipment_id: str
    region_id: str
    name: str
    capability_code: str          # 大流量排水、应急供电等能力代码
    capability: dict[str, Any]    # 流量、扬程等能力参数
    environments: tuple[str, ...]  # 可部署环境标签
    active: bool


@dataclass(frozen=True)
class Crew:
    crew_id: str
    region_id: str
    name: str
    active: bool


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    region_id: str
    name: str
    active: bool


@dataclass(frozen=True)
class Rejection:
    """一条需求行或候选资源的落选原因。"""

    subject: str          # requirement / equipment / vehicle / crew
    code: str
    message: str
    capacity: bool = field(default=False)  # 是否属于可候补的容量竞争原因


@dataclass(frozen=True)
class Allocation:
    """分配器为一次任务需求给出的整套组合方案。"""

    feasible: bool
    items: tuple[dict[str, Any], ...]
    rejections: tuple[Rejection, ...]
    eta_minutes: int | None
