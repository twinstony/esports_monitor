"""discovery/service.py 测试。"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict
from unittest.mock import MagicMock, call

import pytest

from esports_monitor.api.polymarket import MatchMarket
from esports_monitor.discovery.service import (
    DISCOVERY_TASK_NAME,
    DiscoveredMatch,
    DiscoveryResult,
    MarketDiscoveryService,
    mark_discovery_run,
    persist_discovered_matches,
    should_run_discovery,
)
from esports_monitor.utils.time_utils import now_utc, to_utc_iso


class TestDetectGame:
    def setup_method(self):
        self.svc = MarketDiscoveryService(
            gamma_client=MagicMock(),
            clob_client=None,
            config={"games": ["cs2", "dota2", "lol"]},
        )

    def test_from_tags(self):
        event = {"tags": [{"label": "CS2"}]}
        assert self.svc._detect_game(event) == "cs2"

    def test_from_title(self):
        event = {"title": "LCK LoL Finals"}
        assert self.svc._detect_game(event) == "lol"

    def test_from_slug(self):
        event = {"slug": "dota2-international"}
        assert self.svc._detect_game(event) == "dota2"

    def test_no_match(self):
        event = {"tags": [], "title": "Random Event", "slug": "random"}
        assert self.svc._detect_game(event) is None

    def test_tag_as_string(self):
        event = {"tags": ["Counter-Strike"]}
        assert self.svc._detect_game(event) == "cs2"


class TestScreenMarket:
    def setup_method(self):
        self.svc = MarketDiscoveryService(
            gamma_client=MagicMock(),
            clob_client=None,  # 无 CLOB，跳过深度筛选
            config={
                "games": ["lol"],
                "price_min": 0.05, "price_max": 0.95,
                "min_depth": 50, "max_spread": 0.05,
            },
        )

    def test_price_in_range_passes(self):
        future_end = to_utc_iso(now_utc() + timedelta(hours=2))
        market = MatchMarket(
            condition_id="0x1", clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
            end_date=future_end,
        )
        result = self.svc._screen_market({"slug": "test"}, "lol", market)
        assert result is not None
        assert result.team_a == "T1"

    def test_price_out_of_range_rejected(self):
        market = MatchMarket(
            condition_id="0x1", clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.99, price_b=0.01,
        )
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is None

    def test_with_clob_depth_insufficient(self):
        clob = MagicMock()
        clob.get_token_id_and_orderbook.return_value = ("tok", {
            "bids": [{"price": "0.5", "size": "10"}],  # 深度仅 10 < 50
            "asks": [{"price": "0.55", "size": "80"}],
        })
        svc = MarketDiscoveryService(
            gamma_client=MagicMock(),
            clob_client=clob,
            config={
                "games": ["lol"],
                "price_min": 0.05, "price_max": 0.95,
                "min_depth": 50, "max_spread": 0.05,
            },
        )
        market = MatchMarket(
            condition_id="0x1", clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        assert svc._screen_market({"slug": "test"}, "lol", market) is None

    def test_with_clob_spread_too_large(self):
        clob = MagicMock()
        clob.get_token_id_and_orderbook.return_value = ("tok", {
            "bids": [{"price": "0.5", "size": "100"}],
            "asks": [{"price": "0.60", "size": "80"}],  # 价差 0.10 > 0.05
        })
        svc = MarketDiscoveryService(
            gamma_client=MagicMock(),
            clob_client=clob,
            config={
                "games": ["lol"],
                "price_min": 0.05, "price_max": 0.95,
                "min_depth": 50, "max_spread": 0.05,
            },
        )
        market = MatchMarket(
            condition_id="0x1", clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        assert svc._screen_market({"slug": "test"}, "lol", market) is None

    def test_with_clob_passes(self):
        clob = MagicMock()
        clob.get_token_id_and_orderbook.return_value = ("tok", {
            "bids": [{"price": "0.54", "size": "100"}],
            "asks": [{"price": "0.56", "size": "80"}],
        })
        svc = MarketDiscoveryService(
            gamma_client=MagicMock(),
            clob_client=clob,
            config={
                "games": ["lol"],
                "price_min": 0.05, "price_max": 0.95,
                "min_depth": 50, "max_spread": 0.05,
            },
        )
        market = MatchMarket(
            condition_id="0x1", clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        result = svc._screen_market({"slug": "test"}, "lol", market)
        assert result is not None
        assert result.bid_depth_a == 100.0

    def test_clob_returns_none_passes(self):
        # CLOB 返回 None 时不应阻塞发现
        clob = MagicMock()
        clob.get_token_id_and_orderbook.return_value = (None, None)
        svc = MarketDiscoveryService(
            gamma_client=MagicMock(),
            clob_client=clob,
            config={
                "games": ["lol"],
                "price_min": 0.05, "price_max": 0.95,
                "min_depth": 50, "max_spread": 0.05,
            },
        )
        market = MatchMarket(
            condition_id="0x1", clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
        )
        result = svc._screen_market({"slug": "test"}, "lol", market)
        assert result is not None  # 盘口失败按通过处理


class TestScreenMarketTimeWindow:
    """live 时间窗口过滤测试。"""

    def setup_method(self):
        self.svc = MarketDiscoveryService(
            gamma_client=MagicMock(),
            clob_client=None,
            config={
                "games": ["lol"],
                "price_min": 0.0001, "price_max": 0.9999,
                "min_depth": 50, "max_spread": 0.05,
                "live_window_hours": 48,
                "max_match_duration_hours": 48,
            },
        )

    def _make_market(
        self,
        start_date: str = None,
        end_date: str = None,
        price_a: float = 0.55,
        price_b: float = 0.45,
    ) -> MatchMarket:
        return MatchMarket(
            condition_id="0x1", clob_token_ids=["ta", "tb"],
            team_a="T1", team_b="Gen.G",
            price_a=price_a, price_b=price_b,
            start_date=start_date, end_date=end_date,
        )

    def test_ended_market_rejected(self):
        """end_date < now 的比赛应被过滤（已结束）。"""
        now = now_utc()
        past_end = to_utc_iso(now - timedelta(hours=1))
        market = self._make_market(
            start_date=to_utc_iso(now - timedelta(hours=3)),
            end_date=past_end,
        )
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is None

    def test_far_future_market_rejected(self):
        """start_date > now + live_window_hours 的比赛应被过滤（太远的未来）。"""
        now = now_utc()
        far_start = to_utc_iso(now + timedelta(hours=72))  # 72h > 48h
        market = self._make_market(
            start_date=far_start,
            end_date=to_utc_iso(now + timedelta(hours=80)),
        )
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is None

    def test_live_market_passes(self):
        """正在进行中的比赛（start < now < end）应通过。"""
        now = now_utc()
        market = self._make_market(
            start_date=to_utc_iso(now - timedelta(hours=1)),
            end_date=to_utc_iso(now + timedelta(hours=2)),
        )
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is not None

    def test_upcoming_within_window_passes(self):
        """未来 24h 内开始的比赛（在 live_window=48h 内）应通过。"""
        now = now_utc()
        market = self._make_market(
            start_date=to_utc_iso(now + timedelta(hours=24)),
            end_date=to_utc_iso(now + timedelta(hours=30)),
        )
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is not None

    def test_long_duration_market_rejected(self):
        """比赛时长 > max_match_duration_hours 应被过滤（赛季冠军等长期市场）。"""
        now = now_utc()
        market = self._make_market(
            start_date=to_utc_iso(now - timedelta(hours=10)),
            end_date=to_utc_iso(now + timedelta(hours=100)),  # 总时长 110h > 48h
        )
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is None

    def test_extreme_price_low_passes_with_relaxed_config(self):
        """price_min=0.0001 时，0.0005 的极端价格应通过。"""
        now = now_utc()
        market = self._make_market(
            start_date=to_utc_iso(now - timedelta(hours=1)),
            end_date=to_utc_iso(now + timedelta(hours=2)),
            price_a=0.0005, price_b=0.9995,
        )
        result = self.svc._screen_market({"slug": "test"}, "lol", market)
        assert result is not None
        assert result.price_a == 0.0005

    def test_no_end_date_passes(self):
        """无 end_date 时不应被过滤（仅检查 start_date）。"""
        now = now_utc()
        market = self._make_market(
            start_date=to_utc_iso(now - timedelta(hours=1)),
            end_date=None,
        )
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is not None

    def test_no_start_date_passes(self):
        """无 start_date 时不应被过滤（仅检查 end_date）。"""
        now = now_utc()
        market = self._make_market(
            start_date=None,
            end_date=to_utc_iso(now + timedelta(hours=2)),
        )
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is not None

    def test_no_time_info_passes(self):
        """既无 start_date 也无 end_date 时应通过（无法判断时间窗口）。"""
        market = self._make_market(start_date=None, end_date=None)
        assert self.svc._screen_market({"slug": "test"}, "lol", market) is not None


class TestDiscover:
    def test_no_events_returns_empty(self):
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = []
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={"games": ["lol"], "tag_slug": "esports"},
        )
        result = svc.discover()
        assert isinstance(result, DiscoveryResult)
        assert result.new_matches == []
        assert result.has_changes is False

    def test_with_matching_event(self):
        future_end = to_utc_iso(now_utc() + timedelta(hours=2))
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = [
            {
                "slug": "t1-vs-geng",
                "title": "T1 vs Gen.G - LoL LCK",
                "tags": [{"label": "LoL"}],
                "markets": [
                    {
                        "conditionId": "0x1",
                        "outcomes": ["T1", "Gen.G"],
                        "outcomePrices": ["0.55", "0.45"],
                        "clobTokenIds": ["ta", "tb"],
                        "question": "Will T1 defeat Gen.G?",
                        "endDate": future_end,
                    }
                ],
            }
        ]
        gamma.parse_match_markets.return_value = [
            MatchMarket(
                condition_id="0x1", clob_token_ids=["ta", "tb"],
                team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
                end_date=future_end, question="Will T1 defeat Gen.G?",
                slug="t1-vs-geng",
            )
        ]
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["lol"], "tag_slug": "esports",
                "price_min": 0.05, "price_max": 0.95,
                "live_window_hours": 48,
                "max_match_duration_hours": 48,
            },
        )
        result = svc.discover()
        assert len(result.new_matches) == 1
        assert result.new_matches[0].team_a == "T1"

    def test_game_not_in_config_skipped(self):
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = [
            {"slug": "test", "title": "CS2 Match", "tags": [{"label": "CS2"}]}
        ]
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={"games": ["lol"], "tag_slug": "esports"},  # 只关注 lol
        )
        result = svc.discover()
        assert len(result.new_matches) == 0
        assert result.skipped >= 1

    def test_no_markets_in_event(self):
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = [
            {"slug": "test", "title": "LoL Match", "tags": [{"label": "LoL"}]}
        ]
        gamma.parse_match_markets.return_value = []
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={"games": ["lol"], "tag_slug": "esports"},
        )
        result = svc.discover()
        assert len(result.new_matches) == 0

    def test_event_parse_exception_counted(self):
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = [
            {"slug": "test", "title": "LoL Match", "tags": [{"label": "LoL"}]}
        ]
        gamma.parse_match_markets.side_effect = RuntimeError("boom")
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={"games": ["lol"], "tag_slug": "esports"},
        )
        result = svc.discover()
        assert result.errors == 1


class TestDiscoverMultiTag:
    """多 tag 查询测试。"""

    def test_multiple_tag_slugs_aggregated(self):
        """tag_slugs 列表中的所有 tag 都应被查询并聚合。"""
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        # 每个 tag 返回不同的赛事（fetch_events 返回列表）
        def fetch_side_effect(tag_slug="", **kwargs):
            if tag_slug == "counter-strike-2":
                return [{"id": "1", "slug": "cs2-match", "title": "CS2 Match",
                         "tags": [{"label": "CS2"}]}]
            if tag_slug == "dota-2":
                return [{"id": "2", "slug": "dota-match", "title": "Dota Match",
                         "tags": [{"label": "Dota 2"}]}]
            return []

        gamma.fetch_events.side_effect = fetch_side_effect
        gamma.parse_match_markets.return_value = [
            MatchMarket(
                condition_id="0x1", clob_token_ids=["ta", "tb"],
                team_a="A", team_b="B", price_a=0.55, price_b=0.45,
                end_date=to_utc_iso(now_utc() + timedelta(hours=2)),
                slug="test",
            )
        ]
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["cs2", "dota2"],
                "tag_slugs": ["counter-strike-2", "dota-2"],
                "price_min": 0.05, "price_max": 0.95,
                "live_window_hours": 48,
                "max_match_duration_hours": 48,
            },
        )
        result = svc.discover()
        # 两个 tag 都被查询
        assert gamma.fetch_events.call_count == 2
        # 两个赛事都被解析
        assert len(result.new_matches) == 2

    def test_duplicate_events_across_tags_deduplicated(self):
        """同一 event 在多个 tag 下出现时，应按 event id 去重。"""
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        # 两个 tag 返回同一赛事（fetch_events 返回列表）
        shared_event = {
            "id": "shared-1",
            "slug": "cs2-shared",
            "title": "CS2 Match",
            "tags": [{"label": "CS2"}],
        }
        gamma.fetch_events.side_effect = [[shared_event], [shared_event]]
        gamma.parse_match_markets.return_value = [
            MatchMarket(
                condition_id="0x1", clob_token_ids=["ta", "tb"],
                team_a="A", team_b="B", price_a=0.55, price_b=0.45,
                end_date=to_utc_iso(now_utc() + timedelta(hours=2)),
                slug="test",
            )
        ]
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["cs2"],
                "tag_slugs": ["counter-strike-2", "esports"],
                "price_min": 0.05, "price_max": 0.95,
                "live_window_hours": 48,
                "max_match_duration_hours": 48,
            },
        )
        result = svc.discover()
        # 只处理一次（去重）
        assert len(result.new_matches) == 1

    def test_tag_slugs_fallback_to_single_tag_slug(self):
        """未配置 tag_slugs 时，应回退到 tag_slug 单 tag 查询。"""
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = []
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["lol"],
                "tag_slug": "esports",  # 未配置 tag_slugs
                "price_min": 0.05, "price_max": 0.95,
            },
        )
        svc.discover()
        gamma.fetch_events.assert_called_once()
        call_kwargs = gamma.fetch_events.call_args.kwargs
        assert call_kwargs.get("tag_slug") == "esports"

    def test_tag_slugs_fallback_to_default_esports(self):
        """既未配置 tag_slugs 也未配置 tag_slug 时，应回退到默认 esports。"""
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = []
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["lol"],
                "price_min": 0.05, "price_max": 0.95,
            },
        )
        svc.discover()
        gamma.fetch_events.assert_called_once()
        call_kwargs = gamma.fetch_events.call_args.kwargs
        assert call_kwargs.get("tag_slug") == "esports"

    def test_tag_slugs_as_non_list_coerced_to_list(self):
        """tag_slugs 为非列表（如字符串）时，应被强制转换为单元素列表。"""
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = []
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["lol"],
                "tag_slugs": "counter-strike-2",  # 字符串而非列表
                "price_min": 0.05, "price_max": 0.95,
            },
        )
        svc.discover()
        gamma.fetch_events.assert_called_once()
        call_kwargs = gamma.fetch_events.call_args.kwargs
        assert call_kwargs.get("tag_slug") == "counter-strike-2"

    def test_time_filter_params_passed_to_api(self):
        """end_date_min 和 start_date_max 应传递给 Gamma API。"""
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.return_value = []
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["lol"],
                "tag_slugs": ["esports"],
                "live_window_hours": 24,
                "price_min": 0.05, "price_max": 0.95,
            },
        )
        svc.discover()
        gamma.fetch_events.assert_called_once()
        call_kwargs = gamma.fetch_events.call_args.kwargs
        # end_date_min 应为当前时间附近（排除已结束）
        assert "end_date_min" in call_kwargs
        assert call_kwargs["end_date_min"]  # 非空
        # start_date_max 应为 now + 24h 附近
        assert "start_date_max" in call_kwargs
        assert call_kwargs["start_date_max"]  # 非空

    def test_empty_tag_returns_empty_result(self):
        """某个 tag 返回空列表时，不应影响其他 tag 的处理。"""
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 强制 fallback 模式
        gamma.fetch_events.side_effect = [
            [],  # 第一个 tag 空
            [{"id": "2", "slug": "dota-match", "title": "Dota Match",
              "tags": [{"label": "Dota 2"}]}],  # 第二个 tag 有数据
        ]
        gamma.parse_match_markets.return_value = [
            MatchMarket(
                condition_id="0x1", clob_token_ids=["ta", "tb"],
                team_a="A", team_b="B", price_a=0.55, price_b=0.45,
                end_date=to_utc_iso(now_utc() + timedelta(hours=2)),
                slug="test",
            )
        ]
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["dota2"],
                "tag_slugs": ["counter-strike-2", "dota-2"],
                "price_min": 0.05, "price_max": 0.95,
                "live_window_hours": 48,
                "max_match_duration_hours": 48,
            },
        )
        result = svc.discover()
        assert len(result.new_matches) == 1


class TestDiscoverPreciseMode:
    """精确模式测试（以 Polymarket 网页 isLive 标记为准）。"""

    def test_precise_mode_uses_live_slugs(self):
        """精确模式：fetch_live_event_slugs 返回 slug 后，用 fetch_event 获取详情。"""
        gamma = MagicMock()
        future_end = to_utc_iso(now_utc() + timedelta(hours=2))
        gamma.fetch_live_event_slugs.return_value = ["cs2-leo2-ast-2026-06-29"]
        gamma.fetch_event.return_value = {
            "slug": "cs2-leo2-ast-2026-06-29",
            "title": "Leo Team vs ASTRAL",
            "tags": [{"label": "CS2"}],
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["Leo Team", "ASTRAL"],
                    "outcomePrices": ["0.3", "0.7"],
                    "clobTokenIds": ["ta", "tb"],
                    "question": "Will Leo Team defeat ASTRAL?",
                    "groupItemTitle": "Match Winner",
                    "endDate": future_end,
                }
            ],
        }
        gamma.parse_match_markets.return_value = [
            MatchMarket(
                condition_id="0x1", clob_token_ids=["ta", "tb"],
                team_a="Leo Team", team_b="ASTRAL", price_a=0.3, price_b=0.7,
                end_date=future_end, question="Will Leo Team defeat ASTRAL?",
                slug="cs2-leo2-ast-2026-06-29",
            )
        ]
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["cs2"],
                "price_min": 0.01, "price_max": 0.99,
                "live_window_hours": 48,
                "max_match_duration_hours": 48,
            },
        )
        result = svc.discover()
        # 精确模式：fetch_live_event_slugs 被调用
        gamma.fetch_live_event_slugs.assert_called_once()
        # fetch_event 被调用（不是 fetch_events）
        gamma.fetch_event.assert_called_once_with("cs2-leo2-ast-2026-06-29")
        # 发现 1 场比赛
        assert len(result.new_matches) == 1
        assert result.new_matches[0].team_a == "Leo Team"
        assert "mode=precise" in result.summary

    def test_precise_mode_skips_non_target_games(self):
        """精确模式：Valorant/R6 等 isLive 比赛不在 games 配置中应被跳过。"""
        gamma = MagicMock()
        future_end = to_utc_iso(now_utc() + timedelta(hours=2))
        gamma.fetch_live_event_slugs.return_value = [
            "val-jl-bar-2026-06-29",  # Valorant
            "cs2-leo2-ast-2026-06-29",  # CS2
        ]
        # Valorant event
        gamma.fetch_event.side_effect = [
            {
                "slug": "val-jl-bar-2026-06-29",
                "title": "Joblife vs Barça",
                "tags": [{"label": "Valorant"}],
                "markets": [],
            },
            {
                "slug": "cs2-leo2-ast-2026-06-29",
                "title": "Leo Team vs ASTRAL",
                "tags": [{"label": "CS2"}],
                "markets": [
                    {
                        "conditionId": "0x1",
                        "outcomes": ["Leo Team", "ASTRAL"],
                        "outcomePrices": ["0.3", "0.7"],
                        "clobTokenIds": ["ta", "tb"],
                        "question": "Will Leo Team defeat ASTRAL?",
                        "groupItemTitle": "Match Winner",
                        "endDate": future_end,
                    }
                ],
            },
        ]
        gamma.parse_match_markets.return_value = [
            MatchMarket(
                condition_id="0x1", clob_token_ids=["ta", "tb"],
                team_a="Leo Team", team_b="ASTRAL", price_a=0.3, price_b=0.7,
                end_date=future_end, question="Will Leo Team defeat ASTRAL?",
                slug="cs2-leo2-ast-2026-06-29",
            )
        ]
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["cs2", "dota2", "lol"],  # 不含 valorant
                "price_min": 0.01, "price_max": 0.99,
                "live_window_hours": 48,
                "max_match_duration_hours": 48,
            },
        )
        result = svc.discover()
        # Valorant 被跳过，只发现 CS2 1 场
        assert len(result.new_matches) == 1
        assert result.skipped >= 1  # Valorant 被跳过

    def test_fallback_mode_when_page_fetch_fails(self):
        """网页抓取失败时，fallback 到 Gamma API volume24hr 查询。"""
        gamma = MagicMock()
        gamma.fetch_live_event_slugs.return_value = []  # 网页抓取失败
        future_end = to_utc_iso(now_utc() + timedelta(hours=2))
        gamma.fetch_events.return_value = [
            {
                "slug": "t1-vs-geng",
                "title": "T1 vs Gen.G - LoL LCK",
                "tags": [{"label": "LoL"}],
                "markets": [
                    {
                        "conditionId": "0x1",
                        "outcomes": ["T1", "Gen.G"],
                        "outcomePrices": ["0.55", "0.45"],
                        "clobTokenIds": ["ta", "tb"],
                        "question": "Will T1 defeat Gen.G?",
                        "endDate": future_end,
                    }
                ],
            }
        ]
        gamma.parse_match_markets.return_value = [
            MatchMarket(
                condition_id="0x1", clob_token_ids=["ta", "tb"],
                team_a="T1", team_b="Gen.G", price_a=0.55, price_b=0.45,
                end_date=future_end, question="Will T1 defeat Gen.G?",
                slug="t1-vs-geng",
            )
        ]
        svc = MarketDiscoveryService(
            gamma_client=gamma, clob_client=None,
            config={
                "games": ["lol"], "tag_slug": "esports",
                "price_min": 0.05, "price_max": 0.95,
                "live_window_hours": 48,
                "max_match_duration_hours": 48,
            },
        )
        result = svc.discover()
        # fallback 模式：fetch_events 被调用
        gamma.fetch_events.assert_called_once()
        assert len(result.new_matches) == 1
        assert "mode=fallback" in result.summary


class TestPersistDiscoveredMatches:
    def test_persist(self, storage):
        matches = [
            DiscoveredMatch(
                match_id="0x1", slug="t1-vs-geng", game="lol", league="LCK",
                team_a="T1", team_b="Gen.G", condition_id="0x1",
                token_id_a="ta", token_id_b="tb",
                price_a=0.55, price_b=0.45,
                start_time="2026-06-29T10:00:00Z",
                end_time="2026-06-29T13:00:00Z",
            )
        ]
        count = persist_discovered_matches(storage, matches)
        assert count == 1
        m = storage.get_match("0x1")
        assert m is not None
        assert m["team_a"] == "T1"

    def test_persist_empty(self, storage):
        assert persist_discovered_matches(storage, []) == 0

    def test_persist_storage_failure(self, storage):
        bad_storage = MagicMock()
        bad_storage.upsert_match.side_effect = RuntimeError("boom")
        matches = [
            DiscoveredMatch(
                match_id="0x1", slug="s", game="lol", league=None,
                team_a="A", team_b="B", condition_id="0x1",
                token_id_a=None, token_id_b=None,
                price_a=0.5, price_b=0.5, start_time=None, end_time=None,
            )
        ]
        # 不应抛异常
        count = persist_discovered_matches(bad_storage, matches)
        assert count == 0


class TestShouldRunDiscovery:
    def test_no_history_runs(self, storage):
        assert should_run_discovery(storage, interval_hours=6) is True

    def test_recent_run_skipped(self, storage):
        storage.mark_task_run(DISCOVERY_TASK_NAME, "test")
        assert should_run_discovery(storage, interval_hours=6) is False

    def test_old_run_runs(self, storage):
        # 手动写入一个很旧的运行记录
        old = to_utc_iso(now_utc() - timedelta(hours=12))
        with storage._connect() as conn:
            conn.execute(
                "INSERT INTO task_runs (task_name, run_at, result_summary) VALUES (?, ?, ?)",
                (DISCOVERY_TASK_NAME, old, "old"),
            )
            conn.commit()
        assert should_run_discovery(storage, interval_hours=6) is True

    def test_storage_exception_returns_true(self):
        bad_storage = MagicMock()
        bad_storage.get_last_task_run.side_effect = RuntimeError("boom")
        assert should_run_discovery(bad_storage, interval_hours=6) is True


class TestMarkDiscoveryRun:
    def test_mark(self, storage):
        assert mark_discovery_run(storage, summary="test") is True
        assert storage.get_last_task_run(DISCOVERY_TASK_NAME) is not None

    def test_exception_returns_false(self):
        bad_storage = MagicMock()
        bad_storage.mark_task_run.side_effect = RuntimeError("boom")
        assert mark_discovery_run(bad_storage) is False


class TestDiscoveryResult:
    def test_has_changes_false_when_empty(self):
        r = DiscoveryResult()
        assert r.has_changes is False

    def test_has_changes_true_with_matches(self):
        r = DiscoveryResult(new_matches=[
            DiscoveredMatch(
                match_id="x", slug="s", game="lol", league=None,
                team_a="A", team_b="B", condition_id="x",
                token_id_a=None, token_id_b=None,
                price_a=0.5, price_b=0.5, start_time=None, end_time=None,
            )
        ])
        assert r.has_changes is True
