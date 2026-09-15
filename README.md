# Stock Snap

Picks the most active US stock each day, runs technical analysis on it, writes a
Korean narration script, renders a vertical short-form video, and posts it to
Instagram Reels and YouTube Shorts. Runs unattended on GitHub Actions.

Channel: [@stock.snap](https://instagram.com/stock.snap)

## How it works

```
hot stock selection      yfinance, 39 symbols, ranked by |change %| + volume spike
        ↓
technical analysis       RSI, MACD, Bollinger, SMA/EMA, pivots, 20d trendline
        ↓
news collection          Finnhub, Alpha Vantage, SEC EDGAR, Yahoo Finance RSS
        ↓
script generation        Claude picks direction-consistent articles, writes Korean copy
        ↓
narration                edge-tts, one audio file per scene with sentence timings
        ↓
video render             Remotion, 5 scenes, subtitles synced to the TTS timings
        ↓
publish                  GitHub Release asset for the public URL, then Graph API / YouTube
```

The five scenes are: hero (ticker and move), news (why it moved), chart, indicators,
and takeaway. Scene lengths come from the actual TTS duration rather than being fixed,
so narration is never cut off mid-sentence. Total runtime lands around 100-110s.

## Docs

- `PIPELINE.md` — module-level detail for each stage
- `VERIFICATION.md` — rules for attributing a price move to a cause, and for saying
  "no company-specific cause" when that is what the evidence supports

## Setup

```bash
git clone https://github.com/wonnx/stock-snap.git
cd stock-snap

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cd remotion && npm ci && cd ..
```

Requires Python 3.12+, Node 20+, and ffmpeg.

Copy `.env.example` to `.env` and fill in what you need:

| Variable | Needed for |
|---|---|
| `INSTAGRAM_USER_ID`, `INSTAGRAM_ACCESS_TOKEN` | posting to Reels |
| `ANTHROPIC_API_KEY` | LLM news analysis (falls back to templates without it) |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | posting to Shorts |
| `FINNHUB_API_KEY`, `ALPHA_VANTAGE_API_KEY` | extra news sources |
| `KAKAO_ACCESS_TOKEN` | run success/failure notifications |
| `SENTRY_DSN` | error reporting |

Only the Instagram pair is required to publish. Everything else degrades: no Anthropic
key means template copy instead of LLM-written analysis, no news API keys means
yfinance headlines only.

## Running it

```bash
python run_live_short.py              # generate and publish
python run_live_short.py --dry-run    # generate only, skip both uploads
```

Four pipelines share the same modules:

| Script | Output |
|---|---|
| `run_live_short.py` | daily single-stock analysis |
| `run_aftermarket.py` | post-close recap |
| `run_weekly_review.py` | Friday week-in-review |
| `run_premarket_short.py` | pre-market brief |

There is also a CLI for the individual pieces:

```bash
stock-snap scan                       # scan the watchlist for signals
stock-snap backtest NVDA              # backtest one symbol
stock-snap watchlist --add SOFI PLTR
```

## Scheduling

Scheduled runs happen on GitHub Actions only. There is deliberately no cron job: the
two together would post the same content twice.

| Workflow | Cron (UTC) | KST |
|---|---|---|
| `daily-short.yml` | `30 0 * * 1-5` | weekdays 09:30 |
| `aftermarket.yml` | `15 21 * * 1-5` | weekdays 06:15 next day |
| `weekly-review.yml` | `30 21 * * 5` | Saturday 06:30 |

```bash
gh workflow run daily-short.yml -f dry_run=true   # manual, no upload
gh workflow list --all                            # check they are still enabled
```

GitHub disables scheduled workflows after 60 days without repository activity, so the
second command is worth running if posts stop appearing.

## Operational notes

Things that took a while to work out, kept here so they don't have to be rediscovered.

**Rendering is slow on CI.** The composition renders at `--scale 2` (2160x3840) with
`crf 10`, which takes about 500s on a 2-core GitHub runner versus well under a minute
on an M-series Mac. The render timeout is 1800s (`REMOTION_RENDER_TIMEOUT`) and the job
timeout 60 minutes. Lower `--scale` for faster runs at the cost of detail.

**Instagram needs a public URL.** The Graph API does not accept video bytes for Reels;
it fetches from a URL you hand it. catbox.moe filled that role originally but rejects
requests from CI runner IP ranges with a 412. Media is now attached to a rolling GitHub
Release (tag `media`, most recent 20 assets kept) and the release asset URL is what gets
passed to the API. catbox stays as the fallback for local runs. See
`src/stock_snap/upload/media_host.py`.

**The BGM file lives in two places.** `output/` is not tracked, so the copy under
`remotion/public/` is the one CI uses.

## Testing

```bash
pytest                                # 155 tests, no network or secrets required
pytest tests/test_e2e_dry_run.py -v   # whole pipeline against mock fixtures
```

`tests/test_e2e_dry_run.py` walks the full flow with yfinance, the news APIs, TTS,
Remotion, and the upload host all mocked, so it catches wiring breaks without spending
ten minutes on a render. Fixtures for the Finnhub and Alpha Vantage responses are in
`tests/fixtures/`.

`tests/test_instagram_e2e.py` and `tests/test_reels_e2e.py` hit live APIs and are
excluded from CI.

## Layout

```
src/stock_snap/
  hot_stock.py          daily symbol selection
  scanner.py            watchlist scanning
  analysis/             technical indicators
  news/                 multi-source collection and sentiment
  content/              script and caption generation
  media/                TTS, BGM, Remotion render, card news
  upload/               Instagram, YouTube, public URL hosting
  analytics/            Instagram Insights and YouTube Analytics collection
  alerts/               Kakao and Telegram notifications
  backtest/             signal backtesting
remotion/               React video templates
run_*.py                pipeline entry points
```

## License

Private. © 2026 wonnx
