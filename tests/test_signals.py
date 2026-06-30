"""morphology/signals.py 测试。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pytest

from esports_monitor.morphology.models import MorphologyFeatures
from esports_monitor.morphology.signals import (
    N_RESAMPLE,
    SIGNALS,
    SIGNAL_LABELS,
    compute_morphology_features,
    get_predicted_stats,
    get_signal_fn,
    get_signal_label,
    load_backtest_stats,
    resample_series,
    signal_acceleration,
    signal_breakout,
    signal_current_and_recent_leader,
    signal_divergence,
    signal_momentum_catcher,
    signal_stable_spread,
)


def make_features(**kwargs) -> MorphologyFeatures:
    """构造特征对象，缺省字段用 0。"""
    defaults = dict(
        current=0.5, mean_all=0.5, mean_12h=0.5, mean_24h=0.5,
        ret_6h=0.0, ret_12h=0.0, ret_24h=0.0, ret_total=0.0,
        price_6h_ago=0.5, price_12h_ago=0.5,
    )
    defaults.update(kwargs)
    return MorphologyFeatures(**defaults)


class TestComputeMorphologyFeatures:
    def test_basic(self):
        prices = [0.4, 0.45, 0.5, 0.55, 0.6]
        f = compute_morphology_features(prices, t=4)
        assert f.current == 0.6
        assert f.mean_all == pytest.approx(0.5)
        assert f.price_6h_ago == prices[max(0, 4 - 5)]  # = prices[0] = 0.4
        assert f.price_12h_ago == prices[max(0, 4 - 11)]  # = prices[0]
        # ret_6h = (0.6 - 0.4) / 0.4 = 0.5
        assert f.ret_6h == pytest.approx(0.5)
        # ret_total = (0.6 - 0.4) / 0.4 = 0.5
        assert f.ret_total == pytest.approx(0.5)

    def test_empty_prices(self):
        f = compute_morphology_features([], t=0)
        assert f.current == 0.0
        assert f.mean_all == 0.0

    def test_invalid_t(self):
        f = compute_morphology_features([0.5, 0.6], t=10)
        assert f.current == 0.0

    def test_negative_t(self):
        f = compute_morphology_features([0.5, 0.6], t=-1)
        assert f.current == 0.0

    def test_zero_price_no_division_error(self):
        prices = [0.0, 0.0, 0.5]
        f = compute_morphology_features(prices, t=2)
        # 不应抛异常
        assert f.ret_total == 0.0

    def test_to_dict(self):
        f = make_features(current=0.6)
        d = f.to_dict()
        assert d["current"] == 0.6
        assert "mean_12h" in d
        assert len(d) == 10


class TestSignalCurrentAndRecentLeader:
    def test_a_wins(self):
        A = make_features(current=0.6, mean_12h=0.55)
        B = make_features(current=0.4, mean_12h=0.45)
        assert signal_current_and_recent_leader(A, B) == "A"

    def test_b_wins(self):
        A = make_features(current=0.4, mean_12h=0.45)
        B = make_features(current=0.6, mean_12h=0.55)
        assert signal_current_and_recent_leader(A, B) == "B"

    def test_no_signal_current_higher_mean_lower(self):
        A = make_features(current=0.6, mean_12h=0.45)
        B = make_features(current=0.4, mean_12h=0.55)
        assert signal_current_and_recent_leader(A, B) is None

    def test_equal_prices_no_signal(self):
        A = make_features(current=0.5, mean_12h=0.5)
        B = make_features(current=0.5, mean_12h=0.5)
        assert signal_current_and_recent_leader(A, B) is None


class TestSignalMomentumCatcher:
    def test_a_catches_up(self):
        # A 落后但 12 点涨幅明显大于 B 且自身为正
        A = make_features(current=0.4, ret_12h=0.20)
        B = make_features(current=0.6, ret_12h=0.05)
        # A.ret_12h > B.ret_12h + 0.1 → 0.20 > 0.15 ✓
        # A.ret_12h > 0.05 ✓
        assert signal_momentum_catcher(A, B) == "A"

    def test_b_catches_up(self):
        A = make_features(current=0.6, ret_12h=0.05)
        B = make_features(current=0.4, ret_12h=0.20)
        assert signal_momentum_catcher(A, B) == "B"

    def test_no_signal_ret_too_small(self):
        A = make_features(current=0.4, ret_12h=0.06)
        B = make_features(current=0.6, ret_12h=0.05)
        # A.ret_12h - B.ret_12h = 0.01 < 0.1
        assert signal_momentum_catcher(A, B) is None

    def test_no_signal_ret_not_positive(self):
        A = make_features(current=0.4, ret_12h=-0.1)
        B = make_features(current=0.6, ret_12h=-0.3)
        # diff = 0.2 > 0.1 ✓ 但 A.ret_12h = -0.1 不 > 0.05
        assert signal_momentum_catcher(A, B) is None


class TestSignalBreakout:
    def test_a_breakout(self):
        A = make_features(current=0.6, price_6h_ago=0.3, ret_6h=0.3)
        B = make_features(current=0.5, price_6h_ago=0.55)
        # A.price_6h_ago < B.price_6h_ago ✓ (0.3 < 0.55)
        # A.current > B.current ✓ (0.6 > 0.5)
        # A.ret_6h > 0.2 ✓ (0.3)
        assert signal_breakout(A, B) == "A"

    def test_b_breakout(self):
        A = make_features(current=0.5, price_6h_ago=0.55, ret_6h=0.0)
        B = make_features(current=0.6, price_6h_ago=0.3, ret_6h=0.3)
        assert signal_breakout(A, B) == "B"

    def test_no_signal_ret_too_small(self):
        A = make_features(current=0.6, price_6h_ago=0.3, ret_6h=0.15)
        B = make_features(current=0.5, price_6h_ago=0.55)
        assert signal_breakout(A, B) is None

    def test_no_signal_was_leading(self):
        # A 6 点前已领先，不符合"突破反超"
        A = make_features(current=0.6, price_6h_ago=0.7, ret_6h=0.3)
        B = make_features(current=0.5, price_6h_ago=0.4)
        assert signal_breakout(A, B) is None


class TestSignalDivergence:
    def test_a_up_b_down(self):
        A = make_features(ret_12h=0.10)
        B = make_features(ret_12h=-0.10)
        assert signal_divergence(A, B) == "A"

    def test_b_up_a_down(self):
        A = make_features(ret_12h=-0.10)
        B = make_features(ret_12h=0.10)
        assert signal_divergence(A, B) == "B"

    def test_no_signal_both_up(self):
        A = make_features(ret_12h=0.10)
        B = make_features(ret_12h=0.10)
        assert signal_divergence(A, B) is None

    def test_no_signal_threshold_not_met(self):
        A = make_features(ret_12h=0.04)
        B = make_features(ret_12h=-0.04)
        # 0.04 不 > 0.05
        assert signal_divergence(A, B) is None


class TestSignalAcceleration:
    def test_a_accelerates(self):
        A = make_features(current=0.6, ret_6h=0.10, ret_12h=0.05)
        B = make_features(current=0.4)
        # A.current > B.current ✓
        # A.ret_6h > A.ret_12h ✓ (0.10 > 0.05)
        # A.ret_12h > 0 ✓
        assert signal_acceleration(A, B) == "A"

    def test_b_accelerates(self):
        A = make_features(current=0.4)
        B = make_features(current=0.6, ret_6h=0.10, ret_12h=0.05)
        assert signal_acceleration(A, B) == "B"

    def test_no_signal_not_accelerating(self):
        A = make_features(current=0.6, ret_6h=0.03, ret_12h=0.05)
        B = make_features(current=0.4)
        # A.ret_6h < A.ret_12h → 不加速
        assert signal_acceleration(A, B) is None

    def test_no_signal_negative_ret(self):
        A = make_features(current=0.6, ret_6h=0.10, ret_12h=-0.05)
        B = make_features(current=0.4)
        # A.ret_12h 不 > 0
        assert signal_acceleration(A, B) is None


class TestSignalStableSpread:
    def test_a_stable_leader(self):
        A = make_features(current=0.6, ret_24h=0.10)
        B = make_features(current=0.4, ret_24h=0.02)
        assert signal_stable_spread(A, B) == "A"

    def test_b_stable_leader(self):
        A = make_features(current=0.4, ret_24h=0.02)
        B = make_features(current=0.6, ret_24h=0.10)
        assert signal_stable_spread(A, B) == "B"

    def test_no_signal_ret_lower(self):
        A = make_features(current=0.6, ret_24h=0.02)
        B = make_features(current=0.4, ret_24h=0.10)
        # A 领先但 24h 收益不领先
        assert signal_stable_spread(A, B) is None


class TestSignalRegistry:
    def test_all_six_signals_registered(self):
        expected = {
            "current_and_recent_leader", "momentum_catcher", "breakout",
            "divergence", "acceleration", "stable_spread",
        }
        assert set(SIGNALS.keys()) == expected

    def test_labels_for_all(self):
        for name in SIGNALS.keys():
            assert get_signal_label(name) != name  # 标签应不同于 name

    def test_get_signal_fn_existing(self):
        fn = get_signal_fn("breakout")
        assert fn is signal_breakout

    def test_get_signal_fn_missing(self):
        assert get_signal_fn("nonexistent") is None

    def test_get_signal_label_missing(self):
        # 不存在的 name 返回原 name
        assert get_signal_label("nonexistent") == "nonexistent"


class TestResampleSeries:
    def test_insufficient_points(self):
        points = [("2026-01-01T00:00:00Z", 0.5)] * 3
        assert resample_series(points, n=48) is None

    def test_empty(self):
        assert resample_series([], n=48) is None

    def test_valid_resampling(self):
        # 构造 10 个等间隔点
        points: List[tuple] = []
        for i in range(10):
            ts = f"2026-01-01T00:{i:02d}:00Z"
            points.append((ts, float(i) * 0.1))  # 0.0, 0.1, ..., 0.9
        result = resample_series(points, n=10)
        assert result is not None
        assert len(result) == 10
        # 首尾应等于原始首尾
        assert result[0] == pytest.approx(0.0)
        assert result[-1] == pytest.approx(0.9)

    def test_default_n_is_48(self):
        points = [(f"2026-01-01T00:{i:02d}:00Z", float(i)) for i in range(10)]
        result = resample_series(points)
        assert result is not None
        assert len(result) == N_RESAMPLE

    def test_dedup_same_timestamp(self):
        # 同一时间戳多个值，应保留最后一个
        ts = "2026-01-01T00:00:00Z"
        points = [(ts, 0.1), (ts, 0.5)]
        # 去重后只有 1 个点，不足 5 个
        assert resample_series(points, n=10) is None


class TestLoadBacktestStats:
    def test_nonexistent_dir(self, tmp_path):
        result = load_backtest_stats(str(tmp_path / "nonexistent"))
        assert result == {}

    def test_empty_dir(self, tmp_path):
        result = load_backtest_stats(str(tmp_path))
        assert result == {}

    def test_load_valid_file(self, tmp_path):
        stats = {
            "early": {
                "current_and_recent_leader": {
                    "win_rate": 0.556,
                    "avg_pnl": 0.116,
                    "expectancy": 0.116,
                    "profit_factor": 1.61,
                    "trades": 18,
                    "avg_buy_price": 0.45,
                }
            },
            "mid": {},
        }
        path = tmp_path / "first_signal_summary.json"
        path.write_text(json.dumps(stats), encoding="utf-8")
        result = load_backtest_stats(str(tmp_path))
        assert "early" in result
        assert "current_and_recent_leader" in result["early"]
        assert result["early"]["current_and_recent_leader"]["win_rate"] == 0.556

    def test_invalid_json_skipped(self, tmp_path):
        (tmp_path / "first_signal_summary.json").write_text(
            "not json", encoding="utf-8"
        )
        result = load_backtest_stats(str(tmp_path))
        assert result == {}

    def test_non_dict_data_skipped(self, tmp_path):
        (tmp_path / "first_signal_summary.json").write_text(
            json.dumps([1, 2, 3]), encoding="utf-8"
        )
        result = load_backtest_stats(str(tmp_path))
        assert result == {}


class TestGetPredictedStats:
    def test_existing(self):
        stats = {
            "early": {
                "current_and_recent_leader": {
                    "win_rate": 0.556,
                    "avg_pnl": 0.116,
                    "expectancy": 0.116,
                    "profit_factor": 1.61,
                    "trades": 18,
                }
            }
        }
        win, pnl, exp, trades, pf = get_predicted_stats(
            stats, "early", "current_and_recent_leader"
        )
        assert win == 0.556
        assert pnl == 0.116
        assert exp == 0.116
        assert trades == 18
        assert pf == 1.61

    def test_missing_window(self):
        win, pnl, exp, trades, pf = get_predicted_stats({}, "early", "x")
        assert win is None
        assert trades is None

    def test_missing_signal(self):
        stats = {"early": {}}
        result = get_predicted_stats(stats, "early", "x")
        assert all(v is None for v in result)
