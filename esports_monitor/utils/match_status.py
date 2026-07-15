"""比赛状态归一化工具。"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .time_utils import from_utc_iso, now_utc


DEFAULT_MAX_LIVE_HOURS_BY_GAME: Dict[str, float] = {
    "cs2": 6.0,
    "lol": 4.0,
    "dota2": 6.0,
}


def get_max_live_hours(game: str, overrides: Optional[Mapping[str, Any]] = None) -> float:
    """按游戏返回合理赛中时长上限。"""
    key = (game or "").lower()
    if overrides and key in overrides:
        try:
            return float(overrides[key])
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_LIVE_HOURS_BY_GAME.get(key, 6.0)


def determine_match_status(
    match: Dict[str, Any],
    max_live_hours_by_game: Optional[Mapping[str, Any]] = None,
) -> str:
    """基于 DB 状态、开始/结束时间和游戏时长上限判断展示/监控状态。

    Polymarket 的部分电竞市场 end_time 是市场封盘/结算窗口，不是实际比赛结束时间。
    因此比赛开始后超过合理赛中时长时，应归入 ended，避免 Dashboard 和监控
    把陈旧市场继续当作赛中比赛。

    重要：DB 中 status='live' 由市场发现阶段通过 Polymarket 网页 isLive 标记设置，
    是判断比赛是否正在进行的权威依据（Gamma API 的 live 字段不准确）。
    因此当 DB 状态为 live 时，不因 start_time 在未来而降级为 discovered，
    仅在出现结束证据（end_time 已过、超时、winning_team）时才转为 ended。
    """
    raw_status = str(match.get("status") or "discovered").lower()
    if raw_status == "settled":
        return "settled"

    if match.get("winning_team") or match.get("real_end_time") or raw_status == "ended":
        return "ended"

    start_dt = from_utc_iso(match.get("start_time")) if match.get("start_time") else None
    end_dt = from_utc_iso(match.get("end_time")) if match.get("end_time") else None
    now = now_utc()

    if end_dt is not None and now > end_dt:
        return "ended"

    # DB 已标记为 live（由网页 isLive 设置）：信任该状态，仅检查超时
    if raw_status == "live":
        if start_dt is not None:
            max_live_hours = get_max_live_hours(
                str(match.get("game") or ""),
                max_live_hours_by_game,
            )
            hours_since_start = (now - start_dt).total_seconds() / 3600.0
            if hours_since_start > max_live_hours:
                return "ended"
        return "live"

    if start_dt is None:
        return raw_status if raw_status in {"discovered", "live"} else "discovered"

    if now < start_dt:
        return "discovered"

    max_live_hours = get_max_live_hours(
        str(match.get("game") or ""),
        max_live_hours_by_game,
    )
    hours_since_start = (now - start_dt).total_seconds() / 3600.0
    if hours_since_start > max_live_hours:
        return "ended"

    return "live"
