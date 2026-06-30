"""市场发现：电竞市场自动发现+流动性筛选。

参照：polymarket_twitter_monitor/discovery/

发现流程：
1. 查询 Gamma API /events?tag_id={esports_tag}（或 tag_slug）
2. 过滤出 CS2 / Dota2 / LoL 赛事
3. 对每场赛事调 parse_match_markets 获取二元市场
4. 流动性筛选：
   - CLOB 盘口买一深度 >= min_depth（默认 50 USDC）
   - 价差 <= max_spread（默认 0.05）
   - 价格在 [0.05, 0.95] 区间（排除已决出胜负的市场）
5. 写入 matches 表（新增比赛，INSERT OR IGNORE）
6. 记录 task_runs（去重，跨重启有效）

注意：根据需求改动，只采集新市场（活跃市场）的数据进行持久化，
不查询历史已结算市场。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from ..api.polymarket import PolymarketClient
from ..api.clob import ClobClient, parse_orderbook_summary
from ..utils.time_utils import from_utc_iso, now_utc, to_utc_iso

DISCOVERY_TASK_NAME = "market_discovery"


@dataclass
class DiscoveredMatch:
    """流动性筛选通过的电竞比赛。"""

    match_id: str  # condition_id
    slug: str
    game: str
    league: Optional[str]
    team_a: str
    team_b: str
    condition_id: str
    token_id_a: Optional[str]
    token_id_b: Optional[str]
    price_a: float
    price_b: float
    start_time: Optional[str]
    end_time: Optional[str]
    best_bid_a: float = 0.0
    best_ask_a: float = 0.0
    spread_a: float = 0.0
    bid_depth_a: float = 0.0
    best_bid_b: float = 0.0
    best_ask_b: float = 0.0
    spread_b: float = 0.0
    bid_depth_b: float = 0.0


@dataclass
class DiscoveryResult:
    """市场发现结果。"""

    new_matches: List[DiscoveredMatch] = field(default_factory=list)
    skipped: int = 0
    errors: int = 0
    summary: str = ""

    @property
    def has_changes(self) -> bool:
        return len(self.new_matches) > 0


# 游戏 tag 关键字匹配（用于从 event 的 tags/title 中识别游戏类型）
GAME_KEYWORDS: Dict[str, List[str]] = {
    "cs2": ["cs2", "counter-strike", "counter strike", "csgo"],
    "dota2": ["dota2", "dota 2", "dota"],
    "lol": ["lol", "league of legends", "league-of-legends"],
}


class MarketDiscoveryService:
    """电竞市场发现服务。

    所有公开方法均不抛异常——网络/解析错误返回空结果，
    保证主循环不被中断。
    """

    def __init__(
        self,
        gamma_client: PolymarketClient,
        clob_client: Optional[ClobClient] = None,
        config: Optional[Dict[str, Any]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.gamma_client = gamma_client
        self.clob_client = clob_client
        self.config = config or {}
        self._logger = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def discover(self) -> DiscoveryResult:
        """执行一轮市场发现。

        精确发现流程（以 Polymarket 网页 isLive 标记为唯一真相）：
        1. 抓取 Polymarket 电竞页面，解析 "isLive":true 的 event slug 集合
        2. 对每个 live slug 调 Gamma API fetch_event 获取详情
        3. 解析比赛胜负二元市场
        4. 识别游戏类型（cs2/dota2/lol）
        5. 流动性筛选

        若网页抓取失败（返回空 slug 列表），fallback 到 Gamma API
        volume24hr 降序查询（不精确，但保证发现流程不中断）。
        """
        result = DiscoveryResult()
        games = self.config.get("games") or ["cs2", "dota2", "lol"]

        # 1. 从 Polymarket 网页提取 isLive:true 的 slug 集合（精确）
        live_slugs = self.gamma_client.fetch_live_event_slugs()
        use_precise_mode = bool(live_slugs)

        if use_precise_mode:
            # 精确模式：只处理 isLive:true 的赛事
            self._logger.info(
                "市场发现（精确模式）：%d 场直播赛事 %s",
                len(live_slugs), live_slugs,
            )
            events: List[Dict[str, Any]] = []
            for slug in live_slugs:
                event = self.gamma_client.fetch_event(slug)
                if event:
                    events.append(event)
                else:
                    result.errors += 1
        else:
            # Fallback 模式：Gamma API volume24hr 降序查询
            self._logger.warning(
                "市场发现（fallback 模式）：网页抓取失败，使用 Gamma API volume24hr 降序"
            )
            tag_slugs = self.config.get("tag_slugs")
            if not tag_slugs:
                single = self.config.get("tag_slug") or "esports"
                tag_slugs = [single]
            if not isinstance(tag_slugs, list):
                tag_slugs = [str(tag_slugs)]

            live_window_hours = float(self.config.get("live_window_hours", 48))
            now = now_utc()
            end_date_min = to_utc_iso(now)
            start_date_max = to_utc_iso(now + timedelta(hours=live_window_hours))

            events = []
            seen_event_ids: set = set()
            for slug in tag_slugs:
                fetched = self.gamma_client.fetch_events(
                    tag_slug=str(slug),
                    end_date_min=end_date_min,
                    start_date_max=start_date_max,
                )
                if not fetched:
                    continue
                for e in fetched:
                    eid = e.get("id") or e.get("slug")
                    if eid and eid not in seen_event_ids:
                        seen_event_ids.add(eid)
                        events.append(e)

        if not events:
            self._logger.info("市场发现：未拉取到电竞赛事")
            result.summary = "no events fetched"
            return result

        self._logger.info("市场发现：处理 %d 场赛事", len(events))
        for event in events:
            try:
                game = self._detect_game(event)
                if not game or game not in games:
                    result.skipped += 1
                    continue

                markets = self.gamma_client.parse_match_markets(event)
                if not markets:
                    result.skipped += 1
                    continue

                for m in markets:
                    discovered = self._screen_market(event, game, m)
                    if discovered:
                        result.new_matches.append(discovered)
                    else:
                        result.skipped += 1
            except Exception as exc:
                self._logger.debug("赛事解析失败: %s", exc)
                result.errors += 1

        mode = "precise" if use_precise_mode else "fallback"
        result.summary = (
            f"mode={mode} events={len(events)} new_matches={len(result.new_matches)} "
            f"skipped={result.skipped} errors={result.errors}"
        )
        return result

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _detect_game(self, event: Dict[str, Any]) -> Optional[str]:
        """从 event 的 tags/title/slug 中识别游戏类型。"""
        # 1. 检查 tags
        tags = event.get("tags") or []
        if isinstance(tags, list):
            for tag in tags:
                tag_label = ""
                if isinstance(tag, dict):
                    tag_label = str(tag.get("label") or tag.get("slug") or "")
                elif isinstance(tag, str):
                    tag_label = tag
                tag_label_lower = tag_label.lower()
                for game, keywords in GAME_KEYWORDS.items():
                    if any(kw in tag_label_lower for kw in keywords):
                        return game

        # 2. 检查 title
        title = str(event.get("title") or "").lower()
        for game, keywords in GAME_KEYWORDS.items():
            if any(kw in title for kw in keywords):
                return game

        # 3. 检查 slug
        slug = str(event.get("slug") or "").lower()
        for game, keywords in GAME_KEYWORDS.items():
            if any(kw in slug for kw in keywords):
                return game
        return None

    def _screen_market(
        self, event: Dict[str, Any], game: str, market: Any
    ) -> Optional[DiscoveredMatch]:
        """对单个市场执行 live 窗口 + 流动性筛选。

        筛选条件：
        - 价格在 [price_min, price_max] 区间
        - live 时间窗口：end_date >= now 且 start_date <= now + live_window_hours
        - 比赛时长 <= max_match_duration_hours（排除赛季冠军等长期市场）
        - 若 CLOB 可用：买一深度 >= min_depth，价差 <= max_spread
        """
        price_min = float(self.config.get("price_min", 0.05))
        price_max = float(self.config.get("price_max", 0.95))
        max_spread = float(self.config.get("max_spread", 0.05))
        min_depth = float(self.config.get("min_depth", 50))
        # live 窗口：允许发现未来 N 小时内开始的比赛（默认 48h）
        live_window_hours = float(self.config.get("live_window_hours", 48))
        # 单场比赛最大时长（小时）：排除赛季冠军等长期市场（默认 24h）
        max_match_duration_hours = float(
            self.config.get("max_match_duration_hours", 24)
        )

        price_a = market.price_a
        price_b = market.price_b
        if not (price_min <= price_a <= price_max and price_min <= price_b <= price_max):
            return None

        # live 时间窗口筛选
        start_time_str = market.start_date or self._extract_start_time(event)
        end_time_str = market.end_date or event.get("endDate")
        start_dt = from_utc_iso(start_time_str) if start_time_str else None
        end_dt = from_utc_iso(end_time_str) if end_time_str else None
        now = now_utc()

        # 已结束（end_date < now）→ 跳过
        if end_dt is not None and end_dt < now:
            self._logger.debug(
                "比赛已结束 cid=%s end=%s", market.condition_id, end_time_str
            )
            return None
        # 太远的未来（start_date > now + live_window_hours）→ 跳过
        if start_dt is not None and start_dt > now + timedelta(hours=live_window_hours):
            self._logger.debug(
                "比赛开始时间过远 cid=%s start=%s > now+%sh",
                market.condition_id, start_time_str, live_window_hours,
            )
            return None
        # 赛季冠军等长期市场（end - start > max_duration）→ 跳过
        if start_dt is not None and end_dt is not None:
            duration_hours = (end_dt - start_dt).total_seconds() / 3600.0
            if duration_hours > max_match_duration_hours:
                self._logger.debug(
                    "市场时长过长（疑似赛季市场）cid=%s duration=%.1fh > %.1fh",
                    market.condition_id, duration_hours, max_match_duration_hours,
                )
                return None

        league = self._extract_league(event)
        start_time = start_time_str
        end_time = end_time_str

        token_id_a = market.clob_token_ids[0] if len(market.clob_token_ids) >= 1 else None
        token_id_b = market.clob_token_ids[1] if len(market.clob_token_ids) >= 2 else None

        discovered = DiscoveredMatch(
            match_id=market.condition_id,
            slug=market.slug or event.get("slug", ""),
            game=game,
            league=league,
            team_a=market.team_a,
            team_b=market.team_b,
            condition_id=market.condition_id,
            token_id_a=token_id_a,
            token_id_b=token_id_b,
            price_a=price_a,
            price_b=price_b,
            start_time=start_time,
            end_time=end_time,
        )

        # 若无 CLOB 客户端，跳过深度筛选，直接通过（用价格筛选作为最低门槛）
        if not self.clob_client:
            return discovered

        # 若流动性筛选阈值宽松（min_depth=0 且 max_spread>=1.0），
        # 跳过 CLOB 调用加速发现（验收/调试场景）
        if min_depth <= 0 and max_spread >= 1.0:
            return discovered

        # 获取盘口深度
        # 队伍A 的盘口（用 Yes outcome）
        depth_a = self._fetch_depth(market.condition_id, "Yes")
        if depth_a is None:
            # 盘口获取失败，按通过处理（不阻塞发现）
            return discovered
        discovered.best_bid_a = depth_a["best_bid"]
        discovered.best_ask_a = depth_a["best_ask"]
        discovered.spread_a = depth_a["spread"]
        discovered.bid_depth_a = depth_a["total_bid_depth"]

        # 流动性筛选：深度不足或价差过大则跳过
        if discovered.bid_depth_a < min_depth:
            self._logger.debug(
                "流动性不足 cid=%s depth=%.2f < %.2f",
                market.condition_id, discovered.bid_depth_a, min_depth,
            )
            return None
        if discovered.spread_a > max_spread:
            self._logger.debug(
                "价差过大 cid=%s spread=%.4f > %.4f",
                market.condition_id, discovered.spread_a, max_spread,
            )
            return None

        return discovered

    def _fetch_depth(self, condition_id: str, outcome: str) -> Optional[Dict[str, float]]:
        """获取盘口深度摘要。失败返回 None。"""
        if not self.clob_client:
            return None
        try:
            _, orderbook = self.clob_client.get_token_id_and_orderbook(condition_id, outcome)
            if not orderbook:
                return None
            return parse_orderbook_summary(orderbook)
        except Exception as exc:
            self._logger.debug("获取盘口深度失败 cid=%s: %s", condition_id, exc)
            return None

    @staticmethod
    def _extract_league(event: Dict[str, Any]) -> Optional[str]:
        """从 event 中提取联赛名。"""
        # Polymarket event 可能含 league 字段，或在 title 中
        league = event.get("league")
        if league:
            return str(league)
        # slug 中可能含联赛名（如 esl-ipro-league-cs2）
        slug = str(event.get("slug") or "")
        if slug:
            return slug
        return None

    @staticmethod
    def _extract_start_time(event: Dict[str, Any]) -> Optional[str]:
        """从 event 中提取开始时间。"""
        # 优先 startDate
        for key in ("startDate", "start_date", "startTime"):
            v = event.get(key)
            if v:
                return str(v)
        return None


def persist_discovered_matches(
    storage: Any,
    matches: List[DiscoveredMatch],
    logger: Optional[logging.Logger] = None,
) -> int:
    """将发现的比赛写入 matches 表（已存在则更新）。

    Returns:
        成功写入的数量。
    """
    log = logger or logging.getLogger(__name__)
    count = 0
    for m in matches:
        try:
            ok = storage.upsert_match(
                match_id=m.match_id,
                slug=m.slug,
                game=m.game,
                team_a=m.team_a,
                team_b=m.team_b,
                condition_id=m.condition_id,
                league=m.league,
                token_id_a=m.token_id_a,
                token_id_b=m.token_id_b,
                start_time=m.start_time,
                end_time=m.end_time,
                status="discovered",
            )
            if ok:
                count += 1
        except Exception as exc:
            log.error("写入 match 失败 cid=%s: %s", m.condition_id, exc)
    return count


def should_run_discovery(
    storage: Any,
    interval_hours: float,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """判断是否应该执行市场发现任务（基于 task_runs 去重）。

    Args:
        storage: SQLiteStorage 实例
        interval_hours: 发现间隔（小时）
        logger: 可选日志器

    Returns:
        True 表示应该执行，False 表示距上次运行不足间隔。
    """
    log = logger or logging.getLogger(__name__)
    try:
        last_run_iso = storage.get_last_task_run(DISCOVERY_TASK_NAME)
        if not last_run_iso:
            return True
        last_run = from_utc_iso(last_run_iso)
        if last_run is None:
            return True
        elapsed_hours = (now_utc() - last_run).total_seconds() / 3600.0
        if elapsed_hours < interval_hours:
            log.debug(
                "市场发现跳过：距上次运行 %.1f 小时 < 间隔 %.1f 小时",
                elapsed_hours, interval_hours,
            )
            return False
        return True
    except Exception as exc:
        log.error("should_run_discovery 异常: %s", exc)
        return True


def mark_discovery_run(
    storage: Any,
    summary: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """记录市场发现任务已运行。"""
    log = logger or logging.getLogger(__name__)
    try:
        return storage.mark_task_run(DISCOVERY_TASK_NAME, summary)
    except Exception as exc:
        log.error("mark_discovery_run 异常: %s", exc)
        return False
