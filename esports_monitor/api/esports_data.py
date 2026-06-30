"""电竞实时数据接口（预留）。

设计目的：为后续接入电竞实时数据源（Cito API、PandaScore 等）预留抽象接口，
第一阶段为空实现。

形态分析层集成方式：当 provider.is_available() 为 True 时，形态检测器可选消费
MatchState，将比赛进度作为辅助特征（如金币领先作为动量确认）。第一阶段 provider
不可用，形态分析仅基于价格序列。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class MatchState:
    """电竞比赛实时状态。

    所有时间字段为 UTC aware datetime。
    """

    game: str  # cs2 / dota2 / lol
    match_id: str
    timestamp: datetime  # UTC
    score_a: int  # 队伍A得分（回合/小局）
    score_b: int  # 队伍B得分
    gold_diff: float = 0.0  # 金币差（LoL/Dota2）
    kill_diff: int = 0  # 击杀差
    progress_pct: float = 0.0  # 比赛进度百分比 0-1
    extra: Optional[Dict[str, Any]] = field(default=None)  # 游戏特定字段（地图名、龙魂等）


class EsportsDataProvider(ABC):
    """电竞实时数据源抽象基类。"""

    @abstractmethod
    def is_available(self) -> bool:
        """数据源是否可用。"""

    @abstractmethod
    def get_match_state(self, match_id: str) -> Optional[MatchState]:
        """获取比赛实时状态。"""


class NullProvider(EsportsDataProvider):
    """空实现，第一阶段使用。"""

    def is_available(self) -> bool:
        return False

    def get_match_state(self, match_id: str) -> Optional[MatchState]:
        return None


def create_provider(config: Dict[str, Any]) -> EsportsDataProvider:
    """根据配置创建数据源实例。

    Args:
        config: esports_data 配置字典

    Returns:
        EsportsDataProvider 实例；第一阶段或配置未启用时返回 NullProvider。
    """
    if not isinstance(config, dict):
        return NullProvider()
    if not config.get("enabled", False):
        return NullProvider()
    provider_name = (config.get("provider") or "").lower()
    if not provider_name:
        return NullProvider()
    # 后续阶段可在此处扩展 CitoProvider / PandaScoreProvider 等
    # 第一阶段无可用 provider
    return NullProvider()
