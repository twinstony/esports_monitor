"""数据库深度诊断脚本。

功能：
1. 检查数据完整性 - 找出最完整的比赛
2. 检测异常比赛 - 超长比赛时间（400+分钟）
3. 数据起止点分析 - 检查重复和缺失
4. 模拟交易统计 - 收益情况汇总
5. 评估数据对形态策略的满足度
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "esports_history.db")


def parse_iso(iso_str: Optional[str]) -> Optional[datetime]:
    """解析 UTC ISO 时间字符串。"""
    if not iso_str:
        return None
    try:
        if "+" in iso_str:
            iso_str = iso_str.split("+")[0]
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}")
        return 1
    print(f"[INFO] DB: {DB_PATH}")
    print(f"[INFO] Size: {os.path.getsize(DB_PATH)/1024/1024:.1f} MB")
    print("=" * 100)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ========== 1. 检查所有比赛的数据完整性 ==========
    print("=" * 100)
    print("1. 比赛数据完整性分析")
    print("-" * 100)

    cur.execute("SELECT match_id, slug, game, team_a, team_b, status, start_time, end_time FROM matches ORDER BY status DESC")
    matches = [dict(r) for r in cur.fetchall()]

    match_stats: List[Dict[str, Any]] = []
    for m in matches:
        match_id = m["match_id"]
        
        cur.execute("SELECT COUNT(*) FROM price_snapshots WHERE match_id=?", (match_id,))
        price_count = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM orderbook_snapshots WHERE match_id=?", (match_id,))
        ob_count = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM morphology_signals WHERE match_id=?", (match_id,))
        signal_count = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM morphology_trades WHERE match_id=?", (match_id,))
        trade_count = cur.fetchone()[0]
        
        start_dt = parse_iso(m["start_time"])
        end_dt = parse_iso(m["end_time"])
        
        duration_minutes = None
        if start_dt and end_dt:
            duration_minutes = (end_dt - start_dt).total_seconds() / 60.0
        
        now = datetime.now(timezone.utc)
        minutes_since_start = None
        if start_dt:
            minutes_since_start = (now - start_dt).total_seconds() / 60.0
        
        match_stats.append({
            "match_id": match_id,
            "slug": m["slug"],
            "game": m["game"],
            "team_a": m["team_a"],
            "team_b": m["team_b"],
            "status": m["status"],
            "price_count": price_count,
            "ob_count": ob_count,
            "signal_count": signal_count,
            "trade_count": trade_count,
            "duration_minutes": duration_minutes,
            "minutes_since_start": minutes_since_start,
            "start_time": m["start_time"],
            "end_time": m["end_time"],
        })

    # 按数据完整性排序（总数据量）
    match_stats.sort(key=lambda x: x["price_count"] + x["ob_count"], reverse=True)

    print(f"共 {len(match_stats)} 场比赛")
    print(f"  - live/discovered: {sum(1 for m in match_stats if m['status'] in ('live', 'discovered'))}")
    print(f"  - ended: {sum(1 for m in match_stats if m['status'] == 'ended')}")
    print(f"  - settled: {sum(1 for m in match_stats if m['status'] == 'settled')}")
    print()

    # 最完整的前5场比赛
    print("【数据最完整的前5场比赛】")
    for i, m in enumerate(match_stats[:5], 1):
        print(f"{i}. [{m['game']}] {m['team_a']} vs {m['team_b']}")
        print(f"   status={m['status']} | price={m['price_count']} | ob={m['ob_count']}")
        print(f"   signals={m['signal_count']} | trades={m['trade_count']}")
        print(f"   duration={m['duration_minutes']:.0f}min | since_start={m['minutes_since_start']:.0f}min")
        print(f"   start={m['start_time']}")
        print(f"   end={m['end_time']}")
        print()

    # ========== 2. 检测异常比赛（超过400分钟） ==========
    print("=" * 100)
    print("2. 异常比赛检测（超过400分钟）")
    print("-" * 100)

    abnormal_matches = [m for m in match_stats if m["minutes_since_start"] and m["minutes_since_start"] > 400]
    
    if not abnormal_matches:
        print("无异常比赛")
    else:
        print(f"发现 {len(abnormal_matches)} 场异常比赛：")
        for m in abnormal_matches:
            print(f"  [{m['game']}] {m['team_a']} vs {m['team_b']}")
            print(f"    status={m['status']} | since_start={m['minutes_since_start']:.0f}min")
            print(f"    duration={m['duration_minutes']:.0f}min")
            print(f"    start={m['start_time']}")
            print(f"    end={m['end_time']}")
            print()

    # ========== 3. 最完整比赛的数据起止点分析 ==========
    print("=" * 100)
    print("3. 最完整比赛的数据起止点分析")
    print("-" * 100)

    if match_stats:
        best_match = match_stats[0]
        match_id = best_match["match_id"]
        print(f"分析比赛: [{best_match['game']}] {best_match['team_a']} vs {best_match['team_b']}")
        print(f"match_id: {match_id}")
        print()

        # 价格快照起止点
        cur.execute("SELECT MIN(recorded_at), MAX(recorded_at), COUNT(*) FROM price_snapshots WHERE match_id=?", (match_id,))
        row = cur.fetchone()
        min_price = row[0]
        max_price = row[1]
        price_total = row[2]
        
        # 检查重复时间戳
        cur.execute("SELECT recorded_at, COUNT(*) FROM price_snapshots WHERE match_id=? GROUP BY recorded_at HAVING COUNT(*) > 1", (match_id,))
        duplicate_times = cur.fetchall()
        
        # 检查时间间隔分布
        cur.execute("SELECT recorded_at FROM price_snapshots WHERE match_id=? ORDER BY recorded_at", (match_id,))
        price_times = [parse_iso(r[0]) for r in cur.fetchall()]
        
        intervals = []
        for i in range(1, len(price_times)):
            if price_times[i] and price_times[i-1]:
                intervals.append((price_times[i] - price_times[i-1]).total_seconds())
        
        avg_interval = sum(intervals) / len(intervals) if intervals else 0
        max_interval = max(intervals) if intervals else 0
        min_interval = min(intervals) if intervals else 0
        
        print(f"价格快照:")
        print(f"  总数: {price_total}")
        print(f"  起始: {min_price}")
        print(f"  结束: {max_price}")
        print(f"  平均间隔: {avg_interval:.1f}s")
        print(f"  最大间隔: {max_interval:.1f}s")
        print(f"  最小间隔: {min_interval:.1f}s")
        print(f"  重复时间戳数: {len(duplicate_times)}")
        
        # 起止点附近5个时间单位的数据
        if price_times:
            print(f"\n  前5个时间点:")
            for t in price_times[:5]:
                print(f"    {t}")
            print(f"  后5个时间点:")
            for t in price_times[-5:]:
                print(f"    {t}")

        print()

        # 盘口快照起止点
        cur.execute("SELECT MIN(recorded_at), MAX(recorded_at), COUNT(*) FROM orderbook_snapshots WHERE match_id=?", (match_id,))
        row = cur.fetchone()
        min_ob = row[0]
        max_ob = row[1]
        ob_total = row[2]
        
        print(f"盘口快照:")
        print(f"  总数: {ob_total}")
        print(f"  起始: {min_ob}")
        print(f"  结束: {max_ob}")

    # ========== 4. 模拟交易统计 ==========
    print("=" * 100)
    print("4. 模拟交易统计")
    print("-" * 100)

    cur.execute("""
        SELECT 
            COUNT(*) AS total,
            SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN settled=1 AND pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN settled=1 AND pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(CASE WHEN settled=1 THEN pnl_usd ELSE 0 END), 0) AS total_pnl,
            COALESCE(AVG(CASE WHEN settled=1 THEN pnl_usd ELSE NULL END), 0) AS avg_pnl,
            COALESCE(MIN(CASE WHEN settled=1 THEN pnl_usd ELSE NULL END), 0) AS min_pnl,
            COALESCE(MAX(CASE WHEN settled=1 THEN pnl_usd ELSE NULL END), 0) AS max_pnl
        FROM morphology_trades
    """)
    row = cur.fetchone()
    total = row["total"]
    settled = row["settled"]
    wins = row["wins"]
    losses = row["losses"]
    total_pnl = row["total_pnl"]
    avg_pnl = row["avg_pnl"]
    min_pnl = row["min_pnl"]
    max_pnl = row["max_pnl"]
    
    win_rate = (wins / settled * 100) if settled > 0 else 0
    
    print(f"总交易数: {total}")
    print(f"已结算: {settled}")
    print(f"未结算: {total - settled}")
    print(f"胜率: {win_rate:.1f}% ({wins}胜{losses}负)")
    print(f"累计PnL: {total_pnl:+.2f} USD")
    print(f"平均PnL: {avg_pnl:+.2f} USD")
    print(f"最大盈利: {max_pnl:+.2f} USD")
    print(f"最大亏损: {min_pnl:+.2f} USD")

    # 按日期统计
    print("\n【按日期统计】")
    cur.execute("""
        SELECT 
            DATE(opened_at) AS date,
            COUNT(*) AS count,
            SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN settled=1 AND pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN settled=1 AND pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(CASE WHEN settled=1 THEN pnl_usd ELSE 0 END), 0) AS pnl
        FROM morphology_trades
        GROUP BY DATE(opened_at)
        ORDER BY date
    """)
    rows = cur.fetchall()
    for r in rows:
        date = r["date"]
        cnt = r["count"]
        st = r["settled"]
        w = r["wins"]
        lo = r["losses"]
        pnl = r["pnl"]
        wr = (w / st * 100) if st > 0 else 0
        print(f"  {date}: {cnt}单 (结算{st}单, {wr:.0f}%胜率, PnL {pnl:+.2f} USD)")

    # 按信号类型统计
    print("\n【按信号类型统计】")
    cur.execute("""
        SELECT 
            ms.signal_name,
            COUNT(*) AS count,
            SUM(CASE WHEN mt.settled=1 THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN mt.settled=1 AND mt.pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN mt.settled=1 AND mt.pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(CASE WHEN mt.settled=1 THEN mt.pnl_usd ELSE 0 END), 0) AS pnl
        FROM morphology_trades mt
        LEFT JOIN morphology_signals ms ON mt.signal_id = ms.id
        GROUP BY ms.signal_name
        ORDER BY count DESC
    """)
    rows = cur.fetchall()
    for r in rows:
        name = r["signal_name"] or "unknown"
        cnt = r["count"]
        st = r["settled"]
        w = r["wins"]
        lo = r["losses"]
        pnl = r["pnl"]
        wr = (w / st * 100) if st > 0 else 0
        print(f"  {name:25}: {cnt}单 (结算{st}单, {wr:.0f}%胜率, PnL {pnl:+.2f} USD)")

    # 按游戏统计
    print("\n【按游戏统计】")
    cur.execute("""
        SELECT 
            m.game,
            COUNT(*) AS count,
            SUM(CASE WHEN mt.settled=1 THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN mt.settled=1 AND mt.pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN mt.settled=1 AND mt.pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(CASE WHEN mt.settled=1 THEN mt.pnl_usd ELSE 0 END), 0) AS pnl
        FROM morphology_trades mt
        LEFT JOIN matches m ON mt.match_id = m.match_id
        GROUP BY m.game
        ORDER BY count DESC
    """)
    rows = cur.fetchall()
    for r in rows:
        game = r["game"] or "unknown"
        cnt = r["count"]
        st = r["settled"]
        w = r["wins"]
        lo = r["losses"]
        pnl = r["pnl"]
        wr = (w / st * 100) if st > 0 else 0
        print(f"  {game:6}: {cnt}单 (结算{st}单, {wr:.0f}%胜率, PnL {pnl:+.2f} USD)")

    # ========== 5. 数据满足度评估（形态策略） ==========
    print("=" * 100)
    print("5. 数据满足度评估（形态策略）")
    print("-" * 100)

    # 形态策略需要的核心数据：
    # 1. 价格序列（至少5个去重点，用于重采样）
    # 2. 盘口数据（用于VWAP计算）
    # 3. 比赛时间窗口（start_time/end_time）
    # 4. 信号记录（用于回测统计）

    print("【核心数据需求评估】")
    
    # 价格数据充足性
    cur.execute("""
        SELECT 
            m.match_id, m.status,
            COUNT(ps.id) as price_count,
            COUNT(DISTINCT ps.recorded_at) as unique_time_count
        FROM matches m
        LEFT JOIN price_snapshots ps ON m.match_id = ps.match_id
        GROUP BY m.match_id, m.status
        HAVING COUNT(ps.id) > 0
    """)
    rows = cur.fetchall()
    total_matches_with_data = len(rows)
    matches_with_enough_data = sum(1 for r in rows if r["unique_time_count"] >= 5)
    
    print(f"有价格数据的比赛: {total_matches_with_data}")
    print(f"价格数据充足(>=5个去重点): {matches_with_enough_data}")
    print(f"数据充足率: {matches_with_enough_data/total_matches_with_data*100:.1f}%" if total_matches_with_data > 0 else "N/A")

    # 盘口数据充足性
    cur.execute("""
        SELECT 
            m.match_id, m.status,
            COUNT(os.id) as ob_count
        FROM matches m
        LEFT JOIN orderbook_snapshots os ON m.match_id = os.match_id
        GROUP BY m.match_id, m.status
        HAVING COUNT(os.id) > 0
    """)
    rows = cur.fetchall()
    matches_with_ob = len(rows)
    print(f"\n有盘口数据的比赛: {matches_with_ob}")

    # 比赛时间窗口完整性
    cur.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN start_time IS NOT NULL AND end_time IS NOT NULL THEN 1 ELSE 0 END) as has_both,
            SUM(CASE WHEN start_time IS NULL THEN 1 ELSE 0 END) as missing_start,
            SUM(CASE WHEN end_time IS NULL THEN 1 ELSE 0 END) as missing_end
        FROM matches
    """)
    row = cur.fetchone()
    print(f"\n比赛时间窗口完整性:")
    print(f"  总数: {row['total']}")
    print(f"  起止时间都有: {row['has_both']}")
    print(f"  缺少start_time: {row['missing_start']}")
    print(f"  缺少end_time: {row['missing_end']}")

    # 信号分布（各时间窗口）
    print("\n【信号分布（按时间窗口）】")
    cur.execute("""
        SELECT window_label, COUNT(*) as count
        FROM morphology_signals
        GROUP BY window_label
        ORDER BY count DESC
    """)
    rows = cur.fetchall()
    for r in rows:
        print(f"  {r['window_label']:6}: {r['count']}个信号")

    # 信号分布（按信号类型）
    print("\n【信号分布（按类型）】")
    cur.execute("""
        SELECT signal_name, COUNT(*) as count
        FROM morphology_signals
        GROUP BY signal_name
        ORDER BY count DESC
    """)
    rows = cur.fetchall()
    for r in rows:
        print(f"  {r['signal_name']:25}: {r['count']}个信号")

    # 评估结论
    print("\n【数据满足度综合评估】")
    print("-" * 60)
    
    issues = []
    strengths = []
    
    if matches_with_enough_data >= total_matches_with_data * 0.8:
        strengths.append("✓ 价格数据充足率高，满足形态策略的基础需求")
    else:
        issues.append(f"✗ 价格数据充足率较低 ({matches_with_enough_data/total_matches_with_data*100:.0f}%)，部分比赛可能无法生成信号")
    
    if matches_with_ob >= total_matches_with_data * 0.7:
        strengths.append("✓ 盘口数据覆盖率较好，支持VWAP计算")
    else:
        issues.append(f"✗ 盘口数据覆盖率不足 ({matches_with_ob/total_matches_with_data*100:.0f}%)，可能影响模拟交易的准确性")
    
    if row["has_both"] >= row["total"] * 0.9:
        strengths.append("✓ 比赛时间窗口完整性好")
    else:
        issues.append(f"✗ 部分比赛缺少时间窗口信息 ({row['total'] - row['has_both']}场)")
    
    if len(abnormal_matches) > 0:
        issues.append(f"✗ 存在{len(abnormal_matches)}场异常比赛（超过400分钟），需检查监控逻辑")
    else:
        strengths.append("✓ 无异常比赛")
    
    for s in strengths:
        print(s)
    for i in issues:
        print(i)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())