"""Remotion render wrapper tests.

The Remotion CLI is invoked with cwd=remotion/, so a relative output path is written
under remotion/ while the caller looks for it somewhere else. The render then reports
success and the pipeline reports "Video rendering failed" against a file that does
exist, just not where it was expected. Both entry points hit this in production.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stock_snap.content.generator import ContentPackage  # noqa: E402
from stock_snap.media import short_video  # noqa: E402


@pytest.fixture
def pkg() -> ContentPackage:
    return ContentPackage(
        symbol="INTC",
        price=104.47,
        change_pct=8.96,
        direction="rising",
        script="테스트 스크립트",
        card_title="INTC 8.96% 상승",
        card_subtitle="테스트",
        card_body="본문",
        caption="캡션",
        chart_data=[100.0, 101.0, 102.0],
        company_name_ko="인텔",
    )


def _fake_cli(created: list[Path]):
    """Stand in for the Remotion CLI: write the file it was told to write."""

    def _run(cmd, **_kwargs):
        target = next(Path(a) for a in cmd if str(a).endswith((".mp4", ".jpg")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 128)
        created.append(target)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    return _run


@pytest.mark.parametrize(
    ("render", "suffix"),
    [(short_video.generate_short_video, ".mp4"), (short_video.generate_thumbnail, ".jpg")],
)
def test_relative_output_path_is_resolved(render, suffix, pkg, tmp_path, monkeypatch):
    """A relative output path must still be found after the render.

    The CLI resolves it against remotion/, the caller against its own cwd. Unless the
    path is made absolute first those are two different files.
    """
    monkeypatch.chdir(tmp_path)
    created: list[Path] = []
    relative = Path("output") / f"nested{suffix}"

    with patch.object(short_video.subprocess, "run", side_effect=_fake_cli(created)):
        ok = render(pkg, relative)

    assert ok is True
    assert created, "the CLI was never given a path to write"
    assert created[0].is_absolute(), f"CLI received a relative path: {created[0]}"
    assert created[0] == (tmp_path / relative).resolve()


def test_absolute_output_path_is_left_alone(pkg, tmp_path):
    """An already-absolute path must be passed through unchanged."""
    created: list[Path] = []
    absolute = tmp_path / "out" / "video.mp4"

    with patch.object(short_video.subprocess, "run", side_effect=_fake_cli(created)):
        ok = short_video.generate_short_video(pkg, absolute)

    assert ok is True
    assert created[0] == absolute.resolve()


def test_nan_in_props_is_refused_before_spawning_the_cli(pkg, tmp_path):
    """Python writes NaN as a bare `NaN` token, which is not JSON. Remotion rejects the
    whole --props argument and buries the reason under an echo of the input. Catch it
    here, name the field, and never start the process."""
    pkg.price = float("nan")
    created: list[Path] = []
    with patch.object(short_video.subprocess, "run", side_effect=_fake_cli(created)) as run:
        ok = short_video.generate_short_video(pkg, tmp_path / "v.mp4")
    assert ok is False
    run.assert_not_called()


def test_non_finite_paths_names_the_offending_keys():
    props = {"price": float("nan"), "chartData": [1.0, float("inf")], "ok": 1.5, "s": "x"}
    assert sorted(short_video._non_finite_paths(props)) == ["chartData[1]", "price"]
