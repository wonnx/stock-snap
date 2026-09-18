"""Short-form video generation via Remotion."""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from stock_snap.content.generator import ContentPackage

logger = logging.getLogger(__name__)
REMOTION_DIR = Path(__file__).parent.parent.parent.parent / "remotion"

# Render wall-clock budget. The composition renders at --scale 2 (2160x3840) with
# crf 10, which a 2-core CI runner chews through far more slowly than a dev laptop,
# so the ceiling is generous and overridable per environment.
RENDER_TIMEOUT_SECS = int(os.getenv("REMOTION_RENDER_TIMEOUT", "1800"))
STILL_TIMEOUT_SECS = int(os.getenv("REMOTION_STILL_TIMEOUT", "600"))


FPS = 30
SCENE_COUNT = 5
# Floor per scene, so a short narration still leaves the visuals on screen long enough
# to read. Index order is hero, news, chart, indicators, conclusion.
MIN_SCENE_SECS = [5.0, 8.0, 6.0, 8.0, 6.0]
FALLBACK_SCENE_SECS = [5.0, 12.0, 9.0, 11.0, 8.0]


def resolve_bgm_path() -> Path | None:
    """Locate the background music track.

    output/bgm_v2 is the local sample library and is not tracked; remotion/public holds
    the committed copy so CI runners get music too.
    """
    repo_root = REMOTION_DIR.parent
    candidates = [
        repo_root / "output" / "bgm_v2" / "04_moonlight.mp3",
        repo_root / "remotion" / "public" / "04_moonlight.mp3",
    ]
    found = next((c for c in candidates if c.exists()), None)
    if found:
        logger.info("BGM: using Moonlight (%s)", found)
    else:
        logger.warning(
            "BGM file not found in %s — video will have no background music",
            [str(c) for c in candidates],
        )
    return found


def scene_timing(segment_paths: list[Path] | None) -> tuple[list[int], int]:
    """Derive per-scene frame counts from the measured narration length.

    Returns (scene_durations, total_frames). Without one audio file per scene the
    composition falls back to fixed lengths, which is what every pipeline except
    run_live_short.py used to do unconditionally — leaving a 60s script inside a 45s
    video.
    """
    from stock_snap.media.tts import get_audio_duration

    usable = bool(segment_paths) and len(segment_paths) == SCENE_COUNT and all(segment_paths)
    if usable:
        scene_secs = []
        for i, seg in enumerate(segment_paths):
            measured = get_audio_duration(seg)
            measured = measured if measured > 0 else MIN_SCENE_SECS[i]
            scene_secs.append(max(measured + 1.0, MIN_SCENE_SECS[i]))  # +1s buffer
    else:
        scene_secs = list(FALLBACK_SCENE_SECS)

    scene_frames = [math.ceil(s * FPS) for s in scene_secs]
    total_frames = sum(scene_frames) + FPS  # +1s tail
    logger.info(
        "Video timing: scenes=%s total=%.1fs (%d frames)",
        [f"{s:.1f}s" for s in scene_secs], total_frames / FPS, total_frames,
    )
    return scene_frames, total_frames


def _tail(stream: str | bytes | None, limit: int = 500) -> str:
    """Last *limit* characters of a captured stream, for timeout diagnostics."""
    if not stream:
        return "(no output captured)"
    if isinstance(stream, bytes):
        stream = stream.decode("utf-8", errors="replace")
    return stream[-limit:]


def build_render_props(
    pkg: ContentPackage,
    *,
    audio_prop: str = "",
    audio_segment_names: list[str] | None = None,
    script_segments: list[str] | None = None,
    bgm_name: str = "",
    total_frames: int = 1350,
    scene_durations: list[int] | None = None,
    subtitle_timings: list[list[tuple[float, float]]] | None = None,
) -> dict:
    """Props handed to the Remotion composition.

    Split out of generate_short_video so the JSON boundary can be tested without
    running a render. A pipeline that puts a non-primitive on ContentPackage fails
    here at json.dumps, and every pipeline test mocks the render, so nothing else
    exercises it.
    """
    audio_segment_names = audio_segment_names or []
    return {
        "symbol": pkg.symbol,
        "price": pkg.price,
        "changePct": pkg.change_pct,
        "direction": pkg.direction,
        "cardTitle": pkg.card_title,
        "cardSubtitle": pkg.card_subtitle,
        "script": pkg.script,
        "audioPath": audio_prop,
        # Quant analysis data
        "rsi": pkg.rsi,
        "macd": pkg.macd,
        "macdSignal": pkg.macd_signal,
        "volumeRatio": pkg.volume_ratio,
        "bbPosition": pkg.bb_position,
        "emaTrend": pkg.ema_trend,
        "quantSummary": pkg.quant_summary,
        "chartData": pkg.chart_data,
        "companyNameKo": getattr(pkg, "company_name_ko", ""),
        "newsHeadlines": getattr(pkg, "news_headlines", []),
        # Scene-synced audio and subtitles
        "audioSegments": audio_segment_names,
        "scriptSegments": script_segments or [],
        # BGM
        "bgmPath": bgm_name,
        # Dynamic duration
        "totalFrames": total_frames,
        "sceneDurations": scene_durations or [],
        # Subtitle timing data: [segment][sentence][start_sec, end_sec]
        "subtitleTimings": [
            [[s, e] for s, e in seg] for seg in subtitle_timings
        ] if subtitle_timings else [],
    }


def generate_thumbnail(pkg: ContentPackage, output_path: Path) -> bool:
    """Render a still thumbnail image via Remotion CLI. Returns True on success."""
    if not REMOTION_DIR.exists():
        logger.error("Remotion project not found at %s", REMOTION_DIR)
        return False

    # See generate_short_video: the CLI runs from remotion/, so this has to be absolute.
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    props = {
        "symbol": pkg.symbol,
        "price": pkg.price,
        "changePct": pkg.change_pct,
        "cardTitle": pkg.card_title,
        "cardSubtitle": pkg.card_subtitle,
        "rsi": pkg.rsi,
        "macd": pkg.macd,
        "macdSignal": pkg.macd_signal,
        "volumeRatio": pkg.volume_ratio,
        "bbPosition": pkg.bb_position,
        "emaTrend": pkg.ema_trend,
        "chartData": pkg.chart_data,
        "companyNameKo": getattr(pkg, "company_name_ko", ""),
    }

    cmd = [
        "npx", "remotion", "still",
        "StockThumbnail",
        str(output_path),
        "--props", json.dumps(props),
        "--image-format", "jpeg",
        "--jpeg-quality", "92",
        "--scale", "2",
        "--height", "1920",
        "--width", "1080",
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(REMOTION_DIR),
            capture_output=True,
            text=True,
            timeout=STILL_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            logger.error("Remotion still render failed: %s", result.stderr[-500:])
            return False
        return output_path.exists()
    except FileNotFoundError:
        logger.error("npx not found — install Node.js")
        return False
    except subprocess.TimeoutExpired as e:
        logger.error(
            "Remotion still render timed out after %ds; last output: %s",
            STILL_TIMEOUT_SECS, _tail(e.stderr),
        )
        return False


def generate_short_video(
    pkg: ContentPackage,
    output_path: Path,
    audio_path: Path | None = None,
    audio_segment_paths: list[Path] | None = None,
    script_segments: list[str] | None = None,
    bgm_path: Path | None = None,
    total_frames: int = 1350,
    scene_durations: list[int] | None = None,
    subtitle_timings: list[list[tuple[float, float]]] | None = None,
) -> bool:
    """Render a short-form video via Remotion CLI. Returns True on success."""
    if not REMOTION_DIR.exists():
        logger.error("Remotion project not found at %s", REMOTION_DIR)
        return False

    # The CLI runs with cwd=REMOTION_DIR, so a relative output path would be written
    # under remotion/ while the existence check below looks somewhere else entirely.
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    public_dir = REMOTION_DIR / "public"
    public_dir.mkdir(exist_ok=True)

    # Copy audio segment files to Remotion public dir
    audio_segment_names: list[str] = []
    if audio_segment_paths:
        for seg_path in audio_segment_paths:
            # A failed TTS segment arrives as None; Path(None) would raise.
            if seg_path and Path(seg_path).exists():
                dest = public_dir / Path(seg_path).name
                shutil.copy2(seg_path, dest)
                audio_segment_names.append(Path(seg_path).name)
                logger.info("Copied audio segment to remotion/public/%s", dest.name)

    # Fallback: single audio file
    audio_prop = ""
    if not audio_segment_names and audio_path and Path(audio_path).exists():
        dest = public_dir / Path(audio_path).name
        shutil.copy2(audio_path, dest)
        audio_prop = Path(audio_path).name
        logger.info("Copied audio to remotion/public/%s", audio_prop)

    # Copy BGM file to Remotion public dir
    bgm_name = ""
    if bgm_path and Path(bgm_path).exists():
        src = Path(bgm_path).resolve()
        dest = (public_dir / src.name).resolve()
        bgm_name = src.name
        if src == dest:
            # BGM already lives in remotion/public (committed copy) — nothing to do
            logger.info("BGM already in remotion/public/%s", bgm_name)
        else:
            shutil.copy2(src, dest)
            logger.info("Copied BGM to remotion/public/%s", bgm_name)

    props = build_render_props(
        pkg,
        audio_prop=audio_prop,
        audio_segment_names=audio_segment_names,
        script_segments=script_segments,
        bgm_name=bgm_name,
        total_frames=total_frames,
        scene_durations=scene_durations,
        subtitle_timings=subtitle_timings,
    )


    cmd = [
        "npx", "remotion", "render",
        "StockShort",
        str(output_path),
        "--props", json.dumps(props),
        "--codec", "h264",
        "--crf", "10",
        "--scale", "2",
        "--height", "1920",
        "--width", "1080",
        "--image-format", "png",
        "--enforce-audio-track",
    ]

    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(REMOTION_DIR),
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            logger.error("Remotion render failed: %s", result.stderr[-500:])
            return False
        logger.info("Remotion render finished in %.1fs", time.monotonic() - started)
        return output_path.exists()
    except FileNotFoundError:
        logger.error("npx not found — install Node.js")
        return False
    except subprocess.TimeoutExpired as e:
        logger.error(
            "Remotion render timed out after %ds; last output: %s",
            RENDER_TIMEOUT_SECS, _tail(e.stderr),
        )
        return False
