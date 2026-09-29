"""Entry point tests for the run_*.py pipelines.

run_aftermarket.py and run_weekly_review.py imported StockDataFetcher, a class that
never existed, from the day they were added. Nothing caught it: the import sits inside
run(), so it survives compilation, and no test imported either module. Both workflows
died on the first scheduled run that got far enough to execute them.

Two layers here. The first resolves every stock_snap symbol each entry point imports,
which catches that class of mistake across all of them for almost no runtime. The
second actually drives the two previously untested pipelines with the slow and
irreversible parts mocked out.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_snap.news.direction import DirectionResult  # noqa: E402

ENTRY_POINTS = sorted(p.name for p in REPO_ROOT.glob("run_*.py"))


def _stock_snap_imports(path: Path) -> list[tuple[int, str, str]]:
    """Every (lineno, module, name) this file imports from the stock_snap package."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "stock_snap" or node.module.startswith("stock_snap."):
                for alias in node.names:
                    found.append((node.lineno, node.module, alias.name))
    return found


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_entrypoint_imports_resolve(entry_point):
    """Names imported from stock_snap must actually exist.

    These imports live inside run(), so nothing surfaces them until the pipeline is
    executed for real - which, on a schedule, means finding out the next morning.
    """
    path = REPO_ROOT / entry_point
    missing = []
    for lineno, module_name, symbol in _stock_snap_imports(path):
        module = importlib.import_module(module_name)
        if not hasattr(module, symbol):
            missing.append(f"{entry_point}:{lineno} {module_name}.{symbol}")
    assert not missing, "unresolvable imports: " + ", ".join(missing)


# ---------------------------------------------------------------------------
# Mocked pipeline runs
# ---------------------------------------------------------------------------


def _make_ohlcv(rows: int = 120) -> pd.DataFrame:
    """OHLCV with enough history for SMA60 and the 20-day trendline."""
    index = pd.date_range("2026-05-01", periods=rows, freq="B")
    close = np.linspace(100, 130, rows) + np.sin(np.arange(rows) / 3) * 2
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.5,
            "Low": close - 1.5,
            "Close": close,
            "Volume": np.linspace(1_000_000, 3_000_000, rows).astype(int),
        },
        index=index,
    )


def _fake_ticker(*_args, **_kwargs):
    ticker = MagicMock()
    ticker.history.return_value = _make_ohlcv(10)
    ticker.fast_info.previous_close = 120.0
    ticker.news = []
    return ticker


def _fake_tts(_text, out_path, *_args, **_kwargs):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(b"\x00" * 4096)
    return True, [(0.0, 2.0), (2.0, 4.0)]


def _fake_video(_pkg, out_path, *_args, **_kwargs):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(b"\x00" * 2048)
    return True


def _fake_thumbnail(_pkg, out_path, *_args, **_kwargs):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(b"\xff\xd8\xff" + b"\x00" * 2048)
    return True


@pytest.fixture
def mocked_externals():
    """Mock everything that costs money, time, or posts publicly.

    autospec is what makes this worth having. A plain MagicMock accepts any argument
    name, so it happily swallows a call like generate_short_video(..., tts_segment_paths=...)
    against a function whose parameter is audio_segment_paths - which is exactly the
    second bug these pipelines were carrying. autospec validates each call against the
    real signature, so a rename on either side fails here instead of on the runner.
    """
    from stock_snap.upload.instagram import instagram
    from stock_snap.upload.youtube import youtube

    patches = {
        "ohlcv": patch(
            "stock_snap.data.fetcher.MarketDataFetcher.get_ohlcv",
            autospec=True,
            return_value=_make_ohlcv(),
        ),
        "ticker": patch("yfinance.Ticker", side_effect=_fake_ticker),
        "news": patch(
            "stock_snap.news.collector.NewsCollector.fetch_for_symbol",
            autospec=True,
            return_value=[],
        ),
        "tts": patch(
            "stock_snap.media.tts.generate_tts_with_timing",
            autospec=True,
            side_effect=_fake_tts,
        ),
        "duration": patch(
            "stock_snap.media.tts.get_audio_duration", autospec=True, return_value=5.0
        ),
        "video": patch(
            "stock_snap.media.short_video.generate_short_video",
            autospec=True,
            side_effect=_fake_video,
        ),
        "thumbnail": patch(
            "stock_snap.media.short_video.generate_thumbnail",
            autospec=True,
            side_effect=_fake_thumbnail,
        ),
        "host": patch(
            "stock_snap.upload.media_host.publish_media",
            autospec=True,
            return_value="https://example.test/video.mp4",
        ),
        "news_filter": patch(
            "stock_snap.news.direction.select_direction_articles",
            autospec=True,
            return_value=DirectionResult(
                [("테스트 호재 기사", "테스트용 상세 내용입니다.")], "claude_code"
            ),
        ),
        "reel": patch.object(instagram, "upload_reel", autospec=True, return_value=True),
        "short": patch.object(
            youtube, "upload_short", autospec=True, return_value="fake-video-id"
        ),
    }
    mocks = {name: p.start() for name, p in patches.items()}
    yield mocks
    for p in patches.values():
        p.stop()


PUBLISHING_PIPELINES = ["run_live_short", "run_aftermarket", "run_weekly_review"]


@pytest.mark.parametrize("module_name", ["run_aftermarket", "run_weekly_review"])
def test_pipeline_runs_to_completion(module_name, mocked_externals, tmp_path, monkeypatch):
    """The pipeline must reach the upload step without raising or exiting non-zero."""
    module = importlib.import_module(module_name)
    # Both write to a cwd-relative output/ directory; keep that out of the repo.
    monkeypatch.chdir(tmp_path)

    module.run()

    assert mocked_externals["host"].called, "video was never given a public URL"
    assert mocked_externals["reel"].called, "Instagram upload was never attempted"

    # The render runs from remotion/, so anything cwd-relative resolves against the
    # wrong directory. Entry points must hand it an absolute path.
    render_target = mocked_externals["video"].call_args.args[1]
    assert Path(render_target).is_absolute(), f"render got a relative path: {render_target}"


@pytest.mark.parametrize("module_name", ["run_aftermarket", "run_weekly_review"])
def test_on_screen_headlines_come_from_the_filter(
    module_name, mocked_externals, tmp_path, monkeypatch
):
    """StockShort draws pkg.news_headlines on screen, so they must be the filtered,
    Korean titles - not raw NewsItem.title values. The weekly pipeline narrates no
    news and was overlooked for exactly that reason."""
    module = importlib.import_module(module_name)
    monkeypatch.chdir(tmp_path)

    module.run()

    pkg = mocked_externals["video"].call_args.args[0]
    assert pkg.news_headlines == ["테스트 호재 기사"], pkg.news_headlines


@pytest.mark.parametrize("module_name", ["run_aftermarket", "run_weekly_review"])
def test_content_package_survives_json_serialisation(
    module_name, mocked_externals, tmp_path, monkeypatch
):
    """Whatever the pipeline puts on ContentPackage has to reach Remotion as JSON.

    The render is mocked in every pipeline test, so `json.dumps(props)` inside
    generate_short_video never runs here - which is how a DirectionResult ended up in
    ContentPackage.direction and broke three consecutive aftermarket runs while CI
    stayed green. This drives the real props builder over the real package.
    """
    from stock_snap.media.short_video import build_render_props

    module = importlib.import_module(module_name)
    monkeypatch.chdir(tmp_path)

    module.run()

    pkg = mocked_externals["video"].call_args.args[0]
    assert isinstance(pkg.direction, str), f"direction is {type(pkg.direction).__name__}"
    json.dumps(build_render_props(pkg))


@pytest.mark.parametrize("module_name", PUBLISHING_PIPELINES)
def test_every_pipeline_defines_dry_run(module_name):
    """Every pipeline the workflows pass DRY_RUN to must actually read it.

    All three workflows set DRY_RUN in the pipeline step, but for a long time only
    run_live_short.py defined it. `gh workflow run ... -f dry_run=true` on the other two
    published to Instagram for real, which is not a failure mode anyone would guess from
    reading the workflow file.
    """
    module = importlib.import_module(module_name)
    assert hasattr(module, "DRY_RUN"), f"{module_name} ignores the DRY_RUN env var"


@pytest.mark.parametrize("module_name", ["run_aftermarket", "run_weekly_review"])
def test_render_gets_audio_bgm_and_real_timing(
    module_name, mocked_externals, tmp_path, monkeypatch
):
    """The render must receive narration, music, and a duration derived from the TTS.

    All three were missing. The aftermarket script had 3 segments against a 5-scene
    composition, so the template's `audioSegments.length === 5` check fell through to an
    audioPath nobody set and rendered a silent video: measured at -91 dB, and one of
    those went out to Instagram. Neither pipeline passed bgm_path, and neither passed
    total_frames, so both were pinned to the 1350-frame (45s) default regardless of how
    long the narration actually ran.
    """
    module = importlib.import_module(module_name)
    monkeypatch.chdir(tmp_path)

    module.run()

    call = mocked_externals["video"].call_args
    segments = call.kwargs.get("audio_segment_paths") or []
    single = call.args[2] if len(call.args) > 2 else None
    assert segments or single, "render was given no audio at all — the video would be silent"
    if segments:
        assert len(segments) == 5, (
            f"{len(segments)} segments against a 5-scene composition; "
            "the template maps segment i onto scene i"
        )

    assert call.kwargs.get("bgm_path") is not None, "no background music"
    assert call.kwargs.get("total_frames") != 1350, "duration left at the fixed default"
    assert len(call.kwargs.get("scene_durations") or []) == 5


@pytest.mark.parametrize("module_name", ["run_aftermarket", "run_weekly_review"])
def test_dry_run_publishes_nothing(module_name, mocked_externals, tmp_path, monkeypatch):
    """Under DRY_RUN the pipeline still renders, but must not post anywhere."""
    module = importlib.import_module(module_name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "DRY_RUN", True)

    module.run()

    assert mocked_externals["video"].called, "dry run should still render"
    mocked_externals["reel"].assert_not_called()
    # YouTube is told rather than skipped, so assert it was told.
    assert mocked_externals["short"].call_args.kwargs.get("dry_run") is True
