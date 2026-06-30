"""notification/telegram.py 测试。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from esports_monitor.morphology.models import MorphologyAlert, MorphologySignal, MorphologyTrade
from esports_monitor.notification.telegram import (
    TelegramNotifier,
    create_notifier_from_config,
)


class TestConfiguration:
    def test_is_configured_false_when_empty(self):
        n = TelegramNotifier(bot_token="", chat_id="")
        assert n.is_configured() is False

    def test_is_configured_true(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        assert n.is_configured() is True

    def test_can_send_disabled(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat", enabled=False)
        assert n.can_send() is False

    def test_can_send_enabled(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat", enabled=True)
        assert n.can_send() is True

    def test_can_send_not_configured(self):
        n = TelegramNotifier(bot_token="", chat_id="", enabled=True)
        assert n.can_send() is False


class TestSendMessage:
    def test_not_configured_returns_false(self):
        n = TelegramNotifier(bot_token="", chat_id="")
        assert n.send_message("hello") is False

    def test_disabled_returns_false(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat", enabled=False)
        assert n.send_message("hello") is False

    def test_success(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat", enabled=True)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("esports_monitor.notification.telegram.requests.post", return_value=mock_resp) as mock_post:
            assert n.send_message("hello") is True
            mock_post.assert_called_once()

    def test_http_error(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat", enabled=True)
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        with patch("esports_monitor.notification.telegram.requests.post", return_value=mock_resp):
            assert n.send_message("hello") is False

    def test_request_exception(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat", enabled=True)
        with patch(
            "esports_monitor.notification.telegram.requests.post",
            side_effect=requests.exceptions.Timeout("timeout"),
        ):
            assert n.send_message("hello") is False


class TestSendSignalAlert:
    def test_no_strongest_signal_returns_false(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        alert = MorphologyAlert(match_id="m1")
        assert n.send_signal_alert(alert) is False

    def test_success(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        sig = MorphologySignal(
            signal_name="breakout", signal_label="突破反超",
            window_label="mid", direction="A", buy_team="T1",
            buy_price=0.476, predicted_win_prob=0.714, predicted_pnl=0.238,
        )
        alert = MorphologyAlert(
            match_id="m1", team_a="T1", team_b="Gen.G",
            window_label="mid", minutes_since_start=45.0,
            strongest_signal=sig, total_strength=1,
        )
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_signal_alert(alert) is True
            mock_send.assert_called_once()
            # 验证消息内容包含关键信息
            msg = mock_send.call_args[0][0]
            assert "breakout" in msg
            assert "T1" in msg
            assert "0.476" in msg


class TestSendStatusReport:
    def test_success(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_status_report(
                live_count=5,
                game_breakdown={"lol": 3, "cs2": 1, "dota2": 1},
                recent_signals=[{"signal_name": "breakout"}, {"signal_name": "breakout"}],
                trade_stats={"total": 12, "settled": 8, "wins": 5, "losses": 3, "total_pnl": 147.5},
                db_size_mb=245.3,
            ) is True
            msg = mock_send.call_args[0][0]
            assert "5" in msg  # live_count
            assert "147.50" in msg  # PnL
            assert "245.3" in msg  # DB size

    def test_empty_data(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True):
            assert n.send_status_report(
                live_count=0,
                game_breakdown={},
                recent_signals=[],
                trade_stats={"total": 0, "settled": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
                db_size_mb=0.0,
            ) is True


class TestSendSettlementNotification:
    def test_winner(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        trade = MorphologyTrade(
            match_id="m1", signal_id=1, buy_team="T1", buy_price=0.4,
            quantity=250.0, notional_usd=100.0, opened_at="2026-01-01T00:00:00Z",
            pnl_usd=150.0,
        )
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_settlement_notification(
                match_id="m1", team_a="T1", team_b="Gen.G",
                winning_team="T1", trade=trade,
            ) is True
            msg = mock_send.call_args[0][0]
            assert "T1" in msg
            assert "+150.00" in msg

    def test_loser(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        trade = MorphologyTrade(
            match_id="m1", signal_id=1, buy_team="T1", buy_price=0.4,
            quantity=250.0, notional_usd=100.0, opened_at="2026-01-01T00:00:00Z",
            pnl_usd=-100.0,
        )
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_settlement_notification(
                match_id="m1", team_a="T1", team_b="Gen.G",
                winning_team="Gen.G", trade=trade,
            ) is True
            msg = mock_send.call_args[0][0]
            assert "Gen.G" in msg
            assert "-100.00" in msg


class TestSendErrorAlert:
    def test_success(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_error_alert("数据库连接失败") is True
            msg = mock_send.call_args[0][0]
            assert "数据库连接失败" in msg


class TestCreateFromConfig:
    def test_create(self):
        config = {
            "notification": {
                "telegram": {
                    "enabled": True,
                    "bot_token": "tok",
                    "chat_id": "chat",
                }
            }
        }
        n = create_notifier_from_config(config)
        assert n.bot_token == "tok"
        assert n.chat_id == "chat"
        assert n.enabled is True

    def test_empty_config(self):
        n = create_notifier_from_config({})
        assert n.bot_token == ""
        assert n.enabled is True  # 默认 True

    def test_no_telegram_section(self):
        n = create_notifier_from_config({"notification": {}})
        assert n.is_configured() is False


# ----------------------------------------------------------------------
# 新增通知方法测试
# ----------------------------------------------------------------------


class TestSendHeartbeat:
    def test_no_failures_healthy(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_heartbeat(
                processed_matches=5, price_snapshots_saved=10,
                orderbook_snapshots_saved=8, failed_count=0,
                elapsed_seconds=12.3, next_interval_seconds=30,
                alerts_sent=1,
            ) is True
            msg = mock_send.call_args[0][0]
            assert "正常" in msg
            assert "5 场" in msg
            assert "+10" in msg
            assert "12.3s" in msg

    def test_partial_failures_warning(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_heartbeat(
                processed_matches=5, price_snapshots_saved=8,
                orderbook_snapshots_saved=6, failed_count=2,
                elapsed_seconds=15.0, next_interval_seconds=30,
            )
            msg = mock_send.call_args[0][0]
            assert "部分失败" in msg

    def test_all_failed_critical(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_heartbeat(
                processed_matches=3, price_snapshots_saved=0,
                orderbook_snapshots_saved=0, failed_count=3,
                elapsed_seconds=5.0, next_interval_seconds=300,
            )
            msg = mock_send.call_args[0][0]
            assert "全部失败" in msg

    def test_zero_processed_no_fail(self):
        """无比赛且无失败时为正常状态。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_heartbeat(
                processed_matches=0, price_snapshots_saved=0,
                orderbook_snapshots_saved=0, failed_count=0,
                elapsed_seconds=0.5, next_interval_seconds=300,
            )
            msg = mock_send.call_args[0][0]
            assert "正常" in msg


class TestSendDiscoveryReport:
    def test_with_monitoring_matches(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_discovery_report(
                new_count=3, existing_count=8, skipped_count=2,
                errors=0,
                game_breakdown={"cs2": 1, "dota2": 1, "lol": 1},
                monitoring_matches=[
                    {"game": "cs2", "team_a": "NaVi", "team_b": "Vitality",
                     "start_time": "2026-06-29T10:00:00Z"},
                    {"game": "lol", "team_a": "T1", "team_b": "Gen.G",
                     "start_time": "2026-06-29T11:00:00Z"},
                ],
            ) is True
            msg = mock_send.call_args[0][0]
            assert "3" in msg
            assert "CS2" in msg
            assert "NaVi" in msg
            assert "Vitality" in msg
            assert "监控中" in msg

    def test_monitoring_matches_show_start_time(self):
        """监控列表应显示北京时间开始时间。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_discovery_report(
                new_count=1, existing_count=0, skipped_count=0,
                errors=0, game_breakdown={"lol": 1},
                monitoring_matches=[
                    {"game": "lol", "team_a": "T1", "team_b": "Gen.G",
                     "start_time": "2026-06-29T12:00:00Z"},
                ],
            )
            msg = mock_send.call_args[0][0]
            assert "监控中" in msg
            assert "T1" in msg
            assert "Gen.G" in msg
            # 北京时间 20:00
            assert "06-29 20:00" in msg

    def test_no_new_matches(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_discovery_report(
                new_count=0, existing_count=10, skipped_count=5,
                errors=0, game_breakdown={},
            )
            msg = mock_send.call_args[0][0]
            assert "无" in msg  # 游戏分布为"无"

    def test_empty_monitoring_matches(self):
        """无监控比赛时不显示监控区块。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_discovery_report(
                new_count=1, existing_count=0, skipped_count=0,
                errors=0, game_breakdown={"lol": 1},
                monitoring_matches=None,
            )
            msg = mock_send.call_args[0][0]
            assert "监控中" not in msg

    def test_long_team_name_truncated(self):
        """队伍名过长应被截断。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        long_name = "A" * 50
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_discovery_report(
                new_count=1, existing_count=0, skipped_count=0,
                errors=0, game_breakdown={"lol": 1},
                monitoring_matches=[
                    {"game": "lol", "team_a": long_name, "team_b": "Gen.G",
                     "start_time": ""},
                ],
            )
            msg = mock_send.call_args[0][0]
            assert "…" in msg  # 被截断


class TestSendMorphologySkipped:
    def test_cooldown(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_morphology_skipped(
                match_id="m1", team_a="T1", team_b="Gen.G",
                reason="cooldown", detail="剩余 12.5 分钟",
            ) is True
            msg = mock_send.call_args[0][0]
            assert "冷却中" in msg
            assert "剩余 12.5 分钟" in msg

    def test_no_window(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_morphology_skipped(
                match_id="m1", team_a="T1", team_b="Gen.G",
                reason="no_window", detail="进度 105 分钟",
            )
            msg = mock_send.call_args[0][0]
            assert "无匹配窗口" in msg

    def test_no_data(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_morphology_skipped(
                match_id="m1", team_a="T1", team_b="Gen.G",
                reason="no_data", detail="仅 3 点",
            )
            msg = mock_send.call_args[0][0]
            assert "数据不足" in msg

    def test_buy_price_filtered(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_morphology_skipped(
                match_id="m1", team_a="T1", team_b="Gen.G",
                reason="buy_price_filtered", detail="价格 0.96",
            )
            msg = mock_send.call_args[0][0]
            assert "买入价过滤" in msg

    def test_unknown_reason(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_morphology_skipped(
                match_id="m1", team_a="T1", team_b="Gen.G",
                reason="custom_reason",
            )
            msg = mock_send.call_args[0][0]
            assert "custom_reason" in msg

    def test_no_detail_omitted(self):
        """detail 为空时不出现在消息中。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_morphology_skipped(
                match_id="m1", team_a="T1", team_b="Gen.G",
                reason="cooldown", detail="",
            )
            msg = mock_send.call_args[0][0]
            assert "详情" not in msg


class TestSendArchiveReport:
    def test_vacuum_executed_saved_space(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            assert n.send_archive_report(
                archived_rows=15234, deleted_rows=15234,
                archived_months=3, vacuum_executed=True,
                db_size_before_mb=45.6, db_size_after_mb=12.3,
                archive_files_count=12,
            ) is True
            msg = mock_send.call_args[0][0]
            assert "已执行" in msg
            assert "15234" in msg
            assert "45.6" in msg
            assert "12.3" in msg
            assert "节省" in msg

    def test_vacuum_skipped(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_archive_report(
                archived_rows=0, deleted_rows=0,
                archived_months=0, vacuum_executed=False,
                db_size_before_mb=10.0, db_size_after_mb=10.0,
                archive_files_count=0,
            )
            msg = mock_send.call_args[0][0]
            assert "跳过" in msg

    def test_db_size_increased(self):
        """DB 体积变大时显示变化（负数）。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_archive_report(
                archived_rows=100, deleted_rows=100,
                archived_months=1, vacuum_executed=False,
                db_size_before_mb=10.0, db_size_after_mb=15.0,
                archive_files_count=1,
            )
            msg = mock_send.call_args[0][0]
            assert "变化" in msg


class TestStatusReportWithGrouped:
    def test_with_grouped_stats(self):
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_status_report(
                live_count=5,
                game_breakdown={"cs2": 2, "lol": 3},
                recent_signals=[{"signal_name": "breakout"}],
                trade_stats={"total": 10, "settled": 8, "wins": 6,
                             "losses": 2, "total_pnl": 50.0},
                db_size_mb=2.5,
                grouped_stats={
                    "by_signal": [
                        {"group_key": "breakout", "total": 5, "settled": 4,
                         "wins": 3, "losses": 1, "total_pnl": 30.0},
                    ],
                    "by_game": [
                        {"group_key": "cs2", "total": 3, "settled": 3,
                         "wins": 2, "losses": 1, "total_pnl": 10.0},
                    ],
                    "by_window": [
                        {"group_key": "mid", "total": 8, "settled": 6,
                         "wins": 5, "losses": 1, "total_pnl": 40.0},
                    ],
                },
            )
            msg = mock_send.call_args[0][0]
            assert "按信号分组" in msg
            assert "按游戏分组" in msg
            assert "按窗口分组" in msg
            assert "breakout" in msg
            assert "75%" in msg  # 3/4 胜率

    def test_grouped_stats_no_settled_rows_omitted(self):
        """无已结算分组时不显示该维度。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_status_report(
                live_count=0,
                game_breakdown={},
                recent_signals=[],
                trade_stats={"total": 0, "settled": 0, "wins": 0,
                             "losses": 0, "total_pnl": 0.0},
                db_size_mb=0.0,
                grouped_stats={
                    "by_signal": [
                        {"group_key": "x", "total": 5, "settled": 0,
                         "wins": 0, "losses": 0, "total_pnl": 0.0},
                    ],
                },
            )
            msg = mock_send.call_args[0][0]
            assert "按信号分组" not in msg

    def test_grouped_stats_none_omitted(self):
        """grouped_stats=None 时不显示分组。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_status_report(
                live_count=0, game_breakdown={}, recent_signals=[],
                trade_stats={"total": 0, "settled": 0, "wins": 0,
                             "losses": 0, "total_pnl": 0.0},
                db_size_mb=0.0, grouped_stats=None,
            )
            msg = mock_send.call_args[0][0]
            assert "按信号分组" not in msg

    def test_grouped_stats_sorted_by_pnl_desc(self):
        """分组内按 total_pnl 降序。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_status_report(
                live_count=0, game_breakdown={}, recent_signals=[],
                trade_stats={"total": 0, "settled": 0, "wins": 0,
                             "losses": 0, "total_pnl": 0.0},
                db_size_mb=0.0,
                grouped_stats={
                    "by_signal": [
                        {"group_key": "low", "total": 3, "settled": 3,
                         "wins": 1, "losses": 2, "total_pnl": -10.0},
                        {"group_key": "high", "total": 3, "settled": 3,
                         "wins": 3, "losses": 0, "total_pnl": 50.0},
                        {"group_key": "mid", "total": 3, "settled": 3,
                         "wins": 2, "losses": 1, "total_pnl": 20.0},
                    ],
                },
            )
            msg = mock_send.call_args[0][0]
            # high (50) 应排在 mid (20) 之前，mid 在 low (-10) 之前
            assert msg.index("high") < msg.index("mid") < msg.index("low")

    def test_grouped_stats_truncated_to_5(self):
        """每组最多显示 5 条。"""
        n = TelegramNotifier(bot_token="tok", chat_id="chat")
        rows = [
            {"group_key": f"sig_{i}", "total": 5, "settled": 5,
             "wins": 3, "losses": 2, "total_pnl": float(i)}
            for i in range(10)
        ]
        with patch.object(n, "send_message", return_value=True) as mock_send:
            n.send_status_report(
                live_count=0, game_breakdown={}, recent_signals=[],
                trade_stats={"total": 0, "settled": 0, "wins": 0,
                             "losses": 0, "total_pnl": 0.0},
                db_size_mb=0.0,
                grouped_stats={"by_signal": rows},
            )
            msg = mock_send.call_args[0][0]
            # 应只显示 sig_9 (pnl 最大) 到 sig_5 共 5 个
            assert "sig_9" in msg
            assert "sig_5" in msg
            assert "sig_4" not in msg  # 第 6 个不应出现
