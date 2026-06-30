"""UTC 时间工具。

设计原则（参照 polymarket_twitter_monitor/prediction/time_utils.py）：
- 内部时间全 UTC（aware datetime）
- 展示时间转北京时间（UTC+8）
- 禁止 datetime.now()，统一使用 now_utc()
- 数据库存储 ISO 8601 格式的 UTC 时间字符串
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

# 北京时区（UTC+8）
BEIJING_TZ = timezone(timedelta(hours=8))
# UTC 时区
UTC_TZ = timezone.utc


def now_utc() -> datetime:
    """获取当前 UTC 时间（aware datetime）。"""
    return datetime.now(UTC_TZ)


def to_utc_iso(dt: datetime) -> str:
    """将 datetime 转 UTC ISO 字符串。

    若 dt 为 naive datetime，按 UTC 解释。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ).isoformat()


def from_utc_iso(s: str) -> Optional[datetime]:
    """从 UTC ISO 字符串解析为 aware datetime。

    支持以下格式：
    - "2026-06-29T15:30:00+00:00"
    - "2026-06-29T15:30:00Z"
    - "2026-06-29 15:30:00"

    解析失败返回 None。
    """
    if not s:
        return None
    try:
        # 兼容 Z 后缀
        normalized = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC_TZ)
        return dt.astimezone(UTC_TZ)
    except (ValueError, TypeError):
        pass
    # 兜底：常见空格分隔格式
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=UTC_TZ)
    except (ValueError, TypeError):
        return None


def to_beijing_dt(dt: datetime) -> datetime:
    """将 datetime 转为北京时间（aware）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(BEIJING_TZ)


def to_beijing_str(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """转北京时间显示字符串。"""
    return to_beijing_dt(dt).strftime(fmt)


def format_display_label(dt: datetime) -> str:
    """转北京时间简短标签（用于通知标题）。"""
    return to_beijing_dt(dt).strftime("%m-%d %H:%M")


def minutes_between(start: datetime, end: datetime) -> float:
    """计算两个时间之间的分钟数（end - start）。

    若为 naive datetime，按 UTC 解释。
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC_TZ)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC_TZ)
    return (end - start).total_seconds() / 60.0


def hours_between(start: datetime, end: datetime) -> float:
    """计算两个时间之间的小时数（end - start）。"""
    return minutes_between(start, end) / 60.0
