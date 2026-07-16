"""比赛相关 API。

端点：
- GET /api/matches          比赛列表（筛选 + 回溯天数）
- GET /api/matches/{id}     比赛详情
- GET /api/matches/{id}/prices   价格序列
- GET /api/matches/{id}/orderbook 最近盘口快照
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Query

from ...utils.match_status import determine_match_status
from ...utils.time_utils import now_utc, to_utc_iso

router = APIRouter(tags=["matches"])

# 默认数据回溯天数：使用 365 天确保用户能看到所有已结算比赛历史
DEFAULT_DAYS = 365


def _get_deps(request: Request):
    return request.app.state.deps


def _normalize_match_for_dashboard(match: Dict[str, Any], deps: Any) -> Dict[str, Any]:
    """返回 Dashboard 使用的比赛行，保留原始 DB 状态，并附加 Polymarket 链接。"""
    status_cfg = ((deps.config or {}).get("status") or {}).get("max_live_hours_by_game") or {}
    row = dict(match)
    row["raw_status"] = row.get("status")
    row["status"] = determine_match_status(row, status_cfg)
    # 附加 Polymarket 链接，方便用户跳转核对
    row["polymarket_url"] = _build_polymarket_url(row.get("game") or "", row.get("slug") or "")
    return row


def _build_polymarket_url(game: str, slug: str) -> str:
    """构造 Polymarket 赛事页面 URL。

    URL 格式：https://polymarket.com/zh/esports/{game_page_slug}/{event_slug}
    game_page_slug 映射：lol -> league-of-legends, cs2 -> cs2, dota2 -> dota-2
    """
    if not slug:
        return ""
    game_page_slug = {
        "lol": "league-of-legends",
        "cs2": "cs2",
        "dota2": "dota-2",
    }.get(game, game or "esports")
    return f"https://polymarket.com/zh/esports/{game_page_slug}/{slug}"


@router.get("/matches")
async def list_matches(
    request: Request,
    status: Optional[str] = Query(None, description="状态筛选: discovered/live/ended/settled"),
    game: Optional[str] = Query(None, description="游戏筛选: cs2/dota2/lol"),
    team: Optional[str] = Query(None, description="队伍名称模糊匹配（team_a 或 team_b）"),
    days: int = Query(DEFAULT_DAYS, description="回溯天数"),
) -> Dict[str, Any]:
    """获取比赛列表。

    返回字段说明：
    - matches: 比赛列表，每行包含 polymarket_url 字段供前端生成跳转链接
    - ok: True 表示接口成功；False 表示内部异常（error 字段给出原因）
    """
    deps = _get_deps(request)
    try:
        matches = deps.storage.get_all_matches()
        matches = [_normalize_match_for_dashboard(m, deps) for m in matches]

        if status and status in ("discovered", "live", "ended", "settled"):
            matches = [m for m in matches if m.get("status") == status]

        # 回溯天数过滤：仅返回 discovered_at 在 N 天内的比赛
        cutoff = now_utc() - timedelta(days=days)
        cutoff_iso = to_utc_iso(cutoff)
        matches = [m for m in matches if (m.get("discovered_at") or "") >= cutoff_iso]

        # 游戏筛选
        if game:
            matches = [m for m in matches if m.get("game") == game]

        # 队伍名称模糊匹配（不区分大小写，匹配 team_a 或 team_b）
        if team:
            kw = team.lower().strip()
            matches = [
                m for m in matches
                if kw in (m.get("team_a") or "").lower()
                or kw in (m.get("team_b") or "").lower()
            ]

        return {"matches": matches, "total": len(matches), "ok": True}
    except Exception as exc:
        return {"matches": [], "total": 0, "ok": False, "error": str(exc)}


@router.get("/matches/{match_id}")
async def get_match(match_id: str, request: Request) -> Dict[str, Any]:
    """获取比赛详情。"""
    deps = _get_deps(request)
    match = deps.storage.get_match(match_id)
    if not match:
        return {"error": "match not found", "ok": False}
    match = _normalize_match_for_dashboard(match, deps)

    # 附加信号和交易统计
    signals = deps.storage.get_morphology_signals(match_id=match_id)
    trades = deps.storage.get_open_trades(match_id=match_id)

    return {
        "match": match,
        "signals": signals,
        "open_trades": trades,
        "signal_count": len(signals),
        "open_trade_count": len(trades),
        "ok": True,
    }


@router.get("/matches/{match_id}/prices")
async def get_match_prices(
    match_id: str,
    request: Request,
    hours: Optional[float] = Query(None, description="仅返回最近N小时数据"),
) -> Dict[str, Any]:
    """获取比赛价格序列（用于图表）。"""
    deps = _get_deps(request)
    try:
        match = deps.storage.get_match(match_id) or {}
        prices_a = deps.storage.get_price_snapshots(match_id, team="team_a", hours=hours)
        prices_b = deps.storage.get_price_snapshots(match_id, team="team_b", hours=hours)

        return {
            "match_id": match_id,
            "team_a_name": match.get("team_a"),
            "team_b_name": match.get("team_b"),
            "team_a": [{"recorded_at": ts, "price": p} for ts, p in prices_a],
            "team_b": [{"recorded_at": ts, "price": p} for ts, p in prices_b],
            "ok": True,
        }
    except Exception as exc:
        return {
            "match_id": match_id,
            "team_a": [],
            "team_b": [],
            "ok": False,
            "error": str(exc),
        }


@router.get("/matches/{match_id}/orderbook")
async def get_match_orderbook(match_id: str, request: Request) -> Dict[str, Any]:
    """获取比赛最近一条盘口快照。"""
    deps = _get_deps(request)
    try:
        with deps.storage._connect(row_factory=True) as conn:
            cur = conn.execute(
                """
                SELECT obs.*
                FROM orderbook_snapshots obs
                JOIN (
                    SELECT team, MAX(recorded_at) AS recorded_at
                    FROM orderbook_snapshots
                    WHERE match_id=?
                    GROUP BY team
                ) latest
                  ON latest.team = obs.team
                 AND latest.recorded_at = obs.recorded_at
                WHERE obs.match_id=?
                ORDER BY obs.team
                """,
                (match_id, match_id),
            )
            rows = [dict(r) for r in cur.fetchall()]
        # 返回 team_a 和 team_b 各一条最新的
        result: Dict[str, Any] = {"match_id": match_id, "ok": True}
        for row in rows:
            team = row.get("team", "")
            if team and team not in result:
                result[team] = {
                    "best_bid": row.get("best_bid"),
                    "best_ask": row.get("best_ask"),
                    "bid_depth": row.get("bid_depth"),
                    "ask_depth": row.get("ask_depth"),
                    "spread": row.get("spread"),
                    "recorded_at": row.get("recorded_at"),
                }
        return result
    except Exception as exc:
        return {"match_id": match_id, "ok": False, "error": str(exc)}


@router.get("/matches/{match_id}/cooldown")
async def get_match_cooldown(match_id: str, request: Request) -> Dict[str, Any]:
    """获取比赛冷却状态。"""
    deps = _get_deps(request)
    try:
        with deps.storage._connect(row_factory=True) as conn:
            cur = conn.execute(
                "SELECT * FROM morphology_cooldown WHERE match_id=? ORDER BY cooldown_end DESC LIMIT 1",
                (match_id,),
            )
            row = cur.fetchone()
            if row:
                cd = dict(row)
                from ...utils.time_utils import from_utc_iso
                cooldown_end = from_utc_iso(cd.get("cooldown_end"))
                now = now_utc()
                if cooldown_end and cooldown_end > now:
                    remaining = (cooldown_end - now).total_seconds() / 60.0
                    return {
                        "match_id": match_id,
                        "in_cooldown": True,
                        "cooldown_end": cd.get("cooldown_end"),
                        "remaining_minutes": remaining,
                        "last_signal": cd.get("last_signal"),
                        "ok": True,
                    }
                else:
                    return {
                        "match_id": match_id,
                        "in_cooldown": False,
                        "last_signal": cd.get("last_signal"),
                        "ok": True,
                    }
            return {"match_id": match_id, "in_cooldown": False, "ok": True}
    except Exception as exc:
        return {"match_id": match_id, "in_cooldown": False, "ok": False, "error": str(exc)}
