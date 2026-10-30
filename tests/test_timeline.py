import random

import pytest

from core.timeline import TimeSpecError, build_timeline, parse_time_spec


@pytest.mark.parametrize("spec, expected", [
    ("7", 7.0),
    ("00:07", 7.0),
    ("0:01:05.5", 65.5),
    ("50%", 50.0),
    ("999", 100.0),  # clamped to duration
])
def test_parse_time_spec_resolves_against_duration(spec, expected):
    assert parse_time_spec(spec, 100.0, random.Random(1)) == pytest.approx(expected)


def test_random_spec_stays_within_bounds():
    rng = random.Random(42)

    values = [parse_time_spec("random:3-10", 20.0, rng) for _ in range(200)]

    assert all(3.0 <= v <= 10.0 for v in values)


@pytest.mark.parametrize("spec", ["", "abc", "random:5"])
def test_bad_time_spec_raises(spec):
    with pytest.raises(TimeSpecError):
        parse_time_spec(spec, 20.0, random.Random(1))


def test_timeline_without_insertions_applies_speed():
    timeline = build_timeline(1.0, 11.0, [], speed=1.25)

    assert len(timeline.segments) == 1
    assert timeline.duration == pytest.approx(8.0)
    assert timeline.to_output(1.0) == pytest.approx(0.0)
    assert timeline.to_output(6.0) == pytest.approx(4.0)
    assert timeline.to_output(0.5) is None  # trimmed away


def test_ad_insertion_shifts_later_source_time():
    timeline = build_timeline(0.0, 10.0, [(4.0, 3.0)], speed=1.0)

    kinds = [s.kind for s in timeline.segments]
    assert kinds == ["source", "ad", "source"]
    assert timeline.duration == pytest.approx(13.0)
    assert timeline.to_output(3.0) == pytest.approx(3.0)
    assert timeline.to_output(5.0) == pytest.approx(8.0)
    assert timeline.map_interval(3.0, 5.0) == [(pytest.approx(3.0), pytest.approx(4.0)),
                                               (pytest.approx(7.0), pytest.approx(8.0))]


def test_insertion_order_keeps_ad_index():
    timeline = build_timeline(0.0, 10.0, [(8.0, 1.0), (2.0, 2.0)], speed=1.0)

    ads = [s for s in timeline.segments if s.kind == "ad"]
    assert [a.ad_index for a in ads] == [1, 0]


def test_too_short_window_raises():
    with pytest.raises(TimeSpecError):
        build_timeline(5.0, 5.01, [], speed=1.0)
