"""监控编排主类。

参照：polymarket_twitter_monitor/core/monitor.py

主循环：
1. 热重载配置检查
2. 市场发现（按间隔，task_runs 去重）
3. 遍历监控中的比赛
4. 检查已结束比赛的结算
5. 数据归档（每周一次，task_runs 去重）
6. 发送状态报告（即使无信号也定期发送）
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..api.clob import ClobClient
from ..api.esports_data import create_provider
from ..api.polymarket import PolymarketClient
from ..config.manager import (
    config_file_signature,
    load_config,
)
from ..data.sqlite_storage import SQLiteStorage
from ..discovery.service import (
    DISCOVERY_TASK_NAME,
    MarketDiscoveryService,
    mark_discovery_run,
    persist_discovered_matches,
    should_run_discovery,
)
from ..morphology.detector import MorphologyDetector
from ..morphology.repository import MorphologyRepository
from ..morphology.signals import load_backtest_stats
from ..morphology.simulator import MorphologySimulator
from ..notification.telegram import TelegramNotifier, create_notifier_from_config
from ..utils.time_utils import from_utc_iso, now_utc, to_utc_iso

ARCHIVE_TASK_NAME = "data_archive"
STATUS_REPORT_TASK_NAME = "status_report"


class Monitor:
    """监控编排主类。

    单线程 while + time.sleep 主循环。所有 DB/网络操作 try/except，
    失败仅记日志不中断主循环。
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        base_dir: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._base_dir = base_dir or os.getcwd()
        self._logger = logger or logging.getLogger(__name__)
        self.config = config if config is not None else load_config(self._base_dir)
        self._running = False
        self._last_status_report_at: Optional[Any] = None
        self._config_signature: Tuple[Optional[float], Optional[float]] = (
            config_file_signature(self._base_dir)
        )
        self._init_components()

    # ------------------------------------------------------------------
    # 组件初始化
    # ------------------------------------------------------------------

    def _init_components(self) -> None:
        """初始化所有组件（配置热重载时重建）。"""
        poly_cfg = self.config.get("polymarket") or {}
        db_path = (self.config.get("database") or {}).get("path", "esports_history.db")
        if self._base_dir and not os.path.isabs(db_path):
            db_path = os.path.join(self._base_dir, db_path)

        self.storage = SQLiteStorage(db_path=db_path)

        self.gamma_client = PolymarketClient(
            api_base=poly_cfg.get("gamma_api_base"),
            timeout=int(poly_cfg.get("timeout", 10)),
            config=self.config,
        )
        self.clob_client = ClobClient(
            storage=self.storage,
            api_base=poly_cfg.get("clob_api_base"),
            gamma_api_base=poly_cfg.get("gamma_api_base"),
            timeout=float(poly_cfg.get("timeout", 10)),
        )

        # 电竞实时数据源（预留，第一阶段 NullProvider）
        esports_cfg = self.config.get("esports_data") or {}
        self.esports_provider = create_provider(esports_cfg)

        # 形态分析
        morph_cfg = self.config.get("morphology") or {}
        backtest_dir = morph_cfg.get("backtest_dir") or "docs/backtest_results"
        if self._base_dir and not os.path.isabs(backtest_dir):
            backtest_dir = os.path.join(self._base_dir, backtest_dir)
        self.backtest_stats = load_backtest_stats(backtest_dir)

        self.morphology_repo = MorphologyRepository(self.storage, logger=self._logger)
        self.morphology_simulator = MorphologySimulator(
            repository=self.morphology_repo,
            config=morph_cfg,
            clob_client=self.clob_client,
            logger=self._logger,
        )
        self.morphology_detector = MorphologyDetector(
            repository=self.morphology_repo,
            simulator=self.morphology_simulator,
            storage=self.storage,
            config=morph_cfg,
            backtest_stats=self.backtest_stats,
            logger=self._logger,
        )

        # 市场发现
        discovery_cfg = self.config.get("discovery") or {}
        self.discovery_service = MarketDiscoveryService(
            gamma_client=self.gamma_client,
            clob_client=self.clob_client,
            config=discovery_cfg,
            logger=self._logger,
        )

        # 通知
        self.notifier = create_notifier_from_config(self.config, logger=self._logger)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run_continuous(self) -> None:
        """主循环。单线程 while + time.sleep。"""
        polling_cfg = self.config.get("polling") or {}
        interval_seconds = int(polling_cfg.get("interval_seconds", 30))
        idle_interval_seconds = int(polling_cfg.get("idle_interval_seconds", 300))

        self._running = True
        self._logger.info(
            "esports_monitor 启动 (interval=%ss idle=%ss)",
            interval_seconds, idle_interval_seconds,
        )

        # 启动时强制执行一次发现
        self._check_and_run_discovery(force=True)

        while self._running:
            live_matches: List[Dict[str, Any]] = []
            round_start = time.perf_counter()
            round_stats = {
                "processed": 0,
                "price_snapshots": 0,
                "orderbook_snapshots": 0,
                "failed": 0,
                "alerts_sent": 0,
            }
            try:
                # 1. 热重载配置检查
                self._reload_config_if_changed()

                # 2. 市场发现（按间隔，task_runs 去重）
                self._check_and_run_discovery()

                # 3. 遍历监控中的比赛
                live_matches = self.storage.get_live_matches()
                self._update_live_status(live_matches)
                for match in live_matches:
                    ok, price_n, ob_n, alert_n = self._check_single_match(match)
                    round_stats["processed"] += 1
                    if not ok:
                        round_stats["failed"] += 1
                    round_stats["price_snapshots"] += price_n
                    round_stats["orderbook_snapshots"] += ob_n
                    round_stats["alerts_sent"] += alert_n

                # 4. 检查已结束比赛的结算
                self._settle_ended_matches()

                # 5. 数据归档（每周一次，task_runs 去重）
                self._check_and_run_archive()

                # 6. 发送状态报告（即使无信号也定期发送）
                self._check_and_send_status_report()

            except Exception as exc:
                self._logger.error("监控主循环异常: %s", exc)
                round_stats["failed"] += 1
                # 主动推送错误告警
                try:
                    self.notifier.send_error_alert(
                        f"主循环异常: {exc}\n"
                        f"已处理 {round_stats['processed']} 场比赛"
                    )
                except Exception:
                    pass

            # 发送心跳（每轮结束）
            elapsed = time.perf_counter() - round_start
            has_live = len(live_matches) > 0
            next_wait = interval_seconds if has_live else idle_interval_seconds
            try:
                self._send_heartbeat(round_stats, elapsed, next_wait)
            except Exception as exc:
                self._logger.debug("发送心跳异常: %s", exc)

            # 间隔等待（可中断）
            for _ in range(max(1, next_wait)):
                if not self._running:
                    break
                time.sleep(1)

    def _send_heartbeat(
        self,
        round_stats: Dict[str, int],
        elapsed: float,
        next_wait: int,
    ) -> None:
        """发送主循环心跳。"""
        tg_cfg = (self.config.get("notification") or {}).get("telegram") or {}
        if not tg_cfg.get("heartbeat_enabled", True):
            return
        self.notifier.send_heartbeat(
            processed_matches=round_stats.get("processed", 0),
            price_snapshots_saved=round_stats.get("price_snapshots", 0),
            orderbook_snapshots_saved=round_stats.get("orderbook_snapshots", 0),
            failed_count=round_stats.get("failed", 0),
            elapsed_seconds=elapsed,
            next_interval_seconds=next_wait,
            alerts_sent=round_stats.get("alerts_sent", 0),
        )

    def stop(self) -> None:
        """请求停止主循环。"""
        self._running = False

    # ------------------------------------------------------------------
    # 配置热重载
    # ------------------------------------------------------------------

    def _reload_config_if_changed(self) -> None:
        """检查配置文件 mtime，变化时热重载。"""
        current = config_file_signature(self._base_dir)
        if current == self._config_signature:
            return
        try:
            latest = load_config(self._base_dir)
        except Exception as exc:
            self._logger.error("热重载配置失败: %s", exc)
            return
        if latest == self.config:
            self._config_signature = current
            return
        self._logger.info("配置文件变更，热重载...")
        self.config = latest
        self._config_signature = current
        self._init_components()

    # ------------------------------------------------------------------
    # 市场发现
    # ------------------------------------------------------------------

    def _check_and_run_discovery(self, force: bool = False) -> None:
        """按间隔执行市场发现。"""
        discovery_cfg = self.config.get("discovery") or {}
        if not discovery_cfg.get("enabled", True):
            return
        interval_hours = float(discovery_cfg.get("interval_hours", 6))
        if not force and not should_run_discovery(
            self.storage, interval_hours, logger=self._logger
        ):
            return
        try:
            result = self.discovery_service.discover()
            new_count = 0
            if result.has_changes:
                new_count = persist_discovered_matches(
                    self.storage, result.new_matches, logger=self._logger
                )
                self._logger.info(
                    "市场发现完成：新增 %d 场比赛（%s）",
                    new_count, result.summary,
                )
            else:
                self._logger.debug("市场发现无新增：%s", result.summary)
            mark_discovery_run(self.storage, summary=result.summary, logger=self._logger)
            # 推送市场发现报告
            self._send_discovery_report(result, new_count)
        except Exception as exc:
            self._logger.error("市场发现异常: %s", exc)
            try:
                self.notifier.send_error_alert(f"市场发现异常: {exc}")
            except Exception:
                pass

    def _send_discovery_report(
        self,
        result: Any,
        new_count: int,
    ) -> None:
        """发送市场发现报告。

        报告内容：
        1. 本轮发现统计（新增/已存在/剔除/异常）
        2. 当前监控中的完整比赛列表（显示开始时间，供用户判断）

        注意：Polymarket event 的 startDate/endDate 是"市场存续周期"，
        不是"比赛实际开始/结束时间"，无法准确判断是否正在直播。
        因此只列出比赛和开始时间，不强行分类。
        """
        tg_cfg = (self.config.get("notification") or {}).get("telegram") or {}
        if not tg_cfg.get("discovery_report_enabled", True):
            return
        # 按游戏统计新增分布
        game_breakdown: Dict[str, int] = {}
        for m in getattr(result, "new_matches", []) or []:
            g = getattr(m, "game", "") or "unknown"
            game_breakdown[g] = game_breakdown.get(g, 0) + 1

        # 已存在和剔除数从 summary 中无法精确获取，使用 new_matches 数推算
        existing_count = max(0, len(getattr(result, "new_matches", []) or []) - new_count)

        # 构建当前监控中的完整比赛列表
        monitoring_list: List[Dict[str, Any]] = []
        try:
            all_matches = self.storage.get_live_matches()
            for m in all_matches:
                monitoring_list.append({
                    "game": m.get("game") or "?",
                    "team_a": m.get("team_a") or "?",
                    "team_b": m.get("team_b") or "?",
                    "start_time": m.get("start_time") or "",
                })
        except Exception as exc:
            self._logger.debug("获取监控列表异常: %s", exc)

        try:
            self.notifier.send_discovery_report(
                new_count=new_count,
                existing_count=existing_count,
                skipped_count=getattr(result, "skipped", 0),
                errors=getattr(result, "errors", 0),
                game_breakdown=game_breakdown,
                monitoring_matches=monitoring_list,
            )
        except Exception as exc:
            self._logger.debug("发送市场发现报告异常: %s", exc)

    # ------------------------------------------------------------------
    # 单场比赛检查
    # ------------------------------------------------------------------

    def _update_live_status(self, matches: List[Dict[str, Any]]) -> None:
        """根据当前时间更新比赛状态：discovered → live → ended。"""
        if not matches:
            return
        now = now_utc()
        for match in matches:
            try:
                status = match.get("status")
                if status != "discovered" and status != "live":
                    continue
                start_str = match.get("start_time")
                end_str = match.get("end_time")
                start_dt = from_utc_iso(start_str) if start_str else None
                end_dt = from_utc_iso(end_str) if end_str else None
                # 已过结束时间 → ended
                if end_dt is not None and now > end_dt:
                    self.storage.update_match_status(match["match_id"], "ended")
                    continue
                # 已开始未结束 → live
                if status == "discovered" and start_dt is not None and now >= start_dt:
                    self.storage.update_match_status(match["match_id"], "live")
            except Exception as exc:
                self._logger.debug("更新比赛状态失败 %s: %s", match.get("match_id"), exc)

    def _check_single_match(
        self, match: Dict[str, Any]
    ) -> Tuple[bool, int, int, int]:
        """对单场比赛执行一轮检查。

        只对真正 live（已开始且未结束）的比赛采集数据和形态检测。
        未开始的比赛（now < start_time）跳过数据采集。

        Returns:
            (ok, price_snapshot_count, orderbook_snapshot_count, alerts_sent)
        """
        match_id = match.get("match_id") or ""
        slug = match.get("slug") or ""
        price_n = 0
        ob_n = 0
        alert_n = 0
        try:
            # 0. 时间窗口检查：未开始的比赛跳过数据采集和形态检测
            now = now_utc()
            start_str = match.get("start_time")
            end_str = match.get("end_time")
            start_dt = from_utc_iso(start_str) if start_str else None
            end_dt = from_utc_iso(end_str) if end_str else None
            # 已结束 → 跳过（状态应由 _update_live_status 处理为 ended）
            if end_dt is not None and now > end_dt:
                return False, 0, 0, 0
            # 未开始 → 跳过数据采集和形态检测
            if start_dt is not None and now < start_dt:
                self._logger.debug(
                    "比赛未开始，跳过 cid=%s start=%s",
                    match_id, start_str,
                )
                return True, 0, 0, 0  # 返回 ok=True 但不采集数据

            # 1. 获取 Gamma 价格
            event = self.gamma_client.fetch_event(slug)
            if not event:
                return False, 0, 0, 0
            markets = self.gamma_client.parse_match_markets(event)
            if not markets:
                return False, 0, 0, 0

            now_iso = to_utc_iso(now_utc())
            target_market = None
            for m in markets:
                if m.condition_id == match_id:
                    target_market = m
                    break
            if not target_market:
                return False, 0, 0, 0

            # 2. 写入价格快照
            ok1 = self.storage.insert_price_snapshot(
                match_id, "team_a", target_market.price_a,
                target_market.price_b, now_iso,
            )
            ok2 = self.storage.insert_price_snapshot(
                match_id, "team_b", target_market.price_b,
                target_market.price_a, now_iso,
            )
            price_n = int(bool(ok1)) + int(bool(ok2))

            # 3. 获取 CLOB 盘口并写入快照
            ob_n = self._fetch_and_save_orderbook(match, target_market, now_iso)

            # 4. 形态检测
            if self.morphology_detector and (self.config.get("morphology") or {}).get("enabled", True):
                alert = self.morphology_detector.detect(match)
                if alert and alert.status == "normal" and alert.strongest_signal:
                    if self.notifier.send_signal_alert(alert):
                        alert_n = 1
                elif alert and alert.status != "normal":
                    # 形态跳过原因通知
                    self._notify_morphology_skipped(match, alert)
            else:
                # 形态分析禁用
                self._notify_morphology_skipped(
                    match, reason="disabled", detail="morphology.enabled=False"
                )

            return True, price_n, ob_n, alert_n

        except Exception as exc:
            self._logger.error("检查比赛异常 %s: %s", match_id, exc)
            return False, price_n, ob_n, alert_n

    def _notify_morphology_skipped(
        self,
        match: Dict[str, Any],
        alert: Optional[Any] = None,
        reason: str = "",
        detail: str = "",
    ) -> None:
        """发送形态跳过原因通知。"""
        tg_cfg = (self.config.get("notification") or {}).get("telegram") or {}
        if not tg_cfg.get("skipped_notify_enabled", True):
            return
        # 避免对同一场比赛频繁推送（每轮都跳过会刷屏）：只在状态变化或每 30 分钟一次
        # 简化实现：保留每轮发送，由用户通过配置关闭
        match_id = match.get("match_id") or ""
        team_a = match.get("team_a") or ""
        team_b = match.get("team_b") or ""
        if alert is not None and not reason:
            status = getattr(alert, "status", "") or ""
            status_msg = getattr(alert, "status_message", "") or ""
            reason_map = {
                "cooldown": "cooldown",
                "no_window": "no_window",
                "no_data": "no_data",
                "buy_price_filtered": "buy_price_filtered",
                "exception": "exception",
            }
            reason = reason_map.get(status, status or "unknown")
            detail = status_msg or detail
        if not reason:
            return
        try:
            self.notifier.send_morphology_skipped(
                match_id=match_id,
                team_a=team_a, team_b=team_b,
                reason=reason, detail=detail,
            )
        except Exception as exc:
            self._logger.debug("发送形态跳过通知异常: %s", exc)

    def _fetch_and_save_orderbook(
        self, match: Dict[str, Any], market: Any, now_iso: str
    ) -> int:
        """获取盘口并写入快照。返回写入的快照数。"""
        match_id = match.get("match_id") or ""
        condition_id = market.condition_id
        saved = 0
        # 队伍A 对应 Yes outcome
        for team, outcome in (("team_a", "Yes"), ("team_b", "No")):
            try:
                token_id, orderbook = self.clob_client.get_token_id_and_orderbook(
                    condition_id, outcome
                )
                if not token_id or not orderbook:
                    continue
                if self.clob_client.save_orderbook_snapshot(
                    match_id=match_id,
                    team=team,
                    token_id=token_id,
                    orderbook=orderbook,
                    recorded_at=now_iso,
                ):
                    saved += 1
            except Exception as exc:
                self._logger.debug("盘口获取失败 %s %s: %s", match_id, team, exc)
        return saved

    # ------------------------------------------------------------------
    # 比赛结算
    # ------------------------------------------------------------------

    def _settle_ended_matches(self) -> None:
        """检查 status='ended' 的比赛，对其未结算交易执行结算。"""
        try:
            ended_matches = self.storage.get_matches_by_status("ended")
            if not ended_matches:
                return
            open_match_ids = set(self.morphology_repo.get_open_trade_match_ids())
            if not open_match_ids:
                return

            for match in ended_matches:
                match_id = match.get("match_id") or ""
                if match_id not in open_match_ids:
                    continue
                # 获取获胜队伍（从 matches 表的 winning_team 字段）
                winning_team = match.get("winning_team")
                if not winning_team:
                    # 若无 winning_team，从 Gamma API 获取
                    winning_team = self._fetch_winning_team(match)
                    if winning_team:
                        self.storage.update_match_status(
                            match_id, "ended", winning_team=winning_team
                        )
                if not winning_team:
                    continue
                trades = self.morphology_repo.get_open_trades(match_id=match_id)
                for trade in trades:
                    if self.morphology_simulator.settle_trade(trade, winning_team):
                        self.notifier.send_settlement_notification(
                            match_id=match_id,
                            team_a=match.get("team_a") or "",
                            team_b=match.get("team_b") or "",
                            winning_team=winning_team,
                            trade=trade,
                        )
                # 全部结算后更新比赛状态为 settled
                self.storage.update_match_status(match_id, "settled")
        except Exception as exc:
            self._logger.error("结算已结束比赛异常: %s", exc)

    def _fetch_winning_team(self, match: Dict[str, Any]) -> Optional[str]:
        """从 Gamma API 获取获胜队伍（基于 outcomePrices）。

        结算判定：outcomePrices[0] >= 0.99 视为该队伍获胜。
        """
        slug = match.get("slug") or ""
        if not slug:
            return None
        try:
            event = self.gamma_client.fetch_event(slug)
            if not event:
                return None
            for m in event.get("markets", []) or []:
                cid = m.get("conditionId")
                if cid != match.get("condition_id"):
                    continue
                prices = m.get("outcomePrices", [])
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except (ValueError, json.JSONDecodeError):
                        continue
                if not isinstance(prices, list) or len(prices) != 2:
                    continue
                try:
                    yes_price = float(prices[0])
                except (TypeError, ValueError):
                    continue
                if yes_price >= 0.99:
                    return match.get("team_a")
                if yes_price <= 0.01:
                    return match.get("team_b")
            return None
        except Exception as exc:
            self._logger.debug("获取获胜队伍失败 %s: %s", match.get("match_id"), exc)
            return None

    # ------------------------------------------------------------------
    # 数据归档
    # ------------------------------------------------------------------

    def _check_and_run_archive(self) -> None:
        """定期归档旧数据。默认每周运行一次。"""
        retention_cfg = self.config.get("retention") or {}
        if not retention_cfg:
            return
        interval_days = int(retention_cfg.get("archive_interval_days", 7))
        try:
            last_run_iso = self.storage.get_last_task_run(ARCHIVE_TASK_NAME)
            if last_run_iso:
                last_run = from_utc_iso(last_run_iso)
                if last_run is not None and (now_utc() - last_run).days < interval_days:
                    return
        except Exception as exc:
            self._logger.error("检查归档任务去重异常: %s", exc)

        try:
            self._run_archive()
        except Exception as exc:
            self._logger.error("数据归档异常: %s", exc)

    def _run_archive(self) -> None:
        """执行一轮归档。"""
        retention_cfg = self.config.get("retention") or {}
        retention_days = int(retention_cfg.get("days", 90))
        archive_dir = retention_cfg.get("archive_dir", "archives")
        if self._base_dir and not os.path.isabs(archive_dir):
            archive_dir = os.path.join(self._base_dir, archive_dir)
        compress = bool(retention_cfg.get("compress", True))
        vacuum_enabled = bool(retention_cfg.get("vacuum_enabled", True))

        cutoff_dt = now_utc() - timedelta(days=retention_days)
        cutoff_iso = to_utc_iso(cutoff_dt)

        os.makedirs(archive_dir, exist_ok=True)

        # 记录归档前 DB 体积
        db_size_before = self.storage.get_db_size_mb()

        # 1. 按月归档过期数据到压缩文件
        months = self._get_months_before(cutoff_dt)
        archived_total = 0
        for year_month in months:
            for table in (
                "price_snapshots",
                "orderbook_snapshots",
                "morphology_signals",
                "morphology_trades",
            ):
                archived_total += self._archive_table_month(
                    table, year_month, archive_dir, compress
                )

        # 2. 删除已归档的旧数据
        deleted_total = 0
        for table in (
            "price_snapshots",
            "orderbook_snapshots",
            "morphology_signals",
            "morphology_trades",
        ):
            deleted_total += self.storage.delete_before(table, cutoff_iso)

        # 3. 清理冷却状态和过期 task_runs
        self.storage.delete_before("morphology_cooldown", cutoff_iso)
        task_runs_cutoff = to_utc_iso(now_utc() - timedelta(days=180))
        self.storage.delete_before("task_runs", task_runs_cutoff)

        # 4. VACUUM 回收磁盘空间（仅在确实删除了数据时执行）
        vacuum_executed = False
        if vacuum_enabled and deleted_total > 0:
            vacuum_executed = self.storage.vacuum()

        # 5. 记录任务运行
        summary = f"archived_months={len(months)} archived_rows={archived_total} deleted_rows={deleted_total}"
        self.storage.mark_task_run(ARCHIVE_TASK_NAME, summary)
        self._logger.info("数据归档完成: %s", summary)

        # 6. 发送归档报告
        db_size_after = self.storage.get_db_size_mb()
        archive_files_count = 0
        try:
            if os.path.isdir(archive_dir):
                archive_files_count = len([
                    f for f in os.listdir(archive_dir)
                    if f.endswith(".json") or f.endswith(".gz")
                ])
        except Exception:
            pass
        self._send_archive_report(
            archived_rows=archived_total,
            deleted_rows=deleted_total,
            archived_months=len(months),
            vacuum_executed=vacuum_executed,
            db_size_before_mb=db_size_before,
            db_size_after_mb=db_size_after,
            archive_files_count=archive_files_count,
        )

    def _send_archive_report(
        self,
        archived_rows: int,
        deleted_rows: int,
        archived_months: int,
        vacuum_executed: bool,
        db_size_before_mb: float,
        db_size_after_mb: float,
        archive_files_count: int,
    ) -> None:
        """发送数据归档报告。"""
        tg_cfg = (self.config.get("notification") or {}).get("telegram") or {}
        if not tg_cfg.get("archive_report_enabled", True):
            return
        try:
            self.notifier.send_archive_report(
                archived_rows=archived_rows,
                deleted_rows=deleted_rows,
                archived_months=archived_months,
                vacuum_executed=vacuum_executed,
                db_size_before_mb=db_size_before_mb,
                db_size_after_mb=db_size_after_mb,
                archive_files_count=archive_files_count,
            )
        except Exception as exc:
            self._logger.debug("发送归档报告异常: %s", exc)

    def _archive_table_month(
        self,
        table_name: str,
        year_month: str,
        output_dir: str,
        compress: bool,
    ) -> int:
        """将指定月份的数据归档到压缩 JSON 文件。

        幂等设计：归档文件已存在则跳过。
        Returns:
            归档的行数。
        """
        filename = f"{table_name}_{year_month.replace('-', '')}.json.gz"
        filepath = os.path.join(output_dir, filename)
        # 幂等：已存在则跳过
        if os.path.exists(filepath):
            return 0

        start_date = f"{year_month}-01T00:00:00"
        # 计算月末
        year, month = (int(x) for x in year_month.split("-"))
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        end_date = f"{next_year:04d}-{next_month:02d}-01T00:00:00"

        rows = self.storage.query_archive_data(table_name, start_date, end_date)
        if not rows:
            return 0

        try:
            if compress:
                with gzip.open(filepath, "wt", encoding="utf-8") as f:
                    for row in rows:
                        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            else:
                plain_path = filepath[:-3] if filepath.endswith(".gz") else filepath
                with open(plain_path, "w", encoding="utf-8") as f:
                    for row in rows:
                        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            self._logger.error("写入归档文件失败 %s: %s", filepath, exc)
            return 0
        return len(rows)

    @staticmethod
    def _get_months_before(cutoff_dt: Any) -> List[str]:
        """获取 cutoff_dt 之前的所有月份（往前推最多 12 个月）。

        Returns:
            ["2026-01", "2026-02", ...] 升序
        """
        months: List[str] = []
        try:
            year = cutoff_dt.year
            month = cutoff_dt.month
            for _ in range(12):
                months.append(f"{year:04d}-{month:02d}")
                if month == 1:
                    year, month = year - 1, 12
                else:
                    month -= 1
            months.reverse()
        except Exception:
            pass
        return months

    # ------------------------------------------------------------------
    # 状态报告
    # ------------------------------------------------------------------

    def _check_and_send_status_report(self) -> None:
        """定期发送状态报告。"""
        tg_cfg = (self.config.get("notification") or {}).get("telegram") or {}
        if not tg_cfg.get("status_report_enabled", True):
            return
        interval_hours = float(tg_cfg.get("status_report_interval_hours", 6))
        now = now_utc()
        if (
            self._last_status_report_at is not None
            and (now - self._last_status_report_at).total_seconds() < interval_hours * 3600
        ):
            return
        try:
            self._send_status_report()
            self._last_status_report_at = now
        except Exception as exc:
            self._logger.error("发送状态报告异常: %s", exc)

    def _send_status_report(self) -> None:
        """构造并发送状态报告。"""
        live_matches = self.storage.get_live_matches()
        game_breakdown: Dict[str, int] = {}
        for m in live_matches:
            g = m.get("game") or "unknown"
            game_breakdown[g] = game_breakdown.get(g, 0) + 1

        recent_signals = self.morphology_repo.get_signals(hours=6)
        trade_stats = self.morphology_repo.get_trade_stats()
        db_size = self.storage.get_db_size_mb()

        # P5 — 分组胜率
        grouped_stats: Dict[str, List[Dict[str, Any]]] = {
            "by_signal": self.storage.get_trade_stats_grouped("signal_name"),
            "by_game": self.storage.get_trade_stats_grouped("game"),
            "by_window": self.storage.get_trade_stats_grouped("window_label"),
        }

        self.notifier.send_status_report(
            live_count=len(live_matches),
            game_breakdown=game_breakdown,
            recent_signals=recent_signals,
            trade_stats=trade_stats,
            db_size_mb=db_size,
            grouped_stats=grouped_stats,
        )
