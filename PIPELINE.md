# Pipeline reference

Module-level detail for the content pipeline. See `README.md` for setup and day-to-day
usage.

## Stages

### 1. Hot stock selection — `src/stock_snap/hot_stock.py`

Scans 39 US symbols (S&P 500 names plus thematic tickers) and ranks them by a composite
score: absolute price change % (up to 50 points) plus volume spike ratio against the
20-day average (up to 50 points). Returns the top symbol.

Direction matters downstream, so both large gainers and large losers are eligible.

### 2. Technical analysis — `src/stock_snap/analysis/indicators.py`

Computed on 3 months of daily OHLCV:

| Indicator | Parameters | Used for |
|---|---|---|
| RSI | 14 | overbought above 70, oversold below 30 |
| MACD | 12/26/9 | momentum direction |
| Bollinger Bands | 20, 2σ | band position, squeeze detection |
| SMA | 5/20/60 | trend alignment, golden/death cross |
| EMA | 9/21/50 | short-term trend |
| Pivot points | classic H/L/C | support and resistance levels |
| Trendline | 20-day linear regression | direction and R² |
| Volume ratio | vs 20-day average | spike detection above 2x |

### 3. News collection — `src/stock_snap/news/collector.py`

Four sources, 24-48h lookback, deduplicated by title: yfinance, Finnhub, Alpha Vantage,
SEC EDGAR, and Yahoo Finance RSS. Missing API keys just drop that source.

### 4. Script generation — `src/stock_snap/content/generator.py`

Two modes. With `ANTHROPIC_API_KEY` set, Claude filters the collected articles and
rewrites them in Korean. Without it, the pipeline falls back to templates built from the
quant data.

The LLM step exists mainly to enforce one rule: the article has to explain a move in the
same direction as the day's price action, and it has to be about this company. Macro
stories (oil, rates, FX) get dropped even when they rank high in the feed. The prompt is
in `run_live_short.py::analyze_news_direction`.

That direction rule is the known weak point: it always produces an explanation, including
on days when no company-specific cause exists, and dropping macro stories turns a sector
or rates move into a fabricated company claim. `VERIFICATION.md` documents the rules that
should gate this stage — session-time window, re-dated article detection, primary-source
confirmation, and a materiality floor — along with the failure cases that motivated them.

### 5. Narration — `src/stock_snap/media/tts.py`

Microsoft Edge TTS, voice `ko-KR-HyunsuMultilingualNeural`, one MP3 per scene. Sentence
boundary timings come back with the audio and drive subtitle timing in the render.
Tickers are read as Korean company names rather than spelled out in English.

### 6. Video render — `src/stock_snap/media/short_video.py`

Remotion CLI, composition `StockShort`. Scene durations are computed from the measured
TTS length plus a one-second buffer, so total frame count varies per run. Audio segments
and the BGM track are copied into `remotion/public/` before the render because Remotion
resolves them through `staticFile()`.

Render settings are `--scale 2 --height 1920 --width 1080 --crf 10 --codec h264`, which
produces a 2160x3840 file of roughly 12-13 MB for a 110s video.

### 7. Publishing — `src/stock_snap/upload/`

- `media_host.py` gets the file a public URL. On CI it attaches the file to a rolling
  GitHub Release (tag `media`); locally it uses catbox.moe.
- `instagram.py` creates a Reels container from that URL, polls until the container is
  ready, then publishes.
- `youtube.py` uploads the same file as a Short, refreshing the OAuth access token from
  the stored refresh token and tracking daily quota use.

## Running

Paths are relative to the repository root.

```bash
python run_live_short.py              # generate and publish
python run_live_short.py --dry-run    # generate only

stock-snap content --hot --upload     # CLI path, uses the Claude API mode
```

Scheduled execution is GitHub Actions only; see `README.md`.

## Output files

```
output/{SYMBOL}_{timestamp}_short.mp4      rendered video
output/{SYMBOL}_{timestamp}_thumb.jpg      cover image
output/{SYMBOL}_{timestamp}_tts_s{0-4}.mp3 per-scene narration
output/pipeline_report.json                run history: status, symbol, duration, error
```

On a failed workflow run the whole `output/` directory is uploaded as a build artifact,
which is usually the fastest way to see what the pipeline actually produced.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `npx not found` | Node missing from PATH. On CI this means the setup-node step did not run. |
| `Remotion render timed out` | Raise `REMOTION_RENDER_TIMEOUT`; check the job timeout too. |
| Release asset upload 403 | Job needs `permissions: contents: write` and `GITHUB_TOKEN` in the step env. |
| catbox returns 412 | Expected on CI. catbox blocks runner IP ranges; the release host is used there. |
| Hot stock selection returns None | yfinance returned nothing for the whole universe, usually a transient outage. |
| Instagram container never becomes ready | Almost always an unreachable video URL. Open it in a browser. |
