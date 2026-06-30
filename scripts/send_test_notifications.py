"""发送测试通知消息，验证 TG 链路。

使用 config.yaml 中的 bot_token/chat_id 发送全部 7 类消息（P0-P5）。
每次发送间隔 1 秒，避免 Telegram 限流。

用法：
    python scripts/send_test_notifications.py
"""
from __future__ import annotations

import os
import sys
import time

# 将项目根目录加入 sys.path
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from esports_monitor.config.manager import load_config
from esports_monitor.notification.telegram import (
    TelegramNotifier,
    create_notifier_from_config,
)


def main() -> int:
    """发送全部 7 类测试消息。"""
    config = load_config(PROJECT_DIR)
    notifier = create_notifier_from_config(config)

    if not notifier.can_send():
        print("[ERROR] Telegram 未配置或未启用，请检查 config.yaml")
        return 1

    print(f"[INFO] 使用 bot_token={notifier.bot_token[:8]}*** chat_id={notifier.chat_id}")
    print("[INFO] 开始发送测试消息（共 7 条，间隔 1 秒）...")
    print("-" * 60)

    results = []

    # 1. P0 - 主循环心跳
    msg = (
        "✅ <b>[主循环心跳]</b> 正常\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "处理比赛: 5 场\n"
        "价格快照: +10\n"
        "盘口快照: +8\n"
        "信号告警: 1 个\n"
        "失败次数: 0\n"
        "本轮耗时: 12.3s\n"
        "下次间隔: 30s\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⏰ 测试消息 北京时间"
    )
    results.append(("P0 主循环心跳", notifier.send_message(msg)))
    time.sleep(1)

    # 2. P0b - 错误告警
    results.append((
        "错误告警",
        notifier.send_error_alert("这是测试错误告警：示例异常信息（仅测试链路）")
    ))
    time.sleep(1)

    # 3. P2 - 市场发现报告
    results.append((
        "P2 市场发现报告",
        notifier.send_discovery_report(
            new_count=3,
            existing_count=8,
            skipped_count=2,
            errors=0,
            game_breakdown={"cs2": 1, "dota2": 1, "lol": 1},
            new_matches_preview=[
                {"game": "cs2", "team_a": "NaVi", "team_b": "Vitality", "price_a": 0.55},
                {"game": "lol", "team_a": "T1", "team_b": "Gen.G", "price_a": 0.48},
                {"game": "dota2", "team_a": "Team Spirit", "team_b": "PSG.LGD", "price_a": 0.62},
            ],
        )
    ))
    time.sleep(1)

    # 4. P3 - 形态跳过原因（4 种）
    skip_cases = [
        ("cooldown", "剩余冷却 12.5 分钟"),
        ("no_window", "当前进度 105 分钟，无匹配窗口"),
        ("no_data", "价格快照仅 3 点，需至少 5 点"),
        ("buy_price_filtered", "买入价 0.96 超出 [0.05, 0.95]"),
    ]
    for reason, detail in skip_cases:
        results.append((
            f"P3 形态跳过-{reason}",
            notifier.send_morphology_skipped(
                match_id="test_match_001",
                team_a="T1",
                team_b="Gen.G",
                reason=reason,
                detail=detail,
            )
        ))
        time.sleep(1)

    # 5. 信号告警（原有功能，确认未破坏）
    from esports_monitor.morphology.models import (
        MorphologyAlert,
        MorphologySignal,
    )
    fake_sig = MorphologySignal(
        signal_name="breakout",
        signal_label="突破",
        window_label="mid",
        direction="A",
        buy_team="T1",
        buy_price=0.45,
        predicted_win_prob=0.62,
        predicted_pnl=0.25,
    )
    fake_alert = MorphologyAlert(
        match_id="test_match_002",
        team_a="T1",
        team_b="Gen.G",
        window_label="mid",
        minutes_since_start=45.0,
        strongest_signal=fake_sig,
        total_strength=2,
    )
    results.append(("信号告警", notifier.send_signal_alert(fake_alert)))
    time.sleep(1)

    # 6. 结算通知
    from esports_monitor.morphology.models import MorphologyTrade
    fake_trade = MorphologyTrade(
        match_id="test_match_003",
        signal_id=1,
        buy_team="T1",
        buy_price=0.45,
        quantity=222.2,
        notional_usd=100.0,
        opened_at="2026-06-29T10:00:00+00:00",
        settled=1,
        pnl_usd=122.22,
    )
    results.append((
        "结算通知",
        notifier.send_settlement_notification(
            match_id="test_match_003",
            team_a="T1",
            team_b="Gen.G",
            winning_team="T1",
            trade=fake_trade,
        )
    ))
    time.sleep(1)

    # 7. P5 - 状态报告（含分组胜率）
    grouped_stats = {
        "by_signal": [
            {"group_key": "breakout", "total": 10, "settled": 8, "wins": 6,
             "losses": 2, "total_pnl": 75.5},
            {"group_key": "momentum_catcher", "total": 5, "settled": 4, "wins": 2,
             "losses": 2, "total_pnl": -10.0},
            {"group_key": "stable_spread", "total": 3, "settled": 3, "wins": 3,
             "losses": 0, "total_pnl": 45.0},
        ],
        "by_game": [
            {"group_key": "cs2", "total": 8, "settled": 6, "wins": 4,
             "losses": 2, "total_pnl": 50.5},
            {"group_key": "lol", "total": 7, "settled": 6, "wins": 5,
             "losses": 1, "total_pnl": 65.0},
            {"group_key": "dota2", "total": 3, "settled": 3, "wins": 2,
             "losses": 1, "total_pnl": -5.0},
        ],
        "by_window": [
            {"group_key": "mid", "total": 12, "settled": 10, "wins": 7,
             "losses": 3, "total_pnl": 80.0},
            {"group_key": "early", "total": 4, "settled": 3, "wins": 2,
             "losses": 1, "total_pnl": 20.0},
            {"group_key": "late", "total": 2, "settled": 2, "wins": 2,
             "losses": 0, "total_pnl": 10.5},
        ],
    }
    results.append((
        "P5 状态报告",
        notifier.send_status_report(
            live_count=5,
            game_breakdown={"cs2": 2, "lol": 2, "dota2": 1},
            recent_signals=[
                {"signal_name": "breakout"},
                {"signal_name": "breakout"},
                {"signal_name": "momentum_catcher"},
            ],
            trade_stats={
                "total": 18, "settled": 15, "wins": 11, "losses": 4,
                "total_pnl": 110.5,
            },
            db_size_mb=2.3,
            grouped_stats=grouped_stats,
        )
    ))
    time.sleep(1)

    # 8. P4 - 数据归档报告
    results.append((
        "P4 数据归档报告",
        notifier.send_archive_report(
            archived_rows=15234,
            deleted_rows=15234,
            archived_months=3,
            vacuum_executed=True,
            db_size_before_mb=45.6,
            db_size_after_mb=12.3,
            archive_files_count=12,
        )
    ))

    # 汇总结果
    print("-" * 60)
    print("[INFO] 发送结果汇总：")
    print("-" * 60)
    success = 0
    failed = 0
    for name, ok in results:
        icon = "✅" if ok else "❌"
        status = "成功" if ok else "失败"
        print(f"  {icon} {name}: {status}")
        if ok:
            success += 1
        else:
            failed += 1
    print("-" * 60)
    print(f"[INFO] 共 {len(results)} 条消息：成功 {success}，失败 {failed}")

    if failed > 0:
        print("[WARN] 有消息发送失败，请检查：")
        print("  1. bot_token/chat_id 是否正确")
        print("  2. 网络是否能访问 https://api.telegram.org")
        print("  3. bot 是否已加入目标 chat_id（首次需 /start）")
        return 1

    print("[INFO] ✅ 全部测试消息发送成功，TG 链路畅通！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
