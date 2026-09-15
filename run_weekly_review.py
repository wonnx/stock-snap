"""주간 리뷰 숏폼 영상 생성 — 주간 시장 흐름 + 다음 주 관전 포인트 (60초)."""
from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Dry-run: generate everything but publish nothing.
# The workflows have always passed DRY_RUN in; until now only run_live_short.py read it,
# so `gh workflow run ... -f dry_run=true` published for real from here.
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes") or "--dry-run" in sys.argv

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL",
    "AMD", "INTC", "QCOM", "AVGO", "CRM", "SNOW", "PLTR",
    "SOFI", "COIN", "MRNA", "NFLX", "UBER", "SMCI", "MSTR", "ARM",
    "SPY", "QQQ",  # 지수 ETF (시장 전체 흐름 파악용)
]

COMPANY_NAMES_KO = {
    "AAPL": "애플", "MSFT": "마이크로소프트", "GOOGL": "구글", "GOOG": "구글",
    "AMZN": "아마존", "META": "메타", "TSLA": "테슬라", "NVDA": "엔비디아",
    "NFLX": "넷플릭스", "AMD": "AMD", "INTC": "인텔", "CRM": "세일즈포스",
    "ORCL": "오라클", "ADBE": "어도비", "QCOM": "퀄컴", "AVGO": "브로드컴",
    "MRNA": "모더나", "PFE": "화이자",
    "V": "비자", "MA": "마스터카드", "JPM": "JP모건",
    "DIS": "디즈니", "COIN": "코인베이스", "PLTR": "팔란티어", "UBER": "우버",
    "SNOW": "스노우플레이크", "NET": "클라우드플레어", "CRWD": "크라우드스트라이크",
    "MU": "마이크론", "SMCI": "슈퍼마이크로",
    "ARM": "ARM홀딩스", "MSTR": "마이크로스트래티지", "SOFI": "소파이",
    "NIO": "니오", "RIVN": "리비안", "BA": "보잉",
    "SPY": "S&P 500 ETF", "QQQ": "나스닥 100 ETF",
}


def fetch_weekly_data() -> list[dict]:
    """주간(5일) 등락률 기준 종목 데이터 수집."""
    import yfinance as yf

    def _fetch(sym):
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="5d", interval="1d")
            if len(hist) < 2:
                return None
            week_start = float(hist.iloc[0]["Open"])
            week_end = float(hist.iloc[-1]["Close"])
            if week_start <= 0:
                return None
            change_pct = (week_end - week_start) / week_start * 100
            avg_volume = int(hist["Volume"].mean())
            return {
                "symbol": sym,
                "price": week_end,
                "week_start": week_start,
                "change_pct": round(change_pct, 2),
                "avg_volume": avg_volume,
            }
        except Exception:
            return None

    logger.info("Fetching weekly data for %d symbols...", len(UNIVERSE))
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(filter(None, ex.map(_fetch, UNIVERSE)))

    results.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return results


def build_weekly_script(hero_stock: dict, top_gainers: list[dict],
                         top_losers: list[dict], market_etfs: dict) -> list[str]:
    """주간 리뷰 TTS 스크립트 세그먼트 생성 (5개 세그먼트, 약 60초)."""
    sym = hero_stock["symbol"]
    name = COMPANY_NAMES_KO.get(sym, sym)
    chg = hero_stock["change_pct"]
    arrow = "급등" if chg > 0 else "급락"
    abs_chg = abs(chg)

    # 세그먼트 1: 주간 시장 개요 (Hero)
    spy_chg = market_etfs.get("SPY", 0)
    qqq_chg = market_etfs.get("QQQ", 0)
    market_mood = "강세" if spy_chg > 0 else "약세"
    seg_hero = (
        f"이번 주 미국 증시 주간 리뷰입니다. "
        f"이번 주 S&P500은 {abs(spy_chg):.1f}퍼센트 {'상승' if spy_chg > 0 else '하락'}했고, "
        f"나스닥은 {abs(qqq_chg):.1f}퍼센트 {'상승' if qqq_chg > 0 else '하락'}하며 "
        f"전반적으로 {market_mood} 흐름을 보였습니다."
    )

    # 세그먼트 2: 주간 상위 상승 종목
    gainers_text = []
    for r in top_gainers[:3]:
        n = COMPANY_NAMES_KO.get(r["symbol"], r["symbol"])
        gainers_text.append(f"{n} {r['change_pct']:.1f}퍼센트")
    seg_gainers = (
        "이번 주 주요 상승 종목을 살펴보겠습니다. "
        + ", ".join(gainers_text)
        + " 순으로 강세를 보였습니다."
    )

    # 세그먼트 3: 주간 최고 주목 종목 (상세)
    seg_detail = (
        f"이번 주 가장 주목받은 종목은 {name}입니다. "
        f"한 주 동안 {abs_chg:.1f}퍼센트 {arrow}하며 "
        f"투자자들의 관심을 집중시켰습니다."
    )

    # 세그먼트 4: 기술적 지표 요약
    seg_indicators = (
        f"{name}의 기술적 지표를 확인하겠습니다. "
        f"주간 차트를 기준으로 추세와 지지, 저항 구간을 살펴보는 것이 중요합니다."
    )

    # 세그먼트 5: 다음 주 관전 포인트
    if len(top_losers) > 0:
        loser = top_losers[0]
        loser_name = COMPANY_NAMES_KO.get(loser["symbol"], loser["symbol"])
        watch_text = (
            f"다음 주 관전 포인트입니다. "
            f"{loser_name}의 반등 여부와 "
            f"{name}의 추가 {arrow} 지속성을 주목해 보시기 바랍니다. "
            f"투자에는 항상 신중을 기하시기 바랍니다."
        )
    else:
        watch_text = (
            f"다음 주 관전 포인트입니다. "
            f"{name}의 모멘텀 지속 여부와 "
            f"연준 발언 등 매크로 변수에 주목해 보시기 바랍니다. "
            f"투자에는 항상 신중을 기하시기 바랍니다."
        )
    seg_conclusion = watch_text

    return [seg_hero, seg_gainers, seg_detail, seg_indicators, seg_conclusion]


def run():
    """주간 리뷰 파이프라인 실행."""
    import datetime

    from stock_snap.analysis.indicators import TechnicalAnalyzer
    from stock_snap.content.generator import ContentPackage
    from stock_snap.data.fetcher import MarketDataFetcher
    from stock_snap.media.short_video import (
        generate_short_video,
        generate_thumbnail,
        resolve_bgm_path,
        scene_timing,
    )
    from stock_snap.media.tts import generate_tts_with_timing
    from stock_snap.news.collector import NewsCollector
    from stock_snap.upload.instagram import instagram
    from stock_snap.upload.youtube import youtube

    # 1. 주간 데이터 수집
    weekly_data = fetch_weekly_data()
    if not weekly_data:
        logger.error("Weekly data fetch failed")
        sys.exit(1)

    # ETF 등락 추출
    market_etfs = {}
    for r in weekly_data:
        if r["symbol"] in ("SPY", "QQQ"):
            market_etfs[r["symbol"]] = r["change_pct"]

    # 지수 ETF 제외하고 종목만 필터링
    stocks = [r for r in weekly_data if r["symbol"] not in ("SPY", "QQQ")]
    top_gainers = [r for r in stocks if r["change_pct"] > 0][:3]
    top_losers = sorted([r for r in stocks if r["change_pct"] < 0], key=lambda x: x["change_pct"])[:3]

    if not stocks:
        logger.error("No stock data available")
        sys.exit(1)

    # 주간 최고 등락 종목 = 주인공
    hero_stock = stocks[0]
    symbol = hero_stock["symbol"]
    price = hero_stock["price"]
    change_pct = hero_stock["change_pct"]
    direction = "rising" if change_pct > 0 else ("falling" if change_pct < 0 else "flat")
    name = COMPANY_NAMES_KO.get(symbol, symbol)

    logger.info("Weekly hero: %s %+.2f%%", symbol, change_pct)

    # 2. 기술적 분석
    fetcher = MarketDataFetcher()
    df = fetcher.get_ohlcv(symbol, period="3mo", interval="1d")
    analyzer = TechnicalAnalyzer()
    tech = analyzer.compute(symbol, df)
    chart_data = [round(float(v), 2) for v in df["Close"].iloc[-20:].tolist()]

    # 3. 뉴스 수집
    # Headlines are not narrated here but they are drawn on screen (StockShort renders
    # newsHeadlines), so they go through the same filter as the other pipelines.
    from stock_snap.news.direction import select_direction_articles

    collector = NewsCollector()
    raw_items = collector.fetch_for_symbol(symbol, max_items=5)
    direction = select_direction_articles(
        [(n.title, n.summary or "") for n in raw_items], symbol, change_pct
    )
    if direction.degraded:
        logger.warning("news filter degraded: %s", ", ".join(direction.degraded))
    news_items = direction.articles

    # 4. TTS 스크립트 생성
    script_segments = build_weekly_script(hero_stock, top_gainers, top_losers, market_etfs)
    script = " ".join(script_segments)

    # 5. ContentPackage 조립
    arrow_emoji = "▲" if change_pct > 0 else "▼"
    abs_pct = abs(change_pct)
    spy_chg = market_etfs.get("SPY", 0)
    card_title = f"주간 리뷰 — {symbol} {arrow_emoji}{abs_pct:.1f}%"
    card_subtitle = f"S&P500 {'▲' if spy_chg > 0 else '▼'}{abs(spy_chg):.1f}% 주간 마감"
    card_body = (
        f"• 주간 최강: {name} {arrow_emoji}{abs_pct:.1f}%\n"
        f"• S&P500 주간: {'▲' if spy_chg > 0 else '▼'}{abs(spy_chg):.1f}%\n"
        f"• 상승 종목: {len(top_gainers)}개 / 하락: {len(top_losers)}개"
    )
    hashtags = f"#{symbol} #주간리뷰 #미국주식 #주식투자 #위클리"
    caption = (
        f"이번 주 미국 증시 주간 리뷰\n\n"
        f"{card_subtitle}\n\n{hashtags}\n⚠️ 본 콘텐츠는 투자 조언이 아닙니다."
    )

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
        rsi=tech.rsi14,
        macd=tech.macd,
        macd_signal=tech.macd_signal,
        bb_position=tech.bb_position,
        ema_trend=tech.ema_trend,
        volume_ratio=1.0,
        chart_data=chart_data,
        news_headlines=[title for title, _ in news_items[:3]],
    )

    # 6. 출력 경로
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = REPO_ROOT / "output" / f"weekly_{symbol}_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 7. TTS 생성 (세그먼트별)
    tts_segment_paths = []
    all_timings = []
    for i, seg_text in enumerate(script_segments):
        seg_path = output_dir / f"{symbol}_{ts}_tts_s{i}.mp3"
        ok, timings = generate_tts_with_timing(seg_text, seg_path)
        if ok:
            tts_segment_paths.append(seg_path)
            all_timings.append(timings)
            logger.info("TTS segment %d OK", i)
        else:
            logger.error("TTS segment %d FAILED", i)
            tts_segment_paths.append(None)
            all_timings.append([])

    # A dropped segment would shift every later one onto the wrong scene, so if the set
    # is incomplete fall back to one narration file spanning the whole video.
    audio_path = None
    if not all(tts_segment_paths) or len(tts_segment_paths) != 5:
        from stock_snap.media.tts import generate_tts

        logger.warning("TTS segments incomplete — falling back to a single audio file")
        single = output_dir / f"{symbol}_{ts}_tts.mp3"
        if generate_tts(" ".join(script_segments), single):
            audio_path = single
            tts_segment_paths = []
            all_timings = []
        else:
            logger.error("TTS fallback failed — video would be silent")

    # 8. 영상 렌더링
    video_path = output_dir / f"{symbol}_{ts}_weekly.mp4"
    scene_frames, total_frames = scene_timing(tts_segment_paths)
    ok = generate_short_video(
        pkg,
        video_path,
        audio_path,
        script_segments=script_segments,
        audio_segment_paths=tts_segment_paths,
        bgm_path=resolve_bgm_path(),
        total_frames=total_frames,
        scene_durations=scene_frames,
        subtitle_timings=all_timings,
    )
    if not ok:
        logger.error("Video rendering failed")
        sys.exit(1)
    logger.info("Video rendered: %s", video_path)

    # 9. 업로드

    from stock_snap.upload.media_host import publish_media

    def _host(file_path: Path, mime: str = "video/mp4") -> str | None:
        try:
            return publish_media(file_path, mime)
        except Exception as e:
            logger.error("media host upload failed: %s", e)
            return None

    video_url = _host(video_path)
    if not video_url:
        logger.error("media host upload failed — aborting")
        sys.exit(1)

    thumbnail_path = output_dir / f"{symbol}_{ts}_thumb.jpg"
    cover_url = ""
    if generate_thumbnail(pkg, thumbnail_path):
        cover_url = _host(thumbnail_path, "image/jpeg") or ""

    if DRY_RUN:
        logger.info("[dry-run] Instagram Reels upload skipped")
        reels_ok = True
    else:
        reels_ok = instagram.upload_reel(video_url, caption, cover_url=cover_url)

    yt_title = f"{card_title} | Stock Snap 주간리뷰"
    yt_desc = caption + "\n\n#Shorts #주식 #미국주식 #위클리"
    yt_tags = [symbol, "주식", "미국주식", "주간리뷰", "Shorts"]
    yt_video_id = youtube.upload_short(video_path, yt_title, yt_desc, yt_tags, dry_run=DRY_RUN)

    if reels_ok or yt_video_id:
        logger.info("Weekly review published successfully!")
    else:
        logger.error("All platform uploads failed")
        sys.exit(1)


if __name__ == "__main__":
    run()
