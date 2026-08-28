from datetime import datetime

import pytest

from reolink_downloader import _slug, parse_channel_spec, parse_datetime


class TestParseChannelSpec:
    def test_all(self):
        assert parse_channel_spec("all") == "all"
        assert parse_channel_spec("ALL") == "all"

    def test_single(self):
        assert parse_channel_spec("0") == {0}

    def test_comma_list(self):
        assert parse_channel_spec("0,2,5") == {0, 2, 5}

    def test_range(self):
        assert parse_channel_spec("0-3") == {0, 1, 2, 3}

    def test_combined(self):
        assert parse_channel_spec("0,2-4,7") == {0, 2, 3, 4, 7}

    def test_whitespace_tolerant(self):
        assert parse_channel_spec(" 0 , 2 - 4 ") == {0, 2, 3, 4}

    @pytest.mark.parametrize("spec", ["", "x", "3-1", "a-b", ",", "0-"])
    def test_invalid_raises(self, spec):
        with pytest.raises(ValueError):
            parse_channel_spec(spec)


class TestParseDatetime:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("2024-01-15", datetime(2024, 1, 15)),
            ("2024-01-15 14:30", datetime(2024, 1, 15, 14, 30)),
            ("2024-01-15 14:30:05", datetime(2024, 1, 15, 14, 30, 5)),
            ("2024/01/15", datetime(2024, 1, 15)),
            ("2024/01/15 14:30", datetime(2024, 1, 15, 14, 30)),
        ],
    )
    def test_valid_formats(self, text, expected):
        assert parse_datetime(text) == expected

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_datetime("not-a-date")

    @pytest.mark.parametrize("text", ["2024-01-15", "2024/01/15"])
    def test_date_only_defaults_to_midnight_by_default(self, text):
        assert parse_datetime(text) == datetime(2024, 1, 15, 0, 0, 0)

    @pytest.mark.parametrize("text", ["2024-01-15", "2024/01/15"])
    def test_date_only_defaults_to_end_of_day_when_requested(self, text):
        assert parse_datetime(text, end_of_day=True) == datetime(2024, 1, 15, 23, 59, 59)

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("2024-01-15 14:30", datetime(2024, 1, 15, 14, 30)),
            ("2024-01-15 14:30:05", datetime(2024, 1, 15, 14, 30, 5)),
        ],
    )
    def test_explicit_time_is_unaffected_by_end_of_day(self, text, expected):
        # end_of_day only fills in a *missing* time component -- an
        # explicitly given time is never overridden.
        assert parse_datetime(text, end_of_day=True) == expected


class TestSlug:
    def test_basic(self):
        assert _slug("Front Door") == "Front-Door"

    def test_strips_edge_punctuation(self):
        assert _slug("--Garage!!") == "Garage"

    def test_empty_falls_back_to_unnamed(self):
        assert _slug("###") == "unnamed"
        assert _slug("") == "unnamed"
