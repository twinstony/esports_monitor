"""pytest 共享 fixtures。"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from typing import Any, Dict

import pytest

from esports_monitor.data.sqlite_storage import SQLiteStorage
from esports_monitor.utils.time_utils import now_utc, to_utc_iso


@pytest.fixture
def tmp_db_path(tmp_path) -> str:
    """临时数据库文件路径。"""
    return str(tmp_path / "test.db")


@pytest.fixture
def storage(tmp_db_path) -> SQLiteStorage:
    """临时 SQLiteStorage 实例。"""
    return SQLiteStorage(db_path=tmp_db_path)


@pytest.fixture
def sample_match() -> Dict[str, Any]:
    """示例比赛元数据（时间相对于当前动态生成，确保正在进行中）。"""
    now = now_utc()
    return {
        "match_id": "0xabc123",
        "slug": "t1-vs-geng-lck-2026",
        "game": "lol",
        "league": "LCK",
        "team_a": "T1",
        "team_b": "Gen.G",
        "condition_id": "0xabc123",
        "token_id_a": "token_a_123",
        "token_id_b": "token_b_456",
        "start_time": to_utc_iso(now - timedelta(minutes=30)),
        "end_time": to_utc_iso(now + timedelta(hours=2)),
        "status": "discovered",
        "winning_team": None,
        "discovered_at": to_utc_iso(now - timedelta(hours=1)),
        "updated_at": to_utc_iso(now - timedelta(hours=1)),
    }


@pytest.fixture
def sample_config() -> Dict[str, Any]:
    """示例配置。"""
    return {
        "polymarket": {
            "gamma_api_base": "https://gamma-api.polymarket.com",
            "clob_api_base": "https://clob.polymarket.com",
            "timeout": 5,
        },
        "morphology": {
            "enabled": True,
            "cooldown_minutes": 15,
            "resample_points": 48,
            "window_hours": 2.0,
            "buy_price_min": 0.05,
            "buy_price_max": 0.95,
            "observe_all": True,
            "simulation": {
                "enabled": True,
                "notional_usd": 100,
                "use_depth": False,
            },
            "windows": [
                {"label": "early", "min_minutes": 0, "max_minutes": 30,
                 "rules": ["current_and_recent_leader", "stable_spread"]},
                {"label": "mid", "min_minutes": 30, "max_minutes": 90,
                 "rules": ["breakout", "momentum_catcher", "divergence"]},
                {"label": "late", "min_minutes": 90, "max_minutes": 9999,
                 "rules": ["stable_spread", "current_and_recent_leader"]},
            ],
        },
        "discovery": {
            "enabled": True,
            "games": ["cs2", "dota2", "lol"],
            "tag_slug": "esports",
            "min_depth": 50,
            "max_spread": 0.05,
            "price_min": 0.05,
            "price_max": 0.95,
            "interval_hours": 6,
        },
        "notification": {
            "telegram": {
                "enabled": False,
                "bot_token": "",
                "chat_id": "",
            }
        },
    }
