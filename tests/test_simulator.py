"""morphology/simulator.py 测试。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from esports_monitor.morphology.models import MorphologyAlert, MorphologySignal, MorphologyTrade
from esports_monitor.morphology.simulator import MorphologySimulator
from esports_monitor.utils.time_utils import to_utc_iso, now_utc


class TestComputePnl:
    def test_winner_pnl(self):
        # buy_price=0.4, notional=100, 中奖：每份结算 1.0
        # pnl = 100 * (1/0.4 - 1) = 100 * 1.5 = 150
        pnl = MorphologySimulator.compute_pnl("T1", "T1", 0.4, 100.0)
        assert pnl == pytest.approx(150.0)

    def test_loser_pnl(self):
        pnl = MorphologySimulator.compute_pnl("T1", "Gen.G", 0.4, 100.0)
        assert pnl == -100.0

    def test_zero_buy_price(self):
        # 不应除零
        pnl = MorphologySimulator.compute_pnl("T1", "T1", 0.0, 100.0)
        assert pnl == 0.0


class TestOpenTrade:
    def test_disabled(self):
        sim = MorphologySimulator(
            repository=MagicMock(),
            config={"simulation": {"enabled": False, "notional_usd": 100, "use_depth": False}},
        )
        alert = MorphologyAlert(match_id="m1")
        assert sim.open_trade(alert) is None

    def test_no_strongest_signal(self):
        sim = MorphologySimulator(
            repository=MagicMock(),
            config={"simulation": {"enabled": True, "notional_usd": 100, "use_depth": False}},
        )
        alert = MorphologyAlert(match_id="m1", strongest_signal=None)
        assert sim.open_trade(alert) is None

    def test_price_out_of_range(self):
        sim = MorphologySimulator(
            repository=MagicMock(),
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": False},
                "buy_price_min": 0.05, "buy_price_max": 0.95,
            },
        )
        sig = MorphologySignal(
            signal_name="x", signal_label="x", window_label="mid",
            direction="A", buy_team="T1", buy_price=0.01,  # 太低
        )
        alert = MorphologyAlert(match_id="m1", strongest_signal=sig)
        assert sim.open_trade(alert) is None

    def test_successful_open(self):
        repo = MagicMock()
        repo.save_trade.return_value = 42
        sim = MorphologySimulator(
            repository=repo,
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": False},
                "buy_price_min": 0.05, "buy_price_max": 0.95,
            },
        )
        sig = MorphologySignal(
            signal_name="x", signal_label="x", window_label="mid",
            direction="A", buy_team="T1", buy_price=0.4, id=10,
        )
        alert = MorphologyAlert(match_id="m1", strongest_signal=sig)
        trade = sim.open_trade(alert)
        assert trade is not None
        assert trade.buy_price == 0.4
        assert trade.quantity == pytest.approx(250.0)  # 100 / 0.4
        assert trade.id == 42
        assert alert.trade_opened is True
        assert alert.trade_id == 42

    def test_save_trade_fails(self):
        repo = MagicMock()
        repo.save_trade.return_value = None
        sim = MorphologySimulator(
            repository=repo,
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": False},
                "buy_price_min": 0.05, "buy_price_max": 0.95,
            },
        )
        sig = MorphologySignal(
            signal_name="x", signal_label="x", window_label="mid",
            direction="A", buy_team="T1", buy_price=0.4,
        )
        alert = MorphologyAlert(match_id="m1", strongest_signal=sig)
        assert sim.open_trade(alert) is None

    def test_use_depth_disabled_no_vwap(self):
        # use_depth=False 时不应尝试获取 VWAP
        sim = MorphologySimulator(
            repository=MagicMock(),
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": False},
                "buy_price_min": 0.05, "buy_price_max": 0.95,
            },
        )
        sim.clob_client = MagicMock()
        sig = MorphologySignal(
            signal_name="x", signal_label="x", window_label="mid",
            direction="A", buy_team="T1", buy_price=0.4,
        )
        alert = MorphologyAlert(match_id="m1", strongest_signal=sig)
        sim.open_trade(alert)
        sim.clob_client.get_token_id_and_orderbook.assert_not_called()


class TestSettleTrade:
    def test_settle_winner(self):
        repo = MagicMock()
        repo.settle_trade.return_value = True
        sim = MorphologySimulator(repository=repo, config={})
        trade = MorphologyTrade(
            match_id="m1", signal_id=1, buy_team="T1", buy_price=0.4,
            quantity=250.0, notional_usd=100.0, opened_at="2026-01-01T00:00:00Z",
            id=5,
        )
        ok = sim.settle_trade(trade, winning_team="T1")
        assert ok is True
        assert trade.settled == 1
        assert trade.winning_team == "T1"
        assert trade.pnl_usd == pytest.approx(150.0)
        assert trade.settled_at is not None

    def test_settle_loser(self):
        repo = MagicMock()
        repo.settle_trade.return_value = True
        sim = MorphologySimulator(repository=repo, config={})
        trade = MorphologyTrade(
            match_id="m1", signal_id=1, buy_team="T1", buy_price=0.4,
            quantity=250.0, notional_usd=100.0, opened_at="2026-01-01T00:00:00Z",
            id=5,
        )
        ok = sim.settle_trade(trade, winning_team="Gen.G")
        assert ok is True
        assert trade.pnl_usd == -100.0

    def test_settle_storage_failure(self):
        repo = MagicMock()
        repo.settle_trade.return_value = False
        sim = MorphologySimulator(repository=repo, config={})
        trade = MorphologyTrade(
            match_id="m1", signal_id=1, buy_team="T1", buy_price=0.4,
            quantity=250.0, notional_usd=100.0, opened_at="2026-01-01T00:00:00Z",
            id=5,
        )
        ok = sim.settle_trade(trade, winning_team="T1")
        assert ok is False
        assert trade.settled == 0

    def test_settle_exception_returns_false(self):
        """settle_trade 内部抛异常时应被吞并返回 False。"""
        repo = MagicMock()
        repo.settle_trade.side_effect = RuntimeError("db error")
        sim = MorphologySimulator(repository=repo, config={})
        trade = MorphologyTrade(
            match_id="m1", signal_id=1, buy_team="T1", buy_price=0.4,
            quantity=250.0, notional_usd=100.0, opened_at="2026-01-01T00:00:00Z",
            id=5,
        )
        ok = sim.settle_trade(trade, winning_team="T1")
        assert ok is False


class TestOpenTradeEdgeCases:
    def test_buy_price_zero_after_vwap(self):
        """vwap=0 时不应除零。"""
        repo = MagicMock()
        repo.save_trade.return_value = 1
        clob = MagicMock()
        clob.get_token_id_and_orderbook.return_value = ("token", {"asks": []})
        sim = MorphologySimulator(
            repository=repo,
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": True},
                "buy_price_min": 0.05, "buy_price_max": 0.95,
            },
            clob_client=clob,
        )
        sig = MorphologySignal(
            signal_name="x", signal_label="x", window_label="mid",
            direction="A", buy_team="T1", buy_price=0.4, id=10,
        )
        alert = MorphologyAlert(match_id="m1", strongest_signal=sig)
        # 模拟 _try_get_vwap 返回 0，应保持原 buy_price
        with patch.object(sim, "_try_get_vwap", return_value=0.0):
            trade = sim.open_trade(alert)
        assert trade is not None
        assert trade.buy_price == 0.4

    def test_use_depth_with_vwap_replaces_price(self):
        """use_depth=True 且 vwap 有效时用 vwap 替代 buy_price。"""
        repo = MagicMock()
        repo.save_trade.return_value = 42
        sim = MorphologySimulator(
            repository=repo,
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": True},
                "buy_price_min": 0.05, "buy_price_max": 0.95,
            },
            clob_client=MagicMock(),
        )
        sig = MorphologySignal(
            signal_name="x", signal_label="x", window_label="mid",
            direction="A", buy_team="T1", buy_price=0.4, id=10,
        )
        alert = MorphologyAlert(match_id="m1", strongest_signal=sig)
        # vwap=0.5 < 0.95，应替换
        with patch.object(sim, "_try_get_vwap", return_value=0.5):
            trade = sim.open_trade(alert)
        assert trade is not None
        assert trade.buy_price == 0.5
        assert trade.vwap == 0.5
        # notional=100 / vwap=0.5 = 200 份
        assert trade.quantity == pytest.approx(200.0)

    def test_try_get_vwap_with_valid_asks(self):
        """_try_get_vwap 有可用 asks 时应返回 vwap。"""
        from esports_monitor.api.clob import parse_asks  # noqa: F401
        repo = MagicMock()
        clob = MagicMock()
        # Yes outcome 返回有效 orderbook
        clob.get_token_id_and_orderbook.side_effect = [
            ("token_yes", {"asks": [{"price": "0.42", "size": "500"}]}),
            ("token_no", {"asks": [{"price": "0.55", "size": "500"}]}),
        ]
        sim = MorphologySimulator(
            repository=repo,
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": True},
                "buy_price_min": 0.05, "buy_price_max": 0.95,
            },
            clob_client=clob,
        )
        vwap = sim._try_get_vwap("m1", "T1", 100.0)
        assert vwap is not None
        assert vwap > 0

    def test_try_get_vwap_no_orderbook_returns_none(self):
        """orderbook 为空时返回 None。"""
        clob = MagicMock()
        clob.get_token_id_and_orderbook.return_value = (None, None)
        sim = MorphologySimulator(
            repository=MagicMock(),
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": True},
            },
            clob_client=clob,
        )
        assert sim._try_get_vwap("m1", "T1", 100.0) is None

    def test_try_get_vwap_exception_returns_none(self):
        """clob_client 抛异常时返回 None。"""
        clob = MagicMock()
        clob.get_token_id_and_orderbook.side_effect = RuntimeError("net error")
        sim = MorphologySimulator(
            repository=MagicMock(),
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": True},
            },
            clob_client=clob,
        )
        assert sim._try_get_vwap("m1", "T1", 100.0) is None

    def test_try_get_vwap_no_clob_client_returns_none(self):
        """clob_client 为 None 时返回 None。"""
        sim = MorphologySimulator(
            repository=MagicMock(),
            config={
                "simulation": {"enabled": True, "notional_usd": 100, "use_depth": True},
            },
            clob_client=None,
        )
        assert sim._try_get_vwap("m1", "T1", 100.0) is None
