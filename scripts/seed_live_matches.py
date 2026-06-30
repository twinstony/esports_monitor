"""手动插入 3 场"正在进行"的比赛数据，用于全链路验收。

使用 gamma_probe_result.json 中 7 月 1 日 CS2 BO1 比赛的真实 condition_id，
但调整 start_time/end_time 覆盖当前时间，status='live'。

用法：python scripts/seed_live_matches.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import timedelta

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "esports_history.db")
sys.path.insert(0, PROJECT_DIR)

from esports_monitor.utils.time_utils import now_utc, to_utc_iso  # noqa: E402


def main() -> int:
    # 从 probe 结果中提取 3 场 CS2 比赛的 condition_id（2 CS + 1 CS 当 Dota2 用）
    # 实际 Dota2 比赛无法从当前 probe 结果获取（没有活跃的 Dota2 市场）
    # 验收重点：全链路（发现→采集→形态→通知），游戏类型不影响链路验证
    probe_path = os.path.join(PROJECT_DIR, "gamma_probe_result.json")
    with open(probe_path, "r", encoding="utf-8") as f:
        events = json.load(f)

    # 提取 3 场 CS2 BO1 比赛的首个 market（比赛胜负市场）
    target_slugs = [
        "cs2-faze-tyloo-2026-07-01",   # CS 比赛 1
        "cs2-3dmax-nip-2026-07-01",    # CS 比赛 2
        "cs2-9z-eye-2026-07-01",       # 第 3 场（标记为 dota2 验收游戏字段）
    ]

    matches_to_insert = []
    for event in events:
        slug = event.get("slug", "")
        if slug not in target_slugs:
            continue
        markets = event.get("markets") or []
        for m in markets:
            q = m.get("question", "")
            # 只取"比赛胜负"市场（question 含 "Counter-Strike: X vs Y (BO1)"）
            if "Counter-Strike:" in q and " vs " in q and "(BO1)" in q:
                cid = m.get("conditionId")
                token_ids = m.get("clobTokenIds", [])
                if isinstance(token_ids, str):
                    import json as _json
                    token_ids = _json.loads(token_ids)
                prices = m.get("outcomePrices", [])
                if isinstance(prices, str):
                    import json as _json
                    prices = _json.loads(prices)
                if not cid or len(prices) != 2:
                    continue
                # 解析队伍名
                from esports_monitor.api.polymarket import PolymarketClient
                client = PolymarketClient()
                team_a, team_b = client._parse_team_names(q)
                if not team_a or not team_b:
                    continue
                # 清理 team_a（去掉 "Counter-Strike: " 前缀）
                if team_a.startswith("Counter-Strike:"):
                    team_a = team_a.replace("Counter-Strike:", "").strip()
                # 清理 team_b（去掉 "(BO1) - ..." 后缀）
                import re
                team_b = re.split(r"\s*\(BO\d+\)", team_b)[0].strip()

                # 第 3 场标记为 dota2（验收游戏字段，模拟 Dota2 比赛）
                game = "dota2" if slug == "cs2-9z-eye-2026-07-01" else "cs2"

                matches_to_insert.append({
                    "match_id": cid,
                    "slug": slug,
                    "game": game,
                    "team_a": team_a,
                    "team_b": team_b,
                    "condition_id": cid,
                    "token_id_a": token_ids[0] if len(token_ids) >= 1 else None,
                    "token_id_b": token_ids[1] if len(token_ids) >= 2 else None,
                    "price_a": float(prices[0]),
                    "price_b": float(prices[1]),
                })
                break  # 每个 event 只取第一个比赛胜负市场

    if len(matches_to_insert) < 3:
        print(f"[ERROR] 只找到 {len(matches_to_insert)} 场比赛，需要 3 场")
        return 1

    # 调整时间为"正在进行"：start=-1h, end=+3h
    now = now_utc()
    start_iso = to_utc_iso(now - timedelta(hours=1))
    end_iso = to_utc_iso(now + timedelta(hours=3))

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for m in matches_to_insert[:3]:
        cur.execute(
            """
            INSERT OR REPLACE INTO matches
                (match_id, slug, game, team_a, team_b, condition_id,
                 token_id_a, token_id_b, start_time, end_time, status,
                 winning_team, discovered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', NULL, ?, ?)
            """,
            (
                m["match_id"], m["slug"], m["game"],
                m["team_a"], m["team_b"], m["condition_id"],
                m["token_id_a"], m["token_id_b"],
                start_iso, end_iso,
                to_utc_iso(now), to_utc_iso(now),
            ),
        )
        print(
            f"[OK] 插入: {m['game']:6} | {m['team_a']:15} vs {m['team_b']:15} | "
            f"slug={m['slug']:30} | cid={m['match_id'][:14]}... | "
            f"price_a={m['price_a']:.4f}"
        )

    conn.commit()
    print(f"\n[INFO] 共插入 {len(matches_to_insert[:3])} 场 live 比赛")
    print(f"[INFO] start_time={start_iso}")
    print(f"[INFO] end_time={end_iso}")

    # 验证插入
    cur.execute("SELECT match_id, game, team_a, team_b, status FROM matches WHERE status='live'")
    rows = cur.fetchall()
    print(f"\n[INFO] 当前 live 比赛: {len(rows)} 场")
    for r in rows:
        print(f"  {r[1]:6} | {r[2]:15} vs {r[3]:15} | status={r[4]}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
