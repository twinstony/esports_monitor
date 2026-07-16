"""交易相关 API。

端点：
- GET /api/trades              交易列表（筛选 + 回溯天数）
- GET /api/trades/{trade_id}   交易详情（含信号依据）
- GET /api/trades/stats        交易汇总统计
- GET /api/trades/stats/grouped 分组统计
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Query

from ...utils.time_utils import now_utc, to_utc_iso

router = APIRouter(tags=["trades"])

# 默认数据回溯天数：使用 365 天确保用户能看到所有历史模拟开单
DEFAULT_DAYS = 365


def _get_deps(request: Request):
    return request.app.state.deps


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


@router.get("/trades")
async def list_trades(
    request: Request,
    settled: Optional[int] = Query(None, description="结算状态: 0=未结算, 1=已结算"),
    game: Optional[str] = Query(None, description="游戏筛选"),
    signal: Optional[str] = Query(None, description="信号名称筛选"),
    team: Optional[str] = Query(None, description="队伍名称模糊匹配（team_a / team_b / buy_team）"),
    days: int = Query(DEFAULT_DAYS, description="回溯天数"),
) -> Dict[str, Any]:
    """获取交易列表（含比赛和信号信息）。

    返回字段说明：
    - trades: 交易列表，每行包含 polymarket_url 字段供前端生成跳转链接
    - ok: True 表示接口成功；False 表示内部异常（error 字段给出原因）
    """
    deps = _get_deps(request)
    try:
        cutoff = now_utc() - timedelta(days=days)
        cutoff_iso = to_utc_iso(cutoff)

        # 构建查询
        sql_parts = [
            "SELECT "
            "  t.id AS id, t.match_id AS match_id, "
            "  t.signal_id AS signal_id, "
            "  m.game AS game, m.team_a AS team_a, m.team_b AS team_b, "
            "  m.slug AS slug, "
            "  t.buy_team AS buy_team, t.buy_price AS buy_price, "
            "  t.quantity AS quantity, t.notional_usd AS notional_usd, "
            "  t.vwap AS vwap, t.opened_at AS opened_at, "
            "  t.settled AS settled, t.pnl_usd AS pnl_usd, "
            "  t.settled_at AS settled_at, "
            "  s.signal_name AS signal_name, s.window_label AS window_label, "
            "  s.minutes_since_start AS minutes_since_start "
            "FROM morphology_trades t "
            "LEFT JOIN morphology_signals s ON t.signal_id = s.id "
            "LEFT JOIN matches m ON t.match_id = m.match_id "
            "WHERE t.opened_at >= ?"
        ]
        params: list = [cutoff_iso]

        if settled is not None:
            sql_parts.append("AND t.settled = ?")
            params.append(settled)
        if game:
            sql_parts.append("AND m.game = ?")
            params.append(game)
        if signal:
            sql_parts.append("AND s.signal_name = ?")
            params.append(signal)
        if team:
            kw = team.lower().strip()
            sql_parts.append(
                "AND (LOWER(m.team_a) LIKE ? OR LOWER(m.team_b) LIKE ? OR LOWER(t.buy_team) LIKE ?)"
            )
            params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])

        sql_parts.append("ORDER BY t.opened_at DESC")

        with deps.storage._connect(row_factory=True) as conn:
            cur = conn.execute(" ".join(sql_parts), params)
            trades = [dict(r) for r in cur.fetchall()]

        # 附加 Polymarket 链接
        for tr in trades:
            tr["polymarket_url"] = _build_polymarket_url(tr.get("game") or "", tr.get("slug") or "")

        return {"trades": trades, "total": len(trades), "ok": True}
    except Exception as exc:
        return {"trades": [], "total": 0, "ok": False, "error": str(exc)}


@router.get("/trades/stats")
async def trade_stats(
    request: Request,
    days: int = Query(DEFAULT_DAYS, description="回溯天数"),
) -> Dict[str, Any]:
    """获取交易汇总统计。"""
    deps = _get_deps(request)
    try:
        cutoff = now_utc() - timedelta(days=days)
        cutoff_iso = to_utc_iso(cutoff)

        with deps.storage._connect(row_factory=True) as conn:
            cur = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS settled,
                    SUM(CASE WHEN settled=1 AND pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN settled=1 AND pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(CASE WHEN settled=1 THEN pnl_usd ELSE 0 END), 0) AS total_pnl,
                    COALESCE(AVG(CASE WHEN settled=1 THEN pnl_usd END), 0) AS avg_pnl,
                    COALESCE(AVG(buy_price), 0) AS avg_buy_price,
                    COALESCE(AVG(quantity), 0) AS avg_quantity,
                    COALESCE(MAX(CASE WHEN settled=1 AND pnl_usd>0 THEN pnl_usd END), 0) AS max_win,
                    COALESCE(MIN(CASE WHEN settled=1 AND pnl_usd<=0 THEN pnl_usd END), 0) AS max_loss,
                    COALESCE(SUM(CASE WHEN settled=1 AND pnl_usd>0 THEN pnl_usd ELSE 0 END), 0) AS total_wins_pnl,
                    COALESCE(ABS(SUM(CASE WHEN settled=1 AND pnl_usd<=0 THEN pnl_usd ELSE 0 END)), 0) AS total_losses_pnl
                FROM morphology_trades
                WHERE opened_at >= ?
                """,
                (cutoff_iso,),
            )
            row = cur.fetchone()
            if not row:
                return _empty_stats()
            result = dict(row)

            # 计算平均持仓时长
            cur2 = conn.execute(
                """
                SELECT AVG(
                    (julianday(settled_at) - julianday(opened_at)) * 86400
                ) AS avg_hold_seconds
                FROM morphology_trades
                WHERE settled = 1 AND opened_at >= ? AND settled_at IS NOT NULL
                """,
                (cutoff_iso,),
            )
            hold_row = cur2.fetchone()
            result["avg_hold_time"] = (dict(hold_row).get("avg_hold_seconds") if hold_row else None) or 0

        # 计算衍生指标
        settled_count = result.get("settled") or 0
        wins = result.get("wins") or 0
        total_pnl = result.get("total_pnl") or 0.0
        total_losses_pnl = result.get("total_losses_pnl") or 0.0

        result["win_rate"] = (wins / settled_count * 100) if settled_count > 0 else 0.0
        result["profit_factor"] = (
            (result.get("total_wins_pnl") or 0.0) / total_losses_pnl
            if total_losses_pnl > 0 else 0.0
        )
        result["expectancy"] = total_pnl / settled_count if settled_count > 0 else 0.0

        # 清理 None 值
        for k, v in result.items():
            if v is None:
                result[k] = 0.0 if isinstance(v, float) else 0

        result["ok"] = True
        return result
    except Exception as exc:
        return _empty_stats(error=str(exc))


@router.get("/trades/stats/grouped")
async def trade_stats_grouped(
    request: Request,
    group_by: str = Query("signal_name", description="分组维度: signal_name/game/window_label"),
    days: int = Query(DEFAULT_DAYS, description="回溯天数"),
) -> Dict[str, Any]:
    """获取分组统计。"""
    deps = _get_deps(request)
    try:
        cutoff = now_utc() - timedelta(days=days)
        cutoff_iso = to_utc_iso(cutoff)

        # 根据 group_by 构建查询
        join_map = {
            "signal_name": (
                "LEFT JOIN morphology_signals s ON t.signal_id = s.id "
                "LEFT JOIN matches m ON t.match_id = m.match_id",
                "s.signal_name",
            ),
            "game": (
                "LEFT JOIN morphology_signals s ON t.signal_id = s.id "
                "LEFT JOIN matches m ON t.match_id = m.match_id",
                "m.game",
            ),
            "window_label": (
                "LEFT JOIN morphology_signals s ON t.signal_id = s.id "
                "LEFT JOIN matches m ON t.match_id = m.match_id",
                "s.window_label",
            ),
            "buy_team": (
                "LEFT JOIN morphology_signals s ON t.signal_id = s.id "
                "LEFT JOIN matches m ON t.match_id = m.match_id",
                "t.buy_team",
            ),
        }
        if group_by not in join_map:
            return {"groups": [], "ok": False, "error": f"invalid group_by: {group_by}"}

        joins, key_col = join_map[group_by]

        with deps.storage._connect(row_factory=True) as conn:
            cur = conn.execute(
                f"""
                SELECT
                    {key_col} AS group_key,
                    COUNT(*) AS total,
                    SUM(CASE WHEN t.settled=1 THEN 1 ELSE 0 END) AS settled,
                    SUM(CASE WHEN t.settled=1 AND t.pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN t.settled=1 AND t.pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(CASE WHEN t.settled=1 THEN t.pnl_usd ELSE 0 END), 0) AS total_pnl
                FROM morphology_trades t
                {joins}
                WHERE t.opened_at >= ?
                GROUP BY {key_col}
                """,
                (cutoff_iso,),
            )
            rows = [dict(r) for r in cur.fetchall()]

        # 计算每组胜率
        for row in rows:
            row["group_key"] = row.get("group_key") or "unknown"
            settled_count = row.get("settled") or 0
            wins = row.get("wins") or 0
            row["win_rate"] = (wins / settled_count * 100) if settled_count > 0 else 0.0
            for k in ("total", "settled", "wins", "losses"):
                if row.get(k) is None:
                    row[k] = 0
            if row.get("total_pnl") is None:
                row["total_pnl"] = 0.0

        return {"groups": rows, "group_by": group_by, "ok": True}
    except Exception as exc:
        return {"groups": [], "group_by": group_by, "ok": False, "error": str(exc)}


@router.get("/trades/{trade_id}")
async def get_trade(trade_id: int, request: Request) -> Dict[str, Any]:
    """获取交易详情（含信号依据）。"""
    deps = _get_deps(request)
    try:
        with deps.storage._connect(row_factory=True) as conn:
            cur = conn.execute(
                """
                SELECT
                    t.id AS id, t.match_id AS match_id,
                    m.game AS game, m.team_a AS team_a, m.team_b AS team_b,
                    m.slug AS slug,
                    t.buy_team AS buy_team, t.buy_price AS buy_price,
                    t.quantity AS quantity, t.notional_usd AS notional_usd,
                    t.vwap AS vwap, t.opened_at AS opened_at,
                    t.settled AS settled, t.pnl_usd AS pnl_usd,
                    t.settled_at AS settled_at, t.winning_team AS winning_team,
                    t.signal_id AS signal_id,
                    s.signal_name AS signal_name, s.signal_label AS signal_label,
                    s.window_label AS window_label,
                    s.buy_price AS signal_buy_price,
                    s.minutes_since_start AS minutes_since_start,
                    s.hours_before_end AS hours_before_end,
                    s.predicted_win_prob AS predicted_win_prob,
                    s.predicted_pnl AS predicted_pnl,
                    s.predicted_expectancy AS predicted_expectancy,
                    s.historical_trades AS historical_trades,
                    s.historical_profit_factor AS historical_profit_factor,
                    s.signal_strength AS signal_strength,
                    s.morph_features AS morph_features
                FROM morphology_trades t
                LEFT JOIN morphology_signals s ON t.signal_id = s.id
                LEFT JOIN matches m ON t.match_id = m.match_id
                WHERE t.id = ?
                """,
                (trade_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "trade not found", "ok": False}
            trade = dict(row)
            morph_features = _parse_json_object(trade.get("morph_features"))
            trade["morph_features"] = morph_features
            # 附加 Polymarket 链接
            trade["polymarket_url"] = _build_polymarket_url(trade.get("game") or "", trade.get("slug") or "")
            signal = {
                "id": trade.get("signal_id"),
                "signal_name": trade.get("signal_name"),
                "signal_label": trade.get("signal_label"),
                "window_label": trade.get("window_label"),
                "buy_team": trade.get("buy_team"),
                "buy_price": trade.get("signal_buy_price"),
                "minutes_since_start": trade.get("minutes_since_start"),
                "hours_before_end": trade.get("hours_before_end"),
                "predicted_win_prob": trade.get("predicted_win_prob"),
                "predicted_pnl": trade.get("predicted_pnl"),
                "predicted_expectancy": trade.get("predicted_expectancy"),
                "historical_trades": trade.get("historical_trades"),
                "historical_profit_factor": trade.get("historical_profit_factor"),
                "signal_strength": trade.get("signal_strength"),
                "morph_features": morph_features,
            }
            return {"trade": trade, "signal": signal, "ok": True}
    except Exception as exc:
        return {"error": str(exc), "ok": False}


def _parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value or not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _empty_stats(error: str = "") -> Dict[str, Any]:
    result = {
        "total": 0, "settled": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0, "avg_pnl": 0.0, "avg_buy_price": 0.0,
        "avg_quantity": 0.0, "max_win": 0.0, "max_loss": 0.0,
        "win_rate": 0.0, "profit_factor": 0.0, "expectancy": 0.0,
        "total_wins_pnl": 0.0, "total_losses_pnl": 0.0, "avg_hold_time": 0,
    }
    if error:
        result["error"] = error
        result["ok"] = False
    else:
        result["ok"] = True
    return result
