"""api/polymarket.py 测试。"""
from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import requests

from esports_monitor.api.polymarket import MatchMarket, PolymarketClient


class TestParseTeamNames:
    def setup_method(self):
        self.client = PolymarketClient(timeout=1)

    def test_will_defeat_format(self):
        a, b = self.client._parse_team_names("Will T1 defeat Gen.G?")
        assert a == "T1"
        assert b == "Gen.G"

    def test_vs_format(self):
        a, b = self.client._parse_team_names("T1 vs Gen.G")
        assert a == "T1"
        assert b == "Gen.G"

    def test_vs_with_dot(self):
        a, b = self.client._parse_team_names("T1 vs. Gen.G")
        assert a == "T1"
        assert b == "Gen.G"

    def test_empty_question(self):
        assert self.client._parse_team_names("") == ("", "")
        assert self.client._parse_team_names(None) == ("", "")  # type: ignore[arg-type]

    def test_no_match(self):
        assert self.client._parse_team_names("random text") == ("", "")


class TestParseJsonField:
    def test_list_input(self):
        assert PolymarketClient._parse_json_field(["a", "b"]) == ["a", "b"]

    def test_json_string_input(self):
        assert PolymarketClient._parse_json_field('["a","b"]') == ["a", "b"]

    def test_invalid_json_string(self):
        assert PolymarketClient._parse_json_field("not json") == []

    def test_other_type(self):
        assert PolymarketClient._parse_json_field(42) == []
        assert PolymarketClient._parse_json_field(None) == []


class TestParseMatchMarkets:
    def setup_method(self):
        self.client = PolymarketClient(timeout=1)

    def test_valid_binary_market(self):
        event = {
            "slug": "t1-vs-geng",
            "markets": [
                {
                    "conditionId": "0xabc",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.55", "0.45"],
                    "clobTokenIds": ["tok1", "tok2"],
                    "question": "Will T1 defeat Gen.G?",
                    "endDate": "2026-06-29T13:00:00Z",
                }
            ],
        }
        markets = self.client.parse_match_markets(event)
        assert len(markets) == 1
        m = markets[0]
        assert m.condition_id == "0xabc"
        assert m.team_a == "T1"
        assert m.team_b == "Gen.G"
        assert m.price_a == 0.55
        assert m.price_b == 0.45
        assert m.clob_token_ids == ["tok1", "tok2"]
        assert m.slug == "t1-vs-geng"

    def test_json_string_outcomes_and_prices(self):
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0xabc",
                    "outcomes": '["T1","Gen.G"]',
                    "outcomePrices": '["0.55","0.45"]',
                    "clobTokenIds": '["tok1","tok2"]',
                    "question": "T1 vs Gen.G",
                }
            ],
        }
        markets = self.client.parse_match_markets(event)
        assert len(markets) == 1
        assert markets[0].price_a == 0.55

    def test_skip_non_binary_markets(self):
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["A", "B", "C"],  # 三元，跳过
                    "outcomePrices": ["0.3", "0.3", "0.4"],
                    "question": "A vs B vs C",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_skip_no_condition_id(self):
        event = {
            "slug": "test",
            "markets": [
                {
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "T1 vs Gen.G",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_skip_zero_prices(self):
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0", "0"],
                    "question": "T1 vs Gen.G",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_skip_unparseable_question(self):
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "random question",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_non_dict_event(self):
        assert self.client.parse_match_markets(None) == []  # type: ignore[arg-type]
        assert self.client.parse_match_markets("string") == []  # type: ignore[arg-type]

    def test_no_markets_field(self):
        assert self.client.parse_match_markets({"slug": "test"}) == []

    def test_invalid_price_skipped(self):
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["invalid", "0.5"],
                    "question": "T1 vs Gen.G",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_submarket_map_winner_filtered_by_group_item_title(self):
        """groupItemTitle='Map 1 Winner' 的子市场应被过滤。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "T1 vs Gen.G - Map 1 Winner",
                    "groupItemTitle": "Map 1 Winner",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_submarket_map_handicap_filtered_by_group_item_title(self):
        """groupItemTitle='Map Handicap' 的子市场应被过滤。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "Map Handicap: T1 (-1.5) vs Gen.G (+1.5)",
                    "groupItemTitle": "Map Handicap: T1 (-1.5) vs Gen.G (+1.5)",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_submarket_ou_games_filtered_by_group_item_title(self):
        """groupItemTitle='O/U 2.5 Games' 的子市场应被过滤。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["Over", "Under"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "Games Total: O/U 2.5",
                    "groupItemTitle": "O/U 2.5 Games",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_main_market_match_winner_kept(self):
        """groupItemTitle='Match Winner' 的主市场应保留。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.55", "0.45"],
                    "question": "T1 vs Gen.G",
                    "groupItemTitle": "Match Winner",
                }
            ],
        }
        markets = self.client.parse_match_markets(event)
        assert len(markets) == 1
        assert markets[0].team_a == "T1"

    def test_main_market_series_winner_kept(self):
        """groupItemTitle='Series Winner' 的主市场应保留。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.55", "0.45"],
                    "question": "T1 vs Gen.G",
                    "groupItemTitle": "Series Winner",
                }
            ],
        }
        markets = self.client.parse_match_markets(event)
        assert len(markets) == 1

    def test_submarket_filtered_by_question_keyword_when_no_group_title(self):
        """无 groupItemTitle 时，question 含 'map 1' 等关键字的子市场应被过滤。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "T1 vs Gen.G - Map 1 Winner",
                    # 无 groupItemTitle
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_submarket_handicap_filtered_by_question_keyword(self):
        """无 groupItemTitle 时，question 含 'handicap' 的子市场应被过滤。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "Rounds Handicap: T1 vs Gen.G",
                }
            ],
        }
        assert self.client.parse_match_markets(event) == []

    def test_main_market_kept_when_no_group_title_and_clean_question(self):
        """无 groupItemTitle 且 question 不含子市场关键字时，应保留。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.55", "0.45"],
                    "question": "Will T1 defeat Gen.G?",
                }
            ],
        }
        markets = self.client.parse_match_markets(event)
        assert len(markets) == 1

    def test_mixed_main_and_sub_markets_only_main_kept(self):
        """同一 event 混合主市场和子市场时，只保留主市场。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.55", "0.45"],
                    "question": "T1 vs Gen.G",
                    "groupItemTitle": "Match Winner",
                },
                {
                    "conditionId": "0x2",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "T1 vs Gen.G - Map 1 Winner",
                    "groupItemTitle": "Map 1 Winner",
                },
                {
                    "conditionId": "0x3",
                    "outcomes": ["Over", "Under"],
                    "outcomePrices": ["0.5", "0.5"],
                    "question": "Games Total: O/U 2.5",
                    "groupItemTitle": "O/U 2.5 Games",
                },
            ],
        }
        markets = self.client.parse_match_markets(event)
        assert len(markets) == 1
        assert markets[0].condition_id == "0x1"

    def test_start_date_parsed_from_market(self):
        """market 的 startDate 应被解析到 MatchMarket.start_date。"""
        event = {
            "slug": "test",
            "markets": [
                {
                    "conditionId": "0x1",
                    "outcomes": ["T1", "Gen.G"],
                    "outcomePrices": ["0.55", "0.45"],
                    "question": "T1 vs Gen.G",
                    "groupItemTitle": "Match Winner",
                    "startDate": "2026-06-29T10:00:00Z",
                    "endDate": "2026-06-29T13:00:00Z",
                }
            ],
        }
        markets = self.client.parse_match_markets(event)
        assert len(markets) == 1
        assert markets[0].start_date == "2026-06-29T10:00:00Z"
        assert markets[0].end_date == "2026-06-29T13:00:00Z"


class TestFetchEvents:
    def test_success(self):
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"slug": "event1"}, {"slug": "event2"}]
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            events = client.fetch_events(tag_slug="esports")
            assert len(events) == 2
            mock_get.assert_called_once()

    def test_http_error(self):
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client.fetch_events(tag_slug="esports") == []

    def test_request_exception(self):
        client = PolymarketClient(timeout=1)
        with patch.object(
            client._session, "get",
            side_effect=requests.exceptions.Timeout("timeout"),
        ):
            assert client.fetch_events(tag_slug="esports") == []

    def test_json_decode_error(self):
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = json.JSONDecodeError("err", "doc", 0)
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client.fetch_events(tag_slug="esports") == []

    def test_non_list_response(self):
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"not": "a list"}
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client.fetch_events(tag_slug="esports") == []

    def test_end_date_min_passed_to_params(self):
        """end_date_min 参数应传递给 API。"""
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.fetch_events(tag_slug="esports", end_date_min="2026-06-29T00:00:00Z")
            call_kwargs = mock_get.call_args.kwargs.get("params", {})
            assert call_kwargs.get("end_date_min") == "2026-06-29T00:00:00Z"

    def test_start_date_max_passed_to_params(self):
        """start_date_max 参数应传递给 API。"""
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.fetch_events(
                tag_slug="esports",
                start_date_max="2026-07-01T00:00:00Z",
            )
            call_kwargs = mock_get.call_args.kwargs.get("params", {})
            assert call_kwargs.get("start_date_max") == "2026-07-01T00:00:00Z"

    def test_no_time_filter_params_omitted(self):
        """不传时间过滤参数时，params 中不应包含 end_date_min/start_date_max。"""
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.fetch_events(tag_slug="esports")
            call_kwargs = mock_get.call_args.kwargs.get("params", {})
            assert "end_date_min" not in call_kwargs
            assert "start_date_max" not in call_kwargs

    def test_tag_id_takes_precedence(self):
        """tag_id 和 tag_slug 同时传时，两个都应传给 API。"""
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.fetch_events(tag_id="123", tag_slug="esports")
            call_kwargs = mock_get.call_args.kwargs.get("params", {})
            assert call_kwargs.get("tag_id") == "123"
            assert call_kwargs.get("tag_slug") == "esports"

    def test_default_tag_slug_when_neither_provided(self):
        """tag_id 和 tag_slug 都不传时，默认用 tag_slug=esports。"""
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.fetch_events()
            call_kwargs = mock_get.call_args.kwargs.get("params", {})
            assert call_kwargs.get("tag_slug") == "esports"


class TestFetchLiveEventSlugs:
    """fetch_live_event_slugs 测试（从 Polymarket 网页解析 isLive:true）。"""

    def _make_html_with_live_slugs(self, slugs_with_live: list, slugs_without_live: list = None):
        """构造含 isLive:true 标记的 HTML。"""
        slugs_without_live = slugs_without_live or []
        # 构造 __next_f.push 数据
        parts = []
        for slug in slugs_with_live:
            parts.append(f'{{"slug":"{slug}","title":"Match {slug}","isLive":true}}')
        for slug in slugs_without_live:
            parts.append(f'{{"slug":"{slug}","title":"Match {slug}","isLive":false}}')
        combined = ",".join(parts)
        # 包装成 Next.js streaming 格式（需要 unicode_escape 解码）
        html = f'<html><script>self.__next_f.push([1, "{combined}"])</script></html>'
        return html

    def test_extracts_live_slugs(self):
        """应提取 isLive:true 的 slug。"""
        client = PolymarketClient(timeout=1)
        html = self._make_html_with_live_slugs([
            "cs2-leo2-ast-2026-06-29",
            "dota2-nemiga-hive1-2026-06-29",
        ], [
            "cs2-pre-masq-2026-06-29",  # isLive:false
        ])
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        with patch.object(client._session, "get", return_value=mock_resp):
            slugs = client.fetch_live_event_slugs()
        assert "cs2-leo2-ast-2026-06-29" in slugs
        assert "dota2-nemiga-hive1-2026-06-29" in slugs
        assert "cs2-pre-masq-2026-06-29" not in slugs

    def test_filters_non_match_slugs(self):
        """不含日期格式的 slug（如赛季冠军）应被过滤。"""
        client = PolymarketClient(timeout=1)
        html = self._make_html_with_live_slugs([
            "cs2-leo2-ast-2026-06-29",  # 比赛 slug（含日期）
            "lol-lck-2026-season-winner",  # 赛季冠军 slug（无日期后缀）
        ])
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        with patch.object(client._session, "get", return_value=mock_resp):
            slugs = client.fetch_live_event_slugs()
        assert "cs2-leo2-ast-2026-06-29" in slugs
        assert "lol-lck-2026-season-winner" not in slugs

    def test_http_error_returns_empty(self):
        """HTTP 错误返回空列表。"""
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client.fetch_live_event_slugs() == []

    def test_request_exception_returns_empty(self):
        """网络异常返回空列表。"""
        client = PolymarketClient(timeout=1)
        with patch.object(
            client._session, "get",
            side_effect=requests.exceptions.Timeout("timeout"),
        ):
            assert client.fetch_live_event_slugs() == []

    def test_no_push_data_returns_empty(self):
        """HTML 无 __next_f.push 数据返回空列表。"""
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>no data</body></html>"
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client.fetch_live_event_slugs() == []

    def test_deduplicates_slugs(self):
        """重复的 slug 应去重。"""
        client = PolymarketClient(timeout=1)
        # 同一 slug 出现两次（isLive:true）
        combined = (
            '{"slug":"cs2-leo2-ast-2026-06-29","isLive":true}'
            ',{"slug":"cs2-leo2-ast-2026-06-29","isLive":true}'
        )
        html = f'<html><script>self.__next_f.push([1, "{combined}"])</script></html>'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        with patch.object(client._session, "get", return_value=mock_resp):
            slugs = client.fetch_live_event_slugs()
        assert slugs.count("cs2-leo2-ast-2026-06-29") == 1


class TestFetchEvent:
    def test_success(self):
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"slug": "evt", "markets": []}]
        with patch.object(client._session, "get", return_value=mock_resp):
            event = client.fetch_event("evt")
            assert event is not None
            assert event["slug"] == "evt"

    def test_empty_slug(self):
        client = PolymarketClient(timeout=1)
        assert client.fetch_event("") is None

    def test_no_data(self):
        client = PolymarketClient(timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client.fetch_event("evt") is None
