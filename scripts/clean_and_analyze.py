#!/usr/bin/env python3
"""清理与分析脚本：修正比赛实际开始/结束时间，重新分析模拟开单数据。

背景：
- matches.start_time 实际是市场创建/发现时间，并非比赛真实开赛时间。
- 通过 Polymarket Gamma API 的 event.startTime 获取真实开赛时间。
- 通过价格首次触及 >=0.99 或 <=0.01 推断比赛结束时间。
- 据此重新标注价格快照与模拟开单的比赛状态（pre_match / in_match /
  post_match / unknown），并重新统计胜率、PnL、信号窗口分布。

注意：
- 实际数据库中模拟开单表为 morphology_trades（任务描述中称 simulated_trades），
  其状态字段为 settled（1=已结算, 0=未结算），结算价字段不存在，PnL 直接存储于
  pnl_usd。形态信号表 morphology_signals 的时间字段为 detected_at。
- 数据库修改仅限 ALTER TABLE ADD COLUMN（real_start_time / real_end_time），
  不删除或修改任何既有列数据。

用法：python scripts/clean_and_analyze.py
"""
from __future__ import annotations

import html
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "esports_history.db")
REPORT_PATH = os.path.join(PROJECT_DIR, "report_cleaned.html")
sys.path.insert(0, PROJECT_DIR)

from esports_monitor.config.manager import load_config  # noqa: E402
from esports_monitor.api.polymarket import PolymarketClient  # noqa: E402
from esports_monitor.utils.time_utils import from_utc_iso, to_utc_iso, now_utc  # noqa: E402

# 实际表名（任务描述中的 simulated_trades 对应此表）
TRADES_TABLE = "morphology_trades"
SIGNALS_TABLE = "morphology_signals"

# 价格阈值：用于推断比赛结束
PRICE_END_HIGH = 0.99
PRICE_END_LOW = 0.01

# 交易过滤阈值
BUY_PRICE_MIN = 0.05
BUY_PRICE_MAX = 0.95
PNL_ABS_MAX = 10000.0

# 默认信号窗口（与 config 默认值一致，兜底用）
DEFAULT_WINDOWS = [
    {"label": "early", "min_minutes": 0, "max_minutes": 30},
    {"label": "mid", "min_minutes": 30, "max_minutes": 90},
    {"label": "late", "min_minutes": 90, "max_minutes": 9999},
]


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------

def parse_dt(s: Optional[str]) -> Optional[datetime]:
    """解析 UTC ISO 时间字符串为 aware datetime。"""
    return from_utc_iso(s) if s else None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_type: str = "TEXT"
) -> bool:
    """添加列（若不存在）。返回是否新增。"""
    if column_exists(conn, table, column):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    conn.commit()
    print(f"[INFO] 已添加列: {table}.{column} ({col_type})")
    return True


def classify_state(
    dt: Optional[datetime],
    real_start: Optional[datetime],
    real_end: Optional[datetime],
) -> str:
    """根据时间点判断比赛状态。"""
    if real_start is None or dt is None:
        return "unknown"
    if dt < real_start:
        return "pre_match"
    if real_end is not None and dt >= real_end:
        return "post_match"
    return "in_match"


def classify_window(minutes: Optional[float], windows: List[Dict[str, Any]]) -> str:
    """根据 minutes_since_start 归入信号窗口。"""
    if minutes is None:
        return "unknown"
    for w in windows:
        try:
            lo = float(w.get("min_minutes", 0))
            hi = float(w.get("max_minutes", 9999))
        except (TypeError, ValueError):
            continue
        if lo <= minutes < hi:
            return str(w.get("label", "unknown"))
    return windows[-1].get("label", "unknown") if windows else "unknown"


def fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def pct(num: float, den: float) -> str:
    return f"{(num / den * 100):.1f}%" if den else "N/A"


# ----------------------------------------------------------------------
# 步骤1：获取并存储比赛实际开始时间
# ----------------------------------------------------------------------

def step1_fetch_real_start_times(gamma: PolymarketClient) -> Dict[str, int]:
    """从 Polymarket API 获取 event.startTime，写入 matches.real_start_time。

    仅对 real_start_time 为空的比赛发起请求（支持增量重跑）。
    """
    print("\n" + "=" * 80)
    print("步骤1：获取比赛实际开始时间 (event.startTime)")
    print("-" * 80)

    conn = sqlite3.connect(DB_PATH)
    add_column_if_missing(conn, "matches", "real_start_time", "TEXT")

    cur = conn.execute(
        "SELECT match_id, slug, real_start_time FROM matches ORDER BY match_id"
    )
    rows = cur.fetchall()

    total = len(rows)
    already = sum(1 for r in rows if r[2])
    to_fetch = [r for r in rows if not r[2]]
    print(f"[INFO] 总比赛数: {total}，已记录 real_start_time: {already}，需获取: {len(to_fetch)}")

    fetched = 0
    failed = 0
    for i, (match_id, slug, _) in enumerate(to_fetch, 1):
        start_iso: Optional[str] = None
        try:
            if slug:
                event = gamma.fetch_event(slug)
            else:
                event = None
            if isinstance(event, dict):
                start_val = event.get("startTime") or event.get("startTimeISO")
                if start_val:
                    dt = parse_dt(str(start_val))
                    if dt is not None:
                        start_iso = to_utc_iso(dt)
        except Exception as exc:
            print(f"[WARN] match={match_id} slug={slug} 获取失败: {exc}")
        try:
            conn.execute(
                "UPDATE matches SET real_start_time=? WHERE match_id=?",
                (start_iso, match_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[WARN] 写入失败 match={match_id}: {exc}")
            failed += 1
            continue

        if start_iso:
            fetched += 1
        else:
            failed += 1

        if i % 20 == 0 or i == len(to_fetch):
            print(f"[进度] {i}/{len(to_fetch)} 已处理 (成功 {fetched}, 失败/空 {failed})")
        # 礼貌延时，避免触发 Gamma API 限流
        time.sleep(0.2)

    conn.close()
    print(f"[完成] 成功获取 real_start_time: {fetched}，失败/空: {failed}")
    return {"total": total, "already": already, "fetched": fetched, "failed": failed}


# ----------------------------------------------------------------------
# 步骤2：推断比赛结束时间
# ----------------------------------------------------------------------

def step2_infer_real_end_times() -> Dict[str, int]:
    """从价格数据推断比赛结束时间，写入 matches.real_end_time。

    方法：找到价格首次达到 >=0.99 或 <=0.01 的时间点作为 match_end_time。
    """
    print("\n" + "=" * 80)
    print("步骤2：从价格数据推断比赛结束时间")
    print("-" * 80)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    add_column_if_missing(conn, "matches", "real_end_time", "TEXT")

    cur = conn.execute("SELECT match_id FROM matches ORDER BY match_id")
    match_ids = [r[0] for r in cur.fetchall()]

    updated = 0
    no_end = 0
    no_prices = 0
    for i, match_id in enumerate(match_ids, 1):
        # 按时间升序找到首个触及阈值的快照
        cur2 = conn.execute(
            f"SELECT recorded_at FROM price_snapshots "
            f"WHERE match_id=? AND (price >= {PRICE_END_HIGH} OR price <= {PRICE_END_LOW}) "
            f"ORDER BY recorded_at ASC LIMIT 1",
            (match_id,),
        )
        row = cur2.fetchone()
        end_iso: Optional[str] = None
        if row and row[0]:
            dt = parse_dt(row[0])
            if dt is not None:
                end_iso = to_utc_iso(dt)
                updated += 1
            else:
                no_end += 1
        else:
            # 检查是否有任何价格数据
            cnt = conn.execute(
                "SELECT COUNT(*) FROM price_snapshots WHERE match_id=?", (match_id,)
            ).fetchone()[0]
            if cnt == 0:
                no_prices += 1
            else:
                no_end += 1

        try:
            conn.execute(
                "UPDATE matches SET real_end_time=? WHERE match_id=?",
                (end_iso, match_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[WARN] 写入 real_end_time 失败 match={match_id}: {exc}")

        if i % 50 == 0 or i == len(match_ids):
            print(f"[进度] {i}/{len(match_ids)} 已处理 (推断到结束时间 {updated})")

    conn.close()
    print(f"[完成] 推断到 real_end_time: {updated}，无价格触及阈值: {no_end}，无价格数据: {no_prices}")
    return {"total": len(match_ids), "updated": updated, "no_end": no_end, "no_prices": no_prices}


# ----------------------------------------------------------------------
# 步骤3：标注价格快照比赛状态
# ----------------------------------------------------------------------

def step3_classify_snapshots() -> Dict[str, int]:
    """对每个 price_snapshot 标注状态并统计分布。"""
    print("\n" + "=" * 80)
    print("步骤3：标注价格快照的比赛状态")
    print("-" * 80)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 加载每场比赛的 real_start_time / real_end_time
    cur = conn.execute(
        "SELECT match_id, real_start_time, real_end_time FROM matches"
    )
    match_times: Dict[str, Tuple[Optional[datetime], Optional[datetime]]] = {}
    for r in cur.fetchall():
        match_times[r["match_id"]] = (
            parse_dt(r["real_start_time"]),
            parse_dt(r["real_end_time"]),
        )

    counts = {"pre_match": 0, "in_match": 0, "post_match": 0, "unknown": 0}
    total = 0
    cur = conn.execute(
        "SELECT match_id, recorded_at FROM price_snapshots"
    )
    for r in cur:
        total += 1
        rs, re_ = match_times.get(r["match_id"], (None, None))
        state = classify_state(parse_dt(r["recorded_at"]), rs, re_)
        counts[state] = counts.get(state, 0) + 1

    conn.close()
    print(f"[完成] 价格快照总数: {total}")
    for k in ("pre_match", "in_match", "post_match", "unknown"):
        print(f"   {k:12}: {counts[k]} ({pct(counts[k], total)})")
    return {"total": total, **counts}


# ----------------------------------------------------------------------
# 步骤4：重新分析模拟开单数据
# ----------------------------------------------------------------------

def _empty_trade_group() -> Dict[str, Any]:
    return {"total": 0, "settled": 0, "wins": 0, "losses": 0, "pnl": 0.0}


def step4_analyze_trades(windows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """重新分析模拟开单数据。"""
    print("\n" + "=" * 80)
    print("步骤4：重新分析模拟开单数据")
    print("-" * 80)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 比赛时间映射
    cur = conn.execute(
        "SELECT match_id, game, real_start_time, real_end_time FROM matches"
    )
    match_info: Dict[str, Dict[str, Any]] = {}
    for r in cur.fetchall():
        match_info[r["match_id"]] = {
            "game": r["game"] or "unknown",
            "real_start": parse_dt(r["real_start_time"]),
            "real_end": parse_dt(r["real_end_time"]),
        }

    # 信号映射：signal_id -> (signal_name, original_window, original_minutes,
    #                        match_id, detected_at) 用于后续重算窗口分布
    cur = conn.execute(
        f"SELECT id, match_id, signal_name, window_label, minutes_since_start, "
        f"detected_at FROM {SIGNALS_TABLE}"
    )
    signal_info: Dict[int, Dict[str, Any]] = {}
    all_signals: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        info = {
            "signal_name": r["signal_name"] or "unknown",
            "orig_window": r["window_label"] or "unknown",
            "orig_minutes": r["minutes_since_start"],
            "match_id": r["match_id"],
            "detected_at": r["detected_at"],
        }
        signal_info[r["id"]] = info
        all_signals.append(info)

    # 加载全部交易
    cur = conn.execute(
        f"SELECT id, match_id, signal_id, buy_team, buy_price, notional_usd, "
        f"opened_at, settled, pnl_usd FROM {TRADES_TABLE}"
    )
    all_trades = [dict(r) for r in cur.fetchall()]
    conn.close()

    raw_total = len(all_trades)
    print(f"[INFO] 原始交易数: {raw_total}")

    # ---- 修正前交易统计（全部交易，使用 DB 原始字段）----
    before = _empty_trade_group()
    for t in all_trades:
        before["total"] += 1
        settled = bool(t.get("settled"))
        pnl = t.get("pnl_usd")
        if settled:
            before["settled"] += 1
            if pnl is not None and pnl > 0:
                before["wins"] += 1
            else:
                before["losses"] += 1
            if pnl is not None:
                before["pnl"] += pnl

    # ---- 过滤异常交易 ----
    filtered_out_price = 0
    filtered_out_pnl = 0
    kept_trades: List[Dict[str, Any]] = []
    for t in all_trades:
        bp = t.get("buy_price")
        pnl = t.get("pnl_usd")
        if bp is None or bp < BUY_PRICE_MIN or bp > BUY_PRICE_MAX:
            filtered_out_price += 1
            continue
        if pnl is not None and abs(pnl) > PNL_ABS_MAX:
            filtered_out_pnl += 1
            continue
        kept_trades.append(t)

    print(f"[INFO] 过滤 buy_price 越界 (<{BUY_PRICE_MIN} 或 >{BUY_PRICE_MAX}): {filtered_out_price}")
    print(f"[INFO] 过滤 |pnl_usd| > {PNL_ABS_MAX}: {filtered_out_pnl}")
    print(f"[INFO] 保留交易数: {len(kept_trades)}")

    # ---- 修正后交易统计 ----
    after = _empty_trade_group()
    by_state: Dict[str, Dict[str, Any]] = {}
    by_game: Dict[str, Dict[str, Any]] = {}
    by_signal: Dict[str, Dict[str, Any]] = {}
    by_state_game: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_state_signal: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for t in kept_trades:
        match_id = t.get("match_id")
        m = match_info.get(match_id, {})
        rs = m.get("real_start")
        re_ = m.get("real_end")
        game = m.get("game", "unknown")
        opened_dt = parse_dt(t.get("opened_at"))
        state = classify_state(opened_dt, rs, re_)

        sig = signal_info.get(t.get("signal_id"), {})
        signal_name = sig.get("signal_name", "unknown")

        settled = bool(t.get("settled"))
        pnl = t.get("pnl_usd") or 0.0

        after["total"] += 1
        if settled:
            after["settled"] += 1
            if pnl > 0:
                after["wins"] += 1
            else:
                after["losses"] += 1
            after["pnl"] += pnl

        # 分组累加
        def accum(bucket: Dict[str, Any], key: Any) -> None:
            g = bucket.setdefault(key, _empty_trade_group())
            g["total"] += 1
            if settled:
                g["settled"] += 1
                if pnl > 0:
                    g["wins"] += 1
                else:
                    g["losses"] += 1
                g["pnl"] += pnl

        accum(by_state, state)
        accum(by_game, game)
        accum(by_signal, signal_name)
        accum(by_state_game, (state, game))
        accum(by_state_signal, (state, signal_name))

    # ---- 信号窗口分布重算（基于 real_start_time）----
    before_signal_window_dist: Dict[str, int] = {}
    after_signal_window_dist: Dict[str, int] = {}
    before_signal_minutes: List[float] = []
    after_signal_minutes: List[float] = []
    for s in all_signals:
        ow = s.get("orig_window", "unknown")
        before_signal_window_dist[ow] = before_signal_window_dist.get(ow, 0) + 1
        om = s.get("orig_minutes")
        if om is not None:
            before_signal_minutes.append(float(om))

        m = match_info.get(s.get("match_id"), {})
        rs = m.get("real_start")
        det_dt = parse_dt(s.get("detected_at"))
        new_minutes: Optional[float] = None
        if rs is not None and det_dt is not None:
            new_minutes = (det_dt - rs).total_seconds() / 60.0
            after_signal_minutes.append(new_minutes)
        new_window = classify_window(new_minutes, windows)
        after_signal_window_dist[new_window] = after_signal_window_dist.get(new_window, 0) + 1

    result = {
        "raw_total": raw_total,
        "filtered_out_price": filtered_out_price,
        "filtered_out_pnl": filtered_out_pnl,
        "kept_total": len(kept_trades),
        "before": before,
        "after": after,
        "before_window_dist": before_signal_window_dist,
        "after_window_dist": after_signal_window_dist,
        "before_avg_minutes": (sum(before_signal_minutes) / len(before_signal_minutes)) if before_signal_minutes else None,
        "after_avg_minutes": (sum(after_signal_minutes) / len(after_signal_minutes)) if after_signal_minutes else None,
        "by_state": by_state,
        "by_game": by_game,
        "by_signal": by_signal,
        "by_state_game": by_state_game,
        "by_state_signal": by_state_signal,
        "windows": windows,
    }

    print(f"[完成] 修正前: {before['total']}单, 结算{before['settled']}, "
          f"胜率{pct(before['wins'], before['settled'])}, PnL {before['pnl']:+.2f}")
    print(f"[完成] 修正后: {after['total']}单, 结算{after['settled']}, "
          f"胜率{pct(after['wins'], after['settled'])}, PnL {after['pnl']:+.2f}")
    return result


# ----------------------------------------------------------------------
# 数据概览
# ----------------------------------------------------------------------

def collect_overview() -> Dict[str, Any]:
    """收集数据概览信息。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total_matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    has_start = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE real_start_time IS NOT NULL"
    ).fetchone()[0]
    has_end = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE real_end_time IS NOT NULL"
    ).fetchone()[0]

    # 平均赛前时长：real_start - 该比赛最早价格快照
    cur = conn.execute(
        """
        SELECT m.match_id, m.real_start_time, m.game,
               (SELECT MIN(ps.recorded_at) FROM price_snapshots ps WHERE ps.match_id = m.match_id) AS first_price
        FROM matches m
        WHERE m.real_start_time IS NOT NULL
        """
    )
    pre_durations_min: List[float] = []
    game_set: Dict[str, int] = {}
    for r in cur.fetchall():
        rs = parse_dt(r["real_start_time"])
        fp = parse_dt(r["first_price"])
        if rs and fp:
            delta = (rs - fp).total_seconds() / 60.0
            if delta >= 0:
                pre_durations_min.append(delta)
        g = r["game"] or "unknown"
        game_set[g] = game_set.get(g, 0) + 1

    avg_pre_minutes = (sum(pre_durations_min) / len(pre_durations_min)) if pre_durations_min else None

    # 价格快照总数 & 交易总数
    total_prices = conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
    total_trades = conn.execute(f"SELECT COUNT(*) FROM {TRADES_TABLE}").fetchone()[0]
    total_signals = conn.execute(f"SELECT COUNT(*) FROM {SIGNALS_TABLE}").fetchone()[0]

    conn.close()
    return {
        "total_matches": total_matches,
        "has_real_start": has_start,
        "has_real_end": has_end,
        "real_start_ratio": pct(has_start, total_matches),
        "real_end_ratio": pct(has_end, total_matches),
        "avg_pre_minutes": avg_pre_minutes,
        "game_dist": game_set,
        "total_prices": total_prices,
        "total_trades": total_trades,
        "total_signals": total_signals,
    }


# ----------------------------------------------------------------------
# 步骤5：生成 HTML 报告
# ----------------------------------------------------------------------

def _trade_group_rows(group: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """按 total 降序返回 (key, stats) 列表。"""
    return sorted(group.items(), key=lambda kv: kv[1]["total"], reverse=True)


def _group_table(rows: List[Tuple[str, Dict[str, Any]]], key_title: str) -> str:
    if not rows:
        return "<p class='muted'>无数据</p>"
    body = []
    for key, g in rows:
        wr = pct(g["wins"], g["settled"])
        body.append(
            f"<tr><td>{html.escape(str(key))}</td>"
            f"<td>{g['total']}</td><td>{g['settled']}</td>"
            f"<td>{g['wins']}</td><td>{g['losses']}</td>"
            f"<td>{wr}</td>"
            f"<td class='{('pos' if g['pnl'] >= 0 else 'neg')}'>{g['pnl']:+.2f}</td></tr>"
        )
    return f"""
    <table class="data-table">
      <thead><tr><th>{key_title}</th><th>总单数</th><th>已结算</th>
      <th>胜</th><th>负</th><th>胜率</th><th>累计 PnL (USD)</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>"""


def _state_game_table(by_state_game: Dict[Tuple[str, str], Dict[str, Any]]) -> str:
    states = ["pre_match", "in_match", "post_match", "unknown"]
    games = sorted({g for (_, g) in by_state_game.keys()})
    rows = []
    for st in states:
        cells = [f"<td class='state-{st}'>{st}</td>"]
        for g in games:
            gstat = by_state_game.get((st, g))
            if gstat and gstat["total"]:
                wr = pct(gstat["wins"], gstat["settled"])
                cells.append(
                    f"<td>{gstat['total']}单<br><span class='sub'>{wr} | "
                    f"{gstat['pnl']:+.2f}</span></td>"
                )
            else:
                cells.append("<td class='muted'>-</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    header = "<thead><tr><th>状态 \\ 游戏</th>" + "".join(
        f"<th>{html.escape(g)}</th>" for g in games
    ) + "</tr></thead>"
    return f"<table class='data-table matrix'>{header}<tbody>{''.join(rows)}</tbody></table>"


def _state_signal_table(by_state_signal: Dict[Tuple[str, str], Dict[str, Any]]) -> str:
    states = ["pre_match", "in_match", "post_match", "unknown"]
    signals = sorted({s for (_, s) in by_state_signal.keys()})
    if not signals:
        return "<p class='muted'>无数据</p>"
    rows = []
    for st in states:
        cells = [f"<td class='state-{st}'>{st}</td>"]
        for s in signals:
            gstat = by_state_signal.get((st, s))
            if gstat and gstat["total"]:
                wr = pct(gstat["wins"], gstat["settled"])
                cells.append(
                    f"<td>{gstat['total']}<br><span class='sub'>{wr}</span></td>"
                )
            else:
                cells.append("<td class='muted'>-</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    header = "<thead><tr><th>状态 \\ 信号</th>" + "".join(
        f"<th>{html.escape(s)}</th>" for s in signals
    ) + "</tr></thead>"
    return f"<table class='data-table matrix'>{header}<tbody>{''.join(rows)}</tbody></table>"


def _window_dist_table(before_dist: Dict[str, int], after_dist: Dict[str, int]) -> str:
    keys = ["early", "mid", "late", "unknown"]
    btotal = sum(before_dist.values()) or 1
    atotal = sum(after_dist.values()) or 1
    rows = []
    for k in keys:
        b = before_dist.get(k, 0)
        a = after_dist.get(k, 0)
        rows.append(
            f"<tr><td>{k}</td><td>{b} ({pct(b, btotal)})</td>"
            f"<td>{a} ({pct(a, atotal)})</td></tr>"
        )
    return f"""
    <table class="data-table">
      <thead><tr><th>窗口</th><th>修正前 (原始 window_label)</th>
      <th>修正后 (基于 real_start_time 重算)</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def _compare_table(before: Dict[str, Any], after: Dict[str, Any],
                   before_avg: Optional[float], after_avg: Optional[float]) -> str:
    rows = [
        ("交易总数", str(before["total"]), str(after["total"])),
        ("已结算数", str(before["settled"]), str(after["settled"])),
        ("胜率", pct(before["wins"], before["settled"]),
         pct(after["wins"], after["settled"])),
        ("累计 PnL (USD)", f"{before['pnl']:+.2f}", f"{after['pnl']:+.2f}"),
        ("平均 minutes_since_start",
         f"{before_avg:.1f}" if before_avg is not None else "N/A",
         f"{after_avg:.1f}" if after_avg is not None else "N/A"),
    ]
    body = "".join(
        f"<tr><td>{html.escape(n)}</td><td>{b}</td><td>{a}</td></tr>"
        for n, b, a in rows
    )
    return f"""
    <table class="data-table">
      <thead><tr><th>指标</th><th>修正前</th><th>修正后</th></tr></thead>
      <tbody>{body}</tbody>
    </table>"""


def _snapshot_state_table(snap: Dict[str, int]) -> str:
    total = snap.get("total", 0) or 1
    rows = []
    for k in ("pre_match", "in_match", "post_match", "unknown"):
        v = snap.get(k, 0)
        rows.append(
            f"<tr><td class='state-{k}'>{k}</td><td>{v}</td><td>{pct(v, total)}</td></tr>"
        )
    return f"""
    <table class="data-table">
      <thead><tr><th>状态</th><th>快照数</th><th>占比</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def generate_report(overview: Dict[str, Any], snap_stats: Dict[str, int],
                    trade_stats: Dict[str, Any], step1: Dict[str, int],
                    step2: Dict[str, int]) -> None:
    print("\n" + "=" * 80)
    print("步骤5：生成 HTML 报告")
    print("-" * 80)

    before = trade_stats["before"]
    after = trade_stats["after"]

    avg_pre = overview["avg_pre_minutes"]
    avg_pre_str = f"{avg_pre:.1f} 分钟 ({avg_pre / 60:.2f} 小时)" if avg_pre is not None else "N/A"

    game_dist_str = "、".join(
        f"{html.escape(g)}: {c}" for g, c in sorted(overview["game_dist"].items())
    )

    # 结论与建议
    conclusions: List[str] = []
    if overview["real_start_ratio"] != "N/A" and overview["total_matches"]:
        ratio_val = overview["has_real_start"] / overview["total_matches"]
        if ratio_val >= 0.9:
            conclusions.append(f"real_start_time 覆盖率良好（{overview['real_start_ratio']}），"
                               f"时间修正基础可靠。")
        else:
            conclusions.append(f"real_start_time 覆盖率仅 {overview['real_start_ratio']}，"
                               f"部分比赛 API 无法获取 startTime，建议补充数据源或人工核对。")
    if after["settled"]:
        wr_after = after["wins"] / after["settled"]
        wr_before = before["wins"] / before["settled"] if before["settled"] else 0
        if wr_after > wr_before + 0.03:
            conclusions.append("修正后胜率高于修正前，说明原 start_time 的时间窗口划分存在偏差，"
                               "in_match/pre_match 混分影响了策略评估。")
        elif wr_after < wr_before - 0.03:
            conclusions.append("修正后胜率低于修正前，原统计可能高估了策略表现，建议以修正后为准。")
        else:
            conclusions.append("修正前后胜率接近，时间修正对总体胜率影响有限，"
                               "但分阶段（pre/in/post）统计仍有参考价值。")
    if trade_stats["filtered_out_price"]:
        conclusions.append(f"过滤了 {trade_stats['filtered_out_price']} 笔 buy_price 越界交易，"
                           f"这些多为临近结算的极端价格，不应计入策略评估。")
    # 阶段建议
    by_state = trade_stats["by_state"]
    in_match = by_state.get("in_match", _empty_trade_group())
    pre_match = by_state.get("pre_match", _empty_trade_group())
    if in_match["total"] and pre_match["total"]:
        wr_in = in_match["wins"] / in_match["settled"] if in_match["settled"] else 0
        wr_pre = pre_match["wins"] / pre_match["settled"] if pre_match["settled"] else 0
        if wr_in > wr_pre + 0.05:
            conclusions.append("in_match 阶段胜率明显高于 pre_match，建议策略侧重赛中开单。")
        elif wr_pre > wr_in + 0.05:
            conclusions.append("pre_match 阶段胜率明显高于 in_match，建议策略侧重赛前布局。")
    if overview["has_real_end"] < overview["total_matches"] * 0.5:
        conclusions.append("real_end_time 覆盖率偏低，部分比赛价格未触及 0.99/0.01 阈值，"
                           "post_match 统计可能不完整。")

    conclusion_html = "".join(f"<li>{html.escape(c)}</li>" for c in conclusions) or "<li class='muted'>无</li>"

    state_game_html = _state_game_table(trade_stats["by_state_game"])
    state_signal_html = _state_signal_table(trade_stats["by_state_signal"])

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>电竞形态策略数据清洗与分析报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
            "Helvetica Neue", Arial, sans-serif; margin: 0; padding: 0;
         background: #f4f6fa; color: #2c3e50; line-height: 1.6; }}
  .container {{ max-width: 1080px; margin: 0 auto; padding: 32px 24px 64px; }}
  h1 {{ font-size: 28px; color: #1a2744; margin: 0 0 4px; }}
  h2 {{ font-size: 21px; color: #1a2744; margin: 36px 0 14px;
        padding-bottom: 8px; border-bottom: 2px solid #4a6cf7; }}
  h3 {{ font-size: 16px; color: #34495e; margin: 22px 0 10px; }}
  .subtitle {{ color: #7f8c9b; font-size: 14px; margin-bottom: 8px; }}
  .gen-time {{ color: #95a5b8; font-size: 13px; margin-bottom: 24px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px; margin: 18px 0; }}
  .card {{ background: #fff; border-radius: 12px; padding: 18px 20px;
           box-shadow: 0 2px 8px rgba(20,40,80,.06); }}
  .card .label {{ font-size: 13px; color: #7f8c9b; }}
  .card .value {{ font-size: 24px; font-weight: 600; color: #1a2744; margin-top: 4px; }}
  .card .sub {{ font-size: 12px; color: #95a5b8; margin-top: 2px; }}
  table.data-table {{ width: 100%; border-collapse: collapse; margin: 12px 0;
                      background: #fff; border-radius: 10px; overflow: hidden;
                      box-shadow: 0 2px 8px rgba(20,40,80,.06); font-size: 14px; }}
  table.data-table th {{ background: #1a2744; color: #fff; text-align: left;
                         padding: 11px 14px; font-weight: 600; }}
  table.data-table td {{ padding: 10px 14px; border-top: 1px solid #eef1f6; }}
  table.data-table tbody tr:nth-child(even) {{ background: #fafbfd; }}
  table.data-table tbody tr:hover {{ background: #f0f4ff; }}
  table.matrix td {{ text-align: center; }}
  .pos {{ color: #18a058; font-weight: 600; }}
  .neg {{ color: #d03050; font-weight: 600; }}
  .muted {{ color: #95a5b8; }}
  .sub {{ color: #95a5b8; font-size: 12px; }}
  .state-pre_match {{ color: #4a6cf7; font-weight: 600; }}
  .state-in_match {{ color: #18a058; font-weight: 600; }}
  .state-post_match {{ color: #d03050; font-weight: 600; }}
  .state-unknown {{ color: #95a5b8; font-weight: 600; }}
  .note {{ background: #fff8e6; border-left: 4px solid #f0a020; padding: 12px 16px;
           border-radius: 6px; margin: 14px 0; font-size: 13px; color: #6b5318; }}
  ul {{ margin: 8px 0; padding-left: 22px; }}
  li {{ margin: 6px 0; }}
  .section {{ background: #fff; border-radius: 12px; padding: 20px 24px;
              margin: 16px 0; box-shadow: 0 2px 8px rgba(20,40,80,.06); }}
  .footer {{ text-align: center; color: #95a5b8; font-size: 12px; margin-top: 40px; }}
</style>
</head>
<body>
<div class="container">
  <h1>电竞形态策略数据清洗与分析报告</h1>
  <div class="subtitle">基于 Polymarket event.startTime 修正比赛实际开始/结束时间，重新评估模拟开单表现</div>
  <div class="gen-time">生成时间：{html.escape(now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"))}</div>

  <h2>1. 数据概览</h2>
  <div class="cards">
    <div class="card"><div class="label">总比赛数</div><div class="value">{overview['total_matches']}</div></div>
    <div class="card"><div class="label">有 real_start_time</div><div class="value">{overview['has_real_start']}</div>
      <div class="sub">占比 {overview['real_start_ratio']}</div></div>
    <div class="card"><div class="label">有 real_end_time</div><div class="value">{overview['has_real_end']}</div>
      <div class="sub">占比 {overview['real_end_ratio']}</div></div>
    <div class="card"><div class="label">平均赛前时长</div><div class="value">{avg_pre_str}</div></div>
    <div class="card"><div class="label">价格快照总数</div><div class="value">{overview['total_prices']:,}</div></div>
    <div class="card"><div class="label">模拟开单总数</div><div class="value">{overview['total_trades']:,}</div></div>
    <div class="card"><div class="label">形态信号总数</div><div class="value">{overview['total_signals']:,}</div></div>
  </div>
  <div class="section">
    <h3>游戏分布</h3>
    <p>{game_dist_str or '无'}</p>
    <h3>步骤执行情况</h3>
    <ul>
      <li>步骤1（API 获取 startTime）：成功 {step1['fetched']}，失败/空 {step1['failed']}，
          已有 {step1['already']}（共 {step1['total']}）</li>
      <li>步骤2（推断结束时间）：成功 {step2['updated']}，无价格触及阈值 {step2['no_end']}，
          无价格数据 {step2['no_prices']}</li>
    </ul>
  </div>

  <h2>2. 价格快照三状态分布</h2>
  <div class="section">
    {_snapshot_state_table(snap_stats)}
    <p class="note">说明：pre_match = recorded_at &lt; real_start_time；
      in_match = real_start_time ≤ recorded_at &lt; real_end_time；
      post_match = recorded_at ≥ real_end_time；
      real_start_time 为空时标记 unknown。</p>
  </div>

  <h2>3. 修正后的模拟开单统计</h2>
  <div class="section">
    <h3>3.1 按比赛状态分组</h3>
    {_group_table(_trade_group_rows(trade_stats['by_state']), '比赛状态')}
    <h3>3.2 按游戏类型分组</h3>
    {_group_table(_trade_group_rows(trade_stats['by_game']), '游戏')}
    <h3>3.3 按信号类型分组</h3>
    {_group_table(_trade_group_rows(trade_stats['by_signal']), '信号类型')}
    <h3>3.4 状态 × 游戏交叉表</h3>
    {state_game_html}
    <h3>3.5 状态 × 信号交叉表</h3>
    {state_signal_html}
  </div>

  <h2>4. 修正后的信号窗口分布</h2>
  <div class="section">
    {_window_dist_table(trade_stats['before_window_dist'], trade_stats['after_window_dist'])}
    <p class="note">统计对象为形态信号（morphology_signals）。窗口定义：early 0-30 分钟、mid 30-90 分钟、late ≥90 分钟（来自 config morphology.windows）。
      修正前使用 DB 原始 window_label；修正后 minutes_since_start = (detected_at - real_start_time) 并据此重新归窗。</p>
  </div>

  <h2>5. 修正前 vs 修正后关键指标对比</h2>
  <div class="section">
    {_compare_table(before, after, trade_stats['before_avg_minutes'], trade_stats['after_avg_minutes'])}
    <p class="note">交易类指标（总数/结算/胜率/PnL）：修正前 = 全部交易；修正后 = 过滤异常交易（buy_price ∈ [0.05, 0.95] 且 |pnl| ≤ 10000）。
      平均 minutes_since_start 指标基于形态信号：修正前 = signals.minutes_since_start 均值；修正后 = (detected_at - real_start_time) 均值。</p>
  </div>

  <h2>6. 结论与建议</h2>
  <div class="section">
    <ul>{conclusion_html}</ul>
  </div>

  <div class="footer">报告由 scripts/clean_and_analyze.py 自动生成 · 数据库：esports_history.db</div>
</div>
</body>
</html>
"""

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"[完成] HTML 报告已生成: {REPORT_PATH}")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] 数据库不存在: {DB_PATH}")
        return 1
    print(f"[INFO] 数据库: {DB_PATH}")
    print(f"[INFO] 大小: {os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB")

    # 加载配置与 Polymarket 客户端
    cfg = load_config(base_dir=PROJECT_DIR)
    pm = cfg.get("polymarket", {}) or {}
    gamma = PolymarketClient(
        api_base=pm.get("gamma_api_base"),
        timeout=pm.get("timeout", 10),
        config=pm,
    )

    # 信号窗口配置
    morph_cfg = cfg.get("morphology", {}) or {}
    windows = morph_cfg.get("windows") or DEFAULT_WINDOWS

    # 步骤1
    step1 = step1_fetch_real_start_times(gamma)
    # 步骤2
    step2 = step2_infer_real_end_times()
    # 步骤3
    snap_stats = step3_classify_snapshots()
    # 步骤4
    trade_stats = step4_analyze_trades(windows)
    # 概览
    overview = collect_overview()
    # 步骤5
    generate_report(overview, snap_stats, trade_stats, step1, step2)

    print("\n" + "=" * 80)
    print("[全部完成]")
    print(f"  报告路径: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
