"""模拟下单与结算。

参照：polymarket_twitter_monitor/morphology/simulator.py

开仓：
- 计算买入份数 = notional_usd / buy_price
- 若启用 use_depth，用盘口走穿 VWAP 替代当前价
- 写入 morphology_trades 表

结算：
- 中奖：每份结算价 1.0，pnl_usd = notional_usd * (1.0/buy_price - 1.0)
- 未中奖：损失全部投入，pnl_usd = -notional_usd
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..api.clob import ClobClient, parse_asks, simulate_walk_asks
from ..utils.time_utils import now_utc, to_utc_iso
from .models import MorphologyAlert, MorphologyTrade
from .repository import MorphologyRepository


class MorphologySimulator:
    """模拟下单与结算。"""

    def __init__(
        self,
        repository: MorphologyRepository,
        config: Optional[Dict[str, Any]] = None,
        clob_client: Optional[ClobClient] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.repository = repository
        self.clob_client = clob_client
        self.config = config or {}
        self._logger = logger or logging.getLogger(__name__)

        sim_cfg = self.config.get("simulation") or {}
        self.enabled = bool(sim_cfg.get("enabled", True))
        self.notional_usd = float(sim_cfg.get("notional_usd", 100))
        self.use_depth = bool(sim_cfg.get("use_depth", True))
        self.buy_price_min = float(self.config.get("buy_price_min", 0.05))
        self.buy_price_max = float(self.config.get("buy_price_max", 0.95))

    # ------------------------------------------------------------------
    # 开仓
    # ------------------------------------------------------------------

    def open_trade(self, alert: MorphologyAlert) -> Optional[MorphologyTrade]:
        """根据告警的最强信号开模拟仓。

        Returns:
            创建的 MorphologyTrade；禁用或无信号返回 None。
        """
        if not self.enabled:
            return None
        sig = alert.strongest_signal
        if sig is None:
            return None

        buy_price = sig.buy_price
        # 价格边界检查
        if not (self.buy_price_min < buy_price < self.buy_price_max):
            self._logger.debug(
                "买入价 %.4f 超出边界 [%.2f, %.2f]，跳过开仓",
                buy_price, self.buy_price_min, self.buy_price_max,
            )
            return None

        # 可选：用盘口 VWAP 替代当前价
        vwap: Optional[float] = None
        if self.use_depth and self.clob_client:
            vwap = self._try_get_vwap(alert.match_id, sig.buy_team, self.notional_usd)
            if vwap is not None and 0 < vwap < 1.0:
                buy_price = vwap

        # 计算份数
        if buy_price <= 0:
            return None
        quantity = self.notional_usd / buy_price

        trade = MorphologyTrade(
            match_id=alert.match_id,
            signal_id=sig.id or 0,
            buy_team=sig.buy_team,
            buy_price=buy_price,
            quantity=quantity,
            notional_usd=self.notional_usd,
            opened_at=to_utc_iso(now_utc()),
            vwap=vwap,
        )
        trade_id = self.repository.save_trade(trade)
        if trade_id:
            trade.id = trade_id
            alert.trade_opened = True
            alert.trade_id = trade_id
            self._logger.info(
                "开模拟仓 match=%s team=%s price=%.4f qty=%.2f vwap=%s",
                alert.match_id, sig.buy_team, buy_price, quantity,
                f"{vwap:.4f}" if vwap else "N/A",
            )
            return trade
        return None

    def _try_get_vwap(
        self,
        match_id: str,
        buy_team: str,
        target_size: float,
    ) -> Optional[float]:
        """通过盘口走穿模拟获取买入 VWAP。

        失败返回 None（优雅降级到当前价）。
        """
        if not self.clob_client:
            return None
        try:
            # buy_team 决定 outcome：领先队=Yes，对手队=No
            # 此处简化处理：尝试 Yes，若失败尝试 No
            # 实际应由 detector 在 alert 中标记 outcome，这里保守处理
            for outcome in ("Yes", "No"):
                _, orderbook = self.clob_client.get_token_id_and_orderbook(
                    match_id, outcome
                )
                if not orderbook:
                    continue
                asks = parse_asks(orderbook)
                if not asks:
                    continue
                vwap, _, _, _ = simulate_walk_asks(asks, target_size)
                if vwap > 0:
                    return vwap
            return None
        except Exception as exc:
            self._logger.debug("获取 VWAP 失败 match=%s: %s", match_id, exc)
            return None

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------

    @staticmethod
    def compute_pnl(
        buy_team: str,
        winning_team: str,
        buy_price: float,
        notional_usd: float,
    ) -> float:
        """计算结算 PnL。

        中奖：每份结算价 1.0
            pnl = notional_usd * (1.0 / buy_price - 1.0)
        未中奖：损失全部投入
            pnl = -notional_usd
        """
        if buy_team == winning_team:
            if buy_price <= 0:
                return 0.0
            return notional_usd * (1.0 / buy_price - 1.0)
        return -notional_usd

    def settle_trade(
        self,
        trade: MorphologyTrade,
        winning_team: str,
    ) -> bool:
        """结算单笔交易。"""
        try:
            pnl = self.compute_pnl(
                buy_team=trade.buy_team,
                winning_team=winning_team,
                buy_price=trade.buy_price,
                notional_usd=trade.notional_usd,
            )
            ok = self.repository.settle_trade(
                trade_id=trade.id or 0,
                winning_team=winning_team,
                pnl_usd=pnl,
            )
            if ok:
                trade.settled = 1
                trade.winning_team = winning_team
                trade.pnl_usd = pnl
                trade.settled_at = to_utc_iso(now_utc())
            return ok
        except Exception as exc:
            self._logger.error("settle_trade 失败: %s", exc)
            return False
