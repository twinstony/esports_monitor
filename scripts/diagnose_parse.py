"""诊断 parse_match_markets 对真实 Gamma API 数据的处理。

用法：python scripts/diagnose_parse.py
"""
from __future__ import annotations

import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from esports_monitor.api.polymarket import PolymarketClient  # noqa: E402


def main() -> int:
    probe_path = os.path.join(PROJECT_DIR, "gamma_probe_result.json")
    if not os.path.exists(probe_path):
        print(f"[ERROR] {probe_path} 不存在，请先运行 probe_gamma.py")
        return 1

    with open(probe_path, "r", encoding="utf-8") as f:
        events = json.load(f)

    print(f"[INFO] 加载 {len(events)} 个事件")
    print("=" * 80)

    client = PolymarketClient()

    total_markets = 0
    total_parsed = 0
    total_skipped = 0
    skip_reasons = {"no_condition": 0, "outcomes_len": 0, "prices_len": 0,
                    "price_zero": 0, "no_team": 0, "exception": 0}

    for event in events:
        slug = event.get("slug", "")
        title = event.get("title", "")[:60]
        markets = event.get("markets") or []
        parsed = client.parse_match_markets(event)
        total_markets += len(markets)
        total_parsed += len(parsed)

        if not parsed:
            # 分析跳过原因
            for m in markets:
                try:
                    cid = m.get("conditionId")
                    if not cid:
                        skip_reasons["no_condition"] += 1
                        continue
                    outcomes = client._parse_json_field(m.get("outcomes", []))
                    prices = client._parse_json_field(m.get("outcomePrices", []))
                    if len(outcomes) != 2:
                        skip_reasons["outcomes_len"] += 1
                        continue
                    if len(prices) != 2:
                        skip_reasons["prices_len"] += 1
                        continue
                    try:
                        pa = float(prices[0])
                        pb = float(prices[1])
                    except (TypeError, ValueError):
                        skip_reasons["exception"] += 1
                        continue
                    if pa <= 0 or pb <= 0:
                        skip_reasons["price_zero"] += 1
                        # 打印价格为 0 的市场
                        q = m.get("question", "")[:70]
                        print(f"  [price_zero] slug={slug[:30]:30} | q={q} | prices={prices}")
                        continue
                    ta, tb = client._parse_team_names(m.get("question", ""))
                    if not ta or not tb:
                        skip_reasons["no_team"] += 1
                        q = m.get("question", "")[:70]
                        print(f"  [no_team] slug={slug[:30]:30} | q={q}")
                        continue
                except Exception:
                    skip_reasons["exception"] += 1
        else:
            # 打印解析成功的
            for m in parsed:
                print(
                    f"  [OK] slug={slug[:30]:30} | {m.team_a:15} vs {m.team_b:15} | "
                    f"pa={m.price_a:.4f} pb={m.price_b:.4f} | end={m.end_date}"
                )

    print("=" * 80)
    print(f"[INFO] 总市场数: {total_markets}")
    print(f"[INFO] 成功解析: {total_parsed}")
    print(f"[INFO] 跳过总数: {total_markets - total_parsed}")
    print(f"[INFO] 跳过原因分布: {skip_reasons}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
