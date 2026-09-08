import datetime as dt

import pytest

from montauk.dates import Birthday, DatePrecision, FlexDate


class TestFlexDateParse:
    def test_full_precision_string(self):
        d = FlexDate.parse("2026-08-12")
        assert (d.year, d.month, d.day) == (2026, 8, 12)
        assert d.precision == DatePrecision.DAY

    def test_month_precision_string(self):
        d = FlexDate.parse("2026-08")
        assert (d.year, d.month, d.day) == (2026, 8, None)
        assert d.precision == DatePrecision.MONTH

    def test_year_precision_string(self):
        d = FlexDate.parse("2026")
        assert (d.year, d.month, d.day) == (2026, None, None)
        assert d.precision == DatePrecision.YEAR

    def test_bare_int_year(self):
        d = FlexDate.parse(2026)
        assert d.precision == DatePrecision.YEAR
        assert d.year == 2026

    def test_native_yaml_date_object(self):
        # PyYAML auto-resolves unquoted YYYY-MM-DD to datetime.date.
        d = FlexDate.parse(dt.date(2025, 4, 17))
        assert (d.year, d.month, d.day) == (2025, 4, 17)

    def test_native_datetime_object(self):
        d = FlexDate.parse(dt.datetime(2025, 4, 17, 10, 30))
        assert (d.year, d.month, d.day) == (2025, 4, 17)

    def test_round_trip_string(self):
        for s in ("2026-08-12", "2026-08", "2026"):
            assert FlexDate.parse(s).to_string() == s

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "not-a-date",
            "2026-13",  # invalid month
            "2026-02-30",  # invalid day for Feb
            "26-08-12",  # not 4-digit year
            "2026-8-1",  # not zero-padded
            None,
        ],
    )
    def test_rejects_malformed(self, bad):
        with pytest.raises((ValueError, TypeError)):
            FlexDate.parse(bad)

    def test_rejects_bool(self):
        with pytest.raises(ValueError):
            FlexDate.parse(True)


class TestFlexDateLatestAnchoring:
    def test_day_precision_latest_is_itself(self):
        d = FlexDate.parse("2026-08-12")
        assert d.latest() == dt.date(2026, 8, 12)

    def test_month_precision_latest_is_last_day_of_month(self):
        d = FlexDate.parse("2026-02")  # 2026 is not a leap year
        assert d.latest() == dt.date(2026, 2, 28)

    def test_month_precision_latest_leap_year(self):
        d = FlexDate.parse("2024-02")
        assert d.latest() == dt.date(2024, 2, 29)

    def test_year_precision_latest_is_dec_31(self):
        d = FlexDate.parse("2025")
        assert d.latest() == dt.date(2025, 12, 31)

    def test_ordering_uses_latest(self):
        assert FlexDate.parse("2025") < FlexDate.parse("2026-01-01")
        assert FlexDate.parse("2026-08") > FlexDate.parse("2026-07-31")


class TestBirthdayParse:
    def test_full_with_year(self):
        b = Birthday.parse("1982-04-17")
        assert (b.year, b.month, b.day) == (1982, 4, 17)

    def test_month_day_only(self):
        b = Birthday.parse("04-17")
        assert (b.year, b.month, b.day) == (None, 4, 17)

    def test_native_date_object(self):
        b = Birthday.parse(dt.date(1982, 4, 17))
        assert (b.year, b.month, b.day) == (1982, 4, 17)

    def test_feb_29_without_year_is_valid(self):
        b = Birthday.parse("02-29")
        assert (b.month, b.day, b.year) == (2, 29, None)

    def test_month_only(self):
        b = Birthday.parse("03")
        assert (b.year, b.month, b.day) == (None, 3, None)
        assert b.precision.value == "month"

    def test_single_digit_month_only(self):
        assert Birthday.parse("3").to_string() == "03"

    def test_year_and_month_no_day(self):
        b = Birthday.parse("1990-03")
        assert (b.year, b.month, b.day) == (1990, 3, None)

    def test_round_trip(self):
        for s in ("1982-04-17", "04-17", "1990-03", "03"):
            assert Birthday.parse(s).to_string() == s

    @pytest.mark.parametrize("bad", ["", "13-01", "02-30", "2026", "13", "0", "not-a-date"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            Birthday.parse(bad)


class TestBirthdayNextOccurrence:
    def test_upcoming_this_year(self):
        b = Birthday.parse("12-25")
        today = dt.date(2026, 8, 30)
        assert b.next_occurrence(today) == dt.date(2026, 12, 25)

    def test_wraps_to_next_year(self):
        b = Birthday.parse("01-15")
        today = dt.date(2026, 8, 30)
        assert b.next_occurrence(today) == dt.date(2027, 1, 15)

    def test_month_only_anchors_to_first_of_month(self):
        b = Birthday.parse("03")
        assert b.next_occurrence(dt.date(2026, 1, 1)) == dt.date(2026, 3, 1)

    def test_today_counts_as_upcoming(self):
        b = Birthday.parse("08-30")
        today = dt.date(2026, 8, 30)
        assert b.next_occurrence(today) == today

    def test_feb_29_falls_back_to_feb_28_in_non_leap_year(self):
        b = Birthday.parse("02-29")
        today = dt.date(2026, 1, 1)  # 2026 is not a leap year
        assert b.next_occurrence(today) == dt.date(2026, 2, 28)

    def test_feb_29_lands_on_feb_29_in_leap_year(self):
        b = Birthday.parse("02-29")
        today = dt.date(2024, 1, 1)  # 2024 is a leap year
        assert b.next_occurrence(today) == dt.date(2024, 2, 29)
