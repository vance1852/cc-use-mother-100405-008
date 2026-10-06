"""灾害装备战备与调拨服务。

在科技战略协作基础服务（SQLite 事务、哈希审计链、可替换时钟、幂等回执）
之上，提供随时间变化的装备能力承诺、限时预留、原子出动核验、故障与替代、
任务延长、跨区接管、紧急越级与候补转正等能力。
"""

from .service import ReadinessService
from .storage import DisasterDatabase

__all__ = ["ReadinessService", "DisasterDatabase"]
