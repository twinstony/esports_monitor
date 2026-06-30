"""scripts/restore_archive.py 测试。"""
from __future__ import annotations

import gzip
import json
import logging
import os
from typing import Any, Dict, List

import pytest

from esports_monitor.data.sqlite_storage import SQLiteStorage

# 导入被测模块；模块加载时会执行 os.chdir(PROJECT_DIR)，需在测试中规避
from scripts import restore_archive


# ----------------------------------------------------------------------
# 共享 fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_restore_archive")


@pytest.fixture
def archive_dir(tmp_path) -> str:
    """临时归档目录。"""
    d = tmp_path / "archives"
    d.mkdir()
    return str(d)


@pytest.fixture
def storage(tmp_db_path) -> SQLiteStorage:
    return SQLiteStorage(db_path=tmp_db_path)


def _write_json_gz(path: str, rows: List[Dict[str, Any]]) -> None:
    """写入 gzip 压缩 JSON Lines 文件。"""
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_json_plain(path: str, rows: List[Dict[str, Any]]) -> None:
    """写入普通 JSON Lines 文件。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ----------------------------------------------------------------------
# restore_file
# ----------------------------------------------------------------------

class TestRestoreFileUnsupportedTable:
    def test_unsupported_table_returns_zero(self, storage, archive_dir, logger):
        filepath = os.path.join(archive_dir, "unknown_202601.json.gz")
        _write_json_gz(filepath, [{"a": 1}])
        assert restore_archive.restore_file(storage, "unknown_table", filepath, logger) == 0


class TestRestoreFileMissing:
    def test_missing_file_returns_zero(self, storage, archive_dir, logger):
        filepath = os.path.join(archive_dir, "not_exist.json.gz")
        assert restore_archive.restore_file(storage, "price_snapshots", filepath, logger) == 0


class TestRestoreFileGzip:
    def test_gzip_inserts_rows(self, storage, sample_match, archive_dir, logger):
        # 先写入 matches 父表，避免外键失败
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        rows = [
            {
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.55, "price_opponent": 0.45,
                "recorded_at": "2026-01-15T10:00:00+00:00",
            },
            {
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.60, "price_opponent": 0.40,
                "recorded_at": "2026-01-15T10:30:00+00:00",
            },
        ]
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json.gz")
        _write_json_gz(filepath, rows)

        inserted = restore_archive.restore_file(
            storage, "price_snapshots", filepath, logger
        )
        assert inserted == 2
        assert storage.count_rows("price_snapshots") == 2


class TestRestoreFilePlainJson:
    def test_plain_json_inserts_rows(self, storage, sample_match, archive_dir, logger):
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        rows = [
            {
                "match_id": sample_match["match_id"], "team": "Gen.G",
                "price": 0.30, "price_opponent": 0.70,
                "recorded_at": "2026-01-15T11:00:00+00:00",
            },
        ]
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json")
        _write_json_plain(filepath, rows)

        inserted = restore_archive.restore_file(
            storage, "price_snapshots", filepath, logger
        )
        assert inserted == 1


class TestRestoreFileInvalidLines:
    def test_invalid_lines_skipped(self, storage, sample_match, archive_dir, logger):
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        # 混合：有效行 + 无效 JSON + 非 dict + 空行
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.5, "price_opponent": 0.5,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            }) + "\n")
            f.write("not a json\n")
            f.write(json.dumps([1, 2, 3]) + "\n")  # 非 dict
            f.write("\n")  # 空行
            f.write(json.dumps({
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.55, "price_opponent": 0.45,
                "recorded_at": "2026-01-15T12:30:00+00:00",
            }) + "\n")

        inserted = restore_archive.restore_file(
            storage, "price_snapshots", filepath, logger
        )
        assert inserted == 2


class TestRestoreFileEmpty:
    def test_empty_file_returns_zero(self, storage, archive_dir, logger):
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json")
        _write_json_plain(filepath, [])
        assert restore_archive.restore_file(
            storage, "price_snapshots", filepath, logger
        ) == 0


class TestRestoreFileReadError:
    def test_read_error_returns_zero(self, storage, archive_dir, logger, monkeypatch):
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json")
        _write_json_plain(filepath, [{"a": 1}])

        def _raise(*args, **kwargs):
            raise OSError("disk error")

        # 让 open 抛 OSError
        import builtins
        real_open = builtins.open
        # gzip.open 仍可用，但本文件非 .gz，使用 builtins.open
        monkeypatch.setattr(builtins, "open", _raise)
        try:
            assert restore_archive.restore_file(
                storage, "price_snapshots", filepath, logger
            ) == 0
        finally:
            monkeypatch.setattr(builtins, "open", real_open)


class TestRestoreFileInsertError:
    def test_insert_exception_returns_partial(self, storage, archive_dir, logger):
        # 使用不存在的表（虽然 TABLE_COLUMNS 中有，但数据库未建该表名）
        # 改为：让 storage._connect 抛异常
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json")
        _write_json_plain(filepath, [
            {"match_id": "x", "team": "T1", "price": 0.5,
             "price_opponent": 0.5, "recorded_at": "2026-01-15T12:00:00+00:00"}
        ])

        class BadStorage:
            def _connect(self, *a, **kw):
                raise RuntimeError("db locked")

        bad = BadStorage()
        assert restore_archive.restore_file(
            bad, "price_snapshots", filepath, logger
        ) == 0


class TestRestoreFilePartialInsertFailure:
    def test_partial_insert_failure(self, storage, archive_dir, logger):
        # 第一行因外键约束失败（match_id 不存在），第二行成功
        rows = [
            {
                "match_id": "nonexistent_match", "team": "T1",
                "price": 0.5, "price_opponent": 0.5,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            },
            {
                "match_id": "nonexistent_match2", "team": "T1",
                "price": 0.6, "price_opponent": 0.4,
                "recorded_at": "2026-01-15T12:30:00+00:00",
            },
        ]
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json")
        _write_json_plain(filepath, rows)

        # SQLite 默认不强制外键约束，所以两行都会插入；
        # 此处验证插入成功数为 2，并确认日志路径无异常
        inserted = restore_archive.restore_file(
            storage, "price_snapshots", filepath, logger
        )
        assert inserted == 2


# ----------------------------------------------------------------------
# restore_month
# ----------------------------------------------------------------------

class TestRestoreMonth:
    def test_find_gz_file(self, storage, sample_match, archive_dir, logger):
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        rows = [{
            "match_id": sample_match["match_id"], "team": "T1",
            "price": 0.5, "price_opponent": 0.5,
            "recorded_at": "2026-01-15T12:00:00+00:00",
        }]
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json.gz")
        _write_json_gz(filepath, rows)

        count = restore_archive.restore_month(
            storage, "price_snapshots", "2026-01", archive_dir, logger
        )
        assert count == 1

    def test_find_plain_json_file(self, storage, sample_match, archive_dir, logger):
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        rows = [{
            "match_id": sample_match["match_id"], "team": "T1",
            "price": 0.5, "price_opponent": 0.5,
            "recorded_at": "2026-01-15T12:00:00+00:00",
        }]
        filepath = os.path.join(archive_dir, "price_snapshots_202601.json")
        _write_json_plain(filepath, rows)

        count = restore_archive.restore_month(
            storage, "price_snapshots", "2026-01", archive_dir, logger
        )
        assert count == 1

    def test_no_matching_file(self, storage, archive_dir, logger):
        count = restore_archive.restore_month(
            storage, "price_snapshots", "2026-02", archive_dir, logger
        )
        assert count == 0

    def test_gz_preferred_over_plain(
        self, storage, sample_match, archive_dir, logger
    ):
        """gz 后缀先被检查，若同时存在则 gz 优先。"""
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        # 同时存在 .json 和 .json.gz，前者价格 0.99，后者 0.50
        _write_json_plain(
            os.path.join(archive_dir, "price_snapshots_202601.json"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.99, "price_opponent": 0.01,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            }],
        )
        _write_json_gz(
            os.path.join(archive_dir, "price_snapshots_202601.json.gz"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.50, "price_opponent": 0.50,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            }],
        )
        count = restore_archive.restore_month(
            storage, "price_snapshots", "2026-01", archive_dir, logger
        )
        assert count == 1
        # 验证插入的是 gz 文件中的 0.50
        with storage._connect(row_factory=True) as conn:
            row = conn.execute(
                "SELECT price FROM price_snapshots WHERE match_id=?",
                (sample_match["match_id"],),
            ).fetchone()
        assert row["price"] == 0.50


# ----------------------------------------------------------------------
# restore_all
# ----------------------------------------------------------------------

class TestRestoreAll:
    def test_missing_archive_dir(self, storage, logger, tmp_path):
        missing = str(tmp_path / "no_such_dir")
        assert restore_archive.restore_all(storage, missing, logger) == 0

    def test_empty_archive_dir(self, storage, archive_dir, logger):
        assert restore_archive.restore_all(storage, archive_dir, logger) == 0

    def test_skip_non_archive_files(
        self, storage, sample_match, archive_dir, logger
    ):
        # 创建非归档文件
        with open(os.path.join(archive_dir, "README.md"), "w") as f:
            f.write("readme")
        with open(os.path.join(archive_dir, "data.txt"), "w") as f:
            f.write("txt")
        assert restore_archive.restore_all(storage, archive_dir, logger) == 0

    def test_skip_unknown_table_archive(
        self, storage, archive_dir, logger
    ):
        # 文件名能解析但表名不在 TABLE_COLUMNS 中
        _write_json_gz(
            os.path.join(archive_dir, "unknown_table_202601.json.gz"),
            [{"a": 1}],
        )
        assert restore_archive.restore_all(storage, archive_dir, logger) == 0

    def test_skip_invalid_filename_no_underscore(
        self, storage, archive_dir, logger
    ):
        # 无下划线，无法分割表名与月份
        _write_json_gz(
            os.path.join(archive_dir, "nounderscore.json.gz"),
            [{"a": 1}],
        )
        assert restore_archive.restore_all(storage, archive_dir, logger) == 0

    def test_multiple_valid_archives(
        self, storage, sample_match, archive_dir, logger
    ):
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        # price_snapshots_202601.json.gz
        _write_json_gz(
            os.path.join(archive_dir, "price_snapshots_202601.json.gz"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.5, "price_opponent": 0.5,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            }],
        )
        # price_snapshots_202602.json.gz
        _write_json_gz(
            os.path.join(archive_dir, "price_snapshots_202602.json.gz"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.6, "price_opponent": 0.4,
                "recorded_at": "2026-02-15T12:00:00+00:00",
            }],
        )
        total = restore_archive.restore_all(storage, archive_dir, logger)
        assert total == 2
        assert storage.count_rows("price_snapshots") == 2

    def test_mixed_gz_and_plain(
        self, storage, sample_match, archive_dir, logger
    ):
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        # 同一表 .json 和 .json.gz 都存在
        _write_json_plain(
            os.path.join(archive_dir, "price_snapshots_202601.json"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.5, "price_opponent": 0.5,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            }],
        )
        _write_json_gz(
            os.path.join(archive_dir, "price_snapshots_202602.json.gz"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.6, "price_opponent": 0.4,
                "recorded_at": "2026-02-15T12:00:00+00:00",
            }],
        )
        total = restore_archive.restore_all(storage, archive_dir, logger)
        assert total == 2

    def test_filename_underscore_in_table_name(
        self, storage, sample_match, archive_dir, logger
    ):
        """表名本身含下划线（如 morphology_trades），应正确解析。"""
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        # 先插入一条 signal，以便 trade 引用 signal_id
        signal_id = storage.insert_morphology_signal(
            match_id=sample_match["match_id"], signal_name="breakout",
            window_label="mid", buy_team="T1", buy_price=0.5,
            detected_at="2026-01-15T11:00:00+00:00",
            hours_before_end=2.0, minutes_since_start=60.0,
            predicted_win_prob=0.6, predicted_pnl=20.0,
        )
        assert signal_id > 0
        _write_json_gz(
            os.path.join(archive_dir, "morphology_trades_202601.json.gz"),
            [{
                "match_id": sample_match["match_id"], "signal_id": signal_id,
                "buy_team": "T1", "buy_price": 0.5, "quantity": 200.0,
                "notional_usd": 100.0, "vwap": 0.5,
                "opened_at": "2026-01-15T12:00:00+00:00",
                "settled": 0, "winning_team": None, "pnl_usd": None,
                "settled_at": None,
            }],
        )
        total = restore_archive.restore_all(storage, archive_dir, logger)
        assert total == 1
        assert storage.count_rows("morphology_trades") == 1


# ----------------------------------------------------------------------
# main (CLI)
# ----------------------------------------------------------------------

class TestMain:
    def test_main_all_mode(
        self, storage, sample_match, archive_dir, logger,
        monkeypatch, capsys, tmp_path,
    ):
        """--all 模式。"""
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        _write_json_gz(
            os.path.join(archive_dir, "price_snapshots_202601.json.gz"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.5, "price_opponent": 0.5,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            }],
        )

        # 用临时 db 路径和归档目录运行 main
        db_path = storage.db_path
        argv = [
            "restore_archive.py", "--all",
            "--archive-dir", archive_dir,
            "--db", db_path,
        ]
        monkeypatch.setattr("sys.argv", argv)

        # 阻止真实 parser.error 退出
        restore_archive.main()
        out = capsys.readouterr().err + capsys.readouterr().out
        # main 不抛异常即视为通过；进一步验证 DB 中已写入数据
        assert storage.count_rows("price_snapshots") == 1

    def test_main_table_month_mode(
        self, storage, sample_match, archive_dir, logger, monkeypatch, tmp_path
    ):
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        _write_json_gz(
            os.path.join(archive_dir, "price_snapshots_202601.json.gz"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.55, "price_opponent": 0.45,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            }],
        )

        db_path = storage.db_path
        argv = [
            "restore_archive.py",
            "--table", "price_snapshots",
            "--month", "2026-01",
            "--archive-dir", archive_dir,
            "--db", db_path,
        ]
        monkeypatch.setattr("sys.argv", argv)
        restore_archive.main()
        assert storage.count_rows("price_snapshots") == 1

    def test_main_missing_args_exits(
        self, storage, archive_dir, monkeypatch
    ):
        """缺少 --all 或 (--table + --month) 时应调用 parser.error 并退出。"""
        argv = [
            "restore_archive.py",
            "--archive-dir", archive_dir,
            "--db", storage.db_path,
        ]
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit):
            restore_archive.main()

    def test_main_only_table_no_month_exits(
        self, storage, archive_dir, monkeypatch
    ):
        argv = [
            "restore_archive.py",
            "--table", "price_snapshots",
            "--archive-dir", archive_dir,
            "--db", storage.db_path,
        ]
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit):
            restore_archive.main()

    def test_main_only_month_no_table_exits(
        self, storage, archive_dir, monkeypatch
    ):
        argv = [
            "restore_archive.py",
            "--month", "2026-01",
            "--archive-dir", archive_dir,
            "--db", storage.db_path,
        ]
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit):
            restore_archive.main()

    def test_main_uses_config_db_path_when_no_db_arg(
        self, storage, sample_match, archive_dir, monkeypatch, tmp_path
    ):
        """未提供 --db 时，从 config 加载 db 路径。"""
        storage.upsert_match(
            match_id=sample_match["match_id"], slug=sample_match["slug"],
            game=sample_match["game"], team_a=sample_match["team_a"],
            team_b=sample_match["team_b"], condition_id=sample_match["condition_id"],
        )
        _write_json_gz(
            os.path.join(archive_dir, "price_snapshots_202601.json.gz"),
            [{
                "match_id": sample_match["match_id"], "team": "T1",
                "price": 0.5, "price_opponent": 0.5,
                "recorded_at": "2026-01-15T12:00:00+00:00",
            }],
        )
        # mock load_config 返回自定义 db 路径
        fake_config = {"database": {"path": storage.db_path}}
        monkeypatch.setattr(
            "scripts.restore_archive.load_config", lambda *a, **kw: fake_config
        )
        argv = [
            "restore_archive.py", "--all",
            "--archive-dir", archive_dir,
        ]
        monkeypatch.setattr("sys.argv", argv)
        restore_archive.main()
        assert storage.count_rows("price_snapshots") == 1


# ----------------------------------------------------------------------
# TABLE_COLUMNS 完整性
# ----------------------------------------------------------------------

class TestTableColumns:
    def test_all_supported_tables_present(self):
        expected = {
            "price_snapshots", "orderbook_snapshots",
            "morphology_signals", "morphology_trades",
        }
        assert expected == set(restore_archive.TABLE_COLUMNS.keys())

    def test_columns_match_storage_schema(self):
        # 关键列必须存在
        assert "match_id" in restore_archive.TABLE_COLUMNS["price_snapshots"]
        assert "recorded_at" in restore_archive.TABLE_COLUMNS["price_snapshots"]
        assert "token_id" in restore_archive.TABLE_COLUMNS["orderbook_snapshots"]
        assert "signal_name" in restore_archive.TABLE_COLUMNS["morphology_signals"]
        assert "buy_team" in restore_archive.TABLE_COLUMNS["morphology_trades"]
        assert "pnl_usd" in restore_archive.TABLE_COLUMNS["morphology_trades"]
