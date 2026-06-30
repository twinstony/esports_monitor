"""api/clob.py 测试。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from esports_monitor.api.clob import (
    ClobClient,
    parse_asks,
    parse_bids,
    parse_orderbook_summary,
    simulate_walk_asks,
    simulate_walk_bids,
)


class TestParseBids:
    def test_normal(self):
        ob = {"bids": [{"price": "0.5", "size": "100"}, {"price": "0.55", "size": "50"}]}
        bids = parse_bids(ob)
        # 应降序：买一在前
        assert bids == [(0.55, 50), (0.5, 100)]

    def test_empty(self):
        assert parse_bids({}) == []
        assert parse_bids({"bids": []}) == []
        assert parse_bids(None) == []  # type: ignore[arg-type]

    def test_invalid_entries_filtered(self):
        ob = {
            "bids": [
                {"price": "0", "size": "100"},  # price=0 过滤
                {"price": "0.5", "size": "0"},  # size=0 过滤
                {"price": "invalid", "size": "100"},  # 解析失败过滤
                "not-a-dict",  # 非 dict 过滤
                {"price": "0.6", "size": "80"},
            ]
        }
        bids = parse_bids(ob)
        assert bids == [(0.6, 80.0)]


class TestParseAsks:
    def test_normal(self):
        ob = {"asks": [{"price": "0.55", "size": "80"}, {"price": "0.5", "size": "100"}]}
        asks = parse_asks(ob)
        # 应升序：卖一在前
        assert asks == [(0.5, 100), (0.55, 80)]

    def test_empty(self):
        assert parse_asks({}) == []


class TestParseOrderbookSummary:
    def test_full_orderbook(self):
        ob = {
            "bids": [{"price": "0.55", "size": "100"}, {"price": "0.54", "size": "50"}],
            "asks": [{"price": "0.56", "size": "80"}, {"price": "0.57", "size": "60"}],
        }
        s = parse_orderbook_summary(ob)
        assert s["best_bid"] == 0.55
        assert s["best_ask"] == 0.56
        assert s["spread"] == pytest.approx(0.01)
        assert s["mid_price"] == pytest.approx(0.555)
        assert s["total_bid_depth"] == 150.0
        assert s["total_ask_depth"] == 140.0
        assert s["bid_levels"] == 2
        assert s["ask_levels"] == 2

    def test_empty_orderbook(self):
        s = parse_orderbook_summary({})
        assert s["best_bid"] == 0.0
        assert s["best_ask"] == 0.0
        assert s["spread"] == 0.0
        assert s["mid_price"] == 0.0

    def test_only_bids(self):
        s = parse_orderbook_summary({"bids": [{"price": "0.5", "size": "100"}]})
        assert s["best_bid"] == 0.5
        assert s["best_ask"] == 0.0
        assert s["spread"] == 0.0
        assert s["mid_price"] == 0.5  # 仅 bid 时用 bid 作 mid


class TestSimulateWalkBids:
    def test_full_fill(self):
        bids = [(0.55, 100), (0.50, 100)]
        vwap, filled, fully, slip = simulate_walk_bids(bids, 50)
        assert filled == 50
        assert fully is True
        assert vwap == 0.55
        assert slip == 0.0  # 全在买一成交，无滑点

    def test_partial_fill(self):
        bids = [(0.55, 30), (0.50, 100)]
        vwap, filled, fully, slip = simulate_walk_bids(bids, 100)
        assert filled == 100
        assert fully is True
        # 30 @ 0.55 + 70 @ 0.50 = 16.5 + 35 = 51.5; vwap = 0.515
        assert abs(vwap - 0.515) < 1e-6
        # 滑点：vwap/best_bid - 1 = 0.515/0.55 - 1 < 0
        assert slip < 0

    def test_insufficient_depth(self):
        bids = [(0.55, 30)]
        vwap, filled, fully, slip = simulate_walk_bids(bids, 100)
        assert filled == 30
        assert fully is False
        assert vwap == 0.55

    def test_empty_bids(self):
        vwap, filled, fully, slip = simulate_walk_bids([], 100)
        assert vwap == 0.0
        assert filled == 0.0
        assert fully is False

    def test_zero_target(self):
        vwap, filled, fully, slip = simulate_walk_bids([(0.5, 100)], 0)
        assert vwap == 0.0


class TestSimulateWalkAsks:
    def test_full_fill(self):
        asks = [(0.55, 100), (0.60, 100)]
        vwap, filled, fully, slip = simulate_walk_asks(asks, 50)
        assert filled == 50
        assert fully is True
        assert vwap == 0.55
        assert slip == 0.0

    def test_partial_fill_with_slippage(self):
        asks = [(0.55, 30), (0.60, 100)]
        vwap, filled, fully, slip = simulate_walk_asks(asks, 100)
        # 30 @ 0.55 + 70 @ 0.60 = 16.5 + 42 = 58.5; vwap = 0.585
        assert abs(vwap - 0.585) < 1e-6
        # 滑点：vwap/best_ask - 1 = 0.585/0.55 - 1 > 0
        assert slip > 0


class TestClobClientTokenIdCache:
    def test_get_token_id_invalid_outcome(self):
        client = ClobClient(storage=None, timeout=1)
        assert client.get_token_id("cid", "Maybe") is None
        assert client.get_token_id("cid", "") is None
        assert client.get_token_id("", "Yes") is None

    def test_get_token_id_from_cache(self):
        storage = MagicMock()
        storage.get_cached_token_id.return_value = "cached_tok"
        client = ClobClient(storage=storage, timeout=1)
        assert client.get_token_id("cid", "Yes") == "cached_tok"
        storage.get_cached_token_id.assert_called_once_with("cid", "Yes")
        # 不应再调 API
        storage.set_cached_token_id.assert_not_called()

    def test_get_token_id_fetch_from_api(self):
        storage = MagicMock()
        storage.get_cached_token_id.return_value = None
        client = ClobClient(storage=storage, timeout=1)
        with patch.object(client, "_fetch_token_id_from_api", return_value="new_tok") as mock_fetch:
            assert client.get_token_id("cid", "Yes") == "new_tok"
            mock_fetch.assert_called_once_with("cid", "Yes")
            storage.set_cached_token_id.assert_called_once_with("cid", "Yes", "new_tok")

    def test_get_token_id_fetch_fails(self):
        storage = MagicMock()
        storage.get_cached_token_id.return_value = None
        client = ClobClient(storage=storage, timeout=1)
        with patch.object(client, "_fetch_token_id_from_api", return_value=None):
            assert client.get_token_id("cid", "Yes") is None
            storage.set_cached_token_id.assert_not_called()

    def test_cache_read_exception_returns_none(self):
        storage = MagicMock()
        storage.get_cached_token_id.side_effect = RuntimeError("boom")
        client = ClobClient(storage=storage, timeout=1)
        with patch.object(client, "_fetch_token_id_from_api", return_value="tok"):
            # 异常被吞，继续从 API 获取
            assert client.get_token_id("cid", "Yes") == "tok"


class TestClobClientGetOrderbook:
    def test_success(self):
        client = ClobClient(storage=None, timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"bids": [], "asks": []}
        with patch.object(client._session, "get", return_value=mock_resp):
            ob = client.get_orderbook("tok1")
            assert ob == {"bids": [], "asks": []}

    def test_404_returns_none_no_retry(self):
        client = ClobClient(storage=None, timeout=1, max_retries=3)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            assert client.get_orderbook("tok1") is None
            # 404 是终止性，只调用一次
            assert mock_get.call_count == 1

    def test_500_retries(self):
        client = ClobClient(storage=None, timeout=1, max_retries=2)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            assert client.get_orderbook("tok1") is None
            # max_retries + 1 次
            assert mock_get.call_count == 3

    def test_timeout_retries(self):
        client = ClobClient(storage=None, timeout=1, max_retries=1)
        with patch.object(
            client._session, "get",
            side_effect=requests.exceptions.Timeout("timeout"),
        ) as mock_get:
            assert client.get_orderbook("tok1") is None
            assert mock_get.call_count == 2

    def test_empty_token_id(self):
        client = ClobClient(storage=None, timeout=1)
        assert client.get_orderbook("") is None


class TestGetTokenIdAndOrderbook:
    def test_no_token_id(self):
        client = ClobClient(storage=None, timeout=1)
        with patch.object(client, "get_token_id", return_value=None):
            assert client.get_token_id_and_orderbook("cid", "Yes") == (None, None)

    def test_success_first_try(self):
        client = ClobClient(storage=None, timeout=1)
        with patch.object(client, "get_token_id", return_value="tok1"), \
             patch.object(client, "get_orderbook", return_value={"bids": []}):
            tid, ob = client.get_token_id_and_orderbook("cid", "Yes")
            assert tid == "tok1"
            assert ob == {"bids": []}

    def test_404_triggers_cache_invalidation_and_retry(self):
        client = ClobClient(storage=MagicMock(), timeout=1)
        with patch.object(client, "get_token_id", return_value="old_tok"), \
             patch.object(client, "get_orderbook", side_effect=[None, {"bids": []}]), \
             patch.object(client, "_invalidate_cached_token_id") as mock_inval, \
             patch.object(
                 client, "_fetch_token_id_from_api", return_value="new_tok"
             ) as mock_fetch, \
             patch.object(client, "_cache_token_id") as mock_cache:
            tid, ob = client.get_token_id_and_orderbook("cid", "Yes")
            assert tid == "new_tok"
            assert ob == {"bids": []}
            mock_inval.assert_called_once_with("cid", "Yes")
            mock_fetch.assert_called_once_with("cid", "Yes")
            mock_cache.assert_called_once_with("cid", "Yes", "new_tok")

    def test_404_same_token_id_no_retry(self):
        client = ClobClient(storage=MagicMock(), timeout=1)
        with patch.object(client, "get_token_id", return_value="same_tok"), \
             patch.object(client, "get_orderbook", return_value=None), \
             patch.object(client, "_invalidate_cached_token_id"), \
             patch.object(
                 client, "_fetch_token_id_from_api", return_value="same_tok"
             ):
            tid, ob = client.get_token_id_and_orderbook("cid", "Yes")
            assert tid == "same_tok"
            assert ob is None


class TestSaveOrderbookSnapshot:
    def test_no_storage(self):
        client = ClobClient(storage=None, timeout=1)
        assert client.save_orderbook_snapshot("m1", "team_a", "tok", {}, "now") is False

    def test_success(self):
        storage = MagicMock()
        storage.insert_orderbook_snapshot.return_value = True
        client = ClobClient(storage=storage, timeout=1)
        ob = {"bids": [{"price": "0.5", "size": "100"}], "asks": [{"price": "0.55", "size": "80"}]}
        ok = client.save_orderbook_snapshot("m1", "team_a", "tok", ob, "2026-01-01T00:00:00Z")
        assert ok is True
        storage.insert_orderbook_snapshot.assert_called_once()
        call_kwargs = storage.insert_orderbook_snapshot.call_args
        assert call_kwargs.kwargs["best_bid"] == 0.5
        assert call_kwargs.kwargs["best_ask"] == 0.55

    def test_storage_exception(self):
        storage = MagicMock()
        storage.insert_orderbook_snapshot.side_effect = RuntimeError("boom")
        client = ClobClient(storage=storage, timeout=1)
        ok = client.save_orderbook_snapshot("m1", "team_a", "tok", {}, "now")
        assert ok is False


class TestParseTokenId:
    def test_match_by_outcome_label(self):
        market = {
            "clobTokenIds": ["tok_yes", "tok_no"],
            "outcomes": ["Yes", "No"],
        }
        assert ClobClient._parse_token_id(market, "Yes") == "tok_yes"
        assert ClobClient._parse_token_id(market, "No") == "tok_no"

    def test_json_string_fields(self):
        market = {
            "clobTokenIds": '["tok_yes","tok_no"]',
            "outcomes": '["Yes","No"]',
        }
        assert ClobClient._parse_token_id(market, "Yes") == "tok_yes"

    def test_fallback_positional(self):
        market = {
            "clobTokenIds": ["tok_a", "tok_b"],
            "outcomes": [],  # 解析失败
        }
        assert ClobClient._parse_token_id(market, "Yes") == "tok_a"
        assert ClobClient._parse_token_id(market, "No") == "tok_b"

    def test_insufficient_tokens(self):
        assert ClobClient._parse_token_id({"clobTokenIds": ["only_one"]}, "Yes") is None

    def test_invalid_json(self):
        assert ClobClient._parse_token_id(
            {"clobTokenIds": "not json", "outcomes": []}, "Yes"
        ) is None


class TestFetchTokenIdFromApi:
    def test_success(self):
        client = ClobClient(storage=None, timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{
            "clobTokenIds": ["tok_yes", "tok_no"],
            "outcomes": ["Yes", "No"],
        }]
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client._fetch_token_id_from_api("cid", "Yes") == "tok_yes"

    def test_empty_response(self):
        client = ClobClient(storage=None, timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client._fetch_token_id_from_api("cid", "Yes") is None

    def test_json_decode_error(self):
        client = ClobClient(storage=None, timeout=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = json.JSONDecodeError("err", "doc", 0)
        with patch.object(client._session, "get", return_value=mock_resp):
            assert client._fetch_token_id_from_api("cid", "Yes") is None
