"""SQLite 存储层。

参照：polymarket_twitter_monitor/data/sqlite_storage.py

设计原则：
- 使用 @contextmanager 管理连接，每次操作打开/关闭
- row_factory=True 返回 Row 对象
- 所有写入操作 try/except，失败仅记日志不抛异常
- 所有表 CREATE TABLE IF NOT EXISTS

包含：
- 比赛元数据表 matches
- 价格快照表 price_snapshots
- 盘口快照表 orderbook_snapshots
- token 缓存表 clob_token_cache
- 形态信号表 morphology_signals
- 模拟交易表 morphology_trades
- 冷却状态表 morphology_cooldown
- 任务去重表 task_runs
- 全局状态表 global_state

并提供数据归档相关方法（导出/删除/VACUUM）。
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..utils.time_utils import now_utc, to_utc_iso

DEFAULT_DB_PATH = "esports_history.db"


class SQLiteStorage:
    """SQLite 持久化层。

    所有公开方法均不抛异常——DB 错误返回 False/None/[]，
    保证主循环不被中断。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._logger = logging.getLogger(__name__)
        self._init_db()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _connect(self, row_factory: bool = False) -> Iterator[sqlite3.Connection]:
        """打开并关闭连接的上下文管理器。"""
        conn = sqlite3.connect(self.db_path)
        if row_factory:
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 表结构初始化
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """初始化所有表结构。失败仅记日志，不抛异常。"""
        try:
            with self._connect() as conn:
                cur = conn.cursor()

                # 5.2.1 比赛元数据表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS matches (
                        match_id        TEXT PRIMARY KEY,
                        slug            TEXT NOT NULL,
                        game            TEXT NOT NULL,
                        league          TEXT,
                        team_a          TEXT NOT NULL,
                        team_b          TEXT NOT NULL,
                        condition_id    TEXT NOT NULL,
                        token_id_a      TEXT,
                        token_id_b      TEXT,
                        start_time      TEXT,
                        end_time        TEXT,
                        status          TEXT DEFAULT 'discovered',
                        winning_team    TEXT,
                        discovered_at   TEXT NOT NULL,
                        updated_at      TEXT NOT NULL
                    )
                    """
                )

                # 5.2.2 价格快照表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS price_snapshots (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_id        TEXT NOT NULL,
                        team            TEXT NOT NULL,
                        price           REAL NOT NULL,
                        price_opponent  REAL,
                        recorded_at     TEXT NOT NULL,
                        FOREIGN KEY (match_id) REFERENCES matches(match_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_price_snapshots_match_time "
                    "ON price_snapshots(match_id, recorded_at)"
                )

                # 5.2.3 盘口快照表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_id        TEXT NOT NULL,
                        team            TEXT NOT NULL,
                        token_id        TEXT NOT NULL,
                        order_book_json TEXT NOT NULL,
                        best_bid        REAL,
                        best_ask        REAL,
                        bid_depth       REAL,
                        ask_depth       REAL,
                        spread          REAL,
                        recorded_at     TEXT NOT NULL,
                        FOREIGN KEY (match_id) REFERENCES matches(match_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_match_time "
                    "ON orderbook_snapshots(match_id, recorded_at)"
                )

                # 5.2.4 token 缓存表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS clob_token_cache (
                        condition_id    TEXT NOT NULL,
                        outcome         TEXT NOT NULL,
                        token_id        TEXT NOT NULL,
                        cached_at       TEXT NOT NULL,
                        PRIMARY KEY (condition_id, outcome)
                    )
                    """
                )

                # 5.2.5 形态信号表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS morphology_signals (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_id            TEXT NOT NULL,
                        signal_name         TEXT NOT NULL,
                        window_label        TEXT NOT NULL,
                        buy_team            TEXT NOT NULL,
                        buy_price           REAL NOT NULL,
                        hours_before_end    REAL,
                        minutes_since_start REAL,
                        predicted_win_prob  REAL,
                        predicted_pnl       REAL,
                        detected_at         TEXT NOT NULL,
                        FOREIGN KEY (match_id) REFERENCES matches(match_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_morphology_signals_match "
                    "ON morphology_signals(match_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_morphology_signals_signal "
                    "ON morphology_signals(signal_name)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_morphology_signals_time "
                    "ON morphology_signals(detected_at)"
                )

                # 5.2.6 模拟交易表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS morphology_trades (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_id        TEXT NOT NULL,
                        signal_id       INTEGER NOT NULL,
                        buy_team        TEXT NOT NULL,
                        buy_price       REAL NOT NULL,
                        quantity        REAL NOT NULL,
                        notional_usd    REAL NOT NULL,
                        vwap            REAL,
                        opened_at       TEXT NOT NULL,
                        settled         INTEGER DEFAULT 0,
                        winning_team    TEXT,
                        pnl_usd         REAL,
                        settled_at      TEXT,
                        FOREIGN KEY (match_id) REFERENCES matches(match_id),
                        FOREIGN KEY (signal_id) REFERENCES morphology_signals(id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_morphology_trades_match "
                    "ON morphology_trades(match_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_morphology_trades_settled "
                    "ON morphology_trades(settled)"
                )

                # 5.2.7 冷却状态表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS morphology_cooldown (
                        match_id        TEXT PRIMARY KEY,
                        cooldown_end    TEXT NOT NULL,
                        last_signal     TEXT,
                        updated_at      TEXT NOT NULL
                    )
                    """
                )

                # 5.2.8 任务去重表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_runs (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_name       TEXT NOT NULL,
                        run_at          TEXT NOT NULL,
                        result_summary  TEXT,
                        UNIQUE(task_name, run_at)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_runs_name_time "
                    "ON task_runs(task_name, run_at DESC)"
                )

                # 5.2.9 全局状态表
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS global_state (
                        key             TEXT PRIMARY KEY,
                        value           TEXT,
                        updated_at      TEXT NOT NULL
                    )
                    """
                )

                conn.commit()
        except Exception as exc:
            self._logger.error("初始化数据库表结构失败: %s", exc)

    # ------------------------------------------------------------------
    # matches 表操作
    # ------------------------------------------------------------------

    def upsert_match(
        self,
        match_id: str,
        slug: str,
        game: str,
        team_a: str,
        team_b: str,
        condition_id: str,
        league: Optional[str] = None,
        token_id_a: Optional[str] = None,
        token_id_b: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        status: str = "discovered",
        winning_team: Optional[str] = None,
    ) -> bool:
        """插入或更新比赛。已存在则保留 discovered_at，更新其余字段。"""
        try:
            now = to_utc_iso(now_utc())
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO matches (
                        match_id, slug, game, league, team_a, team_b,
                        condition_id, token_id_a, token_id_b,
                        start_time, end_time, status, winning_team,
                        discovered_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id) DO UPDATE SET
                        slug = excluded.slug,
                        game = excluded.game,
                        league = excluded.league,
                        team_a = excluded.team_a,
                        team_b = excluded.team_b,
                        condition_id = excluded.condition_id,
                        token_id_a = COALESCE(excluded.token_id_a, matches.token_id_a),
                        token_id_b = COALESCE(excluded.token_id_b, matches.token_id_b),
                        start_time = COALESCE(excluded.start_time, matches.start_time),
                        end_time = COALESCE(excluded.end_time, matches.end_time),
                        status = excluded.status,
                        winning_team = COALESCE(excluded.winning_team, matches.winning_team),
                        updated_at = excluded.updated_at
                    """,
                    (
                        match_id, slug, game, league, team_a, team_b,
                        condition_id, token_id_a, token_id_b,
                        start_time, end_time, status, winning_team,
                        now, now,
                    ),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("upsert_match 失败: %s", exc)
            return False

    def update_match_status(
        self, match_id: str, status: str, winning_team: Optional[str] = None
    ) -> bool:
        """更新比赛状态。"""
        try:
            now = to_utc_iso(now_utc())
            with self._connect() as conn:
                if winning_team is not None:
                    conn.execute(
                        "UPDATE matches SET status=?, winning_team=?, updated_at=? "
                        "WHERE match_id=?",
                        (status, winning_team, now, match_id),
                    )
                else:
                    conn.execute(
                        "UPDATE matches SET status=?, updated_at=? WHERE match_id=?",
                        (status, now, match_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("update_match_status 失败: %s", exc)
            return False

    def get_match(self, match_id: str) -> Optional[Dict[str, Any]]:
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute("SELECT * FROM matches WHERE match_id=?", (match_id,))
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            self._logger.error("get_match 失败: %s", exc)
            return None

    def get_matches_by_status(self, status: str) -> List[Dict[str, Any]]:
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(
                    "SELECT * FROM matches WHERE status=? ORDER BY discovered_at",
                    (status,),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            self._logger.error("get_matches_by_status 失败: %s", exc)
            return []

    def get_live_matches(self) -> List[Dict[str, Any]]:
        """获取监控中的比赛（status 为 discovered 或 live）。"""
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(
                    "SELECT * FROM matches WHERE status IN ('discovered','live') "
                    "ORDER BY discovered_at"
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            self._logger.error("get_live_matches 失败: %s", exc)
            return []

    def get_all_matches(self) -> List[Dict[str, Any]]:
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute("SELECT * FROM matches ORDER BY discovered_at")
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            self._logger.error("get_all_matches 失败: %s", exc)
            return []

    # ------------------------------------------------------------------
    # price_snapshots 表操作
    # ------------------------------------------------------------------

    def insert_price_snapshot(
        self,
        match_id: str,
        team: str,
        price: float,
        price_opponent: Optional[float],
        recorded_at: str,
    ) -> bool:
        """插入价格快照。失败仅记日志，不抛异常。"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO price_snapshots "
                    "(match_id, team, price, price_opponent, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (match_id, team, price, price_opponent, recorded_at),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("insert_price_snapshot 失败: %s", exc)
            return False

    def get_price_snapshots(
        self, match_id: str, team: Optional[str] = None, hours: Optional[float] = None
    ) -> List[Tuple[str, float]]:
        """获取价格快照，返回 [(recorded_at, price), ...] 升序。

        Args:
            match_id: 比赛主键
            team: 可选，过滤 team
            hours: 可选，只返回最近 N 小时数据
        """
        try:
            sql = ["SELECT recorded_at, price FROM price_snapshots WHERE match_id=?"]
            params: list = [match_id]
            if team:
                sql.append("AND team=?")
                params.append(team)
            if hours is not None and hours > 0:
                sql.append("AND recorded_at >= datetime('now', ? || ' hours')")
                params.append(f"-{hours}")
            sql.append("ORDER BY recorded_at ASC")
            with self._connect() as conn:
                cur = conn.execute(" ".join(sql), params)
                return [(row[0], row[1]) for row in cur.fetchall()]
        except Exception as exc:
            self._logger.error("get_price_snapshots 失败: %s", exc)
            return []

    # ------------------------------------------------------------------
    # orderbook_snapshots 表操作
    # ------------------------------------------------------------------

    def insert_orderbook_snapshot(
        self,
        match_id: str,
        team: str,
        token_id: str,
        order_book_json: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
        bid_depth: Optional[float],
        ask_depth: Optional[float],
        spread: Optional[float],
        recorded_at: str,
    ) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO orderbook_snapshots "
                    "(match_id, team, token_id, order_book_json, "
                    "best_bid, best_ask, bid_depth, ask_depth, spread, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        match_id, team, token_id, order_book_json,
                        best_bid, best_ask, bid_depth, ask_depth, spread, recorded_at,
                    ),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("insert_orderbook_snapshot 失败: %s", exc)
            return False

    # ------------------------------------------------------------------
    # clob_token_cache 表操作
    # ------------------------------------------------------------------

    def get_cached_token_id(
        self, condition_id: str, outcome: str
    ) -> Optional[str]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT token_id FROM clob_token_cache "
                    "WHERE condition_id=? AND outcome=?",
                    (condition_id, outcome),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as exc:
            self._logger.error("get_cached_token_id 失败: %s", exc)
            return None

    def set_cached_token_id(
        self, condition_id: str, outcome: str, token_id: str
    ) -> bool:
        try:
            now = to_utc_iso(now_utc())
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO clob_token_cache (condition_id, outcome, token_id, cached_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(condition_id, outcome) DO UPDATE SET
                        token_id = excluded.token_id,
                        cached_at = excluded.cached_at
                    """,
                    (condition_id, outcome, token_id, now),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("set_cached_token_id 失败: %s", exc)
            return False

    def invalidate_cached_token_id(
        self, condition_id: str, outcome: str
    ) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM clob_token_cache "
                    "WHERE condition_id=? AND outcome=?",
                    (condition_id, outcome),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("invalidate_cached_token_id 失败: %s", exc)
            return False

    # ------------------------------------------------------------------
    # morphology_signals 表操作
    # ------------------------------------------------------------------

    def insert_morphology_signal(
        self,
        match_id: str,
        signal_name: str,
        window_label: str,
        buy_team: str,
        buy_price: float,
        detected_at: str,
        hours_before_end: Optional[float] = None,
        minutes_since_start: Optional[float] = None,
        predicted_win_prob: Optional[float] = None,
        predicted_pnl: Optional[float] = None,
    ) -> Optional[int]:
        """插入形态信号，返回信号 id；失败返回 None。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO morphology_signals (
                        match_id, signal_name, window_label, buy_team, buy_price,
                        hours_before_end, minutes_since_start,
                        predicted_win_prob, predicted_pnl, detected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match_id, signal_name, window_label, buy_team, buy_price,
                        hours_before_end, minutes_since_start,
                        predicted_win_prob, predicted_pnl, detected_at,
                    ),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as exc:
            self._logger.error("insert_morphology_signal 失败: %s", exc)
            return None

    def get_morphology_signals(
        self, match_id: Optional[str] = None, hours: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        try:
            sql = ["SELECT * FROM morphology_signals WHERE 1=1"]
            params: list = []
            if match_id:
                sql.append("AND match_id=?")
                params.append(match_id)
            if hours is not None and hours > 0:
                sql.append("AND detected_at >= datetime('now', ? || ' hours')")
                params.append(f"-{hours}")
            sql.append("ORDER BY detected_at DESC")
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(" ".join(sql), params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            self._logger.error("get_morphology_signals 失败: %s", exc)
            return []

    # ------------------------------------------------------------------
    # morphology_trades 表操作
    # ------------------------------------------------------------------

    def insert_morphology_trade(
        self,
        match_id: str,
        signal_id: int,
        buy_team: str,
        buy_price: float,
        quantity: float,
        notional_usd: float,
        opened_at: str,
        vwap: Optional[float] = None,
    ) -> Optional[int]:
        """插入模拟交易，返回交易 id；失败返回 None。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO morphology_trades (
                        match_id, signal_id, buy_team, buy_price,
                        quantity, notional_usd, vwap, opened_at, settled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        match_id, signal_id, buy_team, buy_price,
                        quantity, notional_usd, vwap, opened_at,
                    ),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as exc:
            self._logger.error("insert_morphology_trade 失败: %s", exc)
            return None

    def settle_morphology_trade(
        self,
        trade_id: int,
        winning_team: str,
        pnl_usd: float,
        settled_at: str,
    ) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE morphology_trades SET settled=1, winning_team=?, "
                    "pnl_usd=?, settled_at=? WHERE id=?",
                    (winning_team, pnl_usd, settled_at, trade_id),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("settle_morphology_trade 失败: %s", exc)
            return False

    def get_open_trades(self, match_id: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            sql = ["SELECT * FROM morphology_trades WHERE settled=0"]
            params: list = []
            if match_id:
                sql.append("AND match_id=?")
                params.append(match_id)
            sql.append("ORDER BY opened_at")
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(" ".join(sql), params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            self._logger.error("get_open_trades 失败: %s", exc)
            return []

    def get_open_trade_match_ids(self) -> List[str]:
        """获取所有有未结算交易的比赛 id。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT DISTINCT match_id FROM morphology_trades WHERE settled=0"
                )
                return [row[0] for row in cur.fetchall()]
        except Exception as exc:
            self._logger.error("get_open_trade_match_ids 失败: %s", exc)
            return []

    def get_trade_stats(self) -> Dict[str, Any]:
        """获取交易汇总统计。"""
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS settled,
                        SUM(CASE WHEN settled=1 AND pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN settled=1 AND pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
                        COALESCE(SUM(CASE WHEN settled=1 THEN pnl_usd ELSE 0 END), 0) AS total_pnl
                    FROM morphology_trades
                    """
                )
                row = cur.fetchone()
                if not row:
                    return {"total": 0, "settled": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
                result = dict(row)
                # SUM 可能返回 None（无结算记录时），统一为 0
                for k in ("settled", "wins", "losses"):
                    if result.get(k) is None:
                        result[k] = 0
                if result.get("total_pnl") is None:
                    result["total_pnl"] = 0.0
                return result
        except Exception as exc:
            self._logger.error("get_trade_stats 失败: %s", exc)
            return {"total": 0, "settled": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

    def get_trade_stats_grouped(self, group_by: str = "signal_name") -> List[Dict[str, Any]]:
        """按维度分组获取交易统计。

        Args:
            group_by: 分组维度，支持 "signal_name" / "game" / "window_label"
                - signal_name: 按 morphology_signals.signal_name 分组
                - game: 按 matches.game 分组
                - window_label: 按 morphology_signals.window_label 分组

        Returns:
            [{"group_key": "breakout", "total": 10, "settled": 8, "wins": 5,
              "losses": 3, "total_pnl": 12.5}, ...]
        """
        join_clauses = {
            "signal_name": (
                "LEFT JOIN morphology_signals ON morphology_trades.signal_id = morphology_signals.id "
                "GROUP BY morphology_signals.signal_name"
            ),
            "game": (
                "LEFT JOIN morphology_signals ON morphology_trades.signal_id = morphology_signals.id "
                "LEFT JOIN matches ON morphology_trades.match_id = matches.match_id "
                "GROUP BY matches.game"
            ),
            "window_label": (
                "LEFT JOIN morphology_signals ON morphology_trades.signal_id = morphology_signals.id "
                "GROUP BY morphology_signals.window_label"
            ),
        }
        join_sql = join_clauses.get(group_by)
        if not join_sql:
            return []

        # 取分组键列名
        key_col = {
            "signal_name": "morphology_signals.signal_name",
            "game": "matches.game",
            "window_label": "morphology_signals.window_label",
        }[group_by]

        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(
                    f"""
                    SELECT
                        {key_col} AS group_key,
                        COUNT(*) AS total,
                        SUM(CASE WHEN morphology_trades.settled=1 THEN 1 ELSE 0 END) AS settled,
                        SUM(CASE WHEN morphology_trades.settled=1 AND morphology_trades.pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN morphology_trades.settled=1 AND morphology_trades.pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
                        COALESCE(SUM(CASE WHEN morphology_trades.settled=1 THEN morphology_trades.pnl_usd ELSE 0 END), 0) AS total_pnl
                    FROM morphology_trades
                    {join_sql}
                    """
                )
                rows = cur.fetchall()
                result = []
                for row in rows:
                    d = dict(row)
                    d["group_key"] = d.get("group_key") or "unknown"
                    for k in ("total", "settled", "wins", "losses"):
                        if d.get(k) is None:
                            d[k] = 0
                    if d.get("total_pnl") is None:
                        d["total_pnl"] = 0.0
                    result.append(d)
                return result
        except Exception as exc:
            self._logger.error("get_trade_stats_grouped 失败: %s", exc)
            return []

    def get_daily_trade_stats(self, date_str: str) -> Dict[str, Any]:
        """获取指定日期的交易统计。

        Args:
            date_str: 日期（YYYY-MM-DD）

        Returns:
            {"total": N, "settled": N, "wins": N, "losses": N, "total_pnl": 0.0}
        """
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS settled,
                        SUM(CASE WHEN settled=1 AND pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN settled=1 AND pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
                        COALESCE(SUM(CASE WHEN settled=1 THEN pnl_usd ELSE 0 END), 0) AS total_pnl
                    FROM morphology_trades
                    WHERE DATE(opened_at) = ?
                    """,
                    (date_str,),
                )
                row = cur.fetchone()
                if not row:
                    return {"total": 0, "settled": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
                result = dict(row)
                for k in ("total", "settled", "wins", "losses"):
                    if result.get(k) is None:
                        result[k] = 0
                if result.get("total_pnl") is None:
                    result["total_pnl"] = 0.0
                return result
        except Exception as exc:
            self._logger.error("get_daily_trade_stats 失败: %s", exc)
            return {"total": 0, "settled": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

    def get_recent_trades_with_details(self, since_iso: str) -> List[Dict[str, Any]]:
        """获取自 since_iso 以来开仓的模拟交易逐单详情（含信号依据与比赛信息）。

        Args:
            since_iso: UTC ISO 时间字符串，查询 opened_at >= since_iso 的交易

        Returns:
            [{"id", "match_id", "game", "team_a", "team_b", "buy_team", "buy_price",
              "notional_usd", "opened_at", "settled", "pnl_usd", "settled_at",
              "signal_name", "window_label", "minutes_since_start"}, ...]
            按 opened_at 升序排列。
        """
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(
                    """
                    SELECT
                        t.id AS id,
                        t.match_id AS match_id,
                        m.game AS game,
                        m.team_a AS team_a,
                        m.team_b AS team_b,
                        t.buy_team AS buy_team,
                        t.buy_price AS buy_price,
                        t.notional_usd AS notional_usd,
                        t.opened_at AS opened_at,
                        t.settled AS settled,
                        t.pnl_usd AS pnl_usd,
                        t.settled_at AS settled_at,
                        s.signal_name AS signal_name,
                        s.window_label AS window_label,
                        s.minutes_since_start AS minutes_since_start
                    FROM morphology_trades t
                    LEFT JOIN morphology_signals s ON t.signal_id = s.id
                    LEFT JOIN matches m ON t.match_id = m.match_id
                    WHERE t.opened_at >= ?
                    ORDER BY t.opened_at ASC
                    """,
                    (since_iso,),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            self._logger.error("get_recent_trades_with_details 失败: %s", exc)
            return []

    def get_recent_monitored_match_count(self, since_iso: str) -> int:
        """获取自 since_iso 以来有价格快照记录的比赛数（即被监控的比赛数）。

        Args:
            since_iso: UTC ISO 时间字符串

        Returns:
            比赛数量
        """
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    SELECT COUNT(DISTINCT match_id) AS cnt
                    FROM price_snapshots
                    WHERE recorded_at >= ?
                    """,
                    (since_iso,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            self._logger.error("get_recent_monitored_match_count 失败: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # morphology_cooldown 表操作
    # ------------------------------------------------------------------

    def set_cooldown(
        self,
        match_id: str,
        cooldown_end: str,
        last_signal: Optional[str] = None,
    ) -> bool:
        try:
            now = to_utc_iso(now_utc())
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO morphology_cooldown (match_id, cooldown_end, last_signal, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(match_id) DO UPDATE SET
                        cooldown_end = excluded.cooldown_end,
                        last_signal = excluded.last_signal,
                        updated_at = excluded.updated_at
                    """,
                    (match_id, cooldown_end, last_signal, now),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("set_cooldown 失败: %s", exc)
            return False

    def get_cooldown(self, match_id: str) -> Optional[Dict[str, Any]]:
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(
                    "SELECT * FROM morphology_cooldown WHERE match_id=?",
                    (match_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            self._logger.error("get_cooldown 失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # task_runs 表操作（任务去重）
    # ------------------------------------------------------------------

    def mark_task_run(self, task_name: str, summary: Optional[str] = None) -> bool:
        try:
            now = to_utc_iso(now_utc())
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO task_runs (task_name, run_at, result_summary) "
                    "VALUES (?, ?, ?)",
                    (task_name, now, summary),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("mark_task_run 失败: %s", exc)
            return False

    def get_last_task_run(self, task_name: str) -> Optional[str]:
        """获取任务最近一次运行的 run_at（UTC ISO 字符串）。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT run_at FROM task_runs WHERE task_name=? "
                    "ORDER BY run_at DESC LIMIT 1",
                    (task_name,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as exc:
            self._logger.error("get_last_task_run 失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # global_state 表操作
    # ------------------------------------------------------------------

    def set_global_state(self, key: str, value: str) -> bool:
        try:
            now = to_utc_iso(now_utc())
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO global_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, now),
                )
                conn.commit()
            return True
        except Exception as exc:
            self._logger.error("set_global_state 失败: %s", exc)
            return False

    def get_global_state(self, key: str) -> Optional[str]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT value FROM global_state WHERE key=?", (key,)
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as exc:
            self._logger.error("get_global_state 失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 数据归档相关
    # ------------------------------------------------------------------

    def query_archive_data(
        self, table_name: str, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        """查询指定时间范围的归档数据。

        Args:
            table_name: 表名（price_snapshots / orderbook_snapshots /
                        morphology_signals / morphology_trades）
            start_date: 起始时间（UTC ISO 字符串）
            end_date: 结束时间（UTC ISO 字符串）

        Returns:
            字典列表，每条记录含所有列。
        """
        allowed = {
            "price_snapshots": "recorded_at",
            "orderbook_snapshots": "recorded_at",
            "morphology_signals": "detected_at",
            "morphology_trades": "opened_at",
        }
        if table_name not in allowed:
            self._logger.error("不支持的归档表: %s", table_name)
            return []
        time_col = allowed[table_name]
        try:
            with self._connect(row_factory=True) as conn:
                cur = conn.execute(
                    f"SELECT * FROM {table_name} "
                    f"WHERE {time_col} >= ? AND {time_col} <= ? "
                    f"ORDER BY {time_col}",
                    (start_date, end_date),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            self._logger.error("query_archive_data 失败: %s", exc)
            return []

    def delete_before(self, table_name: str, cutoff_iso: str) -> int:
        """删除指定时间之前的记录。

        Args:
            table_name: 表名
            cutoff_iso: 截止时间（UTC ISO 字符串），<= 该时间的记录被删除

        Returns:
            删除的行数；失败返回 0。
        """
        allowed = {
            "price_snapshots": "recorded_at",
            "orderbook_snapshots": "recorded_at",
            "morphology_signals": "detected_at",
            "morphology_trades": "opened_at",
            "morphology_cooldown": "updated_at",
            "task_runs": "run_at",
        }
        if table_name not in allowed:
            self._logger.error("不支持的清理表: %s", table_name)
            return 0
        time_col = allowed[table_name]
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    f"DELETE FROM {table_name} WHERE {time_col} <= ?",
                    (cutoff_iso,),
                )
                conn.commit()
                return cur.rowcount or 0
        except Exception as exc:
            self._logger.error("delete_before 失败: %s", exc)
            return 0

    def vacuum(self) -> bool:
        """执行 VACUUM 回收磁盘空间。

        注意：VACUUM 会锁定数据库，执行期间无法读写。
        应在低峰期执行。
        """
        try:
            with self._connect() as conn:
                conn.execute("VACUUM")
            self._logger.info("Database VACUUM completed")
            return True
        except Exception as exc:
            self._logger.error("VACUUM 失败: %s", exc)
            return False

    def get_db_size_mb(self) -> float:
        """获取数据库文件大小（MB）。"""
        try:
            return os.path.getsize(self.db_path) / (1024 * 1024)
        except OSError:
            return 0.0

    def count_rows(self, table_name: str) -> int:
        """统计表行数（用于状态报告）。"""
        allowed = {
            "price_snapshots",
            "orderbook_snapshots",
            "morphology_signals",
            "morphology_trades",
            "matches",
        }
        if table_name not in allowed:
            return 0
        try:
            with self._connect() as conn:
                cur = conn.execute(f"SELECT COUNT(*) FROM {table_name}")
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            self._logger.error("count_rows 失败: %s", exc)
            return 0
