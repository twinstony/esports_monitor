"""归档数据恢复脚本。

将 archives/ 目录中的 .json.gz 归档文件重新导入主库，用于回测分析。

用法：
    python scripts/restore_archive.py --table price_snapshots --month 2026-01
    python scripts/restore_archive.py --all
    python scripts/restore_archive.py --table morphology_trades --month 2026-01 --no-gzip
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from typing import Any, Dict, List

# 将项目根目录加入 sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from esports_monitor.config.manager import load_config
from esports_monitor.data.sqlite_storage import SQLiteStorage


# 各表的列定义（用于 INSERT）
TABLE_COLUMNS: Dict[str, List[str]] = {
    "price_snapshots": [
        "match_id", "team", "price", "price_opponent", "recorded_at",
    ],
    "orderbook_snapshots": [
        "match_id", "team", "token_id", "order_book_json",
        "best_bid", "best_ask", "bid_depth", "ask_depth", "spread", "recorded_at",
    ],
    "morphology_signals": [
        "match_id", "signal_name", "window_label", "buy_team", "buy_price",
        "hours_before_end", "minutes_since_start",
        "predicted_win_prob", "predicted_pnl", "detected_at",
    ],
    "morphology_trades": [
        "match_id", "signal_id", "buy_team", "buy_price",
        "quantity", "notional_usd", "vwap", "opened_at", "settled",
        "winning_team", "pnl_usd", "settled_at",
    ],
}


def restore_file(
    storage: SQLiteStorage,
    table_name: str,
    filepath: str,
    logger: Any,
) -> int:
    """将单个归档文件导入指定表。

    Returns:
        成功导入的行数。
    """
    columns = TABLE_COLUMNS.get(table_name)
    if not columns:
        logger.error("不支持的表: %s", table_name)
        return 0

    if not os.path.exists(filepath):
        logger.warning("归档文件不存在: %s", filepath)
        return 0

    # 读取所有行
    rows: List[Dict[str, Any]] = []
    open_fn = gzip.open if filepath.endswith(".gz") else open
    try:
        with open_fn(filepath, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except (ValueError, json.JSONDecodeError) as exc:
                    logger.warning("跳过无效行: %s", exc)
    except OSError as exc:
        logger.error("读取归档文件失败 %s: %s", filepath, exc)
        return 0

    if not rows:
        return 0

    # 批量 INSERT
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    sql = f"INSERT INTO {table_name} ({col_str}) VALUES ({placeholders})"
    inserted = 0
    try:
        with storage._connect() as conn:
            for row in rows:
                values = [row.get(c) for c in columns]
                try:
                    conn.execute(sql, values)
                    inserted += 1
                except Exception as exc:
                    logger.debug("插入失败: %s", exc)
            conn.commit()
    except Exception as exc:
        logger.error("批量插入失败 %s: %s", table_name, exc)
    return inserted


def restore_month(
    storage: SQLiteStorage,
    table_name: str,
    year_month: str,
    archive_dir: str,
    logger: Any,
) -> int:
    """恢复指定表的指定月份。"""
    for ext in (".json.gz", ".json"):
        filepath = os.path.join(archive_dir, f"{table_name}_{year_month.replace('-', '')}{ext}")
        if os.path.exists(filepath):
            return restore_file(storage, table_name, filepath, logger)
    logger.warning("未找到 %s 的 %s 归档文件", table_name, year_month)
    return 0


def restore_all(
    storage: SQLiteStorage,
    archive_dir: str,
    logger: Any,
) -> int:
    """恢复 archives/ 目录下所有归档文件。"""
    if not os.path.isdir(archive_dir):
        logger.error("归档目录不存在: %s", archive_dir)
        return 0
    total = 0
    for name in sorted(os.listdir(archive_dir)):
        if not (name.endswith(".json.gz") or name.endswith(".json")):
            continue
        # 解析表名：{table}_{YYYYMM}.json.gz
        base = name[:-8] if name.endswith(".json.gz") else name[:-5]
        # 找最后一个下划线分割表名和月份
        idx = base.rfind("_")
        if idx <= 0:
            continue
        table_name = base[:idx]
        if table_name not in TABLE_COLUMNS:
            logger.debug("跳过未知表归档: %s", name)
            continue
        filepath = os.path.join(archive_dir, name)
        total += restore_file(storage, table_name, filepath, logger)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="恢复归档数据到主库")
    parser.add_argument("--table", choices=list(TABLE_COLUMNS.keys()), help="指定表名")
    parser.add_argument("--month", help="指定月份，如 2026-01")
    parser.add_argument("--all", action="store_true", help="恢复所有归档数据")
    parser.add_argument(
        "--archive-dir", default="archives",
        help="归档目录（默认 archives）",
    )
    parser.add_argument(
        "--db", default=None,
        help="数据库路径（默认从 config.yaml 读取）",
    )
    args = parser.parse_args()

    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger("restore_archive")

    # 加载配置以获取数据库路径
    config = load_config(PROJECT_DIR)
    db_path = args.db or (config.get("database") or {}).get("path", "esports_history.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_DIR, db_path)

    archive_dir = args.archive_dir
    if not os.path.isabs(archive_dir):
        archive_dir = os.path.join(PROJECT_DIR, archive_dir)

    storage = SQLiteStorage(db_path=db_path)

    if args.all:
        total = restore_all(storage, archive_dir, logger)
        logger.info("恢复完成：共导入 %d 行", total)
    elif args.table and args.month:
        count = restore_month(storage, args.table, args.month, archive_dir, logger)
        logger.info("恢复完成：%s %s 共导入 %d 行", args.table, args.month, count)
    else:
        parser.error("请指定 --table 和 --month，或使用 --all")


if __name__ == "__main__":
    main()
