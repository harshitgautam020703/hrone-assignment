"""
Business rule unit tests (no database required).
Tests R1-R5 against the exact examples in the spec.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.main import (
    IST,
    UTC,
    attendance_date,
    compute_half_day,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    recompute_derived,
    round_half_up,
    truncate_to_seconds,
)


# ---------------------------------------------------------------------------
# R1 – Attendance date and overnight shift
# ---------------------------------------------------------------------------
class TestAttendanceDate:
    def _ist(self, year, month, day, hour, minute) -> datetime:
        return datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(UTC)

    def test_normal_day_shift(self):
        punch = self._ist(2026, 7, 6, 9, 28)
        assert attendance_date(punch, "09:30", "18:30") == date(2026, 7, 6)

    def test_overnight_punch_in_before_midnight(self):
        """Night shift 22:00-06:00. Punch-in 21:55 → date = July 6."""
        punch = self._ist(2026, 7, 6, 21, 55)
        assert attendance_date(punch, "22:00", "06:00") == date(2026, 7, 6)

    def test_overnight_punch_in_after_midnight_before_end(self):
        """Night shift 22:00-06:00. Punch-in 03:00 next day → belongs to July 6."""
        punch = self._ist(2026, 7, 7, 3, 0)
        assert attendance_date(punch, "22:00", "06:00") == date(2026, 7, 6)

    def test_overnight_punch_in_after_shift_end(self):
        """Night shift 22:00-06:00. Punch-in 07:00 next day → new day July 7."""
        punch = self._ist(2026, 7, 7, 7, 0)
        assert attendance_date(punch, "22:00", "06:00") == date(2026, 7, 7)

    def test_truncate_to_seconds(self):
        dt = datetime(2026, 7, 6, 9, 40, 0, 900_000, tzinfo=UTC)
        assert truncate_to_seconds(dt) == datetime(2026, 7, 6, 9, 40, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# R2 – Late minutes
# ---------------------------------------------------------------------------
class TestLateMinutes:
    def _punch(self, hour, minute, second=0) -> datetime:
        """Create UTC datetime for an IST punch on 2026-07-06."""
        return datetime(2026, 7, 6, hour, minute, second, tzinfo=IST).astimezone(UTC)

    def test_on_time(self):
        # 09:28 → 2 min early → 0
        assert compute_late_minutes(self._punch(9, 28), "09:30", date(2026, 7, 6)) == 0

    def test_within_grace(self):
        # 09:40:00 → exactly 10 min → 0 (spec: strictly more than 10 min)
        assert compute_late_minutes(self._punch(9, 40, 0), "09:30", date(2026, 7, 6)) == 0

    def test_one_second_past_grace(self):
        # 09:40:01 → 10 min 1 sec → 10 (floor of 10.016...)
        assert compute_late_minutes(self._punch(9, 40, 1), "09:30", date(2026, 7, 6)) == 10

    def test_late_example_spec(self):
        # 10:15:59 → 45 min late
        assert compute_late_minutes(self._punch(10, 15, 59), "09:30", date(2026, 7, 6)) == 45

    def test_very_late(self):
        # 12:00 → 150 min
        assert compute_late_minutes(self._punch(12, 0), "09:30", date(2026, 7, 6)) == 150


# ---------------------------------------------------------------------------
# R3 – Overtime minutes
# ---------------------------------------------------------------------------
class TestOvertimeMinutes:
    def _punch_out(self, hour, minute) -> datetime:
        return datetime(2026, 7, 6, hour, minute, tzinfo=IST).astimezone(UTC)

    def test_no_overtime_small(self):
        # 18:35 → 5 min after 18:30 → < 30 → 0
        po = self._punch_out(18, 35)
        assert compute_overtime_minutes(po, "18:30", date(2026, 7, 6), "09:30") == 0

    def test_no_overtime_at_29(self):
        po = self._punch_out(18, 59)
        assert compute_overtime_minutes(po, "18:30", date(2026, 7, 6), "09:30") == 0

    def test_exactly_30_min(self):
        # 19:00 → 30 min → qualifies
        po = self._punch_out(19, 0)
        assert compute_overtime_minutes(po, "18:30", date(2026, 7, 6), "09:30") == 30

    def test_overtime_40(self):
        # 19:10 → 40 min
        po = self._punch_out(19, 10)
        assert compute_overtime_minutes(po, "18:30", date(2026, 7, 6), "09:30") == 40

    def test_overnight_shift_overtime(self):
        # Night shift 22:00-06:00. Punch-out 06:45 next day → 45 min OT
        po = datetime(2026, 7, 7, 6, 45, tzinfo=IST).astimezone(UTC)
        assert compute_overtime_minutes(po, "06:00", date(2026, 7, 6), "22:00") == 45


# ---------------------------------------------------------------------------
# R4 – Work hours (half-up rounding)
# ---------------------------------------------------------------------------
class TestWorkHours:
    def _punch(self, h1, m1, h2, m2) -> tuple[datetime, datetime]:
        d = date(2026, 7, 6)
        pi = datetime(d.year, d.month, d.day, h1, m1, tzinfo=IST).astimezone(UTC)
        po = datetime(d.year, d.month, d.day, h2, m2, tzinfo=IST).astimezone(UTC)
        return pi, po

    def test_nine_hours(self):
        pi, po = self._punch(9, 28, 18, 28)
        assert compute_work_hours(pi, po) == 9.0

    def test_sample_data_9_12(self):
        # 09:28 to 18:35 IST (3:58 UTC to 13:05 UTC) → spec says 9.12
        pi = datetime(2026, 7, 6, 3, 58, tzinfo=UTC)
        po = datetime(2026, 7, 6, 13, 5, tzinfo=UTC)
        assert compute_work_hours(pi, po) == 9.12

    def test_half_up_rounding(self):
        # 5 hours exactly
        pi = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
        po = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)
        assert compute_work_hours(pi, po) == 5.0

    def test_3_5_hours(self):
        # half day test
        pi = datetime(2026, 7, 7, 4, 0, tzinfo=UTC)
        po = datetime(2026, 7, 7, 7, 30, tzinfo=UTC)
        assert compute_work_hours(pi, po) == 3.5


# ---------------------------------------------------------------------------
# R5 – Half day
# ---------------------------------------------------------------------------
class TestHalfDay:
    def test_below_threshold(self):
        assert compute_half_day(4.49) is True

    def test_at_threshold(self):
        assert compute_half_day(4.50) is False  # NOT half day (< 4.50)

    def test_above_threshold(self):
        assert compute_half_day(8.0) is False

    def test_3_5(self):
        assert compute_half_day(3.5) is True


# ---------------------------------------------------------------------------
# R8 – Round half-up
# ---------------------------------------------------------------------------
class TestRoundHalfUp:
    def test_basic(self):
        assert round_half_up(2.345, 2) == 2.35

    def test_banker_vs_halfup(self):
        # Python's built-in round(2.5) = 2 (banker's). round_half_up should give 3.
        assert round_half_up(2.5, 0) == 3.0

    def test_four_decimals(self):
        assert round_half_up(0.12345, 4) == 0.1235

    def test_attendance_pct(self):
        # 22/23 working days ≈ 95.6521... → 95.65 at 2dp
        assert round_half_up(22 / 23 * 100, 2) == 95.65

