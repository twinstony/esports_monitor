"""验证脚本：运行一轮真实数据采集，列出 live 比赛的价格/盘口/持久化数据。

流程：
1. 清空旧数据（matches/price_snapshots/orderbook_snapshots/task_runs/clob_token_cache）
2. 精确市场发现（fetch_live_event_slugs → fetch_event → 解析）
3. 持久化发现的比赛到 matches 表
4. 对每场 live 比赛采集 Gamma 价格 + CLOB 盘口，持久化快照
5. 展示数据库完整内容供核对

注意：不发 Telegram 通知，不跑形态检测，只验证数据采集与持久化。
"""
import os
import sys
import sqlite3
import logging

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from esports_monitor.api.polymarket import PolymarketClient
from esports_monitor.api.clob import ClobClient
from esports_monitor.config.manager import load_config
from esports_monitor.data.sqlite_storage import SQLiteStorage
from esports_monitor.discovery.service import (
    MarketDiscoveryService,
    persist_discovered_matches,
)
from esports_monitor.utils.time_utils import from_utc_iso, now_utc, to_utc_iso

logging.basicConfig(level=logging.WARNING)

DB_PATH = os.path.join(PROJECT_DIR, "esports_history.db")


def reset_db():
    """清空监控相关表，确保从干净状态开始。"""
    if not os.path.exists(DB_PATH):
        print(f"[RESET] DB 不存在，跳过清空: {DB_PATH}")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    tables = [
        "price_snapshots", "orderbook_snapshots", "morphology_signals",
        "morphology_observations", "trade_simulations", "matches",
        "task_runs", "clob_token_cache",
    ]
    for t in tables:
        try:
            cur.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass  # 表不存在
    conn.commit()
    conn.close()
    print(f"[RESET] 已清空监控相关表: {tables}")


def run_discovery(gamma, discovery_cfg):
    """运行精确市场发现。"""
    svc = MarketDiscoveryService(
        gamma_client=gamma, clob_client=None, config=discovery_cfg,
    )
    result = svc.discover()
    print(f"\n[DISCOVERY] {result.summary}")
    return result


def collect_and_persist(gamma, clob, storage, matches):
    """对每场 live 比赛采集价格+盘口并持久化（复用 monitor 逻辑）。"""
    now = now_utc()
    now_iso = to_utc_iso(now)
    print(f"\n[COLLECT] 采集时间(UTC): {now_iso}")
    print(f"[COLLECT] 采集时间(北京): {from_utc_iso(now_iso).strftime('%Y-%m-%d %H:%M:%S CST')}")

    for i, m in enumerate(matches):
        print(f"\n--- 比赛 #{i+1} [{m.game}] {m.team_a} vs {m.team_b} ---")
        print(f"  slug:    {m.slug}")
        print(f"  cid:     {m.condition_id}")
        print(f"  start:   {m.start_time}")
        print(f"  end:     {m.end_time}")
        print(f"  price_a: {m.price_a:.4f}  price_b: {m.price_b:.4f}")

        # 时间窗口检查（与 monitor._check_single_match 一致）
        start_dt = from_utc_iso(m.start_time) if m.start_time else None
        end_dt = from_utc_iso(m.end_time) if m.end_time else None
        if end_dt and now > end_dt:
            print(f"  [SKIP] 比赛已结束 (end < now)")
            continue
        if start_dt and now < start_dt:
            print(f"  [SKIP] 比赛未开始 (start > now)")
            continue

        # 1. 重新 fetch_event 获取最新价格（与 monitor 一致）
        event = gamma.fetch_event(m.slug)
        if not event:
            print(f"  [ERROR] fetch_event 失败")
            continue
        markets = gamma.parse_match_markets(event)
        target = None
        for mk in markets:
            if mk.condition_id == m.match_id:
                target = mk
                break
        if not target:
            print(f"  [ERROR] 未找到匹配的 market")
            continue

        print(f"  [GAMMA 最新价格] {target.team_a}={target.price_a:.4f} / {target.team_b}={target.price_b:.4f}")

        # 2. 写入价格快照
        ok1 = storage.insert_price_snapshot(
            m.match_id, "team_a", target.price_a, target.price_b, now_iso,
        )
        ok2 = storage.insert_price_snapshot(
            m.match_id, "team_b", target.price_b, target.price_a, now_iso,
        )
        print(f"  [PERSIST] price_snapshots: team_a={ok1} team_b={ok2}")

        # 3. 获取 CLOB 盘口并写入
        for team, outcome in (("team_a", "Yes"), ("team_b", "No")):
            try:
                token_id, orderbook = clob.get_token_id_and_orderbook(
                    m.condition_id, outcome
                )
                if not token_id or not orderbook:
                    print(f"  [CLOB] {team}({outcome}): 无数据")
                    continue
                # 解析盘口摘要
                summary = clob.parse_orderbook_summary(orderbook) if hasattr(clob, "parse_orderbook_summary") else None
                from esports_monitor.api.clob import parse_orderbook_summary
                summary = parse_orderbook_summary(orderbook)
                print(
                    f"  [CLOB] {team}({outcome}) token={token_id[:16]}... | "
                    f"bid={summary['best_bid']:.4f} ask={summary['best_ask']:.4f} "
                    f"spread={summary['spread']:.4f} | "
                    f"depth_bid={summary['total_bid_depth']:.0f} "
                    f"depth_ask={summary['total_ask_depth']:.0f}"
                )
                ok = clob.save_orderbook_snapshot(
                    match_id=m.match_id, team=team, token_id=token_id,
                    orderbook=orderbook, recorded_at=now_iso,
                )
                print(f"  [PERSIST] orderbook_snapshot {team}: {ok}")
            except Exception as exc:
                print(f"  [CLOB ERROR] {team}({outcome}): {exc}")


def show_db():
    """展示数据库完整内容。"""
    print("\n" + "=" * 80)
    print("数据库持久化结果")
    print("=" * 80)
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 表行数
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    print("\n[行数统计]")
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            print(f"  {t:30}: {cur.fetchone()[0]}")
        except Exception as e:
            print(f"  {t:30}: ERROR {e}")

    # matches 完整字段
    print("\n[matches 表 - 完整字段]")
    cur.execute(
        "SELECT match_id, slug, game, league, team_a, team_b, condition_id, "
        "token_id_a, token_id_b, start_time, end_time, status, winning_team, "
        "discovered_at, updated_at FROM matches ORDER BY game, match_id"
    )
    for r in cur.fetchall():
        print(f"\n  match_id:    {r['match_id']}")
        print(f"  slug:        {r['slug']}")
        print(f"  game:        {r['game']}  | league: {r['league']}")
        print(f"  team_a:      {r['team_a']}")
        print(f"  team_b:      {r['team_b']}")
        print(f"  condition_id:{r['condition_id']}")
        print(f"  token_id_a:  {r['token_id_a']}")
        print(f"  token_id_b:  {r['token_id_b']}")
        print(f"  start_time:  {r['start_time']}")
        print(f"  end_time:    {r['end_time']}")
        print(f"  status:      {r['status']} | winning: {r['winning_team']}")
        print(f"  discovered:  {r['discovered_at']}")
        print(f"  updated:     {r['updated_at']}")

    # price_snapshots
    print("\n[price_snapshots 表]")
    cur.execute(
        "SELECT match_id, team, price, price_opponent, recorded_at "
        "FROM price_snapshots ORDER BY recorded_at"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        print(
            f"  {r['recorded_at']} | match={r['match_id'][:16]}... | "
            f"team={r['team']:7} | price={r['price']:.4f} | opp={r['price_opponent']:.4f}"
        )

    # orderbook_snapshots
    print("\n[orderbook_snapshots 表]")
    cur.execute(
        "SELECT match_id, team, token_id, best_bid, best_ask, spread, "
        "bid_depth, ask_depth, recorded_at FROM orderbook_snapshots "
        "ORDER BY recorded_at"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        print(
            f"  {r['recorded_at']} | match={r['match_id'][:16]}... | team={r['team']:7} | "
            f"token={r['token_id'][:16]}... | bid={r['best_bid']:.4f} ask={r['best_ask']:.4f} "
            f"spread={r['spread']:.4f} | depth_b={r['bid_depth']:.0f} depth_a={r['ask_depth']:.0f}"
        )

    # clob_token_cache
    print("\n[clob_token_cache 表]")
    cur.execute(
        "SELECT condition_id, outcome, token_id, cached_at FROM clob_token_cache"
    )
    rows = cur.fetchall()
    if not rows:
        print("  (空)")
    for r in rows:
        print(
            f"  cid={r['condition_id'][:16]}... | outcome={r['outcome']:4} | "
            f"token={r['token_id'][:16]}... | cached={r['cached_at']}"
        )

    conn.close()


def main() -> int:
    print("=" * 80)
    print("esports_monitor 数据采集验证")
    print("=" * 80)

    config = load_config(PROJECT_DIR)
    discovery_cfg = config.get("discovery") or {}
    poly_cfg = config.get("polymarket") or {}

    # 1. 清空旧数据
    reset_db()

    # 2. 初始化组件
    storage = SQLiteStorage(DB_PATH)
    gamma = PolymarketClient(timeout=20, config=config)
    clob = ClobClient(
        storage=storage,
        api_base=poly_cfg.get("clob_api_base"),
        gamma_api_base=poly_cfg.get("gamma_api_base"),
        timeout=20.0,
    )

    # 3. 精确市场发现
    print("\n[STEP 1] 精确市场发现（网页 isLive 标记）")
    result = run_discovery(gamma, discovery_cfg)
    if not result.new_matches:
        print("[STOP] 未发现 live 比赛")
        show_db()
        return 0

    print(f"\n[DISCOVERY] 发现 {len(result.new_matches)} 场 live 比赛:")
    for i, m in enumerate(result.new_matches):
        print(f"  {i+1}. [{m.game}] {m.team_a} vs {m.team_b} | {m.price_a:.4f}/{m.price_b:.4f}")

    # 4. 持久化到 matches 表
    print("\n[STEP 2] 持久化到 matches 表")
    n = persist_discovered_matches(storage, result.new_matches)
    print(f"  写入 {n} 场比赛")

    # 5. 采集价格+盘口并持久化
    print("\n[STEP 3] 采集 Gamma 价格 + CLOB 盘口并持久化")
    collect_and_persist(gamma, clob, storage, result.new_matches)

    # 6. 展示数据库
    show_db()

    return 0


if __name__ == "__main__":
    sys.exit(main())
