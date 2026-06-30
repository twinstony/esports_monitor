"""快速检查数据库状态。"""
import os
import sqlite3
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "esports_history.db")


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}")
        return 1
    print(f"[INFO] DB: {DB_PATH}")
    print(f"[INFO] Size: {os.path.getsize(DB_PATH)/1024:.1f} KB")
    print("-" * 80)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 表列表
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    print(f"Tables ({len(tables)}): {tables}")
    print("-" * 80)

    # 各表行数
    print("Row counts:")
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            n = cur.fetchone()[0]
            print(f"  {t:30}: {n}")
        except Exception as e:
            print(f"  {t:30}: ERROR {e}")
    print("-" * 80)

    # matches 详情
    print("Matches (按 status/game 排序):")
    cur.execute(
        "SELECT match_id, slug, game, team_a, team_b, status, winning_team, "
        "start_time, end_time FROM matches "
        "ORDER BY status DESC, game, match_id LIMIT 30"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        cid = (r["match_id"] or "")[:14]
        print(
            f"  {r['game']:6} | {(r['team_a'] or ''):15} vs {(r['team_b'] or ''):15} | "
            f"{r['status']:10} | slug={r['slug']:30} | cid={cid}... | "
            f"start={r['start_time']} | end={r['end_time']} | win={r['winning_team']}"
        )
    print("-" * 80)

    # 最近 5 条 price_snapshots
    print("最近 5 条 price_snapshots:")
    cur.execute(
        "SELECT match_id, team, price, price_opponent, recorded_at "
        "FROM price_snapshots ORDER BY recorded_at DESC LIMIT 5"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        print(
            f"  {r['recorded_at']} | match={r['match_id'][:14]}... | "
            f"team={r['team']:7} | price={r['price']:.4f} | opp={r['price_opponent']:.4f}"
        )
    print("-" * 80)

    # 最近 5 条 orderbook_snapshots
    print("最近 5 条 orderbook_snapshots:")
    cur.execute(
        "SELECT match_id, team, token_id, best_bid, best_ask, spread, "
        "bid_depth, ask_depth, recorded_at "
        "FROM orderbook_snapshots ORDER BY recorded_at DESC LIMIT 5"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        print(
            f"  {r['recorded_at']} | match={r['match_id'][:14]}... | team={r['team']:7} | "
            f"bid={r['best_bid']:.4f} ask={r['best_ask']:.4f} spread={r['spread']:.4f} | "
            f"depth_bid={r['bid_depth']:.0f} depth_ask={r['ask_depth']:.0f}"
        )
    print("-" * 80)

    # task_runs 最近 10 条
    print("最近 10 条 task_runs:")
    cur.execute(
        "SELECT task_name, run_at, result_summary FROM task_runs "
        "ORDER BY run_at DESC LIMIT 10"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        print(f"  {r['task_name']:25} | {r['run_at']} | {r['result_summary']}")
    print("-" * 80)

    # morphology_signals 最近 5 条
    print("最近 5 条 morphology_signals:")
    cur.execute(
        "SELECT match_id, signal_name, window_label, buy_team, buy_price, detected_at "
        "FROM morphology_signals ORDER BY detected_at DESC LIMIT 5"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        print(
            f"  {r['detected_at']} | match={r['match_id'][:14]}... | "
            f"sig={r['signal_name']:20} | win={r['window_label']:6} | "
            f"buy={r['buy_team']:10}@{r['buy_price']:.4f}"
        )
    print("-" * 80)

    # clob_token_cache
    print("clob_token_cache:")
    cur.execute(
        "SELECT condition_id, outcome, token_id, cached_at FROM clob_token_cache LIMIT 10"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        print(
            f"  cid={r['condition_id'][:14]}... | outcome={r['outcome']:4} | "
            f"token={r['token_id'][:14]}... | cached={r['cached_at']}"
        )

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
