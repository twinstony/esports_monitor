"""形态信号/交易持久化仓库。

参照：polymarket_twitter_monitor/morphology/repository.py

封装对 storage 层的调用，提供面向形态分析领域的高层接口。
所有方法均不抛异常，失败返回 False/None/[]。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..utils.time_utils import from_utc_iso, now_utc, to_utc_iso
from .models import MorphologyAlert, MorphologySignal, MorphologyTrade


class MorphologyRepository:
    """形态信号与交易的持久化仓库。"""

    def __init__(self, storage: Any, logger: Optional[logging.Logger] = None):
        self.storage = storage
        self._logger = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # 信号持久化
    # ------------------------------------------------------------------

    def save_signals(self, alert: MorphologyAlert) -> List[int]:
        """批量保存告警中的所有信号。

        Returns:
            成功插入的信号 id 列表。
        """
        ids: List[int] = []
        for sig in alert.signals:
            try:
                sig_id = self.storage.insert_morphology_signal(
                    match_id=alert.match_id,
                    signal_name=sig.signal_name,
                    window_label=sig.window_label,
                    buy_team=sig.buy_team,
                    buy_price=sig.buy_price,
                    detected_at=sig.detected_at or to_utc_iso(now_utc()),
                    hours_before_end=sig.hours_before_end,
                    minutes_since_start=alert.minutes_since_start,
                    predicted_win_prob=sig.predicted_win_prob,
                    predicted_pnl=sig.predicted_pnl,
                )
                if sig_id:
                    sig.id = sig_id
                    ids.append(sig_id)
            except Exception as exc:
                self._logger.error("save_signals 失败: %s", exc)
        return ids

    def get_signals(
        self, match_id: Optional[str] = None, hours: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        try:
            return self.storage.get_morphology_signals(match_id=match_id, hours=hours)
        except Exception as exc:
            self._logger.error("get_signals 失败: %s", exc)
            return []

    # ------------------------------------------------------------------
    # 交易持久化
    # ------------------------------------------------------------------

    def save_trade(self, trade: MorphologyTrade) -> Optional[int]:
        """保存模拟交易，返回交易 id。"""
        try:
            trade_id = self.storage.insert_morphology_trade(
                match_id=trade.match_id,
                signal_id=trade.signal_id,
                buy_team=trade.buy_team,
                buy_price=trade.buy_price,
                quantity=trade.quantity,
                notional_usd=trade.notional_usd,
                opened_at=trade.opened_at,
                vwap=trade.vwap,
            )
            if trade_id:
                trade.id = trade_id
            return trade_id
        except Exception as exc:
            self._logger.error("save_trade 失败: %s", exc)
            return None

    def get_open_trades(self, match_id: Optional[str] = None) -> List[MorphologyTrade]:
        """获取未结算交易列表。"""
        try:
            rows = self.storage.get_open_trades(match_id=match_id)
            return [self._row_to_trade(r) for r in rows]
        except Exception as exc:
            self._logger.error("get_open_trades 失败: %s", exc)
            return []

    def get_open_trade_match_ids(self) -> List[str]:
        try:
            return self.storage.get_open_trade_match_ids()
        except Exception as exc:
            self._logger.error("get_open_trade_match_ids 失败: %s", exc)
            return []

    def settle_trade(
        self,
        trade_id: int,
        winning_team: str,
        pnl_usd: float,
        settled_at: Optional[str] = None,
    ) -> bool:
        try:
            settled_at = settled_at or to_utc_iso(now_utc())
            return self.storage.settle_morphology_trade(
                trade_id=trade_id,
                winning_team=winning_team,
                pnl_usd=pnl_usd,
                settled_at=settled_at,
            )
        except Exception as exc:
            self._logger.error("settle_trade 失败: %s", exc)
            return False

    def get_trade_stats(self) -> Dict[str, Any]:
        try:
            return self.storage.get_trade_stats()
        except Exception as exc:
            self._logger.error("get_trade_stats 失败: %s", exc)
            return {"total": 0, "settled": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

    # ------------------------------------------------------------------
    # 冷却管理
    # ------------------------------------------------------------------

    def set_cooldown(
        self,
        match_id: str,
        cooldown_end: str,
        last_signal: Optional[str] = None,
    ) -> bool:
        try:
            return self.storage.set_cooldown(
                match_id=match_id,
                cooldown_end=cooldown_end,
                last_signal=last_signal,
            )
        except Exception as exc:
            self._logger.error("set_cooldown 失败: %s", exc)
            return False

    def get_cooldown_end(self, match_id: str):
        """获取冷却结束时间（UTC aware datetime）；无冷却或失败返回 None。"""
        try:
            row = self.storage.get_cooldown(match_id)
            if not row:
                return None
            end_str = row.get("cooldown_end") if isinstance(row, dict) else None
            if not end_str:
                return None
            return from_utc_iso(end_str)
        except Exception as exc:
            self._logger.error("get_cooldown_end 失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_trade(row: Dict[str, Any]) -> MorphologyTrade:
        return MorphologyTrade(
            id=row.get("id"),
            match_id=row.get("match_id", ""),
            signal_id=int(row.get("signal_id") or 0),
            buy_team=row.get("buy_team", ""),
            buy_price=float(row.get("buy_price") or 0.0),
            quantity=float(row.get("quantity") or 0.0),
            notional_usd=float(row.get("notional_usd") or 0.0),
            vwap=row.get("vwap"),
            opened_at=row.get("opened_at", ""),
            settled=int(row.get("settled") or 0),
            winning_team=row.get("winning_team"),
            pnl_usd=row.get("pnl_usd"),
            settled_at=row.get("settled_at"),
        )
