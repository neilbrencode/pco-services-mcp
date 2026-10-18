from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from pco_services_mcp.blockouts import build_attributes, resolve_zone
from pco_services_mcp.client import PcoError

LA = ZoneInfo("America/Los_Angeles")


def build(**overrides):
    args = dict(
        start="2027-01-10",
        end=None,
        reason=None,
        repeat="none",
        every=1,
        monthly_by=None,
        repeat_until=None,
        zone=LA,
    )
    args.update(overrides)
    return build_attributes(**args)


def test_whole_day_in_winter_matches_planning_centers_own_convention():
    assert build() == {"starts_at": "2027-01-10T08:00:00Z", "ends_at": "2027-01-11T07:59:59Z"}


def test_whole_day_in_summer_uses_that_dates_offset():
    # Independent of today's date: the offset comes from the zone rules for July.
    assert build(start="2027-07-04") == {
        "starts_at": "2027-07-04T07:00:00Z",
        "ends_at": "2027-07-05T06:59:59Z",
    }


def test_multi_day_range():
    attrs = build(start="2027-12-24", end="2027-12-26")
    assert attrs["starts_at"] == "2027-12-24T08:00:00Z"
    assert attrs["ends_at"] == "2027-12-27T07:59:59Z"


def test_naive_times_are_church_local_and_offsets_are_respected():
    assert (
        build(start="2027-01-10T09:00", end="2027-01-10T12:00")["starts_at"]
        == "2027-01-10T17:00:00Z"
    )
    attrs = build(start="2027-01-10T09:00-05:00", end="2027-01-10T10:00-05:00")
    assert attrs["starts_at"] == "2027-01-10T14:00:00Z"


def test_time_in_the_spring_forward_gap_is_refused():
    with pytest.raises(PcoError, match="doesn't exist"):
        build(start="2027-03-14T02:30", end="2027-03-14T04:00")


def test_ambiguous_fall_back_time_uses_first_occurrence():
    # 01:30 happens twice on 2027-11-07; the first is still PDT (UTC-7).
    assert (
        build(start="2027-11-07T01:30", end="2027-11-07T03:00")["starts_at"]
        == "2027-11-07T08:30:00Z"
    )


def test_datetime_start_requires_an_end():
    with pytest.raises(PcoError, match="needs an end"):
        build(start="2027-01-10T09:00")


def test_end_before_start_is_refused():
    with pytest.raises(PcoError, match="end after"):
        build(start="2027-01-10", end="2027-01-09")


def test_weekly_repeat():
    attrs = build(repeat="weekly", every=2, repeat_until="2027-06-30")
    assert attrs["repeat_period"] == "weekly"
    assert attrs["repeat_frequency"] == "every_2"
    assert attrs["repeat_until"] == "2027-06-30"
    assert "repeat_interval" not in attrs


@pytest.mark.parametrize(
    ("monthly_by", "interval"),
    [
        (None, "exact_day_of_month"),
        ("week_2", "week_of_month_2"),
        ("last_week", "week_of_month_last"),
    ],
)
def test_monthly_repeat_interval(monthly_by, interval):
    assert build(repeat="monthly", monthly_by=monthly_by)["repeat_interval"] == interval


@pytest.mark.parametrize(
    "overrides",
    [
        {"every": 2},
        {"repeat_until": "2027-02-01"},
        {"monthly_by": "week_1"},
        {"repeat": "weekly", "every": 9},
        {"repeat": "weekly", "every": 0},
        {"repeat": "weekly", "monthly_by": "week_1"},
        {"repeat": "weekly", "repeat_until": "2026-01-01"},
        {"repeat": "weekly", "repeat_until": "2027-02-01T00:00"},
        {"start": "next tuesday"},
    ],
)
def test_invalid_combinations_are_refused(overrides):
    with pytest.raises(PcoError):
        build(**overrides)


@pytest.mark.parametrize("name", [None, "", "Pacific Time (US & Canada)", "Not/AZone"])
def test_unusable_zone_is_refused_not_guessed(name):
    with pytest.raises(PcoError, match="PCO_TIME_ZONE"):
        resolve_zone(name)
