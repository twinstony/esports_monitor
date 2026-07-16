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
    # 比赛状态（从 Polymarket event 的 live/ended 字段获取）
    live: bool = False
    ended: bool = False


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
        # 是否启用联赛白名单过滤（在 discover() 中根据 leagues 配置置位）
        self._leagues_filter_active: bool = False

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def discover(self) -> DiscoveryResult:
        """执行一轮市场发现。

        发现流程：
        1. 通过 Gamma API 按 tag_slugs 查询所有活跃赛事（closed=false）
        2. 对每场赛事调 parse_match_markets 获取二元市场
        3. 识别游戏类型（cs2/dota2/lol）
        4. 流动性筛选

        注意：使用 Gamma API 直接查询，获取所有活跃赛事（包括即将开始的），
        不依赖 Polymarket 网页的 isLive 标记（仅获取正在直播的赛事）。
        """
        result = DiscoveryResult()
        games = self.config.get("games") or ["cs2", "dota2", "lol"]

        # 使用 Gamma API 按 tag_slugs 查询所有活跃赛事
        tag_slugs = self.config.get("tag_slugs")
        if not tag_slugs:
            single = self.config.get("tag_slug") or "esports"
            tag_slugs = [single]
        if not isinstance(tag_slugs, list):
            tag_slugs = [str(tag_slugs)]

        live_window_hours = float(self.config.get("live_window_hours", 168))
        now = now_utc()
        end_date_min = to_utc_iso(now)
        start_date_max = to_utc_iso(now + timedelta(hours=live_window_hours))

        events: List[Dict[str, Any]] = []
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

        # 按游戏抓取 Polymarket 页面数据，获取权威的 isLive / startTime。
        # Gamma API 的 live 字段不准确（如 CS2 无直播却被标记），网页 SSR
        # 数据中的 isLive:true 是判断比赛是否正在进行的唯一可靠数据源。
        # 若配置了 leagues 白名单，则抓取联赛专属页（仅返回属于该联赛的赛事），
        # 后续 _screen_market 会用 page_events 作为白名单过滤掉其他联赛的比赛。
        leagues = self.config.get("leagues") or []
        if not isinstance(leagues, list):
            leagues = [str(leagues)]
        page_events_by_game: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for game in games:
            page_events_by_game[game] = {}
            if leagues:
                # 抓取联赛专属页：仅返回属于指定联赛的赛事 slug
                for league in leagues:
                    try:
                        page_data = self.gamma_client.fetch_game_page_events(
                            game, league=str(league)
                        )
                        page_events_by_game[game].update(page_data)
                    except Exception as exc:
                        self._logger.debug(
                            "抓取联赛页失败 %s/%s: %s", game, league, exc
                        )
            else:
                # 无 leagues 配置：抓取游戏总页，返回该游戏下所有赛事
                try:
                    page_events_by_game[game] = self.gamma_client.fetch_game_page_events(game)
                except Exception as exc:
                    self._logger.debug("抓取游戏页面失败 %s: %s", game, exc)

        self._leagues_filter_active = bool(leagues)

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

                page_events = page_events_by_game.get(game) or {}
                # 联赛白名单过滤：若配置了 leagues，仅保留出现在联赛页的赛事
                event_slug = event.get("slug") or ""
                if self._leagues_filter_active and event_slug and page_events and event_slug not in page_events:
                    self._logger.debug(
                        "赛事 %s 不在配置的联赛页 %s 中，跳过",
                        event_slug, leagues,
                    )
                    result.skipped += 1
                    continue

                for m in markets:
                    discovered = self._screen_market(event, game, m, page_events)
                    if discovered:
                        result.new_matches.append(discovered)
                    else:
                        result.skipped += 1
            except Exception as exc:
                self._logger.debug("赛事解析失败: %s", exc)
                result.errors += 1

        result.summary = (
            f"mode=gamma_api events={len(events)} new_matches={len(result.new_matches)} "
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
        self,
        event: Dict[str, Any],
        game: str,
        market: Any,
        page_events: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[DiscoveredMatch]:
        """对单个市场执行 live 窗口 + 流动性筛选。

        筛选条件：
        - 价格在 [price_min, price_max] 区间
        - live 时间窗口：end_date >= now 且 start_date <= now + live_window_hours
        - 比赛时长 <= max_match_duration_hours（排除赛季冠军等长期市场）
        - 若 CLOB 可用：买一深度 >= min_depth，价差 <= max_spread

        page_events: Polymarket 游戏页面抓取的 {slug: {isLive, start_time}} 映射，
        用于获取权威的 isLive 和实际比赛开始时间（优先于 Gamma API 的 live/startDate）。
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

        # 实际比赛开始时间：优先 Polymarket 网页数据（权威），其次 eventStartTime，
        # 最后 startDate（市场创建时间，不准确）
        event_slug = event.get("slug") or ""
        page_info = (page_events or {}).get(event_slug) or {}
        page_start_time = page_info.get("start_time")
        start_time_str = (
            page_start_time
            or market.event_start_time
            or self._extract_start_time(event)
            or market.start_date
        )
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
        # 若联赛页提供了 league slug（如 "esports-world-cup"），优先使用
        if page_info.get("league"):
            league = str(page_info["league"])
        start_time = start_time_str
        end_time = end_time_str

        token_id_a = market.clob_token_ids[0] if len(market.clob_token_ids) >= 1 else None
        token_id_b = market.clob_token_ids[1] if len(market.clob_token_ids) >= 2 else None

        # 从 Polymarket 网页提取权威 isLive 状态（优先于 Gamma API 的 live 字段）。
        # Gamma API 的 live 字段不准确（如 CS2 无直播却被标记），网页 SSR 数据
        # 中的 isLive:true 才是判断比赛是否正在进行的可靠依据。
        page_is_live = bool(page_info.get("isLive", False))
        event_live = page_is_live or bool(event.get("live", False))
        event_ended = bool(event.get("ended", False))

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
            live=event_live,
            ended=event_ended,
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
        """从 event 中提取实际比赛开始时间。

        优先使用 startTime（比赛开始时间），
        其次 eventStartTime，
        最后才用 startDate（市场创建时间，不准确）。
        """
        # 优先 startTime（这是实际比赛开始时间）
        for key in ("startTime", "eventStartTime", "eventDate"):
            v = event.get(key)
            if v:
                return str(v)
        # 最后才用 startDate（市场创建时间）
        for key in ("startDate", "start_date"):
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

    根据 Polymarket event 的 live/ended 字段确定初始状态：
    - ended=True → status="ended"
    - live=True → status="live"
    - 否则 → status="discovered"

    注意：upsert_match 不会降级已有状态（如 live→discovered）。

    Returns:
        成功写入的数量。
    """
    log = logger or logging.getLogger(__name__)
    count = 0
    for m in matches:
        try:
            # 根据事件状态确定初始 status
            if m.ended:
                status = "ended"
            elif m.live:
                status = "live"
            else:
                status = "discovered"
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
                status=status,
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
