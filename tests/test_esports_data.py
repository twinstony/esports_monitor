"""api/esports_data.py 测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from esports_monitor.api.esports_data import (
    EsportsDataProvider,
    MatchState,
    NullProvider,
    create_provider,
)


class TestNullProvider:
    def test_is_available_false(self):
        assert NullProvider().is_available() is False

    def test_get_match_state_none(self):
        assert NullProvider().get_match_state("any") is None


class TestCreateProvider:
    def test_no_config(self):
        assert isinstance(create_provider(None), NullProvider)  # type: ignore[arg-type]

    def test_disabled(self):
        assert isinstance(create_provider({"enabled": False}), NullProvider)

    def test_enabled_no_provider_name(self):
        assert isinstance(create_provider({"enabled": True, "provider": None}), NullProvider)

    def test_enabled_unknown_provider(self):
        # 第一阶段无可用 provider，未知 provider 返回 NullProvider
        assert isinstance(
            create_provider({"enabled": True, "provider": "unknown"}), NullProvider
        )

    def test_cito_provider_not_implemented_yet(self):
        # 第一阶段 CitoProvider 尚未实现，返回 NullProvider
        assert isinstance(
            create_provider({"enabled": True, "provider": "cito"}), NullProvider
        )


class TestMatchState:
    def test_construction(self):
        ms = MatchState(
            game="lol",
            match_id="m1",
            timestamp=datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc),
            score_a=2,
            score_b=1,
            gold_diff=1500.0,
            kill_diff=3,
            progress_pct=0.6,
            extra={"map": "summoners_rift"},
        )
        assert ms.game == "lol"
        assert ms.score_a == 2
        assert ms.gold_diff == 1500.0
        assert ms.extra == {"map": "summoners_rift"}

    def test_defaults(self):
        ms = MatchState(
            game="cs2",
            match_id="m1",
            timestamp=datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc),
            score_a=0,
            score_b=0,
        )
        assert ms.gold_diff == 0.0
        assert ms.kill_diff == 0
        assert ms.progress_pct == 0.0
        assert ms.extra is None


class TestEsportsDataProviderAbstract:
    def test_cannot_instantiate_directly(self):
        import pytest
        with pytest.raises(TypeError):
            EsportsDataProvider()  # type: ignore[abstract]
