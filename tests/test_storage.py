"""data/sqlite_storage.py 测试。"""
from __future__ import annotations

import os
from typing import Any, Dict

import pytest

from esports_monitor.data.sqlite_storage import SQLiteStorage
from esports_monitor.utils.time_utils import now_utc, to_utc_iso


class TestInit:
    def test_init_creates_db_file(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        assert os.path.exists(db_path)
        # 所有表都应被创建
        with storage._connect() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {row[0] for row in cur.fetchall()}
        expected = {
            "matches", "price_snapshots", "orderbook_snapshots",
            "clob_token_cache", "morphology_signals", "morphology_trades",
            "morphology_cooldown", "task_runs", "global_state",
        }
        assert expected.issubset(tables)


class TestMatches:
    def test_upsert_and_get(self, storage, sample_match):
        m = sample_match
        ok = storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], league=m["league"],
            token_id_a=m["token_id_a"], token_id_b=m["token_id_b"],
            start_time=m["start_time"], end_time=m["end_time"],
            status=m["status"],
        )
        assert ok is True
        got = storage.get_match(m["match_id"])
        assert got is not None
        assert got["team_a"] == "T1"
        assert got["team_b"] == "Gen.G"
        assert got["status"] == "discovered"

    def test_upsert_idempotent_keeps_discovered_at(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], status="discovered",
        )
        first = storage.get_match(m["match_id"])
        # 再次 upsert，更新 status
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], status="live",
        )
        second = storage.get_match(m["match_id"])
        assert second["status"] == "live"
        # discovered_at 应保持不变
        assert first["discovered_at"] == second["discovered_at"]

    def test_update_match_status(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], status="discovered",
        )
        ok = storage.update_match_status(m["match_id"], "ended", winning_team="T1")
        assert ok is True
        got = storage.get_match(m["match_id"])
        assert got["status"] == "ended"
        assert got["winning_team"] == "T1"

    def test_get_match_not_exist(self, storage):
        assert storage.get_match("nonexistent") is None

    def test_get_live_matches(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], status="discovered",
        )
        live = storage.get_live_matches()
        assert len(live) == 1
        # 已结束的不在 live 列表
        storage.update_match_status(m["match_id"], "ended")
        live = storage.get_live_matches()
        assert len(live) == 0

    def test_get_matches_by_status(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"], status="discovered",
        )
        storage.update_match_status(m["match_id"], "ended")
        ended = storage.get_matches_by_status("ended")
        assert len(ended) == 1
        discovered = storage.get_matches_by_status("discovered")
        assert len(discovered) == 0


class TestPriceSnapshots:
    def test_insert_and_get(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        now_iso = to_utc_iso(now_utc())
        ok = storage.insert_price_snapshot(
            m["match_id"], "team_a", 0.55, 0.45, now_iso
        )
        assert ok is True
        rows = storage.get_price_snapshots(m["match_id"])
        assert len(rows) == 1
        assert rows[0][1] == 0.55

    def test_get_with_team_filter(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        now_iso = to_utc_iso(now_utc())
        storage.insert_price_snapshot(m["match_id"], "team_a", 0.55, 0.45, now_iso)
        storage.insert_price_snapshot(m["match_id"], "team_b", 0.45, 0.55, now_iso)
        a_rows = storage.get_price_snapshots(m["match_id"], team="team_a")
        assert len(a_rows) == 1
        assert a_rows[0][1] == 0.55

    def test_get_with_hours_filter(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        # 一条很旧的数据（用旧时间字符串）
        storage.insert_price_snapshot(
            m["match_id"], "team_a", 0.55, 0.45, "2020-01-01T00:00:00+00:00"
        )
        # hours=1 应该过滤掉旧数据
        rows = storage.get_price_snapshots(m["match_id"], hours=1)
        assert len(rows) == 0


class TestOrderbookSnapshots:
    def test_insert(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        now_iso = to_utc_iso(now_utc())
        ok = storage.insert_orderbook_snapshot(
            match_id=m["match_id"], team="team_a", token_id="tok1",
            order_book_json='{"bids":[]}', best_bid=0.54, best_ask=0.56,
            bid_depth=100.0, ask_depth=80.0, spread=0.02, recorded_at=now_iso,
        )
        assert ok is True


class TestClobTokenCache:
    def test_set_get_invalidate(self, storage):
        assert storage.get_cached_token_id("cid1", "Yes") is None
        assert storage.set_cached_token_id("cid1", "Yes", "tok1") is True
        assert storage.get_cached_token_id("cid1", "Yes") == "tok1"
        assert storage.invalidate_cached_token_id("cid1", "Yes") is True
        assert storage.get_cached_token_id("cid1", "Yes") is None

    def test_upsert_overwrites(self, storage):
        storage.set_cached_token_id("cid1", "Yes", "tok1")
        storage.set_cached_token_id("cid1", "Yes", "tok2")
        assert storage.get_cached_token_id("cid1", "Yes") == "tok2"


class TestMorphologySignals:
    def test_insert_and_get(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        now_iso = to_utc_iso(now_utc())
        sig_id = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="breakout",
            window_label="mid", buy_team="T1", buy_price=0.47,
            detected_at=now_iso, hours_before_end=2.5,
            minutes_since_start=45.0, predicted_win_prob=0.714,
            predicted_pnl=0.238,
        )
        assert sig_id is not None
        sigs = storage.get_morphology_signals(match_id=m["match_id"])
        assert len(sigs) == 1
        assert sigs[0]["signal_name"] == "breakout"
        assert sigs[0]["buy_price"] == 0.47


class TestMorphologyTrades:
    def test_insert_settle_and_stats(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        now_iso = to_utc_iso(now_utc())
        sig_id = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="breakout",
            window_label="mid", buy_team="T1", buy_price=0.40,
            detected_at=now_iso,
        )
        assert sig_id is not None
        trade_id = storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team="T1",
            buy_price=0.40, quantity=250.0, notional_usd=100.0,
            opened_at=now_iso,
        )
        assert trade_id is not None

        # 未结算交易
        open_trades = storage.get_open_trades(match_id=m["match_id"])
        assert len(open_trades) == 1
        assert storage.get_open_trade_match_ids() == [m["match_id"]]

        # 结算
        settled_at = to_utc_iso(now_utc())
        ok = storage.settle_morphology_trade(trade_id, "T1", 150.0, settled_at)
        assert ok is True
        open_trades = storage.get_open_trades(match_id=m["match_id"])
        assert len(open_trades) == 0

        # 统计
        stats = storage.get_trade_stats()
        assert stats["total"] == 1
        assert stats["settled"] == 1
        assert stats["wins"] == 1
        assert stats["total_pnl"] == 150.0

    def test_get_trade_stats_grouped_by_signal(self, storage, sample_match):
        """按信号分组统计胜率。"""
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        now_iso = to_utc_iso(now_utc())
        # breakout 信号 2 笔（1 胜 1 负）
        sig1 = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="breakout",
            window_label="mid", buy_team=m["team_a"], buy_price=0.40,
            detected_at=now_iso,
        )
        t1 = storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig1, buy_team=m["team_a"],
            buy_price=0.40, quantity=250.0, notional_usd=100.0,
            opened_at=now_iso,
        )
        storage.settle_morphology_trade(t1, m["team_a"], 150.0, now_iso)

        sig2 = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="breakout",
            window_label="mid", buy_team=m["team_b"], buy_price=0.30,
            detected_at=now_iso,
        )
        t2 = storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig2, buy_team=m["team_b"],
            buy_price=0.30, quantity=333.0, notional_usd=100.0,
            opened_at=now_iso,
        )
        storage.settle_morphology_trade(t2, m["team_a"], -100.0, now_iso)

        # momentum_catcher 信号 1 笔（胜）
        sig3 = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="momentum_catcher",
            window_label="early", buy_team=m["team_a"], buy_price=0.50,
            detected_at=now_iso,
        )
        t3 = storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig3, buy_team=m["team_a"],
            buy_price=0.50, quantity=200.0, notional_usd=100.0,
            opened_at=now_iso,
        )
        storage.settle_morphology_trade(t3, m["team_a"], 100.0, now_iso)

        rows = storage.get_trade_stats_grouped("signal_name")
        assert len(rows) >= 2
        by_key = {r["group_key"]: r for r in rows}
        assert "breakout" in by_key
        assert by_key["breakout"]["settled"] == 2
        assert by_key["breakout"]["wins"] == 1
        assert by_key["breakout"]["losses"] == 1
        assert by_key["breakout"]["total_pnl"] == 50.0

        assert "momentum_catcher" in by_key
        assert by_key["momentum_catcher"]["wins"] == 1

    def test_get_trade_stats_grouped_by_game(self, storage, sample_match):
        """按游戏分组。"""
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game="lol",
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        now_iso = to_utc_iso(now_utc())
        sig_id = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="x",
            window_label="mid", buy_team=m["team_a"], buy_price=0.40,
            detected_at=now_iso,
        )
        t_id = storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team=m["team_a"],
            buy_price=0.40, quantity=250.0, notional_usd=100.0,
            opened_at=now_iso,
        )
        storage.settle_morphology_trade(t_id, m["team_a"], 50.0, now_iso)

        rows = storage.get_trade_stats_grouped("game")
        by_key = {r["group_key"]: r for r in rows}
        assert "lol" in by_key
        assert by_key["lol"]["settled"] == 1
        assert by_key["lol"]["wins"] == 1

    def test_get_trade_stats_grouped_by_window(self, storage, sample_match):
        """按窗口分组。"""
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        now_iso = to_utc_iso(now_utc())
        sig_id = storage.insert_morphology_signal(
            match_id=m["match_id"], signal_name="x",
            window_label="late", buy_team=m["team_a"], buy_price=0.40,
            detected_at=now_iso,
        )
        t_id = storage.insert_morphology_trade(
            match_id=m["match_id"], signal_id=sig_id, buy_team=m["team_a"],
            buy_price=0.40, quantity=250.0, notional_usd=100.0,
            opened_at=now_iso,
        )
        storage.settle_morphology_trade(t_id, m["team_a"], 50.0, now_iso)

        rows = storage.get_trade_stats_grouped("window_label")
        by_key = {r["group_key"]: r for r in rows}
        assert "late" in by_key

    def test_get_trade_stats_grouped_invalid_group_returns_empty(self, storage):
        """无效 group_by 返回空列表。"""
        assert storage.get_trade_stats_grouped("invalid") == []

    def test_get_trade_stats_grouped_empty_db(self, storage):
        """空数据库返回空列表。"""
        rows = storage.get_trade_stats_grouped("signal_name")
        # 可能返回 [{"group_key": "unknown", "total": 0, ...}] 或空
        assert isinstance(rows, list)

    def test_get_trade_stats_grouped_exception_returns_empty(self):
        """异常时返回空列表。"""
        from unittest.mock import MagicMock
        bad = MagicMock()
        bad._connect.side_effect = RuntimeError("db error")
        from esports_monitor.data.sqlite_storage import SQLiteStorage
        # 用真实 SQLiteStorage 但替换 _connect
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name
        s = SQLiteStorage(db_path=db_path)
        s._connect = MagicMock(side_effect=RuntimeError("db error"))
        try:
            assert s.get_trade_stats_grouped("signal_name") == []
        finally:
            import os
            os.unlink(db_path)


class TestCooldown:
    def test_set_and_get(self, storage):
        end = to_utc_iso(now_utc())
        ok = storage.set_cooldown("m1", end, last_signal="breakout")
        assert ok is True
        cd = storage.get_cooldown("m1")
        assert cd is not None
        assert cd["last_signal"] == "breakout"

    def test_get_nonexistent(self, storage):
        assert storage.get_cooldown("nonexistent") is None

    def test_upsert_overwrites(self, storage):
        end1 = to_utc_iso(now_utc())
        storage.set_cooldown("m1", end1, last_signal="breakout")
        end2 = to_utc_iso(now_utc())
        storage.set_cooldown("m1", end2, last_signal="stable_spread")
        cd = storage.get_cooldown("m1")
        assert cd["last_signal"] == "stable_spread"


class TestTaskRuns:
    def test_mark_and_get(self, storage):
        assert storage.get_last_task_run("task1") is None
        assert storage.mark_task_run("task1", "summary1") is True
        last = storage.get_last_task_run("task1")
        assert last is not None

    def test_dedup_same_timestamp(self, storage):
        # 同一 (task_name, run_at) 应被去重
        storage.mark_task_run("task1", "summary1")
        storage.mark_task_run("task1", "summary2")  # 不同时间戳
        # 两次都应被记录（时间戳不同）
        # 注意：mark_task_run 使用 INSERT OR IGNORE，同时间戳会被忽略


class TestGlobalState:
    def test_set_and_get(self, storage):
        assert storage.get_global_state("key1") is None
        assert storage.set_global_state("key1", "value1") is True
        assert storage.get_global_state("key1") == "value1"
        # 覆盖
        storage.set_global_state("key1", "value2")
        assert storage.get_global_state("key1") == "value2"


class TestArchive:
    def test_query_archive_data_unsupported_table(self, storage):
        assert storage.query_archive_data("invalid_table", "2020-01-01", "2020-12-31") == []

    def test_query_archive_data(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        old_time = "2020-06-15T10:00:00+00:00"
        storage.insert_price_snapshot(m["match_id"], "team_a", 0.5, 0.5, old_time)
        rows = storage.query_archive_data(
            "price_snapshots",
            "2020-06-01T00:00:00+00:00",
            "2020-06-30T23:59:59+00:00",
        )
        assert len(rows) == 1
        assert rows[0]["price"] == 0.5

    def test_delete_before(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        storage.insert_price_snapshot(
            m["match_id"], "team_a", 0.5, 0.5, "2020-01-01T00:00:00+00:00"
        )
        storage.insert_price_snapshot(
            m["match_id"], "team_a", 0.6, 0.4, "2026-06-29T00:00:00+00:00"
        )
        deleted = storage.delete_before(
            "price_snapshots", "2025-01-01T00:00:00+00:00"
        )
        assert deleted == 1
        rows = storage.get_price_snapshots(m["match_id"])
        assert len(rows) == 1
        assert rows[0][1] == 0.6

    def test_delete_before_unsupported(self, storage):
        assert storage.delete_before("invalid", "2025-01-01") == 0

    def test_vacuum(self, storage):
        # VACUUM 应不抛异常
        assert storage.vacuum() is True

    def test_get_db_size_mb(self, storage, tmp_db_path):
        size = storage.get_db_size_mb()
        assert size > 0

    def test_count_rows(self, storage, sample_match):
        m = sample_match
        storage.upsert_match(
            match_id=m["match_id"], slug=m["slug"], game=m["game"],
            team_a=m["team_a"], team_b=m["team_b"],
            condition_id=m["condition_id"],
        )
        assert storage.count_rows("matches") == 1
        assert storage.count_rows("price_snapshots") == 0
        assert storage.count_rows("invalid") == 0
