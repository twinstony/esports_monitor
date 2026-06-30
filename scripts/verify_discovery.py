"""验证修复后的市场发现能找到真实 live 比赛。

直接调用 MarketDiscoveryService.discover()，不启动 monitor 主循环。
"""
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from esports_monitor.api.polymarket import PolymarketClient
from esports_monitor.config.manager import load_config
from esports_monitor.data.sqlite_storage import SQLiteStorage
from esports_monitor.discovery.service import MarketDiscoveryService


def main() -> int:
    config = load_config(PROJECT_DIR)
    discovery_cfg = config.get("discovery") or {}

    print("=" * 70)
    print("市场发现验证（多 tag + API 时间过滤 + 客户端兜底）")
    print("=" * 70)
    print(f"  tag_slugs: {discovery_cfg.get('tag_slugs')}")
    print(f"  live_window_hours: {discovery_cfg.get('live_window_hours')}")
    print(f"  max_match_duration_hours: {discovery_cfg.get('max_match_duration_hours')}")
    print(f"  price_min/max: {discovery_cfg.get('price_min')} / {discovery_cfg.get('price_max')}")

    gamma = PolymarketClient(timeout=20)
    service = MarketDiscoveryService(
        gamma_client=gamma,
        clob_client=None,  # 不做深度筛选，只验证时间窗口
        config=discovery_cfg,
    )

    print("\n执行 discover()...")
    result = service.discover()

    print("\n" + "=" * 70)
    print(f"发现结果: {result.summary}")
    print(f"  new_matches: {len(result.new_matches)}")
    print(f"  skipped: {result.skipped}")
    print(f"  errors: {result.errors}")
    print("=" * 70)

    print("\n发现的比赛列表:")
    for i, m in enumerate(result.new_matches):
        print(
            f"\n  [{i + 1}] {m.game:6} | {m.team_a:30} vs {m.team_b:30}\n"
            f"      slug:      {m.slug}\n"
            f"      cid:       {m.condition_id[:20]}...\n"
            f"      start:     {m.start_time}\n"
            f"      end:       {m.end_time}\n"
            f"      price_a/b: {m.price_a:.4f} / {m.price_b:.4f}\n"
            f"      league:    {m.league}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())


