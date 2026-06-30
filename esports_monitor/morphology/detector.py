"""形态检测编排主逻辑。

参照：polymarket_twitter_monitor/morphology/detector.py

每轮对每场监控中的比赛执行：
1. 冷却检查
2. 加载价格序列
3. 识别领先队/对手队
4. 计算时间窗口
5. 信号检测
6. 信号聚合
7. 买入价过滤
8. 持久化信号
9. 模拟下单
10. 设置冷却
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..utils.time_utils import (
    from_utc_iso,
    minutes_between,
    now_utc,
    to_utc_iso,
)
from .models import MorphologyAlert, MorphologySignal
from .repository import MorphologyRepository
from .signals import (
    N_RESAMPLE,
    SIGNAL_LABELS,
    compute_morphology_features,
    get_predicted_stats,
    get_signal_fn,
    get_signal_label,
    resample_series,
)
from .simulator import MorphologySimulator


class MorphologyDetector:
    """形态检测编排主类。"""

    def __init__(
        self,
        repository: MorphologyRepository,
        simulator: MorphologySimulator,
        storage: Any,
        config: Optional[Dict[str, Any]] = None,
        backtest_stats: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.repository = repository
        self.simulator = simulator
        self.storage = storage
        self.config = config or {}
        self.backtest_stats = backtest_stats or {}
        self._logger = logger or logging.getLogger(__name__)

        self.enabled = bool(self.config.get("enabled", True))
        self.cooldown_minutes = float(self.config.get("cooldown_minutes", 15))
        self.resample_points = int(self.config.get("resample_points", N_RESAMPLE))
        self.window_hours = float(self.config.get("window_hours", 2.0))
        self.buy_price_min = float(self.config.get("buy_price_min", 0.05))
        self.buy_price_max = float(self.config.get("buy_price_max", 0.95))
        self.observe_all = bool(self.config.get("observe_all", True))
        self.windows = self.config.get("windows") or []

    # ------------------------------------------------------------------
    # 检测入口
    # ------------------------------------------------------------------

    def detect(self, match: Dict[str, Any]) -> Optional[MorphologyAlert]:
        """对单场比赛执行一轮形态检测。

        Args:
            match: matches 表行字典，含 match_id, team_a, team_b, start_time, end_time 等

        Returns:
            MorphologyAlert；冷却期返回 None。
        """
        if not self.enabled:
            return None

        match_id = match.get("match_id") or ""
        team_a = match.get("team_a") or ""
        team_b = match.get("team_b") or ""
        if not match_id or not team_a or not team_b:
            return None

        # 1. 冷却检查
        cooldown_end = self.repository.get_cooldown_end(match_id)
        now = now_utc()
        if cooldown_end is not None and now < cooldown_end:
            self._logger.debug(
                "match %s 在冷却期内（剩余 %.1f 分钟）",
                match_id,
                (cooldown_end - now).total_seconds() / 60.0,
            )
            return None

        # 2. 加载价格序列
        prices_a = self.storage.get_price_snapshots(
            match_id, team="team_a", hours=self.window_hours
        )
        prices_b = self.storage.get_price_snapshots(
            match_id, team="team_b", hours=self.window_hours
        )

        resampled_a = resample_series(prices_a, self.resample_points)
        resampled_b = resample_series(prices_b, self.resample_points)

        # 3. 识别领先队/对手队（当前价格高者为领先）
        if not resampled_a or not resampled_b:
            return self._build_observation(
                match, status="no_data",
                status_message="价格数据不足（需至少 5 个去重点）",
            )

        current_a = resampled_a[-1]
        current_b = resampled_b[-1]
        if current_a >= current_b:
            leader_team, threat_team = team_a, team_b
            leader_price, threat_price = current_a, current_b
            leader_series, threat_series = resampled_a, resampled_b
        else:
            leader_team, threat_team = team_b, team_a
            leader_price, threat_price = current_b, current_a
            leader_series, threat_series = resampled_b, resampled_a

        # 4. 计算时间窗口（基于距开始分钟数）
        minutes_since_start = self._compute_minutes_since_start(match)
        window_label = self._match_window_label(minutes_since_start)
        if not window_label:
            return self._build_observation(
                match, status="no_window",
                status_message="无匹配时间窗口",
                leader_team=leader_team, threat_team=threat_team,
                leader_price=leader_price, threat_price=threat_price,
                minutes_since_start=minutes_since_start,
            )

        # 5. 信号检测
        rules = self._window_rules(window_label) or []
        t = len(leader_series) - 1
        feat_leader = compute_morphology_features(leader_series, t)
        feat_threat = compute_morphology_features(threat_series, t)

        signals: List[MorphologySignal] = []
        for rule_name in rules:
            fn = get_signal_fn(rule_name)
            if fn is None:
                continue
            try:
                direction = fn(feat_leader, feat_threat)
            except Exception as exc:
                self._logger.debug("信号 %s 执行异常: %s", rule_name, exc)
                continue
            if direction is None:
                continue

            # direction: "A" 买领先队 / "B" 买对手队
            if direction == "A":
                buy_team = leader_team
                buy_price = leader_price
            else:
                buy_team = threat_team
                buy_price = threat_price

            # 7. 买入价过滤
            if not (self.buy_price_min < buy_price < self.buy_price_max):
                continue

            # 6. 加载回测预测
            win_rate, avg_pnl, expectancy, trades, profit_factor = get_predicted_stats(
                self.backtest_stats, window_label, rule_name
            )

            sig = MorphologySignal(
                signal_name=rule_name,
                signal_label=get_signal_label(rule_name),
                window_label=window_label,
                direction=direction,
                buy_team=buy_team,
                buy_price=buy_price,
                hours_before_end=self._compute_hours_before_end(match),
                minutes_since_start=minutes_since_start,
                detected_at=to_utc_iso(now),
                morph_features={
                    "leader": feat_leader.to_dict(),
                    "threat": feat_threat.to_dict(),
                },
                predicted_win_prob=win_rate,
                predicted_pnl=avg_pnl,
                predicted_expectancy=expectancy,
                historical_trades=trades,
                historical_profit_factor=profit_factor,
                signal_strength=1,
            )
            signals.append(sig)

        # 无信号处理
        if not signals:
            return self._build_observation(
                match, status="no_signal",
                status_message="本轮无信号触发",
                leader_team=leader_team, threat_team=threat_team,
                leader_price=leader_price, threat_price=threat_price,
                window_label=window_label,
                minutes_since_start=minutes_since_start,
            )

        # 6. 信号聚合：取最强信号
        strongest = max(
            signals,
            key=lambda s: (s.signal_strength, s.predicted_expectancy or 0.0),
        )
        total_strength = sum(s.signal_strength for s in signals)

        alert = MorphologyAlert(
            match_id=match_id,
            team_a=team_a,
            team_b=team_b,
            leader_team=leader_team,
            threat_team=threat_team,
            leader_price=leader_price,
            threat_price=threat_price,
            window_label=window_label,
            minutes_since_start=minutes_since_start,
            hours_before_end=self._compute_hours_before_end(match),
            signals=signals,
            strongest_signal=strongest,
            total_strength=total_strength,
            status="normal",
        )

        # 8. 持久化信号
        self.repository.save_signals(alert)

        # 9. 模拟下单
        if self.simulator:
            trade = self.simulator.open_trade(alert)
            if trade:
                alert.trade_opened = True
                alert.trade_id = trade.id

        # 10. 设置冷却
        cooldown_end_iso = to_utc_iso(now + timedelta(minutes=self.cooldown_minutes))
        self.repository.set_cooldown(
            match_id=match_id,
            cooldown_end=cooldown_end_iso,
            last_signal=strongest.signal_name,
        )

        return alert

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------

    def settle_match(
        self,
        match_id: str,
        winning_team: str,
    ) -> int:
        """结算指定比赛的所有未结算交易。

        Returns:
            成功结算的交易数。
        """
        settled_count = 0
        trades = self.repository.get_open_trades(match_id=match_id)
        for trade in trades:
            if self.simulator.settle_trade(trade, winning_team):
                settled_count += 1
        if settled_count:
            self._logger.info(
                "match %s 结算 %d 笔交易，获胜队伍=%s",
                match_id, settled_count, winning_team,
            )
        return settled_count

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _build_observation(
        self,
        match: Dict[str, Any],
        status: str,
        status_message: str = "",
        leader_team: str = "",
        threat_team: str = "",
        leader_price: float = 0.0,
        threat_price: float = 0.0,
        window_label: str = "",
        minutes_since_start: Optional[float] = None,
    ) -> Optional[MorphologyAlert]:
        """构建无信号观察记录（用于可观测性）。"""
        if not self.observe_all:
            return None
        return MorphologyAlert(
            match_id=match.get("match_id") or "",
            team_a=match.get("team_a") or "",
            team_b=match.get("team_b") or "",
            leader_team=leader_team,
            threat_team=threat_team,
            leader_price=leader_price,
            threat_price=threat_price,
            window_label=window_label,
            minutes_since_start=minutes_since_start,
            hours_before_end=self._compute_hours_before_end(match),
            signals=[],
            strongest_signal=None,
            total_strength=0,
            status=status,
            status_message=status_message,
        )

    def _compute_minutes_since_start(
        self, match: Dict[str, Any]
    ) -> Optional[float]:
        """计算距比赛开始的分钟数。"""
        start_str = match.get("start_time")
        if not start_str:
            return None
        start_dt = from_utc_iso(start_str)
        if start_dt is None:
            return None
        return max(0.0, minutes_between(start_dt, now_utc()))

    def _compute_hours_before_end(
        self, match: Dict[str, Any]
    ) -> Optional[float]:
        """计算距比赛结束的小时数。"""
        end_str = match.get("end_time")
        if not end_str:
            return None
        end_dt = from_utc_iso(end_str)
        if end_dt is None:
            return None
        from ..utils.time_utils import hours_between

        return max(0.0, hours_between(now_utc(), end_dt))

    def _match_window_label(
        self, minutes_since_start: Optional[float]
    ) -> Optional[str]:
        """根据距开始分钟数匹配窗口标签。"""
        if minutes_since_start is None:
            return None
        for w in self.windows:
            try:
                min_m = float(w.get("min_minutes", 0))
                max_m = float(w.get("max_minutes", 9999))
            except (TypeError, ValueError):
                continue
            if min_m <= minutes_since_start <= max_m:
                return w.get("label")
        return None

    def _window_rules(self, window_label: str) -> List[str]:
        """获取指定窗口的规则列表。"""
        for w in self.windows:
            if w.get("label") == window_label:
                rules = w.get("rules") or []
                return list(rules)
        return []
