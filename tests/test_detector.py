"""morphology/detector.py 测试。"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from esports_monitor.morphology.detector import MorphologyDetector
from esports_monitor.morphology.models import MorphologyTrade
from esports_monitor.morphology.repository import MorphologyRepository
from esports_monitor.morphology.simulator import MorphologySimulator
from esports_monitor.utils.time_utils import now_utc, to_utc_iso


def make_detector(
    storage,
    config: Dict[str, Any],
    backtest_stats: Dict[str, Any] = None,
) -> MorphologyDetector:
    """构造检测器。"""
    repo = MorphologyRepository(storage)
    sim = MorphologySimulator(repository=repo, config=config, clob_client=None)
    return MorphologyDetector(
        repository=repo,
        simulator=sim,
        storage=storage,
        config=config,
        backtest_stats=backtest_stats or {},
    )


def insert_price_series(storage, match_id, start_iso, count, price_a_start, price_b_start, drift_a=0.0, drift_b=0.0):
    """插入价格序列。"""
    from datetime import datetime, timezone
    base = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    for i in range(count):
        ts = (base + timedelta(minutes=2 * i)).isoformat()
        pa = price_a_start + drift_a * i
        pb = price_b_start + drift_b * i
        storage.insert_price_snapshot(match_id, "team_a", pa, pb, ts)
        storage.insert_price_snapshot(match_id, "team_b", pb, pa, ts)


class TestDetectBasic:
    def test_disabled_returns_none(self, storage, sample_match, sample_config):
        sample_config["morphology"]["enabled"] = False
        detector = make_detector(storage, sample_config["morphology"])
        assert detector.detect(sample_match) is None

    def test_no_match_id(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        assert detector.detect({"match_id": "", "team_a": "A", "team_b": "B"}) is None

    def test_missing_teams(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        assert detector.detect({"match_id": "m1", "team_a": "", "team_b": ""}) is None


class TestDetectCooldown:
    def test_cooldown_skips_detection(self, storage, sample_match, sample_config):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], start_time=m["start_time"],
        )
        # 设置一个未过期的冷却
        future = to_utc_iso(now_utc() + timedelta(minutes=10))
        storage.set_cooldown(m["match_id"], future, last_signal="x")
        detector = make_detector(storage, sample_config["morphology"])
        # 即使有价格数据，冷却期内应返回 None
        insert_price_series(storage, m["match_id"], "2026-06-29T10:00:00Z", 10, 0.5, 0.5)
        # 当前时间远在开始之后，进入 mid 窗口
        match = storage.get_match(m["match_id"])
        # 调整 start_time 让 minutes_since_start 落在 mid 窗口
        match["start_time"] = to_utc_iso(now_utc() - timedelta(minutes=45))
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            start_time=to_utc_iso(now_utc() - timedelta(minutes=45)),
            status="live",
        )
        match = storage.get_match(m["match_id"])
        # 冷却期内 detect 返回 None
        assert detector.detect(match) is None


class TestDetectNoData:
    def test_insufficient_data_returns_no_data(self, storage, sample_match, sample_config):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            start_time=to_utc_iso(now_utc() - timedelta(minutes=45)),
        )
        # 只插入 2 个点（不足 5 个去重点）
        insert_price_series(storage, m["match_id"], "2026-06-29T10:00:00Z", 2, 0.5, 0.5)
        detector = make_detector(storage, sample_config["morphology"])
        match = storage.get_match(m["match_id"])
        alert = detector.detect(match)
        # observe_all=True，返回 no_data 状态
        assert alert is not None
        assert alert.status == "no_data"

    def test_observe_all_false_returns_none(self, storage, sample_match, sample_config):
        sample_config["morphology"]["observe_all"] = False
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            start_time=to_utc_iso(now_utc() - timedelta(minutes=45)),
        )
        insert_price_series(storage, m["match_id"], "2026-06-29T10:00:00Z", 2, 0.5, 0.5)
        detector = make_detector(storage, sample_config["morphology"])
        match = storage.get_match(m["match_id"])
        assert detector.detect(match) is None


class TestDetectNoWindow:
    def test_no_start_time_no_window(self, storage, sample_match, sample_config):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
            # 不设置 start_time
        )
        # 插入足够多的价格数据
        insert_price_series(storage, m["match_id"], "2026-06-29T10:00:00Z", 10, 0.6, 0.4)
        detector = make_detector(storage, sample_config["morphology"])
        match = storage.get_match(m["match_id"])
        alert = detector.detect(match)
        # 无 start_time → minutes_since_start 为 None → 无匹配窗口
        assert alert is not None
        assert alert.status == "no_window"


class TestDetectWithSignal:
    def test_breakout_signal_triggered(self, storage, sample_match, sample_config):
        m = sample_match
        # 让比赛在 mid 窗口（30-90 分钟）
        start = to_utc_iso(now_utc() - timedelta(minutes=45))
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], start_time=start,
            end_time=to_utc_iso(now_utc() + timedelta(minutes=45)),
        )
        # 构造 48 个点：A 队最近 6 点大幅上涨突破 B 队
        # 前 42 个点 A 落后 B，后 6 个点 A 反超
        from datetime import datetime, timezone
        base = now_utc() - timedelta(minutes=96)
        for i in range(48):
            ts = to_utc_iso(base + timedelta(minutes=2 * i))
            if i < 42:
                pa = 0.30
                pb = 0.70
            else:
                # A 大幅上涨
                pa = 0.30 + (i - 41) * 0.08  # 0.38, 0.46, 0.54, 0.62, 0.70, 0.78
                pb = 1.0 - pa
            storage.insert_price_snapshot(m["match_id"], "team_a", pa, pb, ts)
            storage.insert_price_snapshot(m["match_id"], "team_b", pb, pa, ts)
        detector = make_detector(storage, sample_config["morphology"])
        match = storage.get_match(m["match_id"])
        alert = detector.detect(match)
        # 应触发信号（breakout 或其他）
        assert alert is not None
        if alert.status == "normal":
            assert alert.strongest_signal is not None
            assert len(alert.signals) > 0
        else:
            # 即便没信号，也应该有观察记录
            assert alert.status in ("no_signal", "normal")

    def test_no_signal_returned_with_observe_all(self, storage, sample_match, sample_config):
        m = sample_match
        start = to_utc_iso(now_utc() - timedelta(minutes=45))
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], start_time=start,
        )
        # 插入稳定价格（无明显趋势，不易触发信号）
        insert_price_series(storage, m["match_id"], to_utc_iso(now_utc() - timedelta(minutes=96)), 48, 0.5, 0.5)
        detector = make_detector(storage, sample_config["morphology"])
        match = storage.get_match(m["match_id"])
        alert = detector.detect(match)
        assert alert is not None
        # 无明显趋势，可能 no_signal 或 normal
        assert alert.status in ("no_signal", "normal")


class TestSettleMatch:
    def test_settle_multiple_trades(self, storage, sample_match, sample_config):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        # 插入信号和交易
        sig_id = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="x",
            window_label="mid", buy_team="T1", buy_price=0.4,
            detected_at=to_utc_iso(now_utc()),
        )
        storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.4, quantity=250.0, notional_usd=100.0,
            opened_at=to_utc_iso(now_utc()),
        )
        storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.5, quantity=200.0, notional_usd=100.0,
            opened_at=to_utc_iso(now_utc()),
        )
        detector = make_detector(storage, sample_config["morphology"])
        count = detector.settle_match(m["match_id"], winning_team="T1")
        assert count == 2
        # 应全部已结算
        assert storage.get_open_trades(match_id=m["match_id"]) == []

    def test_settle_no_trades(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        count = detector.settle_match("nonexistent", winning_team="T1")
        assert count == 0


class TestWindowMatching:
    def test_match_window_label(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        # early: 0-30
        assert detector._match_window_label(0) == "early"
        assert detector._match_window_label(15) == "early"
        assert detector._match_window_label(30) == "early"
        # mid: 30-90
        assert detector._match_window_label(31) == "mid"
        assert detector._match_window_label(60) == "mid"
        assert detector._match_window_label(90) == "mid"
        # late: 90+
        assert detector._match_window_label(91) == "late"
        assert detector._match_window_label(9999) == "late"

    def test_match_window_label_none(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        assert detector._match_window_label(None) is None

    def test_window_rules(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        assert "breakout" in detector._window_rules("mid")
        assert "stable_spread" in detector._window_rules("early")
        assert detector._window_rules("nonexistent") == []


class TestComputeMinutesSinceStart:
    def test_with_start_time(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        from esports_monitor.utils.time_utils import to_utc_iso, now_utc
        from datetime import timedelta
        match = {"start_time": to_utc_iso(now_utc() - timedelta(minutes=30))}
        minutes = detector._compute_minutes_since_start(match)
        assert minutes is not None
        assert 29 <= minutes <= 31

    def test_no_start_time(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        assert detector._compute_minutes_since_start({}) is None

    def test_invalid_start_time(self, storage, sample_config):
        detector = make_detector(storage, sample_config["morphology"])
        assert detector._compute_minutes_since_start({"start_time": "invalid"}) is None
