"""为战备流程提供可推进的测试时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class MutableClock:
    """允许测试与离线验收按需要推进当前时间。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("初始时间必须包含时区")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        """返回当前设定的 UTC 时间。"""

        return self._value

    def advance(self, **kwargs: float) -> None:
        """按 timedelta 参数推进当前时间。"""

        self._value = self._value + timedelta(**kwargs)

    def set(self, value: datetime) -> None:
        """直接设定当前时间。"""

        if value.tzinfo is None:
            raise ValueError("设定时间必须包含时区")
        self._value = value.astimezone(timezone.utc)
