"""morphology/repository.py 测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from esports_monitor.morphology.models import MorphologyAlert, MorphologySignal, MorphologyTrade
from esports_monitor.morphology.repository import MorphologyRepository
from esports_monitor.utils.time_utils import now_utc, to_utc_iso


class TestSaveSignals:
    def test_save_multiple_signals(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        repo = MorphologyRepository(storage)
        alert = MorphologyAlert(
            match_id=m["match_id"],
            team_a="T1", team_b="Gen.G",
            leader_team="T1", threat_team="Gen.G",
            leader_price=0.6, threat_price=0.4,
            window_label="mid",
            signals=[
                MorphologySignal(
                    signal_name="breakout", signal_label="突破反超",
                    window_label="mid", direction="A", buy_team="T1",
                    buy_price=0.6, detected_at=to_utc_iso(now_utc()),
                ),
                MorphologySignal(
                    signal_name="stable_spread", signal_label="优势稳定",
                    window_label="mid", direction="A", buy_team="T1",
                    buy_price=0.6, detected_at=to_utc_iso(now_utc()),
                ),
            ],
        )
        ids = repo.save_signals(alert)
        assert len(ids) == 2
        # 信号 id 应被填充
        assert alert.signals[0].id is not None
        assert alert.signals[1].id is not None

    def test_save_empty_signals(self, storage):
        repo = MorphologyRepository(storage)
        alert = MorphologyAlert(match_id="m1")
        assert repo.save_signals(alert) == []

    def test_save_storage_failure(self, storage):
        # storage 抛异常时应被吞掉
        bad_storage = MagicMock()
        bad_storage.insert_morphology_signal.side_effect = RuntimeError("boom")
        repo = MorphologyRepository(bad_storage)
        alert = MorphologyAlert(
            match_id="m1",
            signals=[
                MorphologySignal(
                    signal_name="x", signal_label="x", window_label="mid",
                    direction="A", buy_team="A", buy_price=0.5,
                )
            ],
        )
        ids = repo.save_signals(alert)
        assert ids == []


class TestSaveAndGetTrades:
    def test_save_and_get_open(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        # 先插入 signal
        sig_id = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="breakout",
            window_label="mid", buy_team="T1", buy_price=0.4,
            detected_at=to_utc_iso(now_utc()),
        )
        repo = MorphologyRepository(storage)
        trade = MorphologyTrade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.4, quantity=250.0, notional_usd=100.0,
            opened_at=to_utc_iso(now_utc()),
        )
        trade_id = repo.save_trade(trade)
        assert trade_id is not None
        assert trade.id == trade_id

        open_trades = repo.get_open_trades(match_id=m["match_id"])
        assert len(open_trades) == 1
        assert open_trades[0].buy_team == "T1"

        # 获取有未结算交易的比赛 id
        match_ids = repo.get_open_trade_match_ids()
        assert m["match_id"] in match_ids

    def test_get_open_trades_empty(self, storage):
        repo = MorphologyRepository(storage)
        assert repo.get_open_trades() == []
        assert repo.get_open_trade_match_ids() == []


class TestSettleTrade:
    def test_settle(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        sig_id = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="breakout",
            window_label="mid", buy_team="T1", buy_price=0.4,
            detected_at=to_utc_iso(now_utc()),
        )
        trade_id = storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.4, quantity=250.0, notional_usd=100.0,
            opened_at=to_utc_iso(now_utc()),
        )
        repo = MorphologyRepository(storage)
        ok = repo.settle_trade(trade_id, winning_team="T1", pnl_usd=150.0)
        assert ok is True
        # 不再在 open 列表中
        assert repo.get_open_trades(match_id=m["match_id"]) == []


class TestCooldown:
    def test_set_and_get(self, storage):
        repo = MorphologyRepository(storage)
        end = to_utc_iso(now_utc() + timedelta(minutes=15))
        assert repo.set_cooldown("m1", end, last_signal="breakout") is True
        cd_end = repo.get_cooldown_end("m1")
        assert cd_end is not None

    def test_get_nonexistent(self, storage):
        repo = MorphologyRepository(storage)
        assert repo.get_cooldown_end("nonexistent") is None

    def test_invalid_iso_returns_none(self, storage):
        # 直接写入无效的 cooldown_end
        storage.set_cooldown("m1", "not-a-date", last_signal="x")
        repo = MorphologyRepository(storage)
        assert repo.get_cooldown_end("m1") is None


class TestGetTradeStats:
    def test_empty(self, storage):
        repo = MorphologyRepository(storage)
        stats = repo.get_trade_stats()
        assert stats["total"] == 0
        assert stats["total_pnl"] == 0.0

    def test_with_data(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
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
        repo = MorphologyRepository(storage)
        stats = repo.get_trade_stats()
        assert stats["total"] == 1
        assert stats["settled"] == 0


class TestGetSignals:
    def test_with_filter(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="breakout",
            window_label="mid", buy_team="T1", buy_price=0.4,
            detected_at=to_utc_iso(now_utc()),
        )
        repo = MorphologyRepository(storage)
        sigs = repo.get_signals(match_id=m["match_id"])
        assert len(sigs) == 1

        sigs_recent = repo.get_signals(hours=1)
        assert len(sigs_recent) == 1

    def test_empty(self, storage):
        repo = MorphologyRepository(storage)
        assert repo.get_signals() == []


class TestErrorHandling:
    """所有 except 分支均应被吞并返回默认值。"""

    def _make_bad_storage(self, **method_exceptions):
        bad = MagicMock()
        for name, exc in method_exceptions.items():
            getattr(bad, name).side_effect = exc
        return bad

    def test_get_signals_exception_returns_empty(self):
        bad = self._make_bad_storage(
            get_morphology_signals=RuntimeError("db error")
        )
        repo = MorphologyRepository(bad)
        assert repo.get_signals() == []

    def test_save_trade_exception_returns_none(self):
        bad = self._make_bad_storage(
            insert_morphology_trade=RuntimeError("db error")
        )
        repo = MorphologyRepository(bad)
        trade = MorphologyTrade(
            match_id="m1", signal_id=1, buy_team="T1", buy_price=0.4,
            quantity=250.0, notional_usd=100.0, opened_at="2026-01-01T00:00:00Z",
        )
        assert repo.save_trade(trade) is None

    def test_get_open_trades_exception_returns_empty(self):
        bad = self._make_bad_storage(get_open_trades=RuntimeError("db error"))
        repo = MorphologyRepository(bad)
        assert repo.get_open_trades() == []

    def test_get_open_trade_match_ids_exception_returns_empty(self):
        bad = self._make_bad_storage(
            get_open_trade_match_ids=RuntimeError("db error")
        )
        repo = MorphologyRepository(bad)
        assert repo.get_open_trade_match_ids() == []

    def test_settle_trade_exception_returns_false(self):
        bad = self._make_bad_storage(
            settle_morphology_trade=RuntimeError("db error")
        )
        repo = MorphologyRepository(bad)
        assert repo.settle_trade(1, "T1", 100.0) is False

    def test_get_trade_stats_exception_returns_default(self):
        bad = self._make_bad_storage(get_trade_stats=RuntimeError("db error"))
        repo = MorphologyRepository(bad)
        stats = repo.get_trade_stats()
        assert stats == {"total": 0, "settled": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

    def test_set_cooldown_exception_returns_false(self):
        bad = self._make_bad_storage(set_cooldown=RuntimeError("db error"))
        repo = MorphologyRepository(bad)
        assert repo.set_cooldown("m1", "2026-01-01T00:00:00Z") is False

    def test_get_cooldown_end_exception_returns_none(self):
        bad = self._make_bad_storage(get_cooldown=RuntimeError("db error"))
        repo = MorphologyRepository(bad)
        assert repo.get_cooldown_end("m1") is None

    def test_get_cooldown_end_row_without_end_str(self):
        """row 不含 cooldown_end 字段时返回 None。"""
        bad = MagicMock()
        bad.get_cooldown.return_value = {}  # 空 dict
        repo = MorphologyRepository(bad)
        assert repo.get_cooldown_end("m1") is None

    def test_get_cooldown_end_non_dict_row(self):
        """row 非 dict 时返回 None。"""
        bad = MagicMock()
        bad.get_cooldown.return_value = None
        repo = MorphologyRepository(bad)
        assert repo.get_cooldown_end("m1") is None

    def test_row_to_trade_handles_none_values(self):
        """_row_to_trade 应能处理 None 值。"""
        from esports_monitor.morphology.repository import MorphologyRepository
        trade = MorphologyRepository._row_to_trade({})
        assert trade.match_id == ""
        assert trade.signal_id == 0
        assert trade.buy_price == 0.0
        assert trade.settled == 0
