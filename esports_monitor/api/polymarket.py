"""Gamma API 客户端（市场元数据+价格）。

参照：polymarket_twitter_monitor/api/polymarket.py

Gamma API 是公开只读端点（https://gamma-api.polymarket.com），无需认证，
用于获取市场元数据和滞后价格。

注意：根据需求改动，本模块只采集新市场（活跃市场）的数据进行持久化，
不实现历史已结算市场的批量抓取与回填方法。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests


@dataclass
class MatchMarket:
    """从 event 解析出的二元比赛胜负市场。"""

    condition_id: str
    clob_token_ids: List[str] = field(default_factory=list)
    team_a: str = ""
    team_b: str = ""
    price_a: float = 0.0
    price_b: float = 0.0
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    question: str = ""
    # 关联 event 的 slug，便于后续 fetch_event
    slug: str = ""
    # 实际比赛开始时间（eventStartTime / gameStartTime），区别于市场创建时间 startDate
    event_start_time: Optional[str] = None
    # 原始市场数据（用于调试/扩展）
    raw: Optional[Dict[str, Any]] = None


class PolymarketClient:
    """Gamma API 客户端。

    所有公开方法均不抛异常——网络/解析错误返回 None 或空列表，
    保证主循环不被中断。
    """

    GAMMA_API_BASE = "https://gamma-api.polymarket.com"

    # 队伍名解析正则：支持 "Will Team A defeat Team B?" 与 "Team A vs Team B"
    _RE_DEFEAT = re.compile(r"Will\s+(.+?)\s+defeat\s+(.+?)\s*\?", re.IGNORECASE)
    _RE_VS = re.compile(r"(.+?)\s+vs\.?\s+(.+)", re.IGNORECASE)
    # 开头游戏前缀，如 "Counter-Strike:" / "Dota 2:" / "LoL:" / "Rainbow Six Siege:"
    _RE_GAME_PREFIX = re.compile(r"^[^:]+:\s*")
    # 队伍名后缀："(BO3) ..." 括号后缀 或 " - 赛事名" 后缀
    _RE_TEAM_SUFFIX_PAREN = re.compile(r"\s*\(")
    _RE_TEAM_SUFFIX_DASH = re.compile(r"\s+-\s+")

    def __init__(
        self,
        api_base: Optional[str] = None,
        timeout: int = 10,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.api_base = (api_base or self.GAMMA_API_BASE).rstrip("/")
        self.timeout = timeout
        self.config = config or {}
        self._logger = logging.getLogger(__name__)
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; EsportsMonitor/1.0)"}
        )

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def fetch_events(
        self,
        tag_id: str = "",
        tag_slug: str = "",
        active_only: bool = True,
        end_date_min: str = "",
        start_date_max: str = "",
        order_by_volume: bool = True,
    ) -> List[Dict[str, Any]]:
        """按电竞 tag 获取赛事列表。

        Args:
            tag_id: Polymarket 电竞 tag ID
            tag_slug: Polymarket 电竞 tag slug（如 "esports"）
            active_only: 是否只拉取活跃（未关闭）赛事。默认 True，
                避免拉取大量已结算历史赛事。
            end_date_min: 结束时间下限（ISO 8601），只拉取 endDate >= 此值的赛事。
                传入当前时间可排除已结束赛事，让 live 比赛进入前 100 条限额。
            start_date_max: 开始时间上限（ISO 8601），只拉取 startDate <= 此值的赛事。
                传入 now+live_window_hours 可排除太远的未来赛事。
            order_by_volume: 是否按 24h 交易量降序排列。默认 True。
                关键：Gamma API 默认排序返回的 100 条主要是长期市场，
                真正的 live 比赛在 100 条之后。用 volume24hr 降序可让
                高交易量的 live 比赛排到前面，进入 100 条限额内。

        Returns:
            event 字典列表；失败返回空列表。
        """
        params: Dict[str, Any] = {}
        if tag_id:
            params["tag_id"] = tag_id
        if tag_slug:
            params["tag_slug"] = tag_slug
        if not params:
            params["tag_slug"] = "esports"
        if active_only:
            # 只拉取未关闭的活跃赛事，排除已结算历史赛事
            params["closed"] = "false"
        if end_date_min:
            params["end_date_min"] = end_date_min
        if start_date_max:
            params["start_date_max"] = start_date_max
        if order_by_volume:
            # 按 24h 交易量降序，让 live 比赛排到前面
            params["order"] = "volume24hr"
            params["ascending"] = "false"
        # 限制单页数量，避免过大响应
        params.setdefault("limit", 500)
        return self._get_list("/events", params)

    def fetch_event(self, slug: str) -> Optional[Dict[str, Any]]:
        """获取单场赛事详情（含子市场）。

        Args:
            slug: event slug

        Returns:
            event 字典；失败或无数据返回 None。
        """
        if not slug:
            return None
        data = self._get_list("/events", {"slug": slug})
        if not data:
            return None
        return data[0]

    # Polymarket 电竞页面 URL
    ESPORTS_PAGE_URL = "https://polymarket.com/zh/esports"

    # 游戏到 Polymarket 页面 slug 的映射
    GAME_PAGE_SLUGS: Dict[str, str] = {
        "cs2": "cs2",
        "lol": "league-of-legends",
        "dota2": "dota-2",
    }

    def _fetch_esports_page_html(self, url: str) -> str:
        """抓取 Polymarket 电竞页面 HTML，失败返回空字符串。"""
        try:
            resp = self._session.get(
                url,
                timeout=self.timeout,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            if resp.status_code != 200:
                self._logger.error("Polymarket 页面返回 status %s url=%s", resp.status_code, url)
                return ""
            return resp.text
        except requests.exceptions.RequestException as exc:
            self._logger.error("抓取 Polymarket 页面失败 url=%s: %s", url, exc)
            return ""

    def _merge_next_f_push(self, html: str) -> str:
        """从 HTML 中提取 Next.js streaming 数据并合并为单个字符串。"""
        push_data = re.findall(
            r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.DOTALL
        )
        if not push_data:
            return ""
        combined = ""
        for d in push_data:
            try:
                combined += d.encode().decode("unicode_escape")
            except Exception:
                combined += d
        return combined

    def fetch_live_event_slugs(self) -> List[str]:
        """从 Polymarket 电竞总页抓取正在直播的赛事 slug 列表。

        Gamma API 没有 isLive 字段，无法直接过滤正在直播的比赛。
        Polymarket 网页的 Next.js SSR 数据中包含 "isLive":true 标记，
        这是唯一能精确判断比赛是否正在直播的数据源。

        Returns:
            正在直播的 event slug 列表；失败返回空列表。
        """
        html = self._fetch_esports_page_html(self.ESPORTS_PAGE_URL)
        if not html:
            return []
        combined = self._merge_next_f_push(html)
        if not combined:
            self._logger.warning("Polymarket 页面无 __next_f.push 数据")
            return []

        # 找所有 "isLive":true 的位置，向前搜索最近的 event slug
        live_slugs: List[str] = []
        islive_positions = [
            m.start() for m in re.finditer(r'"isLive"\s*:\s*true', combined)
        ]
        for pos in islive_positions:
            context_before = combined[max(0, pos - 2000):pos]
            slug_matches = list(re.finditer(r'"slug"\s*:\s*"([^"]+)"', context_before))
            if slug_matches:
                slug = slug_matches[-1].group(1)
                if re.search(r"-\d{4}-\d{2}-\d{2}$", slug):
                    if slug not in live_slugs:
                        live_slugs.append(slug)

        self._logger.info(
            "从 Polymarket 页面提取到 %d 场直播赛事: %s",
            len(live_slugs), live_slugs,
        )
        return live_slugs

    def fetch_game_page_events(
        self, game: str, league: str = ""
    ) -> Dict[str, Dict[str, Any]]:
        """抓取游戏专属页面，返回 {slug: {isLive, start_time, league}} 映射。

        用于精确判断某游戏下每场比赛的实时状态与开始时间，
        解决 Gamma API live 字段不准确的问题（如 CS2 无直播却被标记）。

        Args:
            game: 游戏标识（cs2/lol/dota2）
            league: 可选联赛 slug（如 "esports-world-cup"）。传入时抓取
                联赛专属页 https://polymarket.com/zh/esports/{game_slug}/{league_slug}，
                仅返回属于该联赛的赛事。留空时抓取游戏总页，返回该游戏下所有赛事。
        """
        page_slug = self.GAME_PAGE_SLUGS.get(game)
        if not page_slug:
            self._logger.warning("未知游戏 %s，无对应页面 slug", game)
            return {}
        # 联赛专属页：/esports/{game_slug}/{league_slug}
        if league:
            url = f"{self.ESPORTS_PAGE_URL}/{page_slug}/{league}"
        else:
            url = f"{self.ESPORTS_PAGE_URL}/{page_slug}"
        html = self._fetch_esports_page_html(url)
        if not html:
            return {}
        combined = self._merge_next_f_push(html)
        if not combined:
            self._logger.warning("Polymarket 页面无 __next_f.push 数据 url=%s", url)
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        slug_pattern = re.compile(r'"slug"\s*:\s*"([^"]+?)"')
        for m in slug_pattern.finditer(combined):
            slug = m.group(1)
            if not re.search(r"-\d{4}-\d{2}-\d{2}$", slug):
                continue
            if slug in result:
                continue
            # 在 slug 之后 1500 字符内查找 isLive 和 startTime
            after = combined[m.end(): m.end() + 1500]
            is_live = bool(re.search(r'"isLive"\s*:\s*true', after))
            start_time = None
            st_m = re.search(
                r'"(?:startTime|eventStartTime|gameStartTime)"\s*:\s*"([^"]+)"',
                after,
            )
            if st_m:
                start_time = st_m.group(1)
            result[slug] = {
                "isLive": is_live,
                "start_time": start_time,
                "league": league or "",
            }

        self._logger.info(
            "游戏 %s%s 页面提取到 %d 场赛事 (直播 %d)",
            game, f"/{league}" if league else "", len(result),
            sum(1 for v in result.values() if v.get("isLive")),
        )
        return result

    # 子市场关键字：parse_match_markets 跳过包含这些关键字的市场
    _SUB_MARKET_KEYWORDS = (
        "map 1", "map 2", "map 3", "map winner",
        "handicap", "o/u ", "odd/even",
        "total kills", "total rounds", "total games",
        "game 1", "game 2", "game 3",
        "rounds handicap", "games total",
    )
    # 主市场 groupItemTitle 白名单
    _MAIN_MARKET_TITLES = ("match winner", "series winner", "match", "series")

    def parse_match_markets(self, event: Dict[str, Any]) -> List[MatchMarket]:
        """从 event 解析出比赛胜负二元市场（仅主市场）。

        筛选条件：
        - outcomes 为二元（["Yes", "No"] 或两支队伍名）
        - question 包含 vs / defeat 等对阵关键词
        - outcomePrices 有两个有效价格
        - groupItemTitle 为 "Match Winner" / "Series Winner"（排除 Map/Game Handicap 等子市场）
          若无 groupItemTitle 字段，用 question 关键字兜底过滤
        """
        markets: List[MatchMarket] = []
        if not isinstance(event, dict):
            return markets

        for m in event.get("markets", []) or []:
            try:
                condition_id = m.get("conditionId")
                if not condition_id:
                    continue

                outcomes = self._parse_json_field(m.get("outcomes", []))
                prices = self._parse_json_field(m.get("outcomePrices", []))
                if len(outcomes) != 2 or len(prices) != 2:
                    continue

                try:
                    price_a = float(prices[0])
                    price_b = float(prices[1])
                except (TypeError, ValueError):
                    continue

                if price_a <= 0 or price_b <= 0:
                    continue

                question = m.get("question", "")
                team_a, team_b = self._parse_team_names(question)
                if not team_a or not team_b:
                    continue

                # 子市场过滤：优先用 groupItemTitle，无则用 question 关键字
                group_title = str(m.get("groupItemTitle") or "").lower().strip()
                question_lower = question.lower()
                if group_title:
                    # 有 groupItemTitle：只保留主市场
                    if not any(t in group_title for t in self._MAIN_MARKET_TITLES):
                        continue
                else:
                    # 无 groupItemTitle：用 question 关键字兜底过滤子市场
                    if any(kw in question_lower for kw in self._SUB_MARKET_KEYWORDS):
                        continue

                # 实际比赛开始时间：优先 eventStartTime，其次 gameStartTime
                event_start_time = (
                    m.get("eventStartTime")
                    or m.get("gameStartTime")
                )

                markets.append(
                    MatchMarket(
                        condition_id=str(condition_id),
                        clob_token_ids=[str(t) for t in self._parse_json_field(m.get("clobTokenIds", []))],
                        team_a=team_a,
                        team_b=team_b,
                        price_a=price_a,
                        price_b=price_b,
                        start_date=m.get("startDate"),
                        end_date=m.get("endDate"),
                        question=question,
                        slug=event.get("slug", ""),
                        event_start_time=str(event_start_time) if event_start_time else None,
                        raw=m,
                    )
                )
            except Exception as exc:  # 单条市场解析失败不影响其它
                self._logger.debug("Skip market parse: %s", exc)
        return markets

    def _parse_team_names(self, question: str) -> tuple:
        """从市场问题文本解析两队名称。

        支持格式：
        - "Will Team A defeat Team B?"
        - "Team A vs Team B"
        - "Team A v Team B"
        - "游戏名: Team A vs Team B (BO3) - 赛事名"（Polymarket 电竞常见格式）

        会自动去除：
        - 开头的游戏前缀（如 "Counter-Strike:" / "Dota 2:" / "LoL:"）
        - 队伍B 后的 "(BO3)" 括号后缀和 " - 赛事名" 后缀

        Returns:
            (team_a, team_b)；解析失败返回 ("", "")。
        """
        if not question:
            return "", ""
        # 去除开头的游戏前缀，如 "Counter-Strike: " / "Dota 2: " / "Rainbow Six Siege: "
        q = self._RE_GAME_PREFIX.sub("", question.strip(), count=1)
        m = self._RE_DEFEAT.search(q)
        if m:
            return m.group(1).strip(), self._clean_team_name(m.group(2))
        m = self._RE_VS.search(q)
        if m:
            return m.group(1).strip(), self._clean_team_name(m.group(2))
        return "", ""

    @classmethod
    def _clean_team_name(cls, name: str) -> str:
        """清理队伍名：去除末尾的 (BO3) 括号后缀和 " - 赛事名" 后缀。

        Polymarket 电竞 question 格式常为 "队伍B (BO3) - 赛事名"，
        vs 正则的 group2 贪婪匹配会包含这些后缀，需在此清理。
        """
        s = name.strip()
        # 先去除括号后缀 "(BO3) ..."（取括号前部分）
        s = cls._RE_TEAM_SUFFIX_PAREN.split(s, maxsplit=1)[0]
        # 再去除 " - 赛事名" 后缀（队伍名一般不含 " - "）
        s = cls._RE_TEAM_SUFFIX_DASH.split(s, maxsplit=1)[0]
        return s.strip()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_list(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """GET 请求，返回 JSON 列表。失败返回空列表。"""
        url = f"{self.api_base}{path}"
        try:
            resp = self._session.get(url, params=params or {}, timeout=self.timeout)
            if resp.status_code != 200:
                self._logger.error(
                    "Gamma API %s returned status %s", path, resp.status_code
                )
                return []
            data = resp.json()
            if not isinstance(data, list):
                return []
            return data
        except requests.exceptions.RequestException as exc:
            self._logger.error("Gamma API request error %s: %s", path, exc)
            return []
        except (ValueError, json.JSONDecodeError) as exc:
            self._logger.error("Gamma API JSON decode error %s: %s", path, exc)
            return []

    @staticmethod
    def _parse_json_field(value: Any) -> List[Any]:
        """解析可能是 JSON 字符串或列表的字段。

        Gamma API 的 outcomes / outcomePrices / clobTokenIds 字段可能是
        JSON 字符串（如 '["Yes","No"]'）或原生列表。
        """
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (ValueError, json.JSONDecodeError):
                return []
            return []
        if isinstance(value, list):
            return value
        return []
