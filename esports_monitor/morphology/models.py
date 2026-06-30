"""形态分析层数据结构（dataclass）。

参照：polymarket_twitter_monitor/morphology/models.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MorphologyFeatures:
    """单支队伍在时刻 t 的形态特征（10 个）。"""

    current: float
    mean_all: float
    mean_12h: float
    mean_24h: float
    ret_6h: float
    ret_12h: float
    ret_24h: float
    ret_total: float
    price_6h_ago: float
    price_12h_ago: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "current": self.current,
            "mean_all": self.mean_all,
            "mean_12h": self.mean_12h,
            "mean_24h": self.mean_24h,
            "ret_6h": self.ret_6h,
            "ret_12h": self.ret_12h,
            "ret_24h": self.ret_24h,
            "ret_total": self.ret_total,
            "price_6h_ago": self.price_6h_ago,
            "price_12h_ago": self.price_12h_ago,
        }


@dataclass
class MorphologySignal:
    """单个信号触发记录。"""

    signal_name: str
    signal_label: str
    window_label: str
    direction: str  # "A" 买领先队 / "B" 买对手队
    buy_team: str
    buy_price: float
    hours_before_end: Optional[float] = None
    minutes_since_start: Optional[float] = None
    detected_at: str = ""
    # 形态特征（用于调试/审计）
    morph_features: Dict[str, Any] = field(default_factory=dict)
    # 回测预测
    predicted_win_prob: Optional[float] = None
    predicted_pnl: Optional[float] = None
    predicted_expectancy: Optional[float] = None
    historical_trades: Optional[int] = None
    historical_profit_factor: Optional[float] = None
    signal_strength: int = 1
    # 数据库 id（持久化后填充）
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_name": self.signal_name,
            "signal_label": self.signal_label,
            "window_label": self.window_label,
            "direction": self.direction,
            "buy_team": self.buy_team,
            "buy_price": self.buy_price,
            "hours_before_end": self.hours_before_end,
            "minutes_since_start": self.minutes_since_start,
            "detected_at": self.detected_at,
            "morph_features": self.morph_features,
            "predicted_win_prob": self.predicted_win_prob,
            "predicted_pnl": self.predicted_pnl,
            "predicted_expectancy": self.predicted_expectancy,
            "historical_trades": self.historical_trades,
            "historical_profit_factor": self.historical_profit_factor,
            "signal_strength": self.signal_strength,
            "id": self.id,
        }


@dataclass
class MorphologyAlert:
    """单场比赛单次检测的聚合告警。"""

    match_id: str
    team_a: str = ""
    team_b: str = ""
    leader_team: str = ""  # 当前价格高的队伍
    threat_team: str = ""  # 当前价格低的队伍
    leader_price: float = 0.0
    threat_price: float = 0.0
    window_label: str = ""
    minutes_since_start: Optional[float] = None
    hours_before_end: Optional[float] = None
    signals: List[MorphologySignal] = field(default_factory=list)
    strongest_signal: Optional[MorphologySignal] = None
    total_strength: int = 0
    trade_opened: bool = False
    trade_id: Optional[int] = None
    # 检测状态：normal / no_data / no_window / no_signal / cooldown
    status: str = "normal"
    status_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "match_id": self.match_id,
            "team_a": self.team_a,
            "team_b": self.team_b,
            "leader_team": self.leader_team,
            "threat_team": self.threat_team,
            "leader_price": self.leader_price,
            "threat_price": self.threat_price,
            "window_label": self.window_label,
            "minutes_since_start": self.minutes_since_start,
            "hours_before_end": self.hours_before_end,
            "signals": [s.to_dict() for s in self.signals],
            "strongest_signal": (
                self.strongest_signal.to_dict() if self.strongest_signal else None
            ),
            "total_strength": self.total_strength,
            "trade_opened": self.trade_opened,
            "trade_id": self.trade_id,
            "status": self.status,
            "status_message": self.status_message,
        }


@dataclass
class MorphologyTrade:
    """模拟交易记录。"""

    match_id: str
    signal_id: int
    buy_team: str
    buy_price: float
    quantity: float
    notional_usd: float
    opened_at: str
    vwap: Optional[float] = None
    id: Optional[int] = None
    settled: int = 0  # 0=未结算, 1=已结算
    winning_team: Optional[str] = None
    pnl_usd: Optional[float] = None
    settled_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "match_id": self.match_id,
            "signal_id": self.signal_id,
            "buy_team": self.buy_team,
            "buy_price": self.buy_price,
            "quantity": self.quantity,
            "notional_usd": self.notional_usd,
            "vwap": self.vwap,
            "opened_at": self.opened_at,
            "settled": self.settled,
            "winning_team": self.winning_team,
            "pnl_usd": self.pnl_usd,
            "settled_at": self.settled_at,
        }
