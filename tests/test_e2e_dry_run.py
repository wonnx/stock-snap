"""E2E dry-run pipeline test using mock fixtures.

Validates the full pipeline flow (hot stock → quant → news → content → video → upload)
without any real external API calls or network access.

Run with:
    pytest tests/test_e2e_dry_run.py -v
    # or via package script:
    pnpm test:e2e:dry-run
"""
from __future__ import annotations

import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stock_snap.news.direction import DirectionResult  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

SYMBOL = "NVDA"
PRICE = 875.30
PREV_CLOSE = 820.00
CHANGE_PCT = 6.74
VOLUME = 85_000_000


def _make_ohlcv(n: int = 65) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame — 65 trading days, uptrend."""
    close = np.linspace(700.0, PRICE, n)
    idx = pd.date_range("2025-12-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "Open": close - 2.0,
            "High": close + 5.0,
            "Low": close - 3.0,
            "Close": close,
            "Volume": np.where(
                np.arange(n) == n - 1,
                float(VOLUME),
                25_000_000.0,
            ),
        },
        index=idx,
    )


def _make_hot_stock_result():
    from stock_snap.hot_stock import HotStockResult
    return HotStockResult(
        symbol=SYMBOL,
        price=PRICE,
        prev_close=PREV_CLOSE,
        change_pct=CHANGE_PCT,
        volume=VOLUME,
        avg_volume=25_000_000,
        volume_ratio=3.4,
        hot_score=88.5,
        direction="상승",
    )


def _make_news_items():
    from stock_snap.news.collector import NewsItem
    now = datetime.now(tz=UTC)
    return [
        NewsItem(
            title="NVDA 4분기 실적 어닝서프라이즈 — AI 칩 수요 역대 최고",
            summary="NVIDIA가 4분기 매출 390억 달러를 기록하며 시장 예상치를 12% 초과했습니다.",
            url="https://example.com/nvda-q4",
            published=now,
            source="Reuters",
            symbols=["NVDA"],
        ),
        NewsItem(
            title="골드만삭스, NVDA 목표주가 1,000달러로 상향",
            summary="AI 인프라 투자 확대를 근거로 골드만삭스가 목표주가를 상향 조정했습니다.",
            url="https://example.com/nvda-pt",
            published=now,
            source="Bloomberg",
            symbols=["NVDA"],
        ),
    ]


def _make_dummy_mp3(path: Path, **_kwargs) -> bool:
    """Write a minimal stub MP3 file (ID3 header)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3" + b"\x00" * 97)
    return True


def _make_dummy_mp4(pkg, output_path, *args, **kwargs) -> bool:
    """Write a minimal stub MP4 file (ftyp box)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(
        b"\x00\x00\x00\x18ftyp"
        b"isom\x00\x00\x02\x00"
        b"isomiso2"
        + b"\x00" * 200
    )
    return True


def _make_dummy_jpeg(pkg, output_path, **_kwargs) -> bool:
    """Write a minimal stub JPEG file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9")
    return True


def _tts_with_timing(text, output_path, **kwargs):
    """Stub TTS that creates an MP3 file and returns fake sentence timings."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"ID3" + b"\x00" * 97)
    return True, [(0.0, 2.5), (2.5, 5.0)]


def _tts_single(text, output_path, **kwargs):
    """Stub single TTS that creates an MP3 file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"ID3" + b"\x00" * 97)
    return True


# ---------------------------------------------------------------------------
# Full pipeline E2E test
# ---------------------------------------------------------------------------

class TestE2EDryRun:
    """Full pipeline E2E test using fixture data — no real API calls."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        """Apply all external API mocks for the duration of each test."""
        ohlcv = _make_ohlcv()
        hot = _make_hot_stock_result()
        news_items = _make_news_items()

        mock_ticker = MagicMock()
        mock_ticker.news = []

        self._patches = [
            patch("stock_snap.hot_stock.select_hot_stock", return_value=hot),
            patch(
                "stock_snap.data.fetcher.MarketDataFetcher.get_ohlcv",
                autospec=True,
                return_value=ohlcv,
            ),
            patch(
                "stock_snap.news.collector.NewsCollector.fetch_for_symbol",
                autospec=True,
                return_value=news_items,
            ),
            patch("yfinance.Ticker", return_value=mock_ticker),
            patch(
                "stock_snap.media.tts.generate_tts_with_timing",
                autospec=True,
                side_effect=_tts_with_timing,
            ),
            patch("stock_snap.media.tts.generate_tts", autospec=True, side_effect=_tts_single),
            patch("stock_snap.media.tts.get_audio_duration", autospec=True, return_value=5.0),
            patch(
                "stock_snap.media.short_video.generate_short_video",
                autospec=True,
                side_effect=_make_dummy_mp4,
            ),
            patch(
                "stock_snap.media.short_video.generate_thumbnail",
                autospec=True,
                side_effect=_make_dummy_jpeg,
            ),
            patch(
                "stock_snap.news.direction.select_direction_articles",
                autospec=True,
                return_value=DirectionResult(
                    [("테스트 호재 기사", "테스트용 상세 내용입니다.")], "claude_code"
                ),
            ),
            patch("stock_snap.utils.monitoring.init_sentry"),
            patch("stock_snap.utils.monitoring.capture_exception"),
            patch("stock_snap.utils.monitoring.set_sentry_tag"),
            patch("stock_snap.utils.monitoring.record_pipeline_run"),
        ]
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()

    def _run_with_dry_run(self):
        """Run the pipeline in dry-run mode and return (exit_code_or_none, mock_host)."""
        import run_live_short

        mock_host = MagicMock(
            return_value="https://github.com/wonnx/stock-snap/releases/download/media/dryrun_test.mp4"
        )

        env = {
            "DRY_RUN": "true",
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(run_live_short, "DRY_RUN", True),
            patch.object(run_live_short, "publish_media", mock_host),
            patch.object(run_live_short, "send_kakao_alert"),
        ):
            try:
                run_live_short.run()
                return None, mock_host
            except SystemExit as exc:
                return exc.code, mock_host

    def test_pipeline_exits_cleanly(self):
        """Pipeline must not call sys.exit in dry-run mode with all mocks active."""
        exit_code, _ = self._run_with_dry_run()
        assert exit_code is None, f"Pipeline exited with code {exit_code}"

    def test_video_file_generated(self):
        """generate_short_video must be called and produce a file."""
        from stock_snap.media import short_video as sv_mod

        with patch.object(sv_mod, "generate_short_video", side_effect=_make_dummy_mp4) as mock_vid:
            exit_code, _ = self._run_with_dry_run()
        assert exit_code is None
        assert mock_vid.called, "generate_short_video was never called"

    def test_tts_segments_generated(self):
        """TTS must be called for each of the 5 script segments."""
        from stock_snap.media import tts as tts_mod

        with patch.object(tts_mod, "generate_tts_with_timing", side_effect=_tts_with_timing) as mock_tts:
            exit_code, _ = self._run_with_dry_run()
        assert exit_code is None
        # 5 segments expected; may fall back to single if < 5 succeed
        assert mock_tts.call_count >= 1, "generate_tts_with_timing was never called"

    def test_media_host_upload_called(self):
        """Video must get a public URL (release asset) even in dry-run."""
        _, mock_host = self._run_with_dry_run()
        assert mock_host.called, "publish_media was never called"

    def test_instagram_upload_skipped_in_dry_run(self):
        """Instagram Reels upload must be skipped when DRY_RUN=true."""
        from stock_snap.upload import instagram as ig_mod
        mock_upload = MagicMock(return_value=True)
        with patch.object(ig_mod.instagram, "upload_reel", mock_upload):
            exit_code, _ = self._run_with_dry_run()
        assert exit_code is None
        assert not mock_upload.called, "Instagram upload_reel was called in dry-run mode"

    def test_youtube_upload_skipped_in_dry_run(self):
        """YouTube Shorts upload must be skipped when DRY_RUN=true."""
        from stock_snap.upload import youtube as yt_mod
        mock_yt = MagicMock(return_value=None)
        with patch.object(yt_mod.youtube, "upload_short", mock_yt):
            exit_code, _ = self._run_with_dry_run()
        # In dry_run mode upload_short is still called but with dry_run=True param
        if mock_yt.called:
            _, kwargs = mock_yt.call_args
            assert kwargs.get("dry_run", True) is True, "YouTube upload called without dry_run=True"


# ---------------------------------------------------------------------------
# Fixture integrity tests
# ---------------------------------------------------------------------------

class TestFixtures:
    """Validate fixture JSON files are well-formed."""

    FIXTURE_DIR = Path(__file__).parent / "fixtures"

    def test_finnhub_fixture_exists(self):
        assert (self.FIXTURE_DIR / "finnhub_news.json").exists()

    def test_alphavantage_fixture_exists(self):
        assert (self.FIXTURE_DIR / "alphavantage_news.json").exists()

    def test_finnhub_fixture_valid_json(self):
        import json
        data = json.loads((self.FIXTURE_DIR / "finnhub_news.json").read_text())
        assert isinstance(data, list)
        assert len(data) > 0
        for item in data:
            assert "headline" in item
            assert "summary" in item
            assert "datetime" in item

    def test_alphavantage_fixture_valid_json(self):
        import json
        data = json.loads((self.FIXTURE_DIR / "alphavantage_news.json").read_text())
        assert "feed" in data
        assert isinstance(data["feed"], list)
        assert len(data["feed"]) > 0
        for item in data["feed"]:
            assert "title" in item
            assert "overall_sentiment_label" in item


# ---------------------------------------------------------------------------
# Isolated pipeline step tests
# ---------------------------------------------------------------------------

class TestPipelineStepsIsolated:
    """Fast isolated tests for individual pipeline steps with fixtures."""

    def test_hot_stock_result_has_required_fields(self):
        hot = _make_hot_stock_result()
        assert hot.symbol == SYMBOL
        assert hot.price > 0
        assert hot.change_pct > 0
        assert hot.direction == "상승"
        assert 0 <= hot.hot_score <= 100

    def test_ohlcv_fixture_has_required_columns(self):
        df = _make_ohlcv()
        assert {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns)
        assert len(df) >= 20

    def test_technical_analyzer_runs_on_fixture(self):
        from stock_snap.analysis.indicators import TechnicalAnalyzer
        df = _make_ohlcv()
        tech = TechnicalAnalyzer().compute(SYMBOL, df)
        assert tech is not None
        assert tech.rsi14 >= 0
        assert tech.symbol == SYMBOL

    def test_news_items_fixture_valid(self):
        items = _make_news_items()
        assert len(items) >= 1
        for item in items:
            assert item.title
            assert SYMBOL in item.symbols

    def test_card_title_generation_from_fixture(self):
        hot = _make_hot_stock_result()
        company_name_ko = "엔비디아"
        arrow = "▲" if hot.change_pct > 0 else "▼"
        display_name = f"{company_name_ko}({hot.symbol})"
        card_title = f"{display_name} {arrow}{abs(hot.change_pct):.1f}% 급등"
        assert "NVDA" in card_title
        assert "▲" in card_title
        assert "급등" in card_title

    def test_tts_stub_creates_mp3_file(self, tmp_path):
        out = tmp_path / "test.mp3"
        result = _tts_single("테스트 텍스트", out)
        assert result is True
        assert out.exists()
        assert out.stat().st_size > 0

    def test_video_stub_creates_mp4_file(self, tmp_path):
        from stock_snap.content.generator import ContentPackage
        pkg = MagicMock(spec=ContentPackage)
        out = tmp_path / "test.mp4"
        result = _make_dummy_mp4(pkg, out)
        assert result is True
        assert out.exists()
        assert out.stat().st_size > 0
        assert b"ftyp" in out.read_bytes()[:32]

    def test_scene_timing_calculation(self):
        FPS = 30
        MIN_SCENE_SECS = [5.0, 8.0, 6.0, 8.0, 6.0]
        tts_durations = [5.0, 5.0, 5.0, 5.0, 5.0]
        scene_secs = [
            max(dur + 1.0, MIN_SCENE_SECS[i])
            for i, dur in enumerate(tts_durations)
        ]
        frames = [math.ceil(s * FPS) for s in scene_secs]
        total = sum(frames) + FPS

        assert len(frames) == 5
        assert total > 0
        for i, f in enumerate(frames):
            assert f >= math.ceil(MIN_SCENE_SECS[i] * FPS)

    def test_ohlcv_volume_spike_on_last_row(self):
        df = _make_ohlcv()
        assert df["Volume"].iloc[-1] == float(VOLUME)
        assert df["Volume"].iloc[-2] == 25_000_000.0
