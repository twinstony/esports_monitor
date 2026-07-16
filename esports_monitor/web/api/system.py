"""系统状态 API。

端点：
- GET /api/system/status  系统状态（DB大小、各表记录数、配置概览、任务记录）
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request

router = APIRouter(tags=["system"])


def _get_deps(request: Request):
    return request.app.state.deps


@router.get("/system/status")
async def system_status(request: Request) -> Dict[str, Any]:
    """获取系统状态概览。"""
    deps = _get_deps(request)

    try:
        # 数据库状态
        db_size_mb = deps.storage.get_db_size_mb()
        table_counts = {}
        for table in ("matches", "price_snapshots", "orderbook_snapshots",
                       "morphology_signals", "morphology_trades"):
            table_counts[table] = deps.storage.count_rows(table)

        # 配置概览
        morph_cfg = deps.config.get("morphology") or {}
        sim_cfg = morph_cfg.get("simulation") or {}
        polling_cfg = deps.config.get("polling") or {}

        # 最近任务执行记录
        task_runs = {}
        for task_name in ("market_discovery", "data_archive", "status_report", "daily_trade_summary"):
            last_run = deps.storage.get_last_task_run(task_name)
            task_runs[task_name] = last_run

        return {
            "database": {
                "size_mb": round(db_size_mb, 2),
                "table_counts": table_counts,
            },
            "config": {
                "polling_interval_seconds": int(polling_cfg.get("interval_seconds", 30)),
                "idle_interval_seconds": int(polling_cfg.get("idle_interval_seconds", 300)),
                "morphology": {
                    "enabled": bool(morph_cfg.get("enabled", True)),
                    "cooldown_minutes": float(morph_cfg.get("cooldown_minutes", 15)),
                    "resample_points": int(morph_cfg.get("resample_points", 48)),
                    "window_hours": float(morph_cfg.get("window_hours", 2.0)),
                    "buy_price_min": float(morph_cfg.get("buy_price_min", 0.05)),
                    "buy_price_max": float(morph_cfg.get("buy_price_max", 0.95)),
                },
                "simulation": {
                    "enabled": bool(sim_cfg.get("enabled", True)),
                    "notional_usd": float(sim_cfg.get("notional_usd", 100)),
                    "use_depth": bool(sim_cfg.get("use_depth", True)),
                },
            },
            "task_runs": task_runs,
            "ok": True,
        }
    except Exception as exc:
        return {"error": str(exc), "ok": False}
