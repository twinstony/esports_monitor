#!/usr/bin/env python3
"""清理非目标游戏的旧数据，并重置市场发现任务记录。

用途：当监控范围缩小（如仅保留 LoL，移除 cs2/dota2）后，
清除历史遗留的非目标游戏数据，让下一轮发现从干净状态开始。

清理范围（按依赖顺序）：
1. morphology_trades        → 通过 signal_id 关联 morphology_signals
2. morphology_signals       → 通过 match_id 关联 matches
3. morphology_cooldown      → 通过 match_id 关联 matches
4. orderbook_snapshots      → 通过 match_id 关联 matches
5. price_snapshots          → 通过 match_id 关联 matches
6. clob_token_cache         → 通过 condition_id 关联（按 game 间接）
7. matches                  → 仅删除 game IN (排除列表) 的行
8. task_runs                → 清除 market_discovery 记录，强制下次运行

用法：
    # 默认清理 cs2/dota2（保留 lol）
    python scripts/reset_and_rediscover.py

    # 指定要删除的游戏列表
    python scripts/reset_and_rediscover.py --games cs2 dota2

    # 仅预览将删除的数据量，不实际删除
    python scripts/reset_and_rediscover.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 优先使用 DB_PATH 环境变量（容器部署时通常为 /app/data/esports_history.db），
# 其次回退到项目根目录下的 esports_history.db
DEFAULT_DB = os.environ.get("DB_PATH") or os.path.join(
    PROJECT_DIR, "esports_history.db"
)


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return int(row[0]) if row else 0


def main():
    parser = argparse.ArgumentParser(description="清理非目标游戏的旧数据")
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"数据库路径（默认 {DEFAULT_DB}）"
    )
    parser.add_argument(
        "--games", nargs="+", default=["cs2", "dota2"],
        help="要删除的游戏列表（默认 cs2 dota2）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅预览将删除的数据量，不实际删除",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"[ERROR] 数据库不存在: {args.db}")
        sys.exit(1)

    games_to_delete = tuple(args.games)
    placeholders = ",".join("?" * len(games_to_delete))

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = OFF")

    # 1. 统计将删除的数据量
    print(f"\n[清理目标] 游戏: {list(games_to_delete)}")
    print(f"[数据库] {args.db}")
    if args.dry_run:
        print("[模式] dry-run（仅预览，不删除）\n")
    else:
        print("[模式] 实际删除\n")

    match_ids_sql = (
        f"SELECT match_id FROM matches WHERE game IN ({placeholders})",
        games_to_delete,
    )

    # 统计各表将删除的行数
    stats = {}
    match_count = _count(
        conn,
        f"SELECT COUNT(*) FROM matches WHERE game IN ({placeholders})",
        games_to_delete,
    )
    stats["matches"] = match_count

    if match_count > 0:
        # 通过 match_id 关联的表
        for table in [
            "price_snapshots",
            "orderbook_snapshots",
            "morphology_signals",
            "morphology_cooldown",
        ]:
            stats[table] = _count(
                conn,
                f"""SELECT COUNT(*) FROM {table}
                    WHERE match_id IN (
                        SELECT match_id FROM matches WHERE game IN ({placeholders})
                    )""",
                games_to_delete,
            )

        # morphology_trades 通过 signal_id 关联 morphology_signals
        stats["morphology_trades"] = _count(
            conn,
            f"""SELECT COUNT(*) FROM morphology_trades
                WHERE signal_id IN (
                    SELECT id FROM morphology_signals
                    WHERE match_id IN (
                        SELECT match_id FROM matches WHERE game IN ({placeholders})
                    )
                )""",
            games_to_delete,
        )

    # task_runs 中 market_discovery 的记录数
    stats["task_runs(market_discovery)"] = _count(
        conn,
        "SELECT COUNT(*) FROM task_runs WHERE task_name = ?",
        ("market_discovery",),
    )

    # 打印统计
    print("将删除的数据量:")
    for table, n in stats.items():
        print(f"  {table:35s} {n:>8d}")
    print()

    if args.dry_run:
        print("[dry-run] 未执行删除。去掉 --dry-run 参数以实际清理。")
        conn.close()
        return

    if match_count == 0:
        print("[INFO] 无需清理：matches 表中没有目标游戏的数据。")
        # 仍重置 task_runs 让下次发现强制运行
    else:
        # 2. 执行删除（按依赖顺序）
        print("开始删除...")

        # 先删除 morphology_trades（依赖 morphology_signals）
        n = conn.execute(
            f"""DELETE FROM morphology_trades
                WHERE signal_id IN (
                    SELECT id FROM morphology_signals
                    WHERE match_id IN (
                        SELECT match_id FROM matches WHERE game IN ({placeholders})
                    )
                )""",
            games_to_delete,
        ).rowcount
        print(f"  morphology_trades: 删除 {n} 行")

        # morphology_signals
        n = conn.execute(
            f"""DELETE FROM morphology_signals
                WHERE match_id IN (
                    SELECT match_id FROM matches WHERE game IN ({placeholders})
                )""",
            games_to_delete,
        ).rowcount
        print(f"  morphology_signals: 删除 {n} 行")

        # morphology_cooldown
        n = conn.execute(
            f"""DELETE FROM morphology_cooldown
                WHERE match_id IN (
                    SELECT match_id FROM matches WHERE game IN ({placeholders})
                )""",
            games_to_delete,
        ).rowcount
        print(f"  morphology_cooldown: 删除 {n} 行")

        # orderbook_snapshots
        n = conn.execute(
            f"""DELETE FROM orderbook_snapshots
                WHERE match_id IN (
                    SELECT match_id FROM matches WHERE game IN ({placeholders})
                )""",
            games_to_delete,
        ).rowcount
        print(f"  orderbook_snapshots: 删除 {n} 行")

        # price_snapshots
        n = conn.execute(
            f"""DELETE FROM price_snapshots
                WHERE match_id IN (
                    SELECT match_id FROM matches WHERE game IN ({placeholders})
                )""",
            games_to_delete,
        ).rowcount
        print(f"  price_snapshots: 删除 {n} 行")

        # matches
        n = conn.execute(
            f"DELETE FROM matches WHERE game IN ({placeholders})",
            games_to_delete,
        ).rowcount
        print(f"  matches: 删除 {n} 行")

    # 3. 重置 task_runs，强制下次发现任务运行
    n = conn.execute(
        "DELETE FROM task_runs WHERE task_name = ?",
        ("market_discovery",),
    ).rowcount
    print(f"  task_runs(market_discovery): 删除 {n} 行")

    # 4. VACUUM 回收空间
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    print("\n[完成] 清理完毕。下一轮市场发现将仅监控 config 中配置的游戏/联赛。")


if __name__ == "__main__":
    main()
