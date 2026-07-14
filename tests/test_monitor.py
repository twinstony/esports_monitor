"""core/monitor.py 测试。"""
from __future__ import annotations

import gzip
import json
import os
from datetime import timedelta
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from esports_monitor.core.monitor import (
    ARCHIVE_TASK_NAME,
    Monitor,
    STATUS_REPORT_TASK_NAME,
)
from esports_monitor.utils.time_utils import now_utc, to_utc_iso


@pytest.fixture
def monitor(tmp_path, sample_config, monkeypatch):
    """构造一个临时 Monitor 实例（不启动主循环）。"""
    # 调整 db 路径到 tmp_path
    sample_config["database"] = {"path": str(tmp_path / "test.db")}
    sample_config["retention"] = {
        "days": 90,
        "archive_dir": str(tmp_path / "archives"),
        "archive_interval_days": 7,
        "vacuum_enabled": True,
        "compress": True,
    }
    sample_config["notification"]["telegram"]["enabled"] = False
    # 禁用网络调用
    monkeypatch.setattr(
        "esports_monitor.api.polymarket.PolymarketClient.fetch_events",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "esports_monitor.api.polymarket.PolymarketClient.fetch_event",
        lambda *a, **k: None,
    )
    return Monitor(config=sample_config, base_dir=str(tmp_path))


class TestInit:
    def test_init_components(self, monitor):
        assert monitor.storage is not None
        assert monitor.gamma_client is not None
        assert monitor.clob_client is not None
        assert monitor.morphology_detector is not None
        assert monitor.discovery_service is not None
        assert monitor.notifier is not None

    def test_init_creates_db(self, monitor, tmp_path):
        assert os.path.exists(str(tmp_path / "test.db"))


class TestReloadConfig:
    def test_no_change_no_reload(self, monitor):
        old_storage = monitor.storage
        monitor._reload_config_if_changed()
        assert monitor.storage is old_storage

    def test_change_triggers_reload(self, monitor, tmp_path, monkeypatch):
        # 修改 config.yaml 触发重载
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "polymarket:\n  timeout: 99\ndatabase:\n  path: " + str(tmp_path / "test.db") + "\n",
            encoding="utf-8",
        )
        monitor._reload_config_if_changed()
        # 应已重建组件
        assert monitor.config["polymarket"]["timeout"] == 99


class TestCheckAndRunDiscovery:
    def test_disabled_skipped(self, monitor):
        monitor.config["discovery"]["enabled"] = False
        monitor._check_and_run_discovery()
        # 不应抛异常

    def test_force_runs(self, monitor):
        with patch.object(monitor.discovery_service, "discover") as mock_disc:
            mock_disc.return_value = MagicMock(
                has_changes=False, new_matches=[], summary="ok"
            )
            monitor._check_and_run_discovery(force=True)
            mock_disc.assert_called_once()

    def test_with_new_matches_persists(self, monitor, sample_match):
        from esports_monitor.discovery.service import DiscoveredMatch, DiscoveryResult
        m = DiscoveredMatch(
            match_id="0xnew", slug="new", game="lol", league=None,
            team_a="A", team_b="B", condition_id="0xnew",
            token_id_a=None, token_id_b=None,
            price_a=0.5, price_b=0.5, start_time=None, end_time=None,
        )
        result = DiscoveryResult(new_matches=[m], summary="ok")
        with patch.object(monitor.discovery_service, "discover", return_value=result):
            monitor._check_and_run_discovery(force=True)
            got = monitor.storage.get_match("0xnew")
            assert got is not None
            assert got["team_a"] == "A"

    def test_exception_swallowed(self, monitor):
        with patch.object(
            monitor.discovery_service, "discover",
            side_effect=RuntimeError("boom"),
        ):
            # 不应抛异常
            monitor._check_and_run_discovery(force=True)


class TestUpdateLiveStatus:
    def test_discovered_to_live(self, monitor, sample_match):
        m = sample_match
        # start_time 已过
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            start_time=to_utc_iso(now_utc() - timedelta(minutes=30)),
            end_time=to_utc_iso(now_utc() + timedelta(minutes=30)),
            status="discovered",
        )
        matches = monitor.storage.get_live_matches()
        monitor._update_live_status(matches)
        got = monitor.storage.get_match(m["match_id"])
        assert got["status"] == "live"

    def test_live_to_ended(self, monitor, sample_match):
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            start_time=to_utc_iso(now_utc() - timedelta(hours=2)),
            end_time=to_utc_iso(now_utc() - timedelta(minutes=30)),
            status="live",
        )
        matches = monitor.storage.get_live_matches()
        monitor._update_live_status(matches)
        got = monitor.storage.get_match(m["match_id"])
        assert got["status"] == "ended"

    def test_empty_matches(self, monitor):
        monitor._update_live_status([])


class TestCheckSingleMatch:
    def test_no_event_returns(self, monitor, sample_match):
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        # fetch_event 返回 None
        with patch.object(monitor.gamma_client, "fetch_event", return_value=None):
            monitor._check_single_match(m)  # 不应抛异常
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[]):
            monitor._check_single_match(m)

    def test_with_market_writes_snapshots(self, monitor, sample_match):
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            start_time=to_utc_iso(now_utc() - timedelta(minutes=30)),
            end_time=to_utc_iso(now_utc() + timedelta(minutes=30)),
        )
        market = MatchMarket(
            condition_id=m["condition_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)):
            monitor._check_single_match(m)
        # 应有价格快照
        rows = monitor.storage.get_price_snapshots(m["match_id"])
        assert len(rows) == 2

    def test_upcoming_match_skips_data_collection(self, monitor, sample_match):
        """未开始的比赛应跳过数据采集（无价格快照）。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        # start_time 在未来
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            start_time=to_utc_iso(now_utc() + timedelta(hours=2)),
            end_time=to_utc_iso(now_utc() + timedelta(hours=5)),
            status="discovered",
        )
        market = MatchMarket(
            condition_id=m["condition_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)):
            ok, price_n, ob_n, alert_n, _details = monitor._check_single_match(
                monitor.storage.get_match(m["match_id"])
            )
        # ok=True 但不采集数据
        assert ok is True
        assert price_n == 0
        assert ob_n == 0
        assert alert_n == 0
        # 无价格快照
        rows = monitor.storage.get_price_snapshots(m["match_id"])
        assert len(rows) == 0

    def test_ended_match_skips_data_collection(self, monitor, sample_match):
        """已结束的比赛应跳过数据采集。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        # end_time 在过去
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            start_time=to_utc_iso(now_utc() - timedelta(hours=5)),
            end_time=to_utc_iso(now_utc() - timedelta(hours=2)),
            status="live",
        )
        market = MatchMarket(
            condition_id=m["condition_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)):
            ok, price_n, ob_n, alert_n, _details = monitor._check_single_match(
                monitor.storage.get_match(m["match_id"])
            )
        # ok=False，不采集数据
        assert ok is False
        assert price_n == 0

    def test_no_start_time_still_collects(self, monitor, sample_match):
        """无 start_time 的比赛仍应采集数据（无法判断是否开始）。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            # 不设 start_time/end_time
        )
        market = MatchMarket(
            condition_id=m["condition_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)):
            ok, price_n, ob_n, alert_n, _details = monitor._check_single_match(
                monitor.storage.get_match(m["match_id"])
            )
        assert ok is True
        assert price_n == 2  # 有价格快照


class TestSettleEndedMatches:
    def test_no_ended_matches(self, monitor):
        monitor._settle_ended_matches()

    def test_settle_with_winning_team(self, monitor, sample_match):
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            status="ended", winning_team="T1",
        )
        sig_id = monitor.storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="x",
            window_label="mid", buy_team="T1", buy_price=0.4,
            detected_at=to_utc_iso(now_utc()),
        )
        monitor.storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.4, quantity=250.0, notional_usd=100.0,
            opened_at=to_utc_iso(now_utc()),
        )
        monitor._settle_ended_matches()
        # 应被结算
        assert monitor.storage.get_open_trades(match_id=m["match_id"]) == []
        got = monitor.storage.get_match(m["match_id"])
        assert got["status"] == "settled"


class TestArchive:
    def test_get_months_before(self):
        from datetime import datetime, timezone
        dt = datetime(2026, 6, 15, tzinfo=timezone.utc)
        months = Monitor._get_months_before(dt)
        assert len(months) == 12
        assert "2026-06" in months
        assert "2025-07" in months  # 12 个月前

    def test_archive_table_month_creates_file(self, monitor, sample_match, tmp_path):
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        # 插入 2020-06 的旧数据
        old_time = "2020-06-15T10:00:00+00:00"
        monitor.storage.insert_price_snapshot(m["match_id"], "team_a", 0.5, 0.5, old_time)
        archive_dir = str(tmp_path / "archives")
        os.makedirs(archive_dir, exist_ok=True)
        count = monitor._archive_table_month("price_snapshots", "2020-06", archive_dir, compress=True)
        assert count == 1
        # 文件应存在
        filepath = os.path.join(archive_dir, "price_snapshots_202006.json.gz")
        assert os.path.exists(filepath)
        # 验证文件内容
        with gzip.open(filepath, "rt", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["price"] == 0.5

    def test_archive_table_month_idempotent(self, monitor, tmp_path):
        archive_dir = str(tmp_path / "archives")
        os.makedirs(archive_dir, exist_ok=True)
        filepath = os.path.join(archive_dir, "price_snapshots_202006.json.gz")
        # 预创建文件
        with gzip.open(filepath, "wt", encoding="utf-8") as f:
            f.write("")
        count = monitor._archive_table_month("price_snapshots", "2020-06", archive_dir, compress=True)
        assert count == 0  # 已存在则跳过

    def test_archive_table_month_no_data(self, monitor, tmp_path):
        archive_dir = str(tmp_path / "archives")
        os.makedirs(archive_dir, exist_ok=True)
        count = monitor._archive_table_month("price_snapshots", "2099-01", archive_dir, compress=True)
        assert count == 0

    def test_run_archive(self, monitor, sample_match):
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        # 插入 200 天前的旧数据
        old_time = to_utc_iso(now_utc() - timedelta(days=200))
        monitor.storage.insert_price_snapshot(m["match_id"], "team_a", 0.5, 0.5, old_time)
        monitor._run_archive()
        # 应有归档任务记录
        assert monitor.storage.get_last_task_run(ARCHIVE_TASK_NAME) is not None
        # 旧数据应被删除
        rows = monitor.storage.get_price_snapshots(m["match_id"])
        assert len(rows) == 0

    def test_check_and_run_archive_skipped_recent(self, monitor):
        # 模拟最近运行过
        monitor.storage.mark_task_run(ARCHIVE_TASK_NAME, "recent")
        with patch.object(monitor, "_run_archive") as mock_run:
            monitor._check_and_run_archive()
            mock_run.assert_not_called()

    def test_check_and_run_archive_runs_when_due(self, monitor):
        # 写入一个旧的归档记录
        old = to_utc_iso(now_utc() - timedelta(days=30))
        with monitor.storage._connect() as conn:
            conn.execute(
                "INSERT INTO task_runs (task_name, run_at, result_summary) VALUES (?, ?, ?)",
                (ARCHIVE_TASK_NAME, old, "old"),
            )
            conn.commit()
        with patch.object(monitor, "_run_archive") as mock_run:
            monitor._check_and_run_archive()
            mock_run.assert_called_once()


class TestStatusReport:
    def test_disabled_skipped(self, monitor):
        monitor.config["notification"]["telegram"]["status_report_enabled"] = False
        monitor._check_and_send_status_report()

    def test_interval_not_elapsed(self, monitor):
        from datetime import datetime, timezone
        monitor._last_status_report_at = now_utc() - timedelta(minutes=10)
        monitor.config["notification"]["telegram"]["status_report_interval_hours"] = 6
        with patch.object(monitor, "_send_status_report") as mock_send:
            monitor._check_and_send_status_report()
            mock_send.assert_not_called()

    def test_interval_elapsed_sends(self, monitor):
        monitor._last_status_report_at = now_utc() - timedelta(hours=7)
        monitor.config["notification"]["telegram"]["status_report_interval_hours"] = 6
        with patch.object(monitor, "_send_status_report") as mock_send:
            monitor._check_and_send_status_report()
            mock_send.assert_called_once()
        # 应更新 _last_status_report_at
        assert monitor._last_status_report_at is not None

    def test_first_run_sends(self, monitor):
        monitor._last_status_report_at = None
        with patch.object(monitor, "_send_status_report") as mock_send:
            monitor._check_and_send_status_report()
            mock_send.assert_called_once()


class TestStop:
    def test_stop(self, monitor):
        monitor._running = True
        monitor.stop()
        assert monitor._running is False


class TestRunContinuous:
    def test_single_iteration_with_stop(self, monitor, monkeypatch):
        """主循环应在 stop() 后退出。"""
        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                monitor._running = False

        monkeypatch.setattr("esports_monitor.core.monitor.time.sleep", fake_sleep)
        # 避免真实 discovery 调用
        with patch.object(monitor, "_check_and_run_discovery"), \
             patch.object(monitor, "_reload_config_if_changed"), \
             patch.object(monitor, "_update_live_status"), \
             patch.object(monitor, "_check_single_match"), \
             patch.object(monitor, "_settle_ended_matches"), \
             patch.object(monitor, "_check_and_run_archive"), \
             patch.object(monitor, "_check_and_send_status_report"):
            monitor.storage.get_live_matches = MagicMock(return_value=[])
            monitor.run_continuous()
        # 不应抛异常，且至少跑过一次
        assert call_count["n"] >= 1

    def test_main_loop_exception_swallowed(self, monitor, monkeypatch):
        """主循环内异常应被吞并，继续下一轮。"""
        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                monitor._running = False

        monkeypatch.setattr("esports_monitor.core.monitor.time.sleep", fake_sleep)
        # _reload_config 抛异常
        with patch.object(
            monitor, "_reload_config_if_changed",
            side_effect=RuntimeError("boom"),
        ):
            monitor.storage.get_live_matches = MagicMock(return_value=[])
            monitor.run_continuous()
        # 不应抛异常
        assert call_count["n"] >= 1


class TestFetchWinningTeam:
    def test_no_slug_returns_none(self, monitor):
        assert monitor._fetch_winning_team({"slug": ""}) is None

    def test_no_event_returns_none(self, monitor, sample_match):
        m = sample_match
        with patch.object(monitor.gamma_client, "fetch_event", return_value=None):
            assert monitor._fetch_winning_team(m) is None

    def test_yes_price_high_returns_team_a(self, monitor, sample_match):
        m = sample_match
        event = {
            "markets": [{
                "conditionId": m["condition_id"],
                "outcomePrices": ["0.99", "0.01"],
            }]
        }
        with patch.object(monitor.gamma_client, "fetch_event", return_value=event):
            assert monitor._fetch_winning_team(m) == "T1"

    def test_yes_price_low_returns_team_b(self, monitor, sample_match):
        m = sample_match
        event = {
            "markets": [{
                "conditionId": m["condition_id"],
                "outcomePrices": ["0.01", "0.99"],
            }]
        }
        with patch.object(monitor.gamma_client, "fetch_event", return_value=event):
            assert monitor._fetch_winning_team(m) == "Gen.G"

    def test_prices_as_string(self, monitor, sample_match):
        m = sample_match
        event = {
            "markets": [{
                "conditionId": m["condition_id"],
                "outcomePrices": '["0.99", "0.01"]',
            }]
        }
        with patch.object(monitor.gamma_client, "fetch_event", return_value=event):
            assert monitor._fetch_winning_team(m) == "T1"

    def test_prices_invalid_string_skipped(self, monitor, sample_match):
        m = sample_match
        event = {
            "markets": [{
                "conditionId": m["condition_id"],
                "outcomePrices": "not-json",
            }]
        }
        with patch.object(monitor.gamma_client, "fetch_event", return_value=event):
            assert monitor._fetch_winning_team(m) is None

    def test_prices_wrong_length_skipped(self, monitor, sample_match):
        m = sample_match
        event = {
            "markets": [{
                "conditionId": m["condition_id"],
                "outcomePrices": ["0.5"],
            }]
        }
        with patch.object(monitor.gamma_client, "fetch_event", return_value=event):
            assert monitor._fetch_winning_team(m) is None

    def test_prices_non_numeric_skipped(self, monitor, sample_match):
        m = sample_match
        event = {
            "markets": [{
                "conditionId": m["condition_id"],
                "outcomePrices": ["abc", "def"],
            }]
        }
        with patch.object(monitor.gamma_client, "fetch_event", return_value=event):
            assert monitor._fetch_winning_team(m) is None

    def test_no_matching_condition_id(self, monitor, sample_match):
        m = sample_match
        event = {
            "markets": [{
                "conditionId": "different_cid",
                "outcomePrices": ["0.99", "0.01"],
            }]
        }
        with patch.object(monitor.gamma_client, "fetch_event", return_value=event):
            assert monitor._fetch_winning_team(m) is None

    def test_exception_returns_none(self, monitor, sample_match):
        m = sample_match
        with patch.object(
            monitor.gamma_client, "fetch_event",
            side_effect=RuntimeError("net"),
        ):
            assert monitor._fetch_winning_team(m) is None


class TestCheckSingleMatchAdvanced:
    def test_with_morphology_signal_triggers_alert(
        self, monitor, sample_match
    ):
        """形态检测出 strongest_signal 时应发送告警。"""
        from esports_monitor.api.polymarket import MatchMarket
        from esports_monitor.morphology.models import MorphologyAlert, MorphologySignal
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        fake_alert = MorphologyAlert(
            match_id=m["match_id"], status="normal",
            strongest_signal=MorphologySignal(
                signal_name="breakout", signal_label="突破", window_label="mid",
                direction="A", buy_team="T1", buy_price=0.45,
            ),
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)), \
             patch.object(monitor.morphology_detector, "detect", return_value=fake_alert), \
             patch.object(monitor.notifier, "send_signal_alert") as mock_alert:
            monitor._check_single_match(m)
            mock_alert.assert_called_once_with(fake_alert)

    def test_morphology_disabled_skips_detection(self, monitor, sample_match):
        """morphology.enabled=False 时不调用 detector。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        monitor.config["morphology"]["enabled"] = False
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)), \
             patch.object(monitor.morphology_detector, "detect") as mock_detect:
            monitor._check_single_match(m)
            mock_detect.assert_not_called()

    def test_alert_no_strongest_signal_no_notification(self, monitor, sample_match):
        """alert.status=normal 但无 strongest_signal 时不发告警。"""
        from esports_monitor.api.polymarket import MatchMarket
        from esports_monitor.morphology.models import MorphologyAlert
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        fake_alert = MorphologyAlert(match_id=m["match_id"], status="normal")
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)), \
             patch.object(monitor.morphology_detector, "detect", return_value=fake_alert), \
             patch.object(monitor.notifier, "send_signal_alert") as mock_alert:
            monitor._check_single_match(m)
            mock_alert.assert_not_called()

    def test_exception_swallowed(self, monitor, sample_match):
        """_check_single_match 异常应被吞。"""
        m = sample_match
        with patch.object(
            monitor.gamma_client, "fetch_event",
            side_effect=RuntimeError("boom"),
        ):
            monitor._check_single_match(m)  # 不应抛异常


class TestSettleEndedMatchesAdvanced:
    def test_no_open_trades_skipped(self, monitor, sample_match):
        """ended 比赛但无未结算交易时跳过。"""
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            status="ended", winning_team="T1",
        )
        # 无交易
        with patch.object(monitor, "_fetch_winning_team") as mock_fetch:
            monitor._settle_ended_matches()
            mock_fetch.assert_not_called()

    def test_no_ended_match_for_open_trade(self, monitor, sample_match):
        """有未结算交易但无 ended 比赛，不结算。"""
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            status="live",  # 非 ended
        )
        sig_id = monitor.storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="x",
            window_label="mid", buy_team="T1", buy_price=0.4,
            detected_at=to_utc_iso(now_utc()),
        )
        monitor.storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.4, quantity=250.0, notional_usd=100.0,
            opened_at=to_utc_iso(now_utc()),
        )
        monitor._settle_ended_matches()
        # 仍有未结算交易
        assert monitor.storage.get_open_trades(match_id=m["match_id"]) != []

    def test_winning_team_from_api(self, monitor, sample_match):
        """matches.winning_team=None 时从 API 获取。"""
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            status="ended", winning_team=None,  # 缺失，需从 API 获取
        )
        sig_id = monitor.storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="x",
            window_label="mid", buy_team="T1", buy_price=0.4,
            detected_at=to_utc_iso(now_utc()),
        )
        monitor.storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.4, quantity=250.0, notional_usd=100.0,
            opened_at=to_utc_iso(now_utc()),
        )
        with patch.object(monitor, "_fetch_winning_team", return_value="T1"):
            monitor._settle_ended_matches()
        # 应已结算
        got = monitor.storage.get_match(m["match_id"])
        assert got["winning_team"] == "T1"
        assert got["status"] == "settled"

    def test_winning_team_unavailable_skipped(self, monitor, sample_match):
        """API 也无法获取 winning_team 时跳过结算。"""
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            status="ended", winning_team=None,
        )
        sig_id = monitor.storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="x",
            window_label="mid", buy_team="T1", buy_price=0.4,
            detected_at=to_utc_iso(now_utc()),
        )
        monitor.storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.4, quantity=250.0, notional_usd=100.0,
            opened_at=to_utc_iso(now_utc()),
        )
        with patch.object(monitor, "_fetch_winning_team", return_value=None):
            monitor._settle_ended_matches()
        # 仍应未结算
        assert monitor.storage.get_open_trades(match_id=m["match_id"]) != []
        # status 仍为 ended
        got = monitor.storage.get_match(m["match_id"])
        assert got["status"] == "ended"


class TestSendStatusReport:
    def test_send_status_report_calls_notifier(self, monitor, sample_match):
        """_send_status_report 应调用 notifier.send_status_report。"""
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        with patch.object(monitor.notifier, "send_status_report") as mock_send:
            monitor._send_status_report()
            mock_send.assert_called_once()
            # 验证参数含 live_count 等
            kwargs = mock_send.call_args.kwargs
            assert "live_count" in kwargs
            assert "game_breakdown" in kwargs
            assert "db_size_mb" in kwargs

    def test_check_and_send_status_report_exception_swallowed(self, monitor):
        """_send_status_report 抛异常时应被吞。"""
        with patch.object(
            monitor, "_send_status_report",
            side_effect=RuntimeError("net"),
        ):
            monitor._last_status_report_at = None  # 触发首次运行
            monitor._check_and_send_status_report()  # 不应抛异常


class TestCheckAndRunArchiveAdvanced:
    def test_no_retention_config_skipped(self, monitor):
        """无 retention 配置时应跳过。"""
        monitor.config["retention"] = {}
        with patch.object(monitor, "_run_archive") as mock_run:
            monitor._check_and_run_archive()
            mock_run.assert_not_called()

    def test_run_archive_exception_swallowed(self, monitor):
        """_run_archive 抛异常时应被吞。"""
        monitor.config["retention"] = {"days": 1, "archive_interval_days": 999}
        # 写入旧 task_run 让其触发
        old = to_utc_iso(now_utc() - timedelta(days=30))
        with monitor.storage._connect() as conn:
            conn.execute(
                "INSERT INTO task_runs (task_name, run_at, result_summary) VALUES (?, ?, ?)",
                (ARCHIVE_TASK_NAME, old, "old"),
            )
            conn.commit()
        with patch.object(monitor, "_run_archive", side_effect=RuntimeError("boom")):
            monitor._check_and_run_archive()  # 不应抛异常

    def test_check_task_dedup_exception_swallowed(self, monitor):
        """get_last_task_run 抛异常时应被吞。"""
        monitor.config["retention"] = {"days": 90, "archive_interval_days": 7}
        with patch.object(
            monitor.storage, "get_last_task_run",
            side_effect=RuntimeError("db"),
        ):
            with patch.object(monitor, "_run_archive") as mock_run:
                monitor._check_and_run_archive()
                mock_run.assert_called_once()


class TestRunArchiveAdvanced:
    def test_vacuum_skipped_when_no_deletion(self, monitor, tmp_path):
        """deleted_total=0 时不执行 VACUUM。"""
        monitor.config["retention"] = {
            "days": 90,
            "archive_dir": str(tmp_path / "archives"),
            "vacuum_enabled": True,
            "compress": True,
            "archive_interval_days": 999,
        }
        with patch.object(monitor.storage, "vacuum") as mock_vacuum:
            monitor._run_archive()
            mock_vacuum.assert_not_called()
        # 仍应记录 task_run
        assert monitor.storage.get_last_task_run(ARCHIVE_TASK_NAME) is not None

    def test_archive_uncompressed(self, monitor, sample_match, tmp_path):
        """compress=False 时生成 .json 文件。"""
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        old_time = "2020-06-15T10:00:00+00:00"
        monitor.storage.insert_price_snapshot(m["match_id"], "team_a", 0.5, 0.5, old_time)
        archive_dir = str(tmp_path / "archives_no_compress")
        os.makedirs(archive_dir, exist_ok=True)
        # 直接调用 _archive_table_month 测试 compress=False 路径
        count = monitor._archive_table_month(
            "price_snapshots", "2020-06", archive_dir, compress=False
        )
        assert count == 1
        files = [f for f in os.listdir(archive_dir) if f.endswith(".json")]
        assert len(files) >= 1
        # 内容应是普通 JSON Lines
        filepath = os.path.join(archive_dir, "price_snapshots_202006.json")
        assert os.path.exists(filepath)
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["price"] == 0.5

    def test_archive_table_month_write_failure(self, monitor, tmp_path):
        """_archive_table_month 写入失败时返回 0。"""
        archive_dir = str(tmp_path / "archives_fail")
        os.makedirs(archive_dir, exist_ok=True)
        # 插入数据让 query 返回非空
        monitor.storage.upsert_match(
            match_id="m_fail", slug="s", game="lol",
            team_a="A", team_b="B", condition_id="c",
        )
        monitor.storage.insert_price_snapshot(
            "m_fail", "team_a", 0.5, 0.5, "2020-06-15T10:00:00+00:00"
        )
        # 让 gzip.open 抛异常
        with patch(
            "esports_monitor.core.monitor.gzip.open",
            side_effect=OSError("disk full"),
        ):
            count = monitor._archive_table_month(
                "price_snapshots", "2020-06", archive_dir, compress=True
            )
        assert count == 0


# ----------------------------------------------------------------------
# 新增通知接入测试
# ----------------------------------------------------------------------


class TestHeartbeatIntegration:
    def test_heartbeat_sent_after_round(self, monitor, monkeypatch):
        """主循环结束后应发送心跳。"""
        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                monitor._running = False

        monkeypatch.setattr("esports_monitor.core.monitor.time.sleep", fake_sleep)
        with patch.object(monitor, "_reload_config_if_changed"), \
             patch.object(monitor, "_check_and_run_discovery"), \
             patch.object(monitor, "_update_live_status"), \
             patch.object(monitor, "_check_single_match", return_value=(True, 2, 1, 0, None)), \
             patch.object(monitor, "_settle_ended_matches"), \
             patch.object(monitor, "_check_and_run_archive"), \
             patch.object(monitor, "_check_and_send_status_report"), \
             patch.object(monitor.notifier, "send_heartbeat", return_value=True) as mock_hb:
            monitor.storage.get_live_matches = MagicMock(return_value=[{"match_id": "m1"}])
            monitor.run_continuous()
            mock_hb.assert_called_once()
            # 验证心跳参数
            kwargs = mock_hb.call_args.kwargs
            assert kwargs["processed_matches"] == 1
            assert kwargs["price_snapshots_saved"] == 2
            assert kwargs["orderbook_snapshots_saved"] == 1
            assert kwargs["alerts_sent"] == 0

    def test_heartbeat_disabled(self, monitor, monkeypatch):
        """heartbeat_enabled=False 时不发送。"""
        monitor.config["notification"]["telegram"]["heartbeat_enabled"] = False
        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                monitor._running = False

        monkeypatch.setattr("esports_monitor.core.monitor.time.sleep", fake_sleep)
        with patch.object(monitor, "_reload_config_if_changed"), \
             patch.object(monitor, "_check_and_run_discovery"), \
             patch.object(monitor, "_update_live_status"), \
             patch.object(monitor, "_check_single_match", return_value=(True, 0, 0, 0, None)), \
             patch.object(monitor, "_settle_ended_matches"), \
             patch.object(monitor, "_check_and_run_archive"), \
             patch.object(monitor, "_check_and_send_status_report"), \
             patch.object(monitor.notifier, "send_heartbeat") as mock_hb:
            monitor.storage.get_live_matches = MagicMock(return_value=[])
            monitor.run_continuous()
            mock_hb.assert_not_called()

    def test_heartbeat_exception_swallowed(self, monitor, monkeypatch):
        """心跳发送异常应被吞。"""
        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                monitor._running = False

        monkeypatch.setattr("esports_monitor.core.monitor.time.sleep", fake_sleep)
        with patch.object(monitor, "_reload_config_if_changed"), \
             patch.object(monitor, "_check_and_run_discovery"), \
             patch.object(monitor, "_update_live_status"), \
             patch.object(monitor, "_check_single_match", return_value=(True, 0, 0, 0, None)), \
             patch.object(monitor, "_settle_ended_matches"), \
             patch.object(monitor, "_check_and_run_archive"), \
             patch.object(monitor, "_check_and_send_status_report"), \
             patch.object(
                 monitor.notifier, "send_heartbeat",
                 side_effect=RuntimeError("net"),
             ):
            monitor.storage.get_live_matches = MagicMock(return_value=[])
            monitor.run_continuous()  # 不应抛异常


class TestErrorAlertIntegration:
    def test_main_loop_exception_sends_error_alert(self, monitor, monkeypatch):
        """主循环异常时应主动推送错误告警。"""
        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                monitor._running = False

        monkeypatch.setattr("esports_monitor.core.monitor.time.sleep", fake_sleep)
        with patch.object(
            monitor, "_reload_config_if_changed",
            side_effect=RuntimeError("boom"),
        ), patch.object(monitor.notifier, "send_error_alert") as mock_err:
            monitor.storage.get_live_matches = MagicMock(return_value=[])
            monitor.run_continuous()
            mock_err.assert_called_once()
            msg = mock_err.call_args[0][0]
            assert "boom" in msg


class TestDiscoveryReportIntegration:
    def test_discovery_report_sent(self, monitor, sample_match):
        """市场发现执行后应发送发现报告。"""
        from esports_monitor.discovery.service import DiscoveredMatch, DiscoveryResult
        m = DiscoveredMatch(
            match_id="0xnew", slug="new", game="lol", league=None,
            team_a="T1", team_b="Gen.G", condition_id="0xnew",
            token_id_a=None, token_id_b=None,
            price_a=0.55, price_b=0.45, start_time=None, end_time=None,
        )
        result = DiscoveryResult(
            new_matches=[m], skipped=2, errors=0, summary="ok",
        )
        with patch.object(monitor.discovery_service, "discover", return_value=result), \
             patch.object(monitor.notifier, "send_discovery_report") as mock_report:
            monitor._check_and_run_discovery(force=True)
            mock_report.assert_called_once()
            kwargs = mock_report.call_args.kwargs
            assert kwargs["new_count"] == 1
            assert kwargs["skipped_count"] == 2
            assert kwargs["game_breakdown"].get("lol") == 1

    def test_discovery_report_disabled(self, monitor, sample_match):
        """discovery_report_enabled=False 时不发送。"""
        monitor.config["notification"]["telegram"]["discovery_report_enabled"] = False
        from esports_monitor.discovery.service import DiscoveredMatch, DiscoveryResult
        m = DiscoveredMatch(
            match_id="0xnew2", slug="new2", game="lol", league=None,
            team_a="A", team_b="B", condition_id="0xnew2",
            token_id_a=None, token_id_b=None,
            price_a=0.5, price_b=0.5, start_time=None, end_time=None,
        )
        result = DiscoveryResult(new_matches=[m], summary="ok")
        with patch.object(monitor.discovery_service, "discover", return_value=result), \
             patch.object(monitor.notifier, "send_discovery_report") as mock_report:
            monitor._check_and_run_discovery(force=True)
            mock_report.assert_not_called()

    def test_discovery_exception_sends_error_alert(self, monitor):
        """发现异常时应推送错误告警。"""
        with patch.object(
            monitor.discovery_service, "discover",
            side_effect=RuntimeError("net"),
        ), patch.object(monitor.notifier, "send_error_alert") as mock_err:
            monitor._check_and_run_discovery(force=True)
            mock_err.assert_called_once()


class TestMorphologySkippedIntegration:
    def test_skipped_notify_on_no_data(self, monitor, sample_match):
        """形态检测返回 no_data 时应发送跳过通知。"""
        from esports_monitor.api.polymarket import MatchMarket
        from esports_monitor.morphology.models import MorphologyAlert
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        fake_alert = MorphologyAlert(
            match_id=m["match_id"], status="no_data",
            status_message="价格数据不足",
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)), \
             patch.object(monitor.morphology_detector, "detect", return_value=fake_alert), \
             patch.object(monitor.notifier, "send_morphology_skipped") as mock_skip:
            monitor._check_single_match(m)
            mock_skip.assert_called_once()
            kwargs = mock_skip.call_args.kwargs
            assert kwargs["reason"] == "no_data"
            assert "价格数据不足" in kwargs["detail"]

    def test_skipped_notify_disabled_when_config_off(self, monitor, sample_match):
        """skipped_notify_enabled=False 时不发送。"""
        monitor.config["notification"]["telegram"]["skipped_notify_enabled"] = False
        from esports_monitor.api.polymarket import MatchMarket
        from esports_monitor.morphology.models import MorphologyAlert
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        fake_alert = MorphologyAlert(
            match_id=m["match_id"], status="no_data",
            status_message="价格数据不足",
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)), \
             patch.object(monitor.morphology_detector, "detect", return_value=fake_alert), \
             patch.object(monitor.notifier, "send_morphology_skipped") as mock_skip:
            monitor._check_single_match(m)
            mock_skip.assert_not_called()

    def test_morphology_disabled_sends_skip(self, monitor, sample_match):
        """morphology.enabled=False 时发送 disabled 跳过通知。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        monitor.config["morphology"]["enabled"] = False
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)), \
             patch.object(monitor.notifier, "send_morphology_skipped") as mock_skip:
            monitor._check_single_match(m)
            mock_skip.assert_called_once()
            kwargs = mock_skip.call_args.kwargs
            assert kwargs["reason"] == "disabled"


class TestArchiveReportIntegration:
    def test_archive_report_sent(self, monitor, sample_match, tmp_path):
        """归档执行后应发送归档报告。"""
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        # 插入过期数据
        monitor.storage.insert_price_snapshot(
            m["match_id"], "team_a", 0.5, 0.5, "2020-06-15T10:00:00+00:00"
        )
        archive_dir = str(tmp_path / "archives_report")
        monitor.config["retention"] = {
            "days": 1,
            "archive_dir": archive_dir,
            "vacuum_enabled": True,
            "compress": True,
            "archive_interval_days": 999,
        }
        with patch.object(monitor.notifier, "send_archive_report") as mock_report:
            monitor._run_archive()
            mock_report.assert_called_once()
            kwargs = mock_report.call_args.kwargs
            assert kwargs["archived_months"] >= 1
            assert kwargs["deleted_rows"] >= 1
            assert kwargs["db_size_before_mb"] > 0

    def test_archive_report_disabled(self, monitor, sample_match, tmp_path):
        """archive_report_enabled=False 时不发送。"""
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        monitor.storage.insert_price_snapshot(
            m["match_id"], "team_a", 0.5, 0.5, "2020-06-15T10:00:00+00:00"
        )
        archive_dir = str(tmp_path / "archives_disabled")
        monitor.config["retention"] = {
            "days": 1,
            "archive_dir": archive_dir,
            "vacuum_enabled": False,
            "compress": True,
            "archive_interval_days": 999,
        }
        monitor.config["notification"]["telegram"]["archive_report_enabled"] = False
        with patch.object(monitor.notifier, "send_archive_report") as mock_report:
            monitor._run_archive()
            mock_report.assert_not_called()


class TestCheckSingleMatchReturnTuple:
    def test_returns_four_tuple(self, monitor, sample_match):
        """_check_single_match 应返回 (ok, price_n, ob_n, alert_n) 四元组。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(monitor.clob_client, "get_token_id_and_orderbook", return_value=(None, None)):
            result = monitor._check_single_match(m)
            assert isinstance(result, tuple)
            assert len(result) == 5
            ok, price_n, ob_n, alert_n, _details = result
            assert ok is True
            assert price_n == 2  # 两条价格快照
            assert ob_n == 0  # 无盘口
            assert alert_n == 0

    def test_fetch_event_failure_returns_false(self, monitor, sample_match):
        """fetch_event 返回 None 时 ok=False。"""
        m = sample_match
        with patch.object(monitor.gamma_client, "fetch_event", return_value=None):
            result = monitor._check_single_match(m)
            assert result == (False, 0, 0, 0, None)

    def test_exception_returns_false_with_partial_counts(self, monitor, sample_match):
        """中途异常应返回部分统计。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        monitor.storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        # 让 _fetch_and_save_orderbook 抛异常
        with patch.object(monitor.gamma_client, "fetch_event", return_value={"slug": m["slug"]}), \
             patch.object(monitor.gamma_client, "parse_match_markets", return_value=[market]), \
             patch.object(
                 monitor, "_fetch_and_save_orderbook",
                 side_effect=RuntimeError("boom"),
             ):
            result = monitor._check_single_match(m)
            # 异常被吞，返回 False
            assert result[0] is False
            # 但 price_n 已写入
            assert result[1] == 2


class TestFetchAndSaveOrderbook:
    def test_returns_saved_count(self, monitor, sample_match):
        """应返回成功写入的盘口数。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        # 两次都成功
        with patch.object(
            monitor.clob_client, "get_token_id_and_orderbook",
            return_value=("token", {"bids": [], "asks": []}),
        ), patch.object(monitor.clob_client, "save_orderbook_snapshot", return_value=True):
            count = monitor._fetch_and_save_orderbook(m, market, "2026-06-29T00:00:00Z")
            assert count == 2

    def test_partial_failure(self, monitor, sample_match):
        """部分失败时只统计成功的。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        # 第一次成功，第二次返回 None
        with patch.object(
            monitor.clob_client, "get_token_id_and_orderbook",
            side_effect=[("token", {"bids": []}), (None, None)],
        ), patch.object(monitor.clob_client, "save_orderbook_snapshot", return_value=True):
            count = monitor._fetch_and_save_orderbook(m, market, "2026-06-29T00:00:00Z")
            assert count == 1

    def test_exception_returns_zero(self, monitor, sample_match):
        """异常时返回 0。"""
        from esports_monitor.api.polymarket import MatchMarket
        m = sample_match
        market = MatchMarket(
            condition_id=m["match_id"], clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        with patch.object(
            monitor.clob_client, "get_token_id_and_orderbook",
            side_effect=RuntimeError("net"),
        ):
            count = monitor._fetch_and_save_orderbook(m, market, "2026-06-29T00:00:00Z")
            assert count == 0
