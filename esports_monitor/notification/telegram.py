"""Telegram 通知。

参照：polymarket_twitter_monitor/notification/telegram.py

通知类型：
- 形态信号告警
- 状态报告（即使无信号也定期发送，确认系统运行）
- 模拟交易结算通知
- 错误告警
- 主循环心跳（每轮结束发送，含本轮采集/失败/耗时）
- 市场发现报告（每轮发现执行后发送）
- 形态跳过原因（冷却/无窗口/数据不足等）
- 数据归档报告（每次归档任务执行后发送）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from ..morphology.models import MorphologyAlert, MorphologyTrade
from ..utils.time_utils import from_utc_iso, now_utc, to_beijing_str


def _truncate(s: str, max_len: int) -> str:
    """截断字符串到指定长度，超出加省略号。"""
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _to_beijing_short(iso_str: str) -> str:
    """UTC ISO 字符串转北京时间简短显示（MM-DD HH:MM）。"""
    dt = from_utc_iso(iso_str) if iso_str else None
    if dt is None:
        return ""
    return to_beijing_str(dt, "%m-%d %H:%M")


class TelegramNotifier:
    """Telegram Bot 通知器。

    所有方法均不抛异常——网络错误返回 False，保证主循环不被中断。
    """

    API_BASE = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = True,
        logger: Optional[logging.Logger] = None,
    ):
        self.bot_token = bot_token or ""
        self.chat_id = chat_id or ""
        self.enabled = enabled
        self._logger = logger or logging.getLogger(__name__)

    def is_configured(self) -> bool:
        """是否配置了 bot_token 和 chat_id。"""
        return bool(self.bot_token and self.chat_id)

    def can_send(self) -> bool:
        """是否可以发送（启用且已配置）。"""
        return self.enabled and self.is_configured()

    # ------------------------------------------------------------------
    # 基础发送
    # ------------------------------------------------------------------

    def send_message(
        self,
        message: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> bool:
        """发送文本消息。"""
        if not self.can_send():
            return False
        url = f"{self.API_BASE}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True
            self._logger.error(
                "Telegram sendMessage 失败 status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        except requests.exceptions.RequestException as exc:
            self._logger.error("Telegram sendMessage 异常: %s", exc)
            return False

    # ------------------------------------------------------------------
    # 业务通知
    # ------------------------------------------------------------------

    def send_signal_alert(self, alert: MorphologyAlert) -> bool:
        """发送形态信号告警。"""
        if not alert.strongest_signal:
            return False
        sig = alert.strongest_signal
        lines = [
            f"🎮 <b>[形态信号] {sig.signal_name} — {sig.signal_label}</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"比赛: {alert.team_a} vs {alert.team_b}",
            f"窗口: {alert.window_label}",
        ]
        if alert.minutes_since_start is not None:
            lines.append(f"赛中: {alert.minutes_since_start:.0f} 分钟")
        lines.append(f"买入: <b>{sig.buy_team}</b> @ {sig.buy_price:.4f}")
        if sig.predicted_win_prob is not None:
            lines.append(f"预测胜率: {sig.predicted_win_prob*100:.1f}%")
        if sig.predicted_pnl is not None:
            lines.append(f"预测PnL: {sig.predicted_pnl*100:+.1f}%")
        if alert.total_strength > 1:
            lines.append(f"信号强度: {alert.total_strength} 个聚合")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {to_beijing_str(now_utc())} 北京时间")
        return self.send_message("\n".join(lines))

    def send_status_report(
        self,
        live_count: int,
        game_breakdown: Dict[str, int],
        recent_signals: List[Dict[str, Any]],
        trade_stats: Dict[str, Any],
        db_size_mb: float,
        grouped_stats: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> bool:
        """发送状态报告（即使无信号也定期发送）。

        Args:
            live_count: 监控中比赛数
            game_breakdown: 按游戏分布
            recent_signals: 最近信号列表
            trade_stats: 交易汇总
            db_size_mb: 数据库体积 MB
            grouped_stats: 分组胜率（P5 维度）。
                形如 {"by_signal": [...], "by_game": [...], "by_window": [...]}
                每个元素含 group_key/total/settled/wins/losses/total_pnl
        """
        game_str = ", ".join(f"{c} {g.upper()}" for g, c in game_breakdown.items()) or "无"
        signal_count = len(recent_signals)
        # 信号分布
        sig_dist: Dict[str, int] = {}
        for s in recent_signals:
            name = s.get("signal_name") or "unknown"
            sig_dist[name] = sig_dist.get(name, 0) + 1
        sig_dist_str = (
            ", ".join(f"{n}×{c}" for n, c in sig_dist.items()) if sig_dist else "无"
        )

        total = trade_stats.get("total", 0)
        settled = trade_stats.get("settled", 0)
        wins = trade_stats.get("wins", 0)
        losses = trade_stats.get("losses", 0)
        total_pnl = trade_stats.get("total_pnl", 0.0)
        win_rate = (wins / settled * 100) if settled > 0 else 0.0

        lines = [
            "📊 <b>[esports_monitor 状态报告]</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"监控中比赛: {live_count} 场 ({game_str})",
            f"最近信号: {signal_count} 个 ({sig_dist_str})",
            f"模拟交易: 累计 {total} 笔, 已结算 {settled} 笔",
            f"  胜率: {win_rate:.1f}% ({wins}胜{losses}负)",
            f"  累计PnL: {total_pnl:+.2f} USD",
            f"数据库体积: {db_size_mb:.1f} MB",
        ]

        # P5 — 分组胜率
        if grouped_stats:
            grouped_lines = self._format_grouped_stats(grouped_stats)
            if grouped_lines:
                lines.append("━━━━━━━━━━━━━━━━━━━━━")
                lines.extend(grouped_lines)

        lines.append("系统状态: 运行正常")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {to_beijing_str(now_utc())} 北京时间")
        return self.send_message("\n".join(lines))

    @staticmethod
    def _format_grouped_stats(
        grouped_stats: Dict[str, List[Dict[str, Any]]],
    ) -> List[str]:
        """格式化分组胜率统计。"""
        title_map = {
            "by_signal": "🎯 按信号分组",
            "by_game": "🎮 按游戏分组",
            "by_window": "⏱️ 按窗口分组",
        }
        lines: List[str] = []
        for key, title in title_map.items():
            rows = grouped_stats.get(key) or []
            # 只展示有结算数据的分组
            settled_rows = [r for r in rows if r.get("settled", 0) > 0]
            if not settled_rows:
                continue
            lines.append(title + ":")
            # 按 total_pnl 降序
            sorted_rows = sorted(
                settled_rows, key=lambda r: r.get("total_pnl", 0.0), reverse=True
            )
            for r in sorted_rows[:5]:  # 每组最多 5 条
                gk = r.get("group_key") or "?"
                st = r.get("settled", 0)
                w = r.get("wins", 0)
                lo = r.get("losses", 0)
                pnl = r.get("total_pnl", 0.0)
                wr = (w / st * 100) if st > 0 else 0.0
                lines.append(f"  {gk}: {st}笔 {wr:.0f}%胜 {pnl:+.1f}USD")
        return lines

    def send_settlement_notification(
        self,
        match_id: str,
        team_a: str,
        team_b: str,
        winning_team: str,
        trade: MorphologyTrade,
    ) -> bool:
        """发送模拟交易结算通知。"""
        is_winner = trade.buy_team == winning_team
        pnl = trade.pnl_usd or 0.0
        result_emoji = "✅" if is_winner else "❌"
        lines = [
            f"{result_emoji} <b>[模拟交易结算]</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"比赛: {team_a} vs {team_b}",
            f"获胜: <b>{winning_team}</b>",
            f"买入: {trade.buy_team} @ {trade.buy_price:.4f}",
            f"份数: {trade.quantity:.2f}",
            f"PnL: {pnl:+.2f} USD",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"⏰ {to_beijing_str(now_utc())} 北京时间",
        ]
        return self.send_message("\n".join(lines))

    def send_error_alert(self, error_summary: str) -> bool:
        """发送错误告警。"""
        lines = [
            "⚠️ <b>[错误告警]</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            error_summary,
            "━━━━━━━━━━━━━━━━━━━━━",
            f"⏰ {to_beijing_str(now_utc())} 北京时间",
        ]
        return self.send_message("\n".join(lines))

    # ------------------------------------------------------------------
    # P0 — 主循环心跳
    # ------------------------------------------------------------------

    def send_heartbeat(
        self,
        processed_matches: int,
        price_snapshots_saved: int,
        orderbook_snapshots_saved: int,
        failed_count: int,
        elapsed_seconds: float,
        next_interval_seconds: int,
        alerts_sent: int = 0,
        live_details: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """发送主循环心跳（每轮结束发送）。

        Args:
            processed_matches: 本轮处理的比赛数
            price_snapshots_saved: 本轮写入的价格快照数
            orderbook_snapshots_saved: 本轮写入的盘口快照数
            failed_count: 本轮失败次数（API/DB 异常）
            elapsed_seconds: 本轮耗时（秒）
            next_interval_seconds: 下次循环等待秒数
            alerts_sent: 本轮发送的形态信号告警数
            live_details: 本轮监控的比赛详情列表
        """
        # 状态标识：无失败=健康；少量=警告；大量=异常
        if failed_count == 0:
            status_icon, status_text = "✅", "正常"
        elif failed_count < processed_matches:
            status_icon, status_text = "🟡", "部分失败"
        else:
            status_icon, status_text = "🔴", "全部失败"

        lines = [
            f"{status_icon} <b>[主循环心跳]</b> {status_text}",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"处理比赛: {processed_matches} 场",
            f"价格快照: +{price_snapshots_saved}",
            f"盘口快照: +{orderbook_snapshots_saved}",
            f"信号告警: {alerts_sent} 个",
            f"失败次数: {failed_count}",
            f"本轮耗时: {elapsed_seconds:.1f}s",
            f"下次间隔: {next_interval_seconds}s",
        ]

        # 展示本轮监控的比赛详情
        if live_details:
            lines.append("━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"📊 <b>监控中比赛 ({len(live_details)} 场)</b>")
            for d in live_details:
                game_tag = f"[{d.get('game', '?')}]"
                team_a = d.get("team_a", "?")
                team_b = d.get("team_b", "?")
                pa = d.get("price_a")
                pb = d.get("price_b")
                msm = d.get("minutes_since_start")
                ob = d.get("orderbook_snapshots", 0)
                price_str = f"A={pa:.4f} B={pb:.4f}" if pa is not None and pb is not None else "价格获取中"
                min_str = f"{msm:.0f}min" if msm is not None else "?min"
                lines.append(f"  {game_tag} {team_a} vs {team_b}")
                lines.append(f"    {price_str} | 赛中 {min_str} | 盘口+{ob}")

        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {to_beijing_str(now_utc())} 北京时间")
        return self.send_message("\n".join(lines))

    # ------------------------------------------------------------------
    # P2 — 市场发现报告
    # ------------------------------------------------------------------

    def send_discovery_report(
        self,
        new_count: int,
        existing_count: int,
        skipped_count: int,
        errors: int,
        game_breakdown: Dict[str, int],
        monitoring_matches: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """发送市场发现报告。

        报告内容：
        1. 本轮发现统计（新增/已存在/剔除/异常/游戏分布）
        2. 当前监控中的完整比赛列表（显示开始时间）

        注意：Polymarket 的 startDate/endDate 是市场存续周期，不是比赛实际时间，
        因此不强行分类"进行中/即将开始"，只列出比赛和市场开始时间供用户判断。

        Args:
            new_count: 新增比赛数
            existing_count: 已存在比赛数
            skipped_count: 流动性筛选剔除数
            errors: 异常数
            game_breakdown: 按游戏分布 {cs2: 3, dota2: 1, lol: 2}
            monitoring_matches: 监控中的比赛列表，每项含 game/team_a/team_b/start_time
        """
        game_str = ", ".join(
            f"{c} {g.upper()}" for g, c in game_breakdown.items() if c > 0
        ) or "无"

        lines = [
            "🔍 <b>[市场发现]</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"新增: <b>{new_count}</b> 场",
            f"已存在: {existing_count} 场",
            f"流动性剔除: {skipped_count} 场",
            f"异常: {errors}",
            f"游戏分布: {game_str}",
        ]

        # 监控中的比赛列表（显示开始时间）
        if monitoring_matches:
            lines.append("━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"📊 监控中（{len(monitoring_matches)} 场）:")
            for i, m in enumerate(monitoring_matches, 1):
                game = (m.get("game") or "?").upper()
                ta = _truncate(m.get("team_a") or "?", 20)
                tb = _truncate(m.get("team_b") or "?", 20)
                start_str = m.get("start_time") or ""
                start_beijing = _to_beijing_short(start_str)
                time_str = f" @ {start_beijing}" if start_beijing else ""
                lines.append(f"{i}. [{game}] {ta} vs {tb}{time_str}")

        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {to_beijing_str(now_utc())} 北京时间")
        return self.send_message("\n".join(lines))

    # ------------------------------------------------------------------
    # P3 — 形态跳过原因
    # ------------------------------------------------------------------

    def send_morphology_skipped(
        self,
        match_id: str,
        team_a: str,
        team_b: str,
        reason: str,
        detail: str = "",
    ) -> bool:
        """发送形态检测跳过原因。

        Args:
            match_id: 比赛ID
            team_a: 队伍A
            team_b: 队伍B
            reason: 跳过原因标签（cooldown/no_window/no_data/buy_price_filtered/disabled）
            detail: 详细信息（如冷却剩余分钟、当前价格等）
        """
        reason_map = {
            "cooldown": ("❄️", "冷却中"),
            "no_window": ("⏱️", "无匹配窗口"),
            "no_data": ("📉", "数据不足"),
            "buy_price_filtered": ("💰", "买入价过滤"),
            "disabled": ("⚙️", "形态分析禁用"),
            "exception": ("⚠️", "检测异常"),
        }
        icon, label = reason_map.get(reason, ("❔", reason))
        lines = [
            f"{icon} <b>[形态跳过] {label}</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"比赛: {team_a} vs {team_b}",
        ]
        if detail:
            lines.append(f"详情: {detail}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {to_beijing_str(now_utc())} 北京时间")
        return self.send_message("\n".join(lines))

    # ------------------------------------------------------------------
    # P4 — 数据归档报告
    # ------------------------------------------------------------------

    def send_archive_report(
        self,
        archived_rows: int,
        deleted_rows: int,
        archived_months: int,
        vacuum_executed: bool,
        db_size_before_mb: float,
        db_size_after_mb: float,
        archive_files_count: int,
    ) -> bool:
        """发送数据归档报告。

        Args:
            archived_rows: 归档到压缩文件的总行数
            deleted_rows: 从 DB 删除的总行数
            archived_months: 归档的月份数
            vacuum_executed: 是否执行了 VACUUM
            db_size_before_mb: 归档前 DB 体积
            db_size_after_mb: 归档后 DB 体积
            archive_files_count: 归档目录文件数
        """
        saved_mb = db_size_before_mb - db_size_after_mb
        vacuum_str = "✅ 已执行" if vacuum_executed else "⏭️ 跳过"
        lines = [
            "📦 <b>[数据归档报告]</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"归档月份: {archived_months} 个",
            f"归档行数: {archived_rows}",
            f"删除行数: {deleted_rows}",
            f"VACUUM: {vacuum_str}",
            f"DB体积: {db_size_before_mb:.1f} → {db_size_after_mb:.1f} MB",
            f"  节省: {saved_mb:.1f} MB" if saved_mb > 0 else f"  变化: {saved_mb:+.1f} MB",
            f"归档文件: {archive_files_count} 个",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"⏰ {to_beijing_str(now_utc())} 北京时间",
        ]
        return self.send_message("\n".join(lines))

    # ------------------------------------------------------------------
    # 每日模拟开单总结
    # ------------------------------------------------------------------

    # TG 单条消息字符上限（留余量应对 HTML 标签）
    TG_MSG_LIMIT = 3800

    def send_daily_trade_summary(
        self,
        date_str: str,
        daily_stats: Dict[str, Any],
        cumulative_stats: Dict[str, Any],
        monitored_count: int = 0,
        trades_detail: Optional[List[Dict[str, Any]]] = None,
        grouped_stats: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        period_hours: int = 24,
    ) -> bool:
        """发送每日模拟开单总结（支持分多条发送）。

        Args:
            date_str: 报告日期标签（YYYY-MM-DD）
            daily_stats: 过去 period_hours 统计
                {"total", "settled", "wins", "losses", "total_pnl"}
            cumulative_stats: 累计统计（同结构）
            monitored_count: 过去 period_hours 监控的比赛数
            trades_detail: 逐单详情列表，每项含
                {"opened_at", "game", "team_a", "team_b", "buy_team", "buy_price",
                 "signal_name", "window_label", "settled", "pnl_usd", ...}
            grouped_stats: 分组统计（按信号/游戏/窗口）
            period_hours: 统计周期（小时），用于显示
        """
        dt = daily_stats
        ct = cumulative_stats
        trades_detail = trades_detail or []

        dt_total = dt.get("total", 0)
        dt_settled = dt.get("settled", 0)
        dt_wins = dt.get("wins", 0)
        dt_losses = dt.get("losses", 0)
        dt_pnl = dt.get("total_pnl", 0.0)
        dt_win_rate = (dt_wins / dt_settled * 100) if dt_settled > 0 else 0.0

        ct_total = ct.get("total", 0)
        ct_settled = ct.get("settled", 0)
        ct_wins = ct.get("wins", 0)
        ct_losses = ct.get("losses", 0)
        ct_pnl = ct.get("total_pnl", 0.0)
        ct_win_rate = (ct_wins / ct_settled * 100) if ct_settled > 0 else 0.0

        # ---- 第一条：概要 ----
        lines = [
            f"📊 <b>[运行报告 {date_str}]</b>",
            f"📋 统计周期: 过去 {period_hours} 小时",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"🏟 监控比赛数: <b>{monitored_count}</b> 场",
            f"📅 开单数: <b>{dt_total}</b> 笔",
            f"  已结算: {dt_settled} 笔",
            f"  胜率: {dt_win_rate:.1f}% ({dt_wins}胜{dt_losses}负)",
            f"  PnL: <b>{dt_pnl:+.2f} USD</b>",
            "",
            f"🏆 累计交易: {ct_total} 笔",
            f"  已结算: {ct_settled} 笔",
            f"  胜率: {ct_win_rate:.1f}% ({ct_wins}胜{ct_losses}负)",
            f"  累计PnL: {ct_pnl:+.2f} USD",
        ]

        if grouped_stats:
            lines.append("━━━━━━━━━━━━━━━━━━━━━")
            lines.append("📈 分组统计 (按已结算):")
            for key, title in [("by_signal", "按信号"), ("by_game", "按游戏"), ("by_window", "按窗口")]:
                rows = grouped_stats.get(key) or []
                settled_rows = [r for r in rows if r.get("settled", 0) > 0]
                if not settled_rows:
                    continue
                lines.append(f"  {title}:")
                sorted_rows = sorted(settled_rows, key=lambda r: r.get("total_pnl", 0.0), reverse=True)
                for r in sorted_rows[:3]:
                    gk = r.get("group_key") or "?"
                    st = r.get("settled", 0)
                    w = r.get("wins", 0)
                    lo = r.get("losses", 0)
                    pnl = r.get("total_pnl", 0.0)
                    wr = (w / st * 100) if st > 0 else 0.0
                    lines.append(f"    {gk}: {st}笔 {wr:.0f}%胜 {pnl:+.1f}USD")

        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ {to_beijing_str(now_utc())} 北京时间")

        ok = self.send_message("\n".join(lines))
        if not ok:
            return False

        # ---- 后续：逐单详情，按字符上限分条 ----
        if not trades_detail:
            return True

        # 格式化每条交易为一行
        trade_lines: List[str] = []
        for i, t in enumerate(trades_detail, 1):
            opened = _to_beijing_short(t.get("opened_at") or "")
            game = (t.get("game") or "?").upper()
            ta = _truncate(t.get("team_a") or "?", 10)
            tb = _truncate(t.get("team_b") or "?", 10)
            buy_team = _truncate(t.get("buy_team") or "?", 10)
            buy_price = t.get("buy_price")
            bp_str = f"{buy_price:.3f}" if isinstance(buy_price, (int, float)) else "?"
            signal = _truncate(t.get("signal_name") or "?", 18)
            window = t.get("window_label") or "?"
            settled = t.get("settled")
            pnl = t.get("pnl_usd")
            if settled:
                pnl_str = f"<b>{pnl:+.2f}</b>" if isinstance(pnl, (int, float)) else "?"
                status_icon = "✅" if (isinstance(pnl, (int, float)) and pnl > 0) else "❌"
            else:
                pnl_str = "未结算"
                status_icon = "⏳"
            trade_lines.append(
                f"{status_icon}#{i} [{opened}] {game}\n"
                f"  {ta} vs {tb} → <b>买{buy_team}</b>@{bp_str}\n"
                f"  依据: {signal} ({window}) | PnL: {pnl_str}"
            )

        # 拼接并按字符上限分条发送
        header = f"📝 <b>逐单详情 ({len(trade_lines)} 笔)</b>\n" + "━" * 21 + "\n"
        cur_msg = header
        for line in trade_lines:
            # +1 为换行符
            if len(cur_msg) + len(line) + 1 > self.TG_MSG_LIMIT:
                ok = self.send_message(cur_msg)
                if not ok:
                    return False
                cur_msg = f"📝 <b>逐单详情（续）</b>\n" + "━" * 21 + "\n"
            cur_msg += line + "\n"

        if cur_msg.strip():
            ok = self.send_message(cur_msg)
        return ok


def create_notifier_from_config(
    config: Dict[str, Any],
    logger: Optional[logging.Logger] = None,
) -> TelegramNotifier:
    """从配置字典创建 TelegramNotifier。"""
    tg_cfg = (config.get("notification") or {}).get("telegram") or {}
    return TelegramNotifier(
        bot_token=tg_cfg.get("bot_token") or "",
        chat_id=tg_cfg.get("chat_id") or "",
        enabled=bool(tg_cfg.get("enabled", True)),
        logger=logger,
    )
