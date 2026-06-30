"""6 种形态信号 + 特征计算。

参照：polymarket_twitter_monitor/morphology/signals.py
      polymarket_twitter_monitor/scripts/two_zone_morphology_backtest.py

6 种信号签名不变：(A_features, B_features) -> "A" | "B" | None
- A = 领先队特征
- B = 对手队特征
- 返回 "A" 表示买领先队，"B" 表示买对手队，None 表示无信号

注意：特征名沿用推文市场的 mean_12h / ret_6h 等命名（h 代表"小时"），
但在电竞场景中实际对应的是"点数"而非真实小时。保留命名一致性以便复用信号函数。
每个点的时间间隔 = window_hours / N_RESAMPLE。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import MorphologyFeatures

# 重采样点数，与推文市场保持一致
N_RESAMPLE = 48

# 信号函数类型
MorphSignalFn = Callable[[MorphologyFeatures, MorphologyFeatures], Optional[str]]


def compute_morphology_features(prices: List[float], t: int) -> MorphologyFeatures:
    """计算直到索引 t 的形态特征。

    Args:
        prices: 重采样后的价格序列（48 点）
        t: 当前索引

    Returns:
        MorphologyFeatures 含 10 个特征。
    """
    if not prices or t < 0 or t >= len(prices):
        return MorphologyFeatures(
            current=0.0, mean_all=0.0, mean_12h=0.0, mean_24h=0.0,
            ret_6h=0.0, ret_12h=0.0, ret_24h=0.0, ret_total=0.0,
            price_6h_ago=0.0, price_12h_ago=0.0,
        )

    seq = prices[: t + 1]
    n = len(seq)
    current = prices[t]
    mean_all = sum(seq) / n if n > 0 else 0.0

    # 12 点均值（≈ 30min）
    w12_start = max(0, t - 11)
    w12 = prices[w12_start: t + 1]
    mean_12h = sum(w12) / len(w12) if w12 else 0.0

    # 24 点均值（≈ 60min）
    w24_start = max(0, t - 23)
    w24 = prices[w24_start: t + 1]
    mean_24h = sum(w24) / len(w24) if w24 else 0.0

    # 6 点收益率（≈ 15min）
    p6 = prices[max(0, t - 5)]
    ret_6h = (current - p6) / p6 if p6 > 0 else 0.0

    # 12 点收益率
    p12 = prices[max(0, t - 11)]
    ret_12h = (current - p12) / p12 if p12 > 0 else 0.0

    # 24 点收益率
    p24 = prices[max(0, t - 23)]
    ret_24h = (current - p24) / p24 if p24 > 0 else 0.0

    # 累计收益率
    p0 = prices[0]
    ret_total = (current - p0) / p0 if p0 > 0 else 0.0

    return MorphologyFeatures(
        current=current,
        mean_all=mean_all,
        mean_12h=mean_12h,
        mean_24h=mean_24h,
        ret_6h=ret_6h,
        ret_12h=ret_12h,
        ret_24h=ret_24h,
        ret_total=ret_total,
        price_6h_ago=p6,
        price_12h_ago=p12,
    )


# ----------------------------------------------------------------------
# 6 种信号函数
# ----------------------------------------------------------------------


def signal_current_and_recent_leader(
    A: MorphologyFeatures, B: MorphologyFeatures
) -> Optional[str]:
    """信号 1: 强者恒强。

    当前价格更高，且最近 12 点均价也更高。
    """
    if A.current > B.current and A.mean_12h > B.mean_12h:
        return "A"
    if B.current > A.current and B.mean_12h > A.mean_12h:
        return "B"
    return None


def signal_momentum_catcher(
    A: MorphologyFeatures, B: MorphologyFeatures
) -> Optional[str]:
    """信号 2: 后发制人。

    当前落后，但最近 12 点涨幅明显大于对手，且自身为正。
    """
    if (
        A.current < B.current
        and A.ret_12h > B.ret_12h + 0.1
        and A.ret_12h > 0.05
    ):
        return "A"
    if (
        B.current < A.current
        and B.ret_12h > A.ret_12h + 0.1
        and B.ret_12h > 0.05
    ):
        return "B"
    return None


def signal_breakout(
    A: MorphologyFeatures, B: MorphologyFeatures
) -> Optional[str]:
    """信号 3: 突破反超。

    6 点前落后，当前反超，且 6 点涨幅 > 20%。
    """
    if (
        A.price_6h_ago < B.price_6h_ago
        and A.current > B.current
        and A.ret_6h > 0.2
    ):
        return "A"
    if (
        B.price_6h_ago < A.price_6h_ago
        and B.current > A.current
        and B.ret_6h > 0.2
    ):
        return "B"
    return None


def signal_divergence(
    A: MorphologyFeatures, B: MorphologyFeatures
) -> Optional[str]:
    """信号 4: 走势分化。

    一个队伍 12 点上涨 > 5%，另一个 12 点下跌 > 5%。
    """
    if A.ret_12h > 0.05 and B.ret_12h < -0.05:
        return "A"
    if B.ret_12h > 0.05 and A.ret_12h < -0.05:
        return "B"
    return None


def signal_acceleration(
    A: MorphologyFeatures, B: MorphologyFeatures
) -> Optional[str]:
    """信号 5: 加速领先。

    当前领先且 6 点涨幅 > 12 点涨幅（加速），且 12 点涨幅为正。

    注意：推文市场回测显示 acceleration 在 10-24h 和 10-18h 窗口大幅亏损
    （期望 -38% 和 -45.2%），短期加速可能是假突破。电竞市场需重新验证。
    """
    if (
        A.current > B.current
        and A.ret_6h > A.ret_12h
        and A.ret_12h > 0
    ):
        return "A"
    if (
        B.current > A.current
        and B.ret_6h > B.ret_12h
        and B.ret_12h > 0
    ):
        return "B"
    return None


def signal_stable_spread(
    A: MorphologyFeatures, B: MorphologyFeatures
) -> Optional[str]:
    """信号 6: 优势稳定。

    当前领先，且 24 点收益也领先。
    """
    if A.current > B.current and A.ret_24h > B.ret_24h:
        return "A"
    if B.current > A.current and B.ret_24h > A.ret_24h:
        return "B"
    return None


# 信号注册表
SIGNALS: Dict[str, MorphSignalFn] = {
    "current_and_recent_leader": signal_current_and_recent_leader,
    "momentum_catcher": signal_momentum_catcher,
    "breakout": signal_breakout,
    "divergence": signal_divergence,
    "acceleration": signal_acceleration,
    "stable_spread": signal_stable_spread,
}

# 信号中文标签
SIGNAL_LABELS: Dict[str, str] = {
    "current_and_recent_leader": "强者恒强",
    "momentum_catcher": "后发制人",
    "breakout": "突破反超",
    "divergence": "走势分化",
    "acceleration": "加速领先",
    "stable_spread": "优势稳定",
}


def get_signal_fn(name: str) -> Optional[MorphSignalFn]:
    """按名称获取信号函数。"""
    return SIGNALS.get(name)


def get_signal_label(name: str) -> str:
    """获取信号中文标签。"""
    return SIGNAL_LABELS.get(name, name)


# ----------------------------------------------------------------------
# 价格序列重采样
# ----------------------------------------------------------------------


def resample_series(
    points: List[Tuple[str, float]],
    n: int = N_RESAMPLE,
) -> Optional[List[float]]:
    """将不等间隔的价格序列重采样为 n 个等间隔点。

    线性插值。要求至少 5 个去重后的点，否则返回 None。

    Args:
        points: [(recorded_at_iso, price), ...] 升序
        n: 重采样点数

    Returns:
        n 个等间隔价格点；数据不足返回 None。
    """
    if not points or n <= 0:
        return None

    # 去重时间戳（保留最后一个）
    seen: Dict[str, float] = {}
    for ts, price in points:
        seen[ts] = price
    deduped = sorted(seen.items())
    if len(deduped) < 5:
        return None

    # 将 ISO 时间字符串转为秒数（相对首个时间戳）
    from ..utils.time_utils import from_utc_iso

    base_dt = from_utc_iso(deduped[0][0])
    last_dt = from_utc_iso(deduped[-1][0])
    if base_dt is None or last_dt is None:
        return None
    total_seconds = (last_dt - base_dt).total_seconds()
    if total_seconds <= 0:
        return None

    # 构造 (seconds, price) 序列
    times_secs: List[float] = []
    values: List[float] = []
    for ts, price in deduped:
        dt = from_utc_iso(ts)
        if dt is None:
            continue
        times_secs.append((dt - base_dt).total_seconds())
        values.append(float(price))
    if len(values) < 5:
        return None

    # 线性插值到 n 个等间隔点
    result: List[float] = []
    for i in range(n):
        target = total_seconds * i / (n - 1) if n > 1 else 0.0
        # 二分查找上下界
        lo = 0
        hi = len(times_secs) - 1
        # 找到 times_secs[lo] <= target <= times_secs[hi]
        if target <= times_secs[0]:
            result.append(values[0])
            continue
        if target >= times_secs[-1]:
            result.append(values[-1])
            continue
        # 二分
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if times_secs[mid] <= target:
                lo = mid
            else:
                hi = mid
        # 线性插值
        t0, t1 = times_secs[lo], times_secs[hi]
        v0, v1 = values[lo], values[hi]
        if t1 == t0:
            result.append(v0)
        else:
            ratio = (target - t0) / (t1 - t0)
            result.append(v0 + (v1 - v0) * ratio)
    return result


# ----------------------------------------------------------------------
# 回测统计加载
# ----------------------------------------------------------------------


def load_backtest_stats(backtest_dir: str = "docs/backtest_results") -> Dict[str, Dict[str, Dict[str, Any]]]:
    """加载回测统计，为实时信号提供预测胜率/PnL。

    返回结构:
    {
        "early": {
            "current_and_recent_leader": {
                "win_rate": 0.556,
                "avg_pnl": 0.116,
                "expectancy": 0.116,
                "profit_factor": 1.61,
                "trades": 18,
                "avg_buy_price": 0.45,
            },
            ...
        },
        "mid": {...},
        "late": {...}
    }

    第一阶段：加载推文市场回测统计（docs/backtest_results/）
    后续阶段：加载电竞市场回测统计（基于采集数据生成）

    跨市场迁移说明：第一阶段使用推文市场回测统计作为初始参考，
    但需明确标注"跨市场迁移可信度待验证"。

    文件缺失或解析错误不抛异常，返回空字典。
    """
    logger = logging.getLogger(__name__)
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    if not backtest_dir or not os.path.isdir(backtest_dir):
        logger.warning("回测统计目录不存在: %s", backtest_dir)
        return result

    # 查找 first_signal_summary.json 或类似文件
    candidates = [
        os.path.join(backtest_dir, "first_signal_summary.json"),
        os.path.join(backtest_dir, "summary.json"),
    ]
    # 也扫描目录下所有 *_summary.json
    try:
        for name in os.listdir(backtest_dir):
            if name.endswith("_summary.json") or name == "summary.json":
                p = os.path.join(backtest_dir, name)
                if p not in candidates:
                    candidates.append(p)
    except OSError:
        pass

    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            # 数据格式：{window_label: {rule_name: stats_dict}}
            for window_label, rules in data.items():
                if not isinstance(rules, dict):
                    continue
                result.setdefault(window_label, {})
                for rule_name, stats in rules.items():
                    if not isinstance(stats, dict):
                        continue
                    result[window_label][rule_name] = {
                        "win_rate": float(stats.get("win_rate", 0) or 0),
                        "avg_pnl": float(stats.get("avg_pnl", 0) or 0),
                        "expectancy": float(stats.get("expectancy", 0) or 0),
                        "profit_factor": float(stats.get("profit_factor", 0) or 0),
                        "trades": int(stats.get("trades", 0) or 0),
                        "avg_buy_price": float(stats.get("avg_buy_price", 0) or 0),
                    }
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.error("加载回测统计失败 %s: %s", path, exc)
            continue

    return result


def get_predicted_stats(
    backtest_stats: Dict[str, Dict[str, Dict[str, Any]]],
    window_label: str,
    signal_name: str,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[int], Optional[float]]:
    """从回测统计中获取预测胜率/PnL/期望/历史交易数/利润因子。

    Returns:
        (win_rate, avg_pnl, expectancy, trades, profit_factor)
        任一缺失返回对应 None。
    """
    window_stats = backtest_stats.get(window_label) or {}
    rule_stats = window_stats.get(signal_name) or {}
    if not rule_stats:
        return None, None, None, None, None
    return (
        rule_stats.get("win_rate"),
        rule_stats.get("avg_pnl"),
        rule_stats.get("expectancy"),
        rule_stats.get("trades"),
        rule_stats.get("profit_factor"),
    )
