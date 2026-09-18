"""Generate a short-form video from live market data and upload to Instagram Reels + YouTube Shorts."""
import os
import sys
import logging
import time
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Dry-run: skip actual uploads (set DRY_RUN=true env var or pass --dry-run flag)
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes") or "--dry-run" in sys.argv

# Initialize Sentry early (no-op if SENTRY_DSN not set)
from stock_snap.utils.monitoring import init_sentry, capture_exception, set_sentry_tag, record_pipeline_run
from stock_snap.utils.retry import with_retry
from stock_snap.upload.media_host import publish_media
init_sentry()


def send_kakao_alert(text: str) -> None:
    """Send a Kakao 'Send to Me' message (best-effort, no exception raised)."""
    try:
        from stock_snap.alerts.kakao import alerter
        alerter.send_text(text)
    except Exception as e:
        logger.warning("Kakao alert failed: %s", e)


def run():
    _pipeline_start = time.monotonic()

    from stock_snap.hot_stock import select_hot_stock
    from stock_snap.data.fetcher import fetcher
    from stock_snap.analysis.indicators import TechnicalAnalyzer
    from stock_snap.news.collector import NewsCollector
    from stock_snap.content.generator import ContentPackage
    from stock_snap.media.short_video import generate_short_video
    from stock_snap.upload.instagram import instagram
    from stock_snap.upload.youtube import youtube

    # 1. Hot stock selection
    logger.info("Selecting hottest stock...")
    hot = select_hot_stock()
    if not hot:
        msg = "[Stock Snap] 파이프라인 실패: 핫 종목 선정 실패"
        logger.error("Hot stock selection failed")
        send_kakao_alert(msg)
        record_pipeline_run(
            status="failure",
            error="hot stock selection returned None",
            duration_secs=time.monotonic() - _pipeline_start,
            run_id=os.getenv("GITHUB_RUN_ID", ""),
        )
        sys.exit(1)

    symbol = hot.symbol
    logger.info("Selected: %s %+.2f%% (score %.1f)", symbol, hot.change_pct, hot.hot_score)
    set_sentry_tag("symbol", symbol)

    # Korean company name lookup
    COMPANY_NAMES_KO = {
        "AAPL": "애플", "MSFT": "마이크로소프트", "GOOGL": "구글", "GOOG": "구글",
        "AMZN": "아마존", "META": "메타", "TSLA": "테슬라", "NVDA": "엔비디아",
        "NFLX": "넷플릭스", "AMD": "AMD", "INTC": "인텔", "CRM": "세일즈포스",
        "ORCL": "오라클", "ADBE": "어도비", "CSCO": "시스코", "QCOM": "퀄컴",
        "AVGO": "브로드컴", "TXN": "텍사스인스트루먼트", "BNTX": "바이오엔텍",
        "MRNA": "모더나", "PFE": "화이자", "JNJ": "존슨앤존슨", "UNH": "유나이티드헬스",
        "V": "비자", "MA": "마스터카드", "JPM": "JP모건", "BAC": "뱅크오브아메리카",
        "GS": "골드만삭스", "WMT": "월마트", "COST": "코스트코", "HD": "홈디포",
        "DIS": "디즈니", "PYPL": "페이팔", "SQ": "블록", "COIN": "코인베이스",
        "PLTR": "팔란티어", "UBER": "우버", "ABNB": "에어비앤비", "SNAP": "스냅",
        "SHOP": "쇼피파이", "ROKU": "로쿠", "ZM": "줌", "DDOG": "데이터독",
        "SNOW": "스노우플레이크", "NET": "클라우드플레어", "CRWD": "크라우드스트라이크",
        "MU": "마이크론", "MRVL": "마벨", "SMCI": "슈퍼마이크로",
        "ARM": "ARM홀딩스", "MSTR": "마이크로스트래티지", "SOFI": "소파이",
        "NIO": "니오", "RIVN": "리비안", "LCID": "루시드", "LI": "리오토",
        "BA": "보잉", "CAT": "캐터필러", "XOM": "엑슨모빌", "CVX": "셰브론",
    }
    company_name_ko = COMPANY_NAMES_KO.get(symbol, "")

    # 2. Quant analysis
    logger.info("Running quant analysis...")
    df = fetcher.get_ohlcv(symbol, period="3mo", interval="1d")
    analyzer = TechnicalAnalyzer()
    tech = analyzer.compute(symbol, df)
    chart_data = [round(float(v), 2) for v in df["Close"].iloc[-20:].tolist()]

    price = hot.price
    change_pct = hot.change_pct
    rsi = tech.rsi14
    macd = tech.macd
    macd_signal = tech.macd_signal
    bb_pos = tech.bb_position
    ema = tech.ema_trend
    vol_ratio = hot.volume_ratio

    logger.info("RSI=%.1f, MACD=%.3f, BB=%s, EMA=%s", rsi, macd, bb_pos, ema)

    # 3. News collection — multi-source + LLM direction validation
    logger.info("Collecting news from multiple sources...")
    import re
    import yfinance as yf

    raw_news: list[tuple[str, str]] = []  # (title, detail) pairs

    # Source A: yfinance
    try:
        ticker_obj = yf.Ticker(symbol)
        yf_news = ticker_obj.news or []
        for n in yf_news[:10]:
            content = n.get("content", {})
            title = content.get("title", "").strip()
            if not title:
                continue
            desc_html = content.get("description", "")
            desc = re.sub(r"<[^>]+>", "", desc_html).strip()
            raw_news.append((title, desc[:300] if desc else ""))
        logger.info("yfinance news: %d articles", len(raw_news))
    except Exception as e:
        logger.warning("yfinance news fetch failed: %s", e)

    # Source B: NewsCollector (Finnhub + Alpha Vantage + RSS)
    try:
        collector = NewsCollector(lookback_hours=48)
        collected = collector.fetch_for_symbol(symbol, max_items=8)
        existing_titles = {t.lower() for t, _ in raw_news}
        added = 0
        for item in collected:
            if item.title.lower() not in existing_titles:
                raw_news.append((item.title, item.summary[:300] if item.summary else ""))
                existing_titles.add(item.title.lower())
                added += 1
        logger.info("NewsCollector added %d new articles (total=%d)", added, len(raw_news))
    except Exception as e:
        logger.warning("NewsCollector fetch failed: %s", e)

    # LLM direction analysis — shared backend (Claude Code CLI on CI, API if keyed).
    # Anything it does not return is not narrated: an unfiltered headline read as "the
    # cause" is worse than saying no cause was established.
    from stock_snap.news.direction import select_direction_articles

    news_filter = select_direction_articles(raw_news, symbol, change_pct)
    if news_filter.degraded:
        logger.warning("news filter degraded: %s", ", ".join(news_filter.degraded))

    news_headlines = []
    for title_ko, detail_ko in news_filter.articles:
        if detail_ko and len(detail_ko) > 10:
            news_headlines.append(f"{title_ko}\n{detail_ko[:200]}")
        else:
            news_headlines.append(title_ko)

    logger.info(
        "Final news headlines: %d items (backend=%s)", len(news_headlines), news_filter.backend
    )

    # 4. Template-based content generation (no ANTHROPIC_API_KEY needed)
    direction = hot.direction
    sign = "+" if change_pct > 0 else "-"
    arrow = "▲" if change_pct > 0 else "▼"
    rsi_label = "overbought" if rsi >= 70 else ("oversold" if rsi <= 30 else "neutral")
    macd_label = "golden cross" if macd > macd_signal else "death cross"

    rsi_label_ko = "과매수" if rsi >= 70 else ("과매도" if rsi <= 30 else "중립")
    macd_label_ko = "골든크로스" if macd > macd_signal else "데드크로스"
    bb_label_ko = "상단 돌파" if bb_pos == "upper" else ("하단 지지" if bb_pos == "lower" else "중간대")
    ema_label_ko = "상승추세" if ema == "bullish" else ("하락추세" if ema == "bearish" else "혼조")

    display_name = f"{company_name_ko}({symbol})" if company_name_ko else symbol
    card_title = f"{display_name} {arrow}{abs(change_pct):.1f}% {'급등' if change_pct > 0 else '급락'}"
    card_subtitle = f"RSI {rsi_label_ko}({rsi:.0f}) | MACD {macd_label_ko}"
    card_body = (
        f"현재가: ${price:,.2f}\n"
        f"변동: {arrow} {abs(change_pct):.2f}%\n"
        f"거래량: 평균 대비 {vol_ratio:.1f}배"
    )

    tts_name = company_name_ko if company_name_ko else symbol

    # Build TTS script segments — natural sentence narration style
    # S1 Hero: 종목 소개 + 가격 + 등락률
    seg_hero = (
        f"오늘은 {tts_name} 종목을 분석합니다. "
        f"현재 {tts_name}의 주가는 {price:,.2f}달러이며, "
        f"전일 대비 {abs(change_pct):.2f}퍼센트 {'상승했습니다' if change_pct > 0 else '하락했습니다'}. "
        f"EMA 기준으로 {'상승 추세가 이어지고 있습니다' if ema == 'bullish' else '하락 추세에 있습니다' if ema == 'bearish' else '혼조 양상을 보이고 있습니다'}."
    )

    # S2 News: 왜 급등/급락했는지 자연스러운 문장으로
    if news_headlines:
        intro = f"{tts_name} 주가가 {'급등' if change_pct > 0 else '급락'}한 주요 배경을 살펴보겠습니다. "
        items = []
        for i, h in enumerate(news_headlines[:3]):
            parts = h.split("\n")
            title = parts[0]
            detail = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
            prefix = ["첫째로", "둘째로", "셋째로"][i]
            items.append(f"{prefix}, {title}.")
        seg_news = intro + " ".join(items)
    else:
        # No article survived the filter (or the filter could not run). Say so; do not
        # manufacture a market-sentiment story the data does not support.
        seg_news = (
            f"{tts_name}의 {'급등' if change_pct > 0 else '급락'}을 설명하는 "
            f"확인된 회사 관련 뉴스는 없습니다. 차트와 지표를 중심으로 살펴보겠습니다."
        )

    # S3 Chart: 차트 추이 자연스럽게
    if chart_data and len(chart_data) >= 2:
        pct_20d = ((chart_data[-1] / chart_data[0]) - 1) * 100
        trend = "강한 상승추세" if pct_20d > 5 else "완만한 상승 흐름" if pct_20d > 0 else "완만한 하락 흐름" if pct_20d > -5 else "가파른 하락세"
        seg_chart = (
            f"최근 20거래일간의 주가 흐름을 살펴보겠습니다. "
            f"20일 전 {chart_data[0]:.2f}달러에서 현재 {chart_data[-1]:.2f}달러로, "
            f"{trend}를 보이고 있으며 총 {abs(pct_20d):.1f}퍼센트 {'상승했습니다' if pct_20d > 0 else '하락했습니다'}. "
            f"{'매수세가 꾸준히 우위를 보이고 있습니다.' if pct_20d > 5 else '점진적으로 회복 중입니다.' if pct_20d > 0 else '지지선 이탈 여부를 주목해야 합니다.' if pct_20d > -5 else '큰 폭의 조정이 진행 중입니다.'}"
        )
    else:
        seg_chart = "최근 20거래일간의 차트 추이를 분석합니다. 가격 변동을 통해 추세를 확인하시기 바랍니다."

    # S4 Indicators: 지표 해석 자연스럽게
    seg_indicators = (
        f"핵심 기술적 지표를 살펴보겠습니다. "
        f"RSI는 {rsi:.0f}로 {rsi_label_ko} 구간에 위치해 있으며, "
        f"{'과매수 상태로 조정 가능성에 유의하세요' if rsi >= 70 else '과매도 상태로 반등 가능성이 있습니다' if rsi <= 30 else '중립 구간으로 극단적 신호는 없습니다'}. "
        f"MACD는 {'골든크로스를 기록하며 상승 모멘텀을 시사하고 있습니다' if macd > macd_signal else '데드크로스로 하방 압력이 지속되고 있습니다'}. "
        f"볼린저밴드는 {bb_label_ko}이며, 거래량은 평균 대비 {vol_ratio:.1f}배를 기록했습니다."
    )

    # S5 Conclusion: 결론 자연스럽게
    seg_conclusion = (
        f"오늘의 분석을 정리하겠습니다. "
        f"{card_title}입니다. "
        f"RSI {rsi_label_ko}에 MACD {macd_label_ko}로, "
        f"{'상승 모멘텀이 유지되고 있으나 신중한 접근을 권합니다' if change_pct > 0 else '하방 압력이 지속되고 있어 지지선 확인이 중요합니다'}. "
        f"스톡스냅을 팔로우하시면 매일 핵심 종목 분석을 받아보실 수 있습니다."
    )

    script_segments = [seg_hero, seg_news, seg_chart, seg_indicators, seg_conclusion]
    script = " ".join(script_segments)

    news_summary_items = []
    for h in news_headlines[:3]:
        parts = h.split("\n")
        title_part = parts[0]
        detail_part = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
        if detail_part:
            # 문장 단위로 자르기 (120자 초과 시 마침표/온점 기준으로 잘라 완결된 문장 유지)
            if len(detail_part) > 120:
                cutoff = detail_part.rfind(".", 0, 120)
                detail_part = detail_part[: cutoff + 1] if cutoff > 0 else detail_part[:120]
            news_summary_items.append(f"• {title_part}\n  → {detail_part}")
        else:
            news_summary_items.append(f"• {title_part}")
    news_summary = "\n".join(news_summary_items) if news_summary_items else ""
    rsi_outlook = (
        "RSI 과매도 접근 — 반등 가능 구간입니다." if rsi <= 35
        else ("RSI 과매수 영역 — 조정 가능성에 유의하세요." if rsi >= 65
              else "RSI 중립 — 추세 지속 가능성이 높습니다.")
    )

    from datetime import datetime
    import pytz
    kst = pytz.timezone("Asia/Seoul")
    now_kst = datetime.now(kst)
    time_label = now_kst.strftime("%Y년 %m월 %d일 %H:%M") + " (한국시간) 기준"

    caption = (
        f"{display_name} {arrow}{abs(change_pct):.1f}% "
        f"{'급락' if change_pct < 0 else '급등'}!\n\n"
        f"[분석 기준] {time_label}\n\n"
        f"[기술적 분석]\n"
        f"- 현재가: ${price:,.2f} ({sign}{abs(change_pct):.2f}%)\n"
        f"- RSI {rsi:.0f} ({rsi_label_ko}) | MACD: {macd_label_ko}\n"
        f"- 거래량: 20일 평균 대비 {vol_ratio:.1f}배 급증\n"
        f"- 볼린저: {bb_label_ko} | EMA 추세: {ema_label_ko}\n\n"
        + (f"[주요 뉴스]\n{news_summary}\n\n" if news_summary else "")
        + f"[단기 전망]\n"
        f"{'하방 압력 지속 여부를 주시하세요.' if change_pct < 0 else '저항선 돌파 여부를 주시하세요.'} "
        f"{rsi_outlook}\n\n"
        f"#{symbol} #주식 #미국주식 #주식투자 #투자\n"
        f"본 콘텐츠는 투자 조언이 아닙니다."
    )

    quant_summary = f"RSI {rsi:.0f}({rsi_label_ko}), MACD {macd_label_ko}, 볼린저 {bb_label_ko}, EMA {ema_label_ko}"

    pkg = ContentPackage(
        symbol=symbol,
        price=price,
        change_pct=change_pct,
        direction=direction,
        script=script,
        card_title=card_title,
        card_subtitle=card_subtitle,
        card_body=card_body,
        caption=caption,
        rsi=rsi,
        macd=macd,
        macd_signal=macd_signal,
        volume_ratio=vol_ratio,
        bb_position=bb_pos,
        ema_trend=ema,
        quant_summary=quant_summary,
        forecast_detail=(
            f"{'하방 압력이 관찰됩니다.' if change_pct < 0 else '상승 모멘텀이 관찰됩니다.'} "
            f"RSI {rsi:.0f} ({rsi_label_ko}). "
            f"MACD {macd_label_ko}. "
            f"거래량 평균 대비 {vol_ratio:.1f}배. "
            f"{'지지선 확인 후 진입을 검토하세요.' if change_pct < 0 else '저항선 확인 후 추가 매수를 검토하세요.'}"
        ),
        chart_data=chart_data,
        company_name_ko=company_name_ko,
        news_headlines=news_headlines[:4],
    )

    logger.info("Content generated: %s", card_title)

    # 5. Remotion video rendering
    output_dir = REPO_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_path = output_dir / f"{symbol}_{ts}_short.mp4"

    # 4b. TTS narration — generate per-scene audio segments with timing data
    from stock_snap.media.tts import generate_tts_with_timing, generate_tts
    tts_segment_paths = []
    subtitle_timings: list[list[tuple[float, float]]] = []
    tts_all_ok = True
    for i, seg_text in enumerate(script_segments):
        seg_path = output_dir / f"{symbol}_{ts}_tts_s{i}.mp3"
        ok, seg_timings = generate_tts_with_timing(seg_text, seg_path)
        if ok:
            tts_segment_paths.append(seg_path)
            subtitle_timings.append(seg_timings)
            logger.info("TTS segment %d: %s (%d sentence timings)", i, seg_path, len(seg_timings))
        else:
            tts_all_ok = False
            logger.warning("TTS segment %d failed", i)
            break

    # Fallback: single audio file if segments fail
    audio_path = None
    if not tts_all_ok or len(tts_segment_paths) != 5:
        tts_path = output_dir / f"{symbol}_{ts}_tts.mp3"
        if generate_tts(script, tts_path):
            audio_path = tts_path
            tts_segment_paths = []
            subtitle_timings = []
            logger.info("TTS fallback (single file): %s", tts_path)
        else:
            logger.warning("TTS generation skipped")

    # 4c. Timing and BGM come from the shared helpers so all three pipelines agree.
    from stock_snap.media.short_video import resolve_bgm_path, scene_timing

    scene_dur_frames, total_frames = scene_timing(tts_segment_paths)
    bgm_path = resolve_bgm_path()

    logger.info("Rendering video (%.1fs, %d frames)...", total_frames / 30, total_frames)
    ok = generate_short_video(
        pkg, video_path, audio_path,
        audio_segment_paths=tts_segment_paths if tts_segment_paths else None,
        script_segments=script_segments,
        bgm_path=bgm_path,
        total_frames=total_frames,
        scene_durations=scene_dur_frames,
        subtitle_timings=subtitle_timings if len(subtitle_timings) == 5 else None,
    )
    if not ok or not video_path.exists():
        msg = f"[Stock Snap] 파이프라인 실패: 영상 렌더링 오류 ({symbol})"
        logger.error("Video rendering failed")
        send_kakao_alert(msg)
        record_pipeline_run(
            status="failure",
            symbol=symbol,
            change_pct=change_pct,
            error="video rendering failed",
            duration_secs=time.monotonic() - _pipeline_start,
            run_id=os.getenv("GITHUB_RUN_ID", ""),
        )
        sys.exit(1)

    logger.info("Video rendered: %s (%.1f MB)", video_path, video_path.stat().st_size / 1024**2)

    # 5b. Thumbnail generation
    from stock_snap.media.short_video import generate_thumbnail
    thumbnail_path = output_dir / f"{symbol}_{ts}_thumb.jpg"
    logger.info("Rendering thumbnail...")
    thumb_ok = generate_thumbnail(pkg, thumbnail_path)
    if thumb_ok:
        logger.info("Thumbnail rendered: %s (%.0f KB)", thumbnail_path, thumbnail_path.stat().st_size / 1024)
    else:
        logger.warning("Thumbnail rendering failed — uploading without cover image")

    # 6. Publish to a public URL for the Graph API to fetch (with retry)
    logger.info("Uploading video to temporary host...")
    try:
        video_url = with_retry(
            lambda: publish_media(video_path, "video/mp4"),
            max_attempts=3,
            base_delay=5.0,
            label="video host upload",
        )
    except Exception as exc:
        video_url = None
        capture_exception(exc, {"step": "video_host_upload", "symbol": symbol})

    if not video_url:
        msg = f"[Stock Snap] 파이프라인 실패: 임시 호스팅 업로드 오류 ({symbol})"
        logger.error("Temporary hosting failed")
        send_kakao_alert(msg)
        record_pipeline_run(
            status="failure",
            symbol=symbol,
            change_pct=change_pct,
            error="video host upload failed",
            duration_secs=time.monotonic() - _pipeline_start,
            run_id=os.getenv("GITHUB_RUN_ID", ""),
        )
        sys.exit(1)
    logger.info("Public URL: %s", video_url)

    cover_url = ""
    if thumb_ok and thumbnail_path.exists():
        logger.info("Uploading thumbnail to temporary host...")
        try:
            cover_url = with_retry(
                lambda: publish_media(thumbnail_path, "image/jpeg") or "",
                max_attempts=2,
                base_delay=3.0,
                label="thumbnail host upload",
            )
        except Exception:
            cover_url = ""
        if cover_url:
            logger.info("Thumbnail URL: %s", cover_url)
        else:
            logger.warning("Thumbnail upload failed — uploading reel without cover")

    # 7. Instagram Reels upload (with retry)
    if DRY_RUN:
        logger.info("[dry-run] Instagram Reels upload skipped")
        reels_ok = True
    else:
        logger.info("Uploading to Instagram Reels...")
        try:
            reels_ok = with_retry(
                lambda: instagram.upload_reel(video_url, caption, cover_url=cover_url),
                max_attempts=3,
                base_delay=10.0,
                label="instagram reels upload",
            )
        except Exception as exc:
            capture_exception(exc, {"step": "instagram_upload", "symbol": symbol})
            reels_ok = False

    # 8. YouTube Shorts upload
    yt_title = f"{card_title} | Stock Snap 주식분석"
    yt_description = caption + "\n\n#Shorts #주식 #미국주식 #투자"
    yt_tags = [symbol, "주식", "미국주식", "투자", "Shorts", "주식분석"]
    if company_name_ko:
        yt_tags.insert(0, company_name_ko)
    logger.info("Uploading to YouTube Shorts%s...", " [dry-run]" if DRY_RUN else "")
    yt_video_id = youtube.upload_short(video_path, yt_title, yt_description, yt_tags, dry_run=DRY_RUN)
    yt_ok = yt_video_id is not None
    if yt_ok:
        logger.info("YouTube Shorts uploaded: https://youtu.be/%s", yt_video_id)
    else:
        logger.warning("YouTube Shorts upload failed (pipeline continues)")

    # 9. Final status report
    arrow_str = "▲" if change_pct > 0 else "▼"
    _elapsed = time.monotonic() - _pipeline_start
    if reels_ok or yt_ok:
        platforms = []
        if reels_ok:
            platforms.append("Instagram")
        if yt_ok:
            platforms.append(f"YouTube(https://youtu.be/{yt_video_id})")
        platform_str = " + ".join(platforms)
        success_msg = (
            f"[Stock Snap] 게시 완료\n"
            f"종목: {display_name} {arrow_str}{abs(change_pct):.1f}%\n"
            f"플랫폼: {platform_str}"
        )
        send_kakao_alert(success_msg)
        record_pipeline_run(
            status="success",
            symbol=symbol,
            change_pct=change_pct,
            platforms=[p.split("(")[0] for p in platforms],
            duration_secs=_elapsed,
            run_id=os.getenv("GITHUB_RUN_ID", ""),
        )
        logger.info("Pipeline complete: %s (%.1fs)", platform_str, _elapsed)
        print(f"\nDone!")
        print(f"Stock: {symbol} {arrow_str}{abs(change_pct):.1f}%")
        print(f"Video: {video_path}")
        print(f"URL: {video_url}")
        if yt_ok:
            print(f"YouTube: https://youtu.be/{yt_video_id}")
        if cover_url:
            print(f"Thumbnail: {cover_url}")
    else:
        msg = f"[Stock Snap] 모든 플랫폼 업로드 실패 ({symbol})"
        logger.error("All platform uploads failed")
        send_kakao_alert(msg)
        record_pipeline_run(
            status="failure",
            symbol=symbol,
            change_pct=change_pct,
            error="all platform uploads failed",
            duration_secs=_elapsed,
            run_id=os.getenv("GITHUB_RUN_ID", ""),
        )
        sys.exit(1)


if __name__ == "__main__":
    run()
