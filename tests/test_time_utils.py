"""utils/time_utils.py 测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from esports_monitor.utils.time_utils import (
    BEIJING_TZ,
    UTC_TZ,
    format_display_label,
    from_utc_iso,
    hours_between,
    minutes_between,
    now_utc,
    to_beijing_dt,
    to_beijing_str,
    to_utc_iso,
)


class TestNowUtc:
    def test_returns_aware_datetime(self):
        dt = now_utc()
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)

    def test_close_to_current_time(self):
        dt = now_utc()
        expected = datetime.now(timezone.utc)
        assert abs((dt - expected).total_seconds()) < 2


class TestToUtcIso:
    def test_aware_datetime(self):
        dt = datetime(2026, 6, 29, 15, 30, 0, tzinfo=UTC_TZ)
        assert to_utc_iso(dt) == "2026-06-29T15:30:00+00:00"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime(2026, 6, 29, 15, 30, 0)
        assert to_utc_iso(dt) == "2026-06-29T15:30:00+00:00"

    def test_beijing_datetime_converted_to_utc(self):
        # 北京时间 23:30 = UTC 15:30
        dt = datetime(2026, 6, 29, 23, 30, 0, tzinfo=BEIJING_TZ)
        assert to_utc_iso(dt) == "2026-06-29T15:30:00+00:00"


class TestFromUtcIso:
    def test_with_z_suffix(self):
        dt = from_utc_iso("2026-06-29T15:30:00Z")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 6
        assert dt.day == 29
        assert dt.hour == 15
        assert dt.tzinfo == UTC_TZ

    def test_with_offset(self):
        dt = from_utc_iso("2026-06-29T15:30:00+00:00")
        assert dt is not None
        assert dt.tzinfo == UTC_TZ

    def test_naive_string_treated_as_utc(self):
        dt = from_utc_iso("2026-06-29T15:30:00")
        assert dt is not None
        assert dt.tzinfo == UTC_TZ

    def test_space_separated_format(self):
        dt = from_utc_iso("2026-06-29 15:30:00")
        assert dt is not None
        assert dt.hour == 15
        assert dt.tzinfo == UTC_TZ

    def test_empty_string(self):
        assert from_utc_iso("") is None
        assert from_utc_iso(None) is None  # type: ignore[arg-type]

    def test_invalid_string(self):
        assert from_utc_iso("not-a-date") is None


class TestBeijingConversion:
    def test_to_beijing_dt(self):
        dt = datetime(2026, 6, 29, 15, 30, 0, tzinfo=UTC_TZ)
        bj = to_beijing_dt(dt)
        assert bj.hour == 23
        assert bj.tzinfo == BEIJING_TZ

    def test_to_beijing_str(self):
        dt = datetime(2026, 6, 29, 15, 30, 0, tzinfo=UTC_TZ)
        assert to_beijing_str(dt) == "2026-06-29 23:30:00"

    def test_format_display_label(self):
        dt = datetime(2026, 6, 29, 15, 30, 0, tzinfo=UTC_TZ)
        assert format_display_label(dt) == "06-29 23:30"

    def test_naive_to_beijing(self):
        dt = datetime(2026, 6, 29, 15, 30, 0)
        bj = to_beijing_dt(dt)
        assert bj.hour == 23


class TestBetweenFunctions:
    def test_minutes_between_positive(self):
        start = datetime(2026, 6, 29, 15, 0, 0, tzinfo=UTC_TZ)
        end = datetime(2026, 6, 29, 15, 30, 0, tzinfo=UTC_TZ)
        assert minutes_between(start, end) == 30.0

    def test_minutes_between_negative(self):
        start = datetime(2026, 6, 29, 15, 30, 0, tzinfo=UTC_TZ)
        end = datetime(2026, 6, 29, 15, 0, 0, tzinfo=UTC_TZ)
        assert minutes_between(start, end) == -30.0

    def test_hours_between(self):
        start = datetime(2026, 6, 29, 15, 0, 0, tzinfo=UTC_TZ)
        end = datetime(2026, 6, 29, 17, 0, 0, tzinfo=UTC_TZ)
        assert hours_between(start, end) == 2.0

    def test_naive_datetimes_treated_as_utc(self):
        start = datetime(2026, 6, 29, 15, 0, 0)
        end = datetime(2026, 6, 29, 15, 30, 0)
        assert minutes_between(start, end) == 30.0
