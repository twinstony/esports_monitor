"""信号相关 API。

端点：
- GET /api/signals        信号列表（30天限制）
- GET /api/signals/types  6种信号类型定义（含中英文标签）
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Query

from ...morphology.signals import SIGNAL_LABELS
from ...utils.time_utils import now_utc, to_utc_iso

router = APIRouter(tags=["signals"])

DEFAULT_DAYS = 30


def _get_deps(request: Request):
    return request.app.state.deps


SIGNAL_EXAMPLES: Dict[str, Dict[str, List[float]]] = {
    "current_and_recent_leader": {
        "team_a": [0.52, 0.54, 0.55, 0.57, 0.59, 0.61, 0.63],
        "team_b": [0.48, 0.47, 0.46, 0.45, 0.43, 0.41, 0.39],
    },
    "momentum_catcher": {
        "team_a": [0.34, 0.35, 0.36, 0.39, 0.43, 0.48, 0.52],
        "team_b": [0.66, 0.64, 0.62, 0.59, 0.56, 0.53, 0.50],
    },
    "breakout": {
        "team_a": [0.35, 0.34, 0.36, 0.42, 0.49, 0.57, 0.64],
        "team_b": [0.65, 0.66, 0.64, 0.58, 0.51, 0.43, 0.36],
    },
    "divergence": {
        "team_a": [0.49, 0.50, 0.52, 0.55, 0.59, 0.63, 0.67],
        "team_b": [0.51, 0.50, 0.48, 0.45, 0.41, 0.37, 0.33],
    },
    "acceleration": {
        "team_a": [0.50, 0.51, 0.52, 0.54, 0.58, 0.64, 0.72],
        "team_b": [0.50, 0.49, 0.48, 0.46, 0.42, 0.36, 0.28],
    },
    "stable_spread": {
        "team_a": [0.58, 0.59, 0.60, 0.61, 0.62, 0.63, 0.64],
        "team_b": [0.42, 0.41, 0.40, 0.39, 0.38, 0.37, 0.36],
    },
}


@router.get("/signals")
async def list_signals(
    request: Request,
    match_id: Optional[str] = Query(None, description="比赛ID筛选"),
    signal_name: Optional[str] = Query(None, description="信号名称筛选"),
    days: int = Query(DEFAULT_DAYS, description="回溯天数"),
) -> Dict[str, Any]:
    """获取信号列表。"""
    deps = _get_deps(request)
    try:
        cutoff = now_utc() - timedelta(days=days)
        cutoff_iso = to_utc_iso(cutoff)

        sql_parts = [
            """
            SELECT
                s.*,
                m.game AS game,
                m.team_a AS team_a,
                m.team_b AS team_b,
                m.league AS league
            FROM morphology_signals s
            LEFT JOIN matches m ON s.match_id = m.match_id
            WHERE s.detected_at >= ?
            """
        ]
        params: list = [cutoff_iso]

        if match_id:
            sql_parts.append("AND s.match_id = ?")
            params.append(match_id)
        if signal_name:
            sql_parts.append("AND s.signal_name = ?")
            params.append(signal_name)

        sql_parts.append("ORDER BY s.detected_at DESC")

        with deps.storage._connect(row_factory=True) as conn:
            cur = conn.execute(" ".join(sql_parts), params)
            signals = [dict(r) for r in cur.fetchall()]

        return {"signals": signals, "total": len(signals)}
    except Exception as exc:
        return {"signals": [], "total": 0, "error": str(exc)}


@router.get("/signals/{signal_id}/series")
async def signal_series(signal_id: int, request: Request) -> Dict[str, Any]:
    """获取某条信号对应比赛的价格序列（用于形态走势图）。"""
    deps = _get_deps(request)
    try:
        with deps.storage._connect(row_factory=True) as conn:
            cur = conn.execute(
                "SELECT match_id FROM morphology_signals WHERE id = ?",
                (signal_id,),
            )
            row = cur.fetchone()
        if not row:
            return {"signal_id": signal_id, "error": "signal not found"}
        match_id = dict(row).get("match_id") or ""
        match = deps.storage.get_match(match_id) or {}
        prices_a = deps.storage.get_price_snapshots(match_id, team="team_a")
        prices_b = deps.storage.get_price_snapshots(match_id, team="team_b")
        return {
            "signal_id": signal_id,
            "match_id": match_id,
            "team_a_name": match.get("team_a"),
            "team_b_name": match.get("team_b"),
            "team_a": [{"recorded_at": ts, "price": p} for ts, p in prices_a],
            "team_b": [{"recorded_at": ts, "price": p} for ts, p in prices_b],
        }
    except Exception as exc:
        return {"signal_id": signal_id, "error": str(exc)}


@router.get("/signals/types")
async def signal_types() -> Dict[str, Any]:
    """获取6种信号类型定义（含中英文标签）。"""
    types = []
    descriptions = {
        "current_and_recent_leader": {
            "zh": "当前价格更高，且最近12点均价也更高",
            "en": "Current price and recent 12-point average are both higher",
        },
        "momentum_catcher": {
            "zh": "当前落后，但最近12点涨幅明显大于对手，且自身为正",
            "en": "Currently trailing but recent 12-point gain significantly exceeds opponent",
        },
        "breakout": {
            "zh": "6点前落后，当前反超，且6点涨幅超过20%",
            "en": "Trailing 6 points ago, now leading with 6-point gain over 20%",
        },
        "divergence": {
            "zh": "一个队伍12点上涨超过5%，另一个12点下跌超过5%",
            "en": "One team up over 5% in 12 points, the other down over 5%",
        },
        "acceleration": {
            "zh": "当前领先且6点涨幅大于12点涨幅（加速），且12点涨幅为正",
            "en": "Currently leading with 6-point gain exceeding 12-point gain (acceleration)",
        },
        "stable_spread": {
            "zh": "当前领先，且24点收益也领先",
            "en": "Currently leading and also ahead in 24-point return",
        },
    }

    for name, label in SIGNAL_LABELS.items():
        desc = descriptions.get(name, {"zh": "", "en": ""})
        types.append({
            "name": name,
            "label_zh": label,
            "label_en": name.replace("_", " ").title(),
            "description_zh": desc["zh"],
            "description_en": desc["en"],
            "example": SIGNAL_EXAMPLES.get(name, {"team_a": [], "team_b": []}),
        })

    return {"types": types}
