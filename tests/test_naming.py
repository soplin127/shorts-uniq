import random
import re

import pytest

from core.naming import pick_output_path, render_name


def _render(template, seed=1, **overrides):
    kwargs = dict(date_compact="20260115", time_compact="101500", stem="my clip",
                  variant=2, index=7)
    kwargs.update(overrides)
    return render_name(template, random.Random(seed), **kwargs)


def test_tokens_are_substituted():
    name = _render("IMG_{random4}_{date}_{time}_{stem}_{variant}_{index}")

    assert re.fullmatch(r"IMG_\d{4}_20260115_101500_my clip_2_0007", name)


def test_uuid_token_is_deterministic_for_a_seed():
    assert _render("{uuid}", seed=5) == _render("{uuid}", seed=5)
    assert _render("{uuid}", seed=5) != _render("{uuid}", seed=6)


def test_unknown_token_is_left_visible():
    assert _render("v_{nope}") == "v_{nope}"


@pytest.mark.parametrize("template, expected", [
    ('a<b>c:d"e|f?g*h', "a_b_c_d_e_f_g_h"),
    ("CON", "_CON"),
    ("trailing.", "trailing"),
    ("{stem}", "x_y"),
])
def test_names_are_safe_on_windows(template, expected):
    assert _render(template, stem="x/y") == expected


def test_long_names_are_truncated():
    assert len(_render("a" * 300)) == 120


def test_pick_output_path_avoids_disk_and_reserved_names(tmp_path):
    (tmp_path / "IMG_1.mp4").write_bytes(b"")
    names = iter(["IMG_1", "img_2", "IMG_2", "IMG_3"])
    taken = {"img_2.mp4"}

    path = pick_output_path(tmp_path, lambda: next(names), taken)

    assert path.name == "IMG_3.mp4"
    assert "img_3.mp4" in taken


def test_pick_output_path_gives_up_on_constant_template(tmp_path):
    (tmp_path / "same.mp4").write_bytes(b"")

    with pytest.raises(RuntimeError):
        pick_output_path(tmp_path, lambda: "same", set(), attempts=3)
