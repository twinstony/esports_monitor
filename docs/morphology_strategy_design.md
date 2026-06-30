# 两区间形态交易策略 — 功能设计文档

> **版本**: v1.0
> **日期**: 2026-06-25
> **状态**: 待编码
> **依赖**: `polymarket_monitor` 主框架、`scripts/two_zone_morphology_backtest.py` 回测结论

---

## 1. 概述

### 1.1 目标

将回测验证过的 6 种两区间形态对比信号集成到实时监控系统中，实现：

1. **无缝集成**：作为独立模块挂载到 `Monitor` 主循环，`enabled=false` 时零开销
2. **持续分析**：每轮轮询对每个监控市场在 4 个时间窗口（1-5h、10-18h、10-24h、24-48h）检测 top3 策略信号
3. **信号通知**：信号触发时通过 Telegram（默认开启）和 Webhook（默认关闭）发送告警
4. **模拟交易**：信号触发时模拟开单，市场结算后统计 PnL，开单/结算均持久化并通知

### 1.2 核心约束

- 回测时不知道哪个区间是王牌区，实时检测时同样不知道——用当前价格最高的区间作为"领先区"，威胁最大的区间作为"对手区"
- 所有时间内部 UTC，展示转北京时间（遵循项目数值规范）
- 买入价过滤 `[0.05, 0.95]`，避免无意义价位
- 模块失败不影响主循环，所有外部操作捕获异常

### 1.3 回测结论摘要

| 窗口 | 最佳规则 | 交易数 | 胜率 | 期望值 | 平均买入价 |
|------|----------|--------|------|--------|------------|
| 1-5h | momentum_catcher | 12 | 33.3% | +2.9% | 0.305 |
| 10-18h | stable_spread | 17 | 64.7% | +7.9% | 0.580 |
| 10-24h | breakout | 7 | 71.4% | +23.8% | 0.476 |
| 24-48h | current_and_recent_leader | 18 | 55.6% | +11.6% | 0.440 |

---

## 2. 包结构

```
polymarket_monitor/morphology/
├── __init__.py          # 导出 MorphologyDetector
├── models.py            # 数据结构定义
├── signals.py           # 6 个形态信号函数 + compute_morphology_features
├── window.py            # 从 markets 表加载价格序列 + 王牌/准王牌识别
├── detector.py          # 检测编排主逻辑
├── repository.py        # 数据库持久化
└── simulator.py         # 模拟下单与结算
```

依赖关系：
```
detector.py
  ├── signals.py    ← 信号检测
  ├── window.py     ← 数据加载
  ├── repository.py ← 持久化
  └── simulator.py  ← 模拟交易
```

---

## 3. 数据结构 (models.py)

```python
"""两区间形态策略数据结构定义。"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MorphologySignal:
    """单个信号触发结果。

    包含实时检测信息和基于历史回测数据的预测盈利概率与 PnL。
    """
    # --- 信号基本信息 ---
    rule_name: str                    # 信号规则名（如 "breakout"）
    rule_label: str                   # 中文标签（如 "突破反超"）
    window_label: str                 # 时间窗口（如 "10-24h"）
    direction: str                    # "A" 或 "B"，买入领先区或对手区
    buy_range: str                    # 买入区间（如 "300-319"）
    buy_range_lower: int
    buy_range_upper: int
    buy_price: float                  # 买入价（当前价格）
    hours_left: float                 # 距结算小时数
    detected_at: str                  # 检测时间 UTC ISO

    # --- 形态特征 ---
    morph_features: Dict[str, float] = field(default_factory=dict)
    # 包含: current, mean_12h, mean_24h, ret_6h, ret_12h, ret_24h, price_6h_ago, price_12h_ago

    # --- 历史回测预测数据 ---
    predicted_win_probability: Optional[float] = None    # 历史胜率（0-1）
    predicted_pnl: Optional[float] = None                # 历史平均 PnL（0-1，相对于买入价）
    predicted_expectancy: Optional[float] = None         # 历史期望值
    historical_trades: Optional[int] = None              # 历史样本数
    historical_profit_factor: Optional[float] = None     # 历史盈亏比

    # --- 信号强度 ---
    signal_strength: int = 1            # 同向叠加倍数，默认 1
    # 多个规则同时指向同一区间时累加；不同向则抵消

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "rule_label": self.rule_label,
            "window_label": self.window_label,
            "direction": self.direction,
            "buy_range": self.buy_range,
            "buy_range_lower": self.buy_range_lower,
            "buy_range_upper": self.buy_range_upper,
            "buy_price": self.buy_price,
            "hours_left": self.hours_left,
            "detected_at": self.detected_at,
            "morph_features": self.morph_features,
            "predicted_win_probability": self.predicted_win_probability,
            "predicted_pnl": self.predicted_pnl,
            "predicted_expectancy": self.predicted_expectancy,
            "historical_trades": self.historical_trades,
            "historical_profit_factor": self.historical_profit_factor,
            "signal_strength": self.signal_strength,
        }


@dataclass
class MorphologyAlert:
    """一个市场一轮检测的聚合告警。"""
    market_slug: str
    market_label: str
    detected_at: str                   # UTC ISO
    hours_left: float                  # 距结算小时数
    signals: List[MorphologySignal] = field(default_factory=list)

    # --- 聚合信息 ---
    strongest_signal: Optional[MorphologySignal] = None    # 信号强度最高的
    total_strength: int = 0                                 # 所有信号强度之和
    unique_buy_ranges: List[str] = field(default_factory=list)  # 去重买入区间

    # --- 模拟交易 ---
    trade_opened: bool = False
    trade_id: Optional[int] = None

    # --- 王牌/准王牌识别（实时） ---
    leader_range: str = ""             # 当前价格最高的区间
    threat_range: str = ""             # 对领先区威胁最大的区间
    leader_price: float = 0.0
    threat_price: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_slug": self.market_slug,
            "market_label": self.market_label,
            "detected_at": self.detected_at,
            "hours_left": self.hours_left,
            "signals": [s.to_dict() for s in self.signals],
            "strongest_signal": self.strongest_signal.to_dict() if self.strongest_signal else None,
            "total_strength": self.total_strength,
            "unique_buy_ranges": self.unique_buy_ranges,
            "trade_opened": self.trade_opened,
            "trade_id": self.trade_id,
            "leader_range": self.leader_range,
            "threat_range": self.threat_range,
            "leader_price": self.leader_price,
            "threat_price": self.threat_price,
        }


@dataclass
class MorphologyTrade:
    """模拟交易记录。"""
    id: Optional[int] = None
    market_slug: str = ""
    market_label: str = ""
    opened_at: str = ""                # UTC ISO
    settled_at: Optional[str] = None   # UTC ISO

    # 开单信息
    buy_range: str = ""
    buy_range_lower: int = 0
    buy_range_upper: int = 0
    buy_price: float = 0.0
    quantity: float = 0.0              # 买入份数
    notional_usd: float = 0.0          # 投入资金（USD）
    vwap: Optional[float] = None       # 盘口感知 VWAP（若启用）

    # 信号信息
    rule_name: str = ""
    window_label: str = ""
    signal_strength: int = 1
    predicted_win_probability: Optional[float] = None
    predicted_pnl: Optional[float] = None

    # 结算信息
    status: str = "open"               # open / settled
    winning_range: Optional[str] = None
    is_winner: Optional[bool] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    settlement_price: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}
```

---

## 4. 信号算法 (signals.py)

从 `scripts/two_zone_morphology_backtest.py` 原样移植，不做逻辑修改。

### 4.1 形态特征计算

```python
def compute_morphology_features(prices: List[float], t: int) -> Dict[str, float]:
    """基于截至 t 的连续价格序列提取形态特征。

    与回测脚本完全一致，确保实时信号与回测信号可比。
    """
    seq = prices[: t + 1]
    n = len(seq)
    current = prices[t]

    mean_all = sum(seq) / n
    window_12 = prices[max(0, t - 11): t + 1]
    mean_12h = sum(window_12) / len(window_12)
    window_24 = prices[max(0, t - 23): t + 1]
    mean_24h = sum(window_24) / len(window_24)

    ret_12h = (current - prices[max(0, t - 11)]) / prices[max(0, t - 11)] if prices[max(0, t - 11)] > 0 else 0.0
    ret_24h = (current - prices[max(0, t - 23)]) / prices[max(0, t - 23)] if prices[max(0, t - 23)] > 0 else 0.0
    ret_6h = (current - prices[max(0, t - 5)]) / prices[max(0, t - 5)] if prices[max(0, t - 5)] > 0 else 0.0
    ret_total = (current - prices[0]) / prices[0] if prices[0] > 0 else 0.0

    price_6h_ago = prices[max(0, t - 5)]
    price_12h_ago = prices[max(0, t - 11)]

    return {
        "current": current,
        "mean_all": mean_all,
        "mean_12h": mean_12h,
        "mean_24h": mean_24h,
        "ret_6h": ret_6h,
        "ret_12h": ret_12h,
        "ret_24h": ret_24h,
        "ret_total": ret_total,
        "price_6h_ago": price_6h_ago,
        "price_12h_ago": price_12h_ago,
    }
```

### 4.2 六个信号函数

```python
MorphSignalFn = Callable[[Dict[str, float], Dict[str, float]], Optional[str]]

# 以下函数与回测脚本完全一致，签名 (A_features, B_features) -> "A" | "B" | None

def signal_current_and_recent_leader(A, B) -> Optional[str]:
    """强者恒强：当前价格更高，且最近12h均价也更高。"""

def signal_momentum_catcher(A, B) -> Optional[str]:
    """后发制人：当前仍落后，但最近12h涨幅明显大于对手（差>10%），且自身为正（>5%）。"""

def signal_breakout(A, B) -> Optional[str]:
    """突破反超：6h前落后，当前反超，且6h涨幅>20%。"""

def signal_divergence(A, B) -> Optional[str]:
    """走势分化：一个12h涨>5%，另一个12h跌>5%。"""

def signal_acceleration(A, B) -> Optional[str]:
    """加速领先：当前领先且6h涨幅>12h涨幅（加速），且12h涨幅为正。"""

def signal_stable_spread(A, B) -> Optional[str]:
    """稳定领先：当前领先，且24h收益也领先。"""

SIGNALS: Dict[str, MorphSignalFn] = {
    "current_and_recent_leader": signal_current_and_recent_leader,
    "momentum_catcher": signal_momentum_catcher,
    "breakout": signal_breakout,
    "divergence": signal_divergence,
    "acceleration": signal_acceleration,
    "stable_spread": signal_stable_spread,
}

SIGNAL_LABELS: Dict[str, str] = {
    "current_and_recent_leader": "强者恒强",
    "momentum_catcher": "后发制人",
    "breakout": "突破反超",
    "divergence": "走势分化",
    "acceleration": "加速领先",
    "stable_spread": "优势稳定",
}
```

### 4.3 历史回测数据加载

```python
def load_backtest_stats(backtest_dir: str = "docs/backtest_results") -> Dict[str, Dict[str, Dict[str, Any]]]:
    """加载各窗口各规则的历史回测统计，用于实时信号的预测概率和 PnL。

    返回结构:
    {
        "1-5h": {
            "momentum_catcher": {
                "win_rate": 0.333,
                "avg_pnl": 0.029,
                "expectancy": 0.029,
                "trades": 12,
                "profit_factor": 1.14,
                "avg_buy_price": 0.305,
            },
            ...
        },
        "10-18h": {...},
        "10-24h": {...},
        "24-48h": {...},
    }

    数据来源: docs/backtest_results/two_zone_morphology_backtest_window_*.json
    读取 first_signal_summary 节（First Signal 模式更贴近实时单次触发场景）。
    """
```

---

## 5. 数据加载 (window.py)

### 5.1 设计要点

实时场景与回测的关键区别：
- **回测**：已知 winner，用 winner 作为王牌区，选择对其威胁最大的区间作为准王牌区
- **实时**：未知 winner，用当前价格最高的区间作为"领先区"（leader），选择对其威胁最大的区间作为"对手区"（threat）

### 5.2 核心函数

```python
class MorphologyWindowAnalyzer:
    """从 markets 表加载价格序列并识别领先区/对手区。"""

    def __init__(self, sqlite_storage: Any, logger: Any = None):
        self._storage = sqlite_storage
        self._logger = logger or logging.getLogger(__name__)

    def load_market_ranges(self, market_slug: str) -> List[Tuple[int, int]]:
        """获取市场所有区间。"""

    def load_price_series(
        self,
        market_slug: str,
        lower: int,
        upper: int,
        hours: int = 48,
    ) -> List[Tuple[str, float]]:
        """从 markets 表加载指定区间过去 hours 小时的价格序列。

        SQL:
            SELECT recorded_at, price_yes
            FROM markets
            WHERE market_slug = ? AND lower = ? AND upper = ?
              AND recorded_at >= ? AND recorded_at <= ?
            ORDER BY recorded_at ASC

        时间 bound 使用 strftime("%Y-%m-%d %H:%M:%S") 格式（与数据库存储格式一致）。
        """

    def resample_series(
        self,
        points: List[Tuple[str, float]],
        n: int = 48,
    ) -> Optional[List[float]]:
        """将不等间隔的价格序列重采样为 n 个等间隔点（线性插值）。

        与回测脚本 resample_series 完全一致。
        """

    def identify_leader_and_threat(
        self,
        range_data: Dict[str, Dict[str, Any]],
    ) -> Tuple[Optional[str], Optional[str], List[float], List[float]]:
        """识别领先区和对手区。

        算法：
        1. 取每个区间重采样序列的最后一个价格作为"当前价格"
        2. 当前价格最高的区间 = 领先区（leader）
        3. 对领先区威胁最大的区间 = 对手区（threat）
           threat = argmax(max(threat_prices[i] - leader_prices[i]) for i in range(n))
        4. 额外过滤：对手区当前价格必须 > 0.05（排除已归零区间）

        返回: (leader_key, threat_key, leader_prices, threat_prices)
        """

    def prepare_market_data(
        self,
        market_slug: str,
        hours: int = 48,
    ) -> Optional[Dict[str, Any]]:
        """完整的数据准备流程。

        返回:
        {
            "leader_range": "300-319",
            "threat_range": "280-299",
            "leader_prices": [...],   # 48 个点
            "threat_prices": [...],   # 48 个点
            "all_ranges": [(lower, upper), ...],
        }

        若数据不足（任一区间 < 5 条记录）或无法识别对手区，返回 None。
        """
```

---

## 6. 检测编排 (detector.py)

### 6.1 主类

```python
class MorphologyDetector:
    """两区间形态策略检测器。

    在每轮监控中对每个市场执行：
    1. 加载过去 48h 价格数据
    2. 识别领先区和对手区
    3. 在 4 个时间窗口分别检测 top3 策略信号
    4. 聚合信号，计算信号强度
    5. 触发模拟下单
    """

    def __init__(
        self,
        window_analyzer: MorphologyWindowAnalyzer,
        repository: MorphologyRepository,
        simulator: MorphologySimulator,
        config: Dict[str, Any],
        backtest_stats: Dict[str, Dict[str, Dict[str, Any]]],
        logger: Any = None,
    ):
        self._window = window_analyzer
        self._repo = repository
        self._sim = simulator
        self._config = config
        self._backtest_stats = backtest_stats  # 历史回测统计
        self._logger = logger or logging.getLogger(__name__)
        self._enabled = config.get("enabled", True)
        self._windows = config.get("windows", [])
        self._cooldown_minutes = config.get("cooldown_minutes", 30)

    def detect(
        self,
        market_slug: str,
        market_label: str,
        markets: List[Dict[str, Any]],
        hours_left: Optional[float] = None,
    ) -> Optional[MorphologyAlert]:
        """执行一轮检测。

        参数:
            market_slug: 市场 slug
            market_label: 市场显示名
            markets: 当前市场所有区间的实时快照（PolymarketClient.fetch_market_data 返回的 markets 列表）
            hours_left: 距结算小时数（若已知）

        返回:
            MorphologyAlert 或 None（无信号或冷却中）
        """
```

### 6.2 检测流程

```python
def detect(self, market_slug, market_label, markets, hours_left=None):
    if not self._enabled:
        return None

    # 1. 冷却检查
    if self._repo.is_in_cooldown(market_slug):
        return None

    # 2. 加载历史数据
    market_data = self._window.prepare_market_data(market_slug, hours=48)
    if market_data is None:
        return None

    leader_prices = market_data["leader_prices"]
    threat_prices = market_data["threat_prices"]
    n = len(leader_prices)

    # 3. 计算距结算时间（若未知，从 slug 解析）
    if hours_left is None:
        hours_left = self._estimate_hours_left(market_slug)
    if hours_left is None or hours_left <= 0:
        return None

    # 4. 在 4 个时间窗口分别检测
    all_signals: List[MorphologySignal] = []
    for window_cfg in self._windows:
        w_label = window_cfg["label"]
        w_min = window_cfg["min_hours"]
        w_max = window_cfg["max_hours"]
        w_rules = window_cfg["rules"]

        # 当前 hours_left 是否落在该窗口
        if not (w_min <= hours_left <= w_max):
            continue

        # 取序列最后一个点作为"当前时刻"
        t = n - 1
        feat_leader = compute_morphology_features(leader_prices, t)
        feat_threat = compute_morphology_features(threat_prices, t)

        # 检测该窗口的 top3 规则
        w_stats = self._backtest_stats.get(w_label, {})
        for rule_name in w_rules:
            signal_fn = SIGNALS.get(rule_name)
            if signal_fn is None:
                continue
            side = signal_fn(feat_leader, feat_threat)
            if side is None:
                continue

            # 确定买入区间和价格
            if side == "A":
                buy_lower = market_data["leader_range_lower"]
                buy_upper = market_data["leader_range_upper"]
                buy_price = leader_prices[t]
            else:
                buy_lower = market_data["threat_range_lower"]
                buy_upper = market_data["threat_range_upper"]
                buy_price = threat_prices[t]

            # 买入价过滤
            if buy_price <= 0.05 or buy_price >= 0.95:
                continue

            # 加载历史回测预测数据
            rule_stats = w_stats.get(rule_name, {})
            signal = MorphologySignal(
                rule_name=rule_name,
                rule_label=SIGNAL_LABELS.get(rule_name, rule_name),
                window_label=w_label,
                direction=side,
                buy_range=f"{buy_lower}-{buy_upper}",
                buy_range_lower=buy_lower,
                buy_range_upper=buy_upper,
                buy_price=buy_price,
                hours_left=hours_left,
                detected_at=utc_now().isoformat(),
                morph_features=feat_leader if side == "A" else feat_threat,
                predicted_win_probability=rule_stats.get("win_rate"),
                predicted_pnl=rule_stats.get("avg_pnl"),
                predicted_expectancy=rule_stats.get("expectancy"),
                historical_trades=rule_stats.get("trades"),
                historical_profit_factor=rule_stats.get("profit_factor"),
            )
            all_signals.append(signal)

    if not all_signals:
        return None

    # 5. 聚合信号强度
    # 同一买入区间的信号累加强度，不同买入区间的信号各自独立
    range_strength: Dict[str, int] = {}
    for sig in all_signals:
        range_strength[sig.buy_range] = range_strength.get(sig.buy_range, 0) + 1
    for sig in all_signals:
        sig.signal_strength = range_strength[sig.buy_range]

    # 最强信号（信号强度最高，其次预测期望值最高）
    strongest = max(all_signals, key=lambda s: (s.signal_strength, s.predicted_expectancy or 0))

    # 6. 构建告警
    alert = MorphologyAlert(
        market_slug=market_slug,
        market_label=market_label,
        detected_at=utc_now().isoformat(),
        hours_left=hours_left,
        signals=all_signals,
        strongest_signal=strongest,
        total_strength=sum(s.signal_strength for s in all_signals),
        unique_buy_ranges=list(range_strength.keys()),
        leader_range=market_data["leader_range"],
        threat_range=market_data["threat_range"],
        leader_price=leader_prices[-1],
        threat_price=threat_prices[-1],
    )

    # 7. 持久化信号记录
    self._repo.save_signals(alert)

    # 8. 触发模拟下单
    trade = self._sim.open_trade(alert)
    if trade:
        alert.trade_opened = True
        alert.trade_id = trade.id

    # 9. 设置冷却
    self._repo.set_cooldown(market_slug, utc_now() + timedelta(minutes=self._cooldown_minutes))

    return alert
```

### 6.3 结算检测

```python
def settle_market(self, market_slug: str, winning_lower: int, winning_upper: int) -> None:
    """市场结算时调用，结算所有未平仓模拟单。

    参数:
        market_slug: 市场 slug
        winning_lower: 中奖区间下界
        winning_upper: 中奖区间上界
    """
    open_trades = self._repo.get_open_trades(market_slug)
    for trade in open_trades:
        is_winner = (trade.buy_range_lower == winning_lower and
                     trade.buy_range_upper == winning_upper)
        if is_winner:
            pnl_usd = trade.notional_usd * (1.0 / trade.buy_price - 1.0)
            # 即投入 notional_usd 美元，以 buy_price 价格买入 quantity = notional_usd / buy_price 份
            # 中奖后每份价值 $1，总收入 = quantity * 1 = notional_usd / buy_price
            # PnL = 总收入 - 投入 = notional_usd / buy_price - notional_usd
            settlement_price = 1.0
        else:
            pnl_usd = -trade.notional_usd
            settlement_price = 0.0

        pnl_pct = pnl_usd / trade.notional_usd if trade.notional_usd > 0 else 0.0

        self._repo.settle_trade(
            trade_id=trade.id,
            winning_range=f"{winning_lower}-{winning_upper}",
            is_winner=is_winner,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            settlement_price=settlement_price,
            settled_at=utc_now().isoformat(),
        )
```

---

## 7. 持久化 (repository.py)

### 7.1 表结构

```sql
-- 形态信号记录表
CREATE TABLE IF NOT EXISTS morphology_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,          -- UTC
    market_slug TEXT NOT NULL,
    market_label TEXT,
    hours_left REAL,
    window_label TEXT,                                        -- 1-5h / 10-18h / 10-24h / 24-48h
    rule_name TEXT NOT NULL,                                  -- 信号规则名
    rule_label TEXT,                                          -- 中文标签
    direction TEXT,                                           -- A（买领先区） / B（买对手区）
    buy_range TEXT,                                           -- 如 "300-319"
    buy_range_lower INTEGER,
    buy_range_upper INTEGER,
    buy_price REAL,
    signal_strength INTEGER DEFAULT 1,
    morph_features_json TEXT,                                 -- 形态特征 JSON
    predicted_win_probability REAL,                           -- 历史胜率
    predicted_pnl REAL,                                       -- 历史平均 PnL
    predicted_expectancy REAL,                                -- 历史期望值
    historical_trades INTEGER,                                -- 历史样本数
    historical_profit_factor REAL,                            -- 历史盈亏比
    leader_range TEXT,                                        -- 领先区
    threat_range TEXT,                                        -- 对手区
    leader_price REAL,
    threat_price REAL,
    trade_id INTEGER                                          -- 关联模拟交易 ID（若有）
);

CREATE INDEX IF NOT EXISTS idx_morph_signals_market ON morphology_signals(market_slug);
CREATE INDEX IF NOT EXISTS idx_morph_signals_time ON morphology_signals(detected_at);
CREATE INDEX IF NOT EXISTS idx_morph_signals_rule ON morphology_signals(rule_name);

-- 形态模拟交易表
CREATE TABLE IF NOT EXISTS morphology_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug TEXT NOT NULL,
    market_label TEXT,
    opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,             -- UTC
    settled_at DATETIME,                                      -- UTC，结算后回填
    buy_range TEXT NOT NULL,
    buy_range_lower INTEGER,
    buy_range_upper INTEGER,
    buy_price REAL NOT NULL,
    quantity REAL NOT NULL,                                   -- 买入份数
    notional_usd REAL NOT NULL,                               -- 投入资金 USD
    vwap REAL,                                                -- 盘口感知 VWAP
    rule_name TEXT,                                           -- 触发规则
    window_label TEXT,                                        -- 触发窗口
    signal_strength INTEGER DEFAULT 1,
    predicted_win_probability REAL,
    predicted_pnl REAL,
    status TEXT DEFAULT 'open',                               -- open / settled
    winning_range TEXT,                                       -- 中奖区间
    is_winner BOOLEAN,                                        -- 是否中奖
    pnl_usd REAL,                                             -- 盈亏 USD
    pnl_pct REAL,                                             -- 盈亏百分比
    settlement_price REAL                                     -- 结算价格（1.0 或 0.0）
);

CREATE INDEX IF NOT EXISTS idx_morph_trades_market ON morphology_trades(market_slug);
CREATE INDEX IF NOT EXISTS idx_morph_trades_status ON morphology_trades(status);
CREATE INDEX IF NOT EXISTS idx_morph_trades_opened ON morphology_trades(opened_at);

-- 冷却状态表
CREATE TABLE IF NOT EXISTS morphology_cooldown (
    market_slug TEXT PRIMARY KEY,
    cooldown_end DATETIME NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 7.2 核心方法

```python
class MorphologyRepository:
    """形态策略数据库持久化。"""

    def __init__(self, sqlite_storage: Any):
        self._storage = sqlite_storage
        self._logger = logging.getLogger(__name__)
        self._init_tables()

    def _init_tables(self) -> None:
        """创建表（IF NOT EXISTS），捕获异常不抛出。"""

    def save_signals(self, alert: MorphologyAlert) -> None:
        """批量保存一轮检测的所有信号记录。"""

    def is_in_cooldown(self, market_slug: str) -> bool:
        """检查市场是否在冷却期内。"""

    def set_cooldown(self, market_slug: str, cooldown_end: datetime) -> None:
        """设置冷却结束时间。"""

    def save_trade(self, trade: MorphologyTrade) -> int:
        """保存模拟交易开单记录，返回 trade_id。"""

    def get_open_trades(self, market_slug: str) -> List[MorphologyTrade]:
        """获取某市场所有未结算的模拟交易。"""

    def settle_trade(
        self,
        trade_id: int,
        winning_range: str,
        is_winner: bool,
        pnl_usd: float,
        pnl_pct: float,
        settlement_price: float,
        settled_at: str,
    ) -> None:
        """结算模拟交易，回填 PnL。"""

    def get_all_trades(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询所有交易（可按状态过滤），用于统计报告。"""

    def get_trade_stats(self) -> Dict[str, Any]:
        """返回汇总统计：总交易数、胜率、总 PnL、平均 PnL 等。"""
```

---

## 8. 模拟交易 (simulator.py)

```python
class MorphologySimulator:
    """模拟下单与结算管理。

    信号触发时模拟开单，市场结算时计算 PnL。
    可选盘口深度感知以贴合真实市场。
    """

    def __init__(
        self,
        repository: MorphologyRepository,
        config: Dict[str, Any],
        clob_client: Any = None,       # 可选，用于盘口深度感知
        logger: Any = None,
    ):
        self._repo = repository
        self._config = config
        self._clob = clob_client
        self._logger = logger or logging.getLogger(__name__)

        sim_cfg = config.get("simulation", {})
        self._enabled = sim_cfg.get("enabled", True)
        self._notional_usd = sim_cfg.get("notional_usd", 100.0)
        self._use_depth = sim_cfg.get("use_depth", True)
        self._buy_price_min = sim_cfg.get("buy_price_min", 0.05)
        self._buy_price_max = sim_cfg.get("buy_price_max", 0.95)

    def open_trade(self, alert: MorphologyAlert) -> Optional[MorphologyTrade]:
        """根据告警开模拟单。

        规则：
        1. 选择信号强度最高的买入区间
        2. 买入价必须在 [buy_price_min, buy_price_max] 范围内
        3. 每单投入 notional_usd 美元
        4. 若 use_depth=True 且 clob_client 可用，尝试获取盘口 VWAP
        5. 每个市场每个冷却周期内只开一个模拟单

        份数计算:
            quantity = notional_usd / buy_price
            （若启用盘口深度，buy_price 替换为 vwap）
        """
        if not self._enabled:
            return None

        strongest = alert.strongest_signal
        if strongest is None:
            return None

        buy_price = strongest.buy_price
        if buy_price < self._buy_price_min or buy_price > self._buy_price_max:
            return None

        # 盘口深度感知（可选）
        vwap = None
        if self._use_depth and self._clob:
            vwap = self._try_get_vwap(
                alert.market_slug,
                strongest.buy_range_lower,
                strongest.buy_range_upper,
                self._notional_usd,
            )
            if vwap and 0 < vwap < 1.0:
                buy_price = vwap

        quantity = self._notional_usd / buy_price if buy_price > 0 else 0

        trade = MorphologyTrade(
            market_slug=alert.market_slug,
            market_label=alert.market_label,
            opened_at=utc_now().isoformat(),
            buy_range=strongest.buy_range,
            buy_range_lower=strongest.buy_range_lower,
            buy_range_upper=strongest.buy_range_upper,
            buy_price=buy_price,
            quantity=quantity,
            notional_usd=self._notional_usd,
            vwap=vwap,
            rule_name=strongest.rule_name,
            window_label=strongest.window_label,
            signal_strength=strongest.signal_strength,
            predicted_win_probability=strongest.predicted_win_probability,
            predicted_pnl=strongest.predicted_pnl,
        )

        trade_id = self._repo.save_trade(trade)
        trade.id = trade_id
        return trade

    def _try_get_vwap(
        self,
        market_slug: str,
        lower: int,
        upper: int,
        target_usd: float,
    ) -> Optional[float]:
        """尝试通过 ClobClient 获取盘口 VWAP。

        若获取失败返回 None，回退到当前价格。
        需要从 markets 快照中获取 condition_id，再查询 CLOB 盘口。
        """
        # 实现时参考 convert/depth.py 的 DepthAnalyzer.assess 逻辑
        # 简化版：只获取 best_bid/best_ask 的中间价作为 VWAP 近似
        # 完整版：模拟走穿盘口计算真实 VWAP
        pass
```

---

## 9. 配置设计

### 9.1 config.yaml 新增节

```yaml
morphology:
  # 主开关
  enabled: true

  # 通知开关（独立于全局 telegram.enabled / webhook.enabled）
  telegram_enabled: true       # 默认开启
  webhook_enabled: false       # 默认关闭

  # 冷却时间（分钟），同一市场冷却期内不重复检测
  cooldown_minutes: 30

  # 模拟交易配置
  simulation:
    enabled: true              # 是否启用模拟下单
    notional_usd: 100          # 每单投入资金（USD）
    use_depth: true            # 是否启用盘口深度感知
    buy_price_min: 0.05        # 最低买入价
    buy_price_max: 0.95        # 最高买入价

  # 时间窗口与 top3 策略配置
  # rules 列表中的策略在该窗口内被检测
  # 顺序代表优先级（用于模拟单选择）
  windows:
    - label: "1-5h"
      min_hours: 1
      max_hours: 5
      rules:
        - momentum_catcher
        - stable_spread
        - current_and_recent_leader

    - label: "10-18h"
      min_hours: 10
      max_hours: 18
      rules:
        - stable_spread
        - current_and_recent_leader
        - divergence

    - label: "10-24h"
      min_hours: 10
      max_hours: 24
      rules:
        - breakout
        - momentum_catcher
        - divergence

    - label: "24-48h"
      min_hours: 24
      max_hours: 48
      rules:
        - current_and_recent_leader
        - breakout
        - stable_spread

  # 回测数据路径（用于加载历史预测概率和 PnL）
  backtest_dir: "docs/backtest_results"
```

### 9.2 DEFAULT_CONFIG 新增

在 `polymarket_monitor/config/manager.py` 的 `DEFAULT_CONFIG` 中新增：

```python
"morphology": {
    "enabled": False,
    "telegram_enabled": True,
    "webhook_enabled": False,
    "cooldown_minutes": 30,
    "simulation": {
        "enabled": True,
        "notional_usd": 100,
        "use_depth": True,
        "buy_price_min": 0.05,
        "buy_price_max": 0.95,
    },
    "windows": [
        {"label": "1-5h", "min_hours": 1, "max_hours": 5,
         "rules": ["momentum_catcher", "stable_spread", "current_and_recent_leader"]},
        {"label": "10-18h", "min_hours": 10, "max_hours": 18,
         "rules": ["stable_spread", "current_and_recent_leader", "divergence"]},
        {"label": "10-24h", "min_hours": 10, "max_hours": 24,
         "rules": ["breakout", "momentum_catcher", "divergence"]},
        {"label": "24-48h", "min_hours": 24, "max_hours": 48,
         "rules": ["current_and_recent_leader", "breakout", "stable_spread"]},
    ],
    "backtest_dir": "docs/backtest_results",
},
```

---

## 10. Monitor 集成改动

### 10.1 新增导入 (monitor.py 头部)

```python
from polymarket_monitor.morphology.detector import MorphologyDetector
from polymarket_monitor.morphology.window import MorphologyWindowAnalyzer
from polymarket_monitor.morphology.repository import MorphologyRepository
from polymarket_monitor.morphology.simulator import MorphologySimulator
from polymarket_monitor.morphology.signals import load_backtest_stats
```

### 10.2 初始化 (_init_components 末尾)

```python
def _init_morphology_components(self) -> None:
    """初始化形态策略模块。enabled=false 时零开销。"""
    morph_config = self.config.get("morphology", {}) or {}
    if not morph_config.get("enabled", False):
        self.morphology_detector = None
        return
    try:
        repository = MorphologyRepository(self.sqlite_storage)
        window_analyzer = MorphologyWindowAnalyzer(self.sqlite_storage)
        backtest_stats = load_backtest_stats(
            morph_config.get("backtest_dir", "docs/backtest_results")
        )
        # 复用 convert 模块的 clob_client（若已初始化）
        clob_client = getattr(self, "_clob_client", None)
        simulator = MorphologySimulator(
            repository=repository,
            config=morph_config,
            clob_client=clob_client,
        )
        self.morphology_detector = MorphologyDetector(
            window_analyzer=window_analyzer,
            repository=repository,
            simulator=simulator,
            config=morph_config,
            backtest_stats=backtest_stats,
        )
    except Exception as exc:
        self._logger.error("形态策略模块初始化失败: %s", exc)
        self.morphology_detector = None
```

### 10.3 调用点 (run_single_check_for_market 末尾)

在 `convert_alert = self._run_convert_detection(...)` 之后添加：

```python
# 形态策略检测
morphology_alert = self._run_morphology_detection(
    slug=slug,
    market_label=market_label,
    markets=markets,
    hours_left=hours_left,
)
```

返回值字典新增：

```python
return {
    ...
    "morphology_alert": morphology_alert.to_dict() if morphology_alert else None,
}
```

### 10.4 检测方法

```python
def _run_morphology_detection(
    self,
    slug: str,
    market_label: str,
    markets: List[Dict[str, Any]],
    hours_left: Optional[float],
) -> Optional[Any]:
    """执行形态策略检测并发送通知。"""
    if self.morphology_detector is None:
        return None
    try:
        alert = self.morphology_detector.detect(
            market_slug=slug,
            market_label=market_label,
            markets=markets,
            hours_left=hours_left,
        )
        if alert:
            self._send_morphology_notification(alert)
        return alert
    except Exception as exc:
        self._logger.error("形态策略检测失败: %s", exc)
        return None
```

### 10.5 市场结算检测

在 `run_continuous` 主循环中，已有的市场结算检测逻辑（如 `_generate_convert_summaries`）附近添加：

```python
def _check_and_settle_morphology_trades(self) -> None:
    """检查已结算市场，结算形态模拟交易。"""
    if self.morphology_detector is None:
        return
    try:
        # 查询所有有未结算模拟单的市场
        repo = self.morphology_detector._repo
        open_markets = repo.get_open_trade_markets()
        for market_slug in open_markets:
            # 从 market_settlements.json 或 Gamma API 获取结算结果
            settlement = self._get_market_settlement(market_slug)
            if settlement and settlement.get("status") == "settled":
                winning_lower = settlement["winning_lower"]
                winning_upper = settlement["winning_upper"]
                settled_trades = self.morphology_detector.settle_market(
                    market_slug, winning_lower, winning_upper
                )
                for trade in settled_trades:
                    self._send_morphology_settlement_notification(trade)
    except Exception as exc:
        self._logger.error("形态模拟交易结算失败: %s", exc)
```

---

## 11. 通知消息格式

### 11.1 信号告警通知

```
🔔 形态策略信号

市场: {market_label}
距结算: {hours_left:.1f} 小时
领先区: {leader_range} ({leader_price:.3f})
对手区: {threat_range} ({threat_price:.3f})

触发信号 ({signal_count} 个):
┌─────────────────────────────────────────
│ [{window_label}] {rule_label} ({rule_name})
│   买入: {buy_range} @ {buy_price:.3f}
│   方向: {direction_label}
│   强度: {signal_strength} 个规则同向
│   历史胜率: {predicted_win_probability:.1%}
│   历史期望: {predicted_expectancy:+.1%}
│   历史样本: {historical_trades} 笔
└─────────────────────────────────────────
[重复每个信号]

最强信号: {strongest_rule_label}
建议买入: {strongest_buy_range} @ {strongest_buy_price:.3f}

{模拟单信息（若已开单）}
📋 模拟单已开
  区间: {trade_buy_range}
  价格: {trade_buy_price:.3f}
  数量: {trade_quantity:.1f} 份
  投入: ${trade_notional:.2f}
  VWAP: {trade_vwap:.3f}（若启用盘口）
```

### 11.2 结算通知

```
📊 形态策略模拟单结算

市场: {market_label}
中奖区间: {winning_range}

持仓: {buy_range} @ {buy_price:.3f}
投入: ${notional:.2f}
结果: {'✅ 中奖' if is_winner else '❌ 未中奖'}
PnL: {pnl_usd:+.2f} USD ({pnl_pct:+.1%})

--- 汇总 ---
总交易数: {total_trades}
总胜率: {win_rate:.1%}
总 PnL: {total_pnl:+.2f} USD
```

### 11.3 通知发送方法

```python
def _send_morphology_notification(self, alert: MorphologyAlert) -> None:
    """发送形态策略信号通知。"""
    morph_config = self.config.get("morphology", {}) or {}
    telegram_enabled = morph_config.get("telegram_enabled", True)
    webhook_enabled = morph_config.get("webhook_enabled", False)

    message = self._format_morphology_alert_message(alert)

    # Terminal 输出（始终）
    self._logger.info("形态策略信号:\n%s", message)

    # Telegram
    if telegram_enabled and self.telegram_notifier.can_send():
        self.telegram_notifier.send_message(message)

    # Webhook
    if webhook_enabled and self.webhook_notifier.can_send():
        self.webhook_notifier.send_event(
            event_type="morphology_signal",
            message=message,
            details=alert.summary if hasattr(alert, 'summary') else "",
            market_slug=alert.market_slug,
            market_label=alert.market_label,
            morphology_alert=alert.to_dict(),
        )

def _send_morphology_settlement_notification(self, trade: MorphologyTrade) -> None:
    """发送模拟单结算通知。"""
    morph_config = self.config.get("morphology", {}) or {}
    telegram_enabled = morph_config.get("telegram_enabled", True)
    webhook_enabled = morph_config.get("webhook_enabled", False)

    message = self._format_morphology_settlement_message(trade)

    if telegram_enabled and self.telegram_notifier.can_send():
        self.telegram_notifier.send_message(message)

    if webhook_enabled and self.webhook_notifier.can_send():
        self.webhook_notifier.send_event(
            event_type="morphology_settlement",
            message=message,
            market_slug=trade.market_slug,
            trade=trade.to_dict(),
        )
```

---

## 12. 实时场景与回测的关键差异

### 12.1 王牌区/准王牌区识别

| 维度 | 回测 | 实时 |
|------|------|------|
| 王牌区 | 已知 winner | 当前价格最高的区间（leader） |
| 准王牌区 | 对 winner 威胁最大的区间 | 对 leader 威胁最大的区间 |
| 数据终点 | settlement_dt（结束日 16:00 UTC） | 当前时刻 |
| 数据范围 | settlement_dt - 48h ~ settlement_dt | now - 48h ~ now |

### 12.2 hours_left 计算

实时场景下，`hours_left` 从 market_slug 解析的结束日期计算：

```python
def _estimate_hours_left(self, market_slug: str) -> Optional[float]:
    """从 slug 解析结束日期，计算距结算的小时数。

    settlement_dt = datetime(end_year, end_month, end_day, 16, 0, 0)
    hours_left = (settlement_dt - utc_now()).total_seconds() / 3600
    """
    date_range = self._storage._parse_market_slug_dates(market_slug)
    if not date_range:
        return None
    _, end_dt = date_range
    settlement_dt = datetime.datetime(end_dt.year, end_dt.month, end_dt.day, 16, 0, 0)
    delta = (settlement_dt - utc_now()).total_seconds() / 3600
    return delta if delta > 0 else None
```

### 12.3 数据质量保障

实时场景下的数据质量检查：
1. 至少有 12 个数据点（约 12 分钟，1 分钟 1 轮）
2. 领先区和对手区的价格序列长度一致
3. 买入价在 `[0.05, 0.95]` 范围内
4. 冷却期内不重复检测

---

## 13. 文件清单

### 新建文件

| 文件路径 | 用途 |
|----------|------|
| `polymarket_monitor/morphology/__init__.py` | 包导出 |
| `polymarket_monitor/morphology/models.py` | 数据结构 |
| `polymarket_monitor/morphology/signals.py` | 信号函数 + 回测数据加载 |
| `polymarket_monitor/morphology/window.py` | 数据加载与区间识别 |
| `polymarket_monitor/morphology/detector.py` | 检测编排 |
| `polymarket_monitor/morphology/repository.py` | 数据库持久化 |
| `polymarket_monitor/morphology/simulator.py` | 模拟交易 |

### 修改文件

| 文件路径 | 改动内容 |
|----------|----------|
| `polymarket_monitor/core/monitor.py` | 新增导入、`_init_morphology_components()`、`_run_morphology_detection()`、`_send_morphology_notification()`、`_send_morphology_settlement_notification()`、`_check_and_settle_morphology_trades()` |
| `polymarket_monitor/config/manager.py` | `DEFAULT_CONFIG` 新增 `morphology` 节 |
| `config.yaml` | 新增 `morphology` 配置节 |

### 不修改的文件

- `scripts/two_zone_morphology_backtest.py`（回测脚本保持不变）
- `scripts/two_zone_morphology_combined_report.py`（报告脚本保持不变）
- `polymarket_monitor/signals/`（现有信号系统不变）
- `polymarket_monitor/convert/`（convert 策略不变）

---

## 14. 验证步骤

### 14.1 单元测试

1. **signals.py 测试**：验证 6 个信号函数与回测脚本输出一致
2. **window.py 测试**：验证价格序列加载和重采样
3. **repository.py 测试**：验证表创建、信号保存、交易保存、结算回填
4. **simulator.py 测试**：验证 PnL 计算（中奖/未中奖）

### 14.2 集成测试

1. `morphology.enabled=false` 时主循环正常运行，无额外开销
2. `morphology.enabled=true` 时每轮检测执行，信号触发后 TG 收到通知
3. 模拟单开单后数据库有记录
4. 市场结算后模拟单自动结算，PnL 正确，TG 收到结算通知

### 14.3 回归验证

1. 运行主程序 `python main.py`，确认无报错
2. 确认 convert 策略、预测、事件检测等原有功能不受影响
3. 确认数据库表结构正确创建

---

## 15. 后续扩展方向

1. **信号组合优化**：多信号同向时调整模拟单投入资金（如强度 ≥3 时加倍）
2. **实时回测校准**：定期将实时模拟单结果与历史回测对比，动态调整 top3 策略列表
3. **多市场聚合**：同一时刻多个市场触发信号时，按信号强度排序优先处理
4. **盘口深度完整集成**：使用 `DepthAnalyzer._simulate_walk` 计算真实 VWAP 和滑点
5. **Telegram 汇总推送**：将实时信号和模拟交易汇总后通过 Telegram 推送
