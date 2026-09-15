# News verification

Rules for deciding *why* a stock moved, and for refusing to answer when the evidence
does not support an answer. Applies to stage 3-4 of the pipeline (news collection and
script generation) in `PIPELINE.md`.

## Status (2026-09-15)

The section below records the state this document was written against, on 2026-09-09.
Since then:

- **Implemented** — `src/stock_snap/news/direction.py` (PR #10, weekly in #13). The
  filter now runs on every daily-short, aftermarket and weekly-review run, through
  Claude Code on the subscription rather than an API key. Weekly narrates no news but
  draws up to three headlines on screen, which is why it needed the same wiring. Every
  failure path returns no articles plus a `degraded` reason; the raw-headline fallback
  is gone. The "market sentiment" template sentence is replaced by a plain statement
  that no company news was confirmed (rule 1). `translate_to_korean` is deleted (rule
  6). Kept headlines are logged.
- **Not implemented** — rules 2 through 5 (session-time gate, re-dated article
  detection, primary-source check, materiality floor), and the `universe` and
  `cause_type` fields of the output contract. `DirectionResult.degraded` exists;
  `cause_found` is a boolean, not a typed cause.

## Why this exists

Every video published so far has asserted an unverified English headline as the cause of
a price move. Not because a filter chose badly, but because no filter has ever run.

Verified on 2026-09-09 against the repository and the run logs:

| Check | Result |
|---|---|
| `ANTHROPIC_API_KEY` | Not set. `.env` contains only `INSTAGRAM_USER_ID` and `INSTAGRAM_ACCESS_TOKEN`; the same two are the only repository secrets. |
| `analyze_news_direction` (`run_live_short.py:157-159`) | `if not api_key or not articles: return articles` — returns the input unchanged. |
| `deep_translator` | Not installed (`ModuleNotFoundError`) and absent from `pyproject.toml` dependencies. `translate_to_korean` (`run_live_short.py:33-36`) catches the ImportError and returns the English text. |
| `NewsItem` (`src/stock_snap/news/collector.py:30-38`) | Fields are `title, summary, url, published, source, symbols`. There is no `title_ko`, so `run_aftermarket.py:118`'s `getattr(top_news, "title_ko", None) or ...` always falls through to the English title. |
| Fallback frequency | `output/cron.log`: 125 runs, all logging `Final news headlines: 4 items`. The empty-candidate branch has never executed. |

So the production path is: take the top four yfinance headlines, keep them verbatim, read
them aloud in Korean TTS. No direction check, no relevance check, no recency check, no
source check. The carefully written prompt at `run_live_short.py:167-182` — the one that
excludes macro stories and allows an empty result — has never been sent to a model.

The observable symptom is a Korean sentence with an English headline embedded in it:

```
인텔의 상승 배경을 살펴보면, Intel Stock Jumps After Report of
New Foundry Customer 이 핵심 요인으로 작용했습니다.
```

That sentence also states a cause. "핵심 요인으로 작용했습니다" asserts that this headline
caused the move, with no check that the article was published in the right session, that
it is not a re-dated older story, that it is material, or that it is even about the right
company.

### Two things follow

**The rules below apply to the current, unfiltered path.** They are not a refinement of
the LLM filter; they are the gate that does not exist today. Turning the filter on would
help, but the filter checks direction and relevance — it does not check publication time,
re-dating, primary sources, or materiality, which are the four failure modes that
produced every wrong attribution in the table below.

**Prerequisites come first, and they are outside this document's scope.** Nothing here
matters until (1) `ANTHROPIC_API_KEY` is set, or the template path is rewritten to not
assert causes, and (2) the translation path either gets its dependency or is removed. A
reasonable order:

1. Decide on `ANTHROPIC_API_KEY` — user decision, gates everything else
2. Add `deep_translator` to dependencies, or delete the translation path
3. Replace the empty-candidate fallbacks with the rule-1 behaviour, and adopt the output
   contract
4. Add the primary-source and materiality checks

Steps 3 and 4 are what this document specifies.

### The empty-candidate fallback

Once the filter is on, the empty-candidate branch starts firing for the first time. It
currently fabricates a cause (`run_live_short.py:267-271`):

```python
    seg_news = intro + " ".join(items)
else:                                    # candidate list is empty
    seg_news = (
        f"{tts_name}의 {'급등' if change_pct > 0 else '급락'} 배경을 살펴보겠습니다. "
        f"{'시장 전반의 강한 매수세와 투자 심리 개선이 주요 요인으로 분석됩니다.'
           if change_pct > 0 else
           '시장 전반의 매도 압력과 투자 심리 위축이 주요 원인으로 분석됩니다.'}"
    )
```

`run_aftermarket.py:124-128` has the same shape ("시장 전반적인 … 심리와 섹터 모멘텀이
작용했습니다"). Both assert a cause no source supports, with false precision — "주요
요인으로 분석됩니다" claims an analysis that was never performed — and the branch is
chosen purely by the sign of `change_pct`, so the sentence carries no information beyond
the price change the viewer already saw.

How often that branch will fire is unmeasured. The 125 logged runs are all from the
filter-off state, so 0 is a floor, not a forecast.

The failure is not hypothetical. Every case below was checked against primary sources
(SEC EDGAR, exchange data, official press releases) and the popular explanation was
wrong.

| Date | Ticker | Move | What the feed said | What the primary source showed |
|---|---|---|---|---|
| 2026-09-04 | SNDK | +11.90% close $1,740.00 | S&P 100 inclusion | S&P DJI release timestamped 19:15 ET, **3h15m after the 16:00 close**. Cannot be the cause. SOX closed +3.38% — sector-wide move. |
| 2026-08-24 | ASTS | -9.18% close $62.35 | "$1B raise, dilution fears" | EDGAR CIK 1780312: the only August 8-K was Aug 10 (Q2 results). The $1B convertible deal closed **2026-07-20**. Aggregator re-dated a July filing. |
| 2026-09-03 | PL | -8.20% close $18.35, volume 27.9M vs ~6M typical | New defense contract (positive) | Business Wire, same morning: a **7-figure, 1-year** agreement. Q2 backlog was $814.9M, so the deal is at most **1.1% of backlog**, and the Q2 release listed it as awarded "In August" — already inside the reported quarter. |
| 2026-09-08 | ASTS / LUNR / RDW | +6.11% / +5.87% / +6.65% | Several company-specific stories | EDGAR: last 8-K was 2026-08-10 / 2026-08-13 / 2026-08-05. **Nothing filed Sep 4-8.** Actual driver was a Nasdaq-100 rebalance story about a different company plus an oversold bounce. |
| 2026-09-04 | IMRN | +62.16% | "Record A$7.7M sales", "IMM-529 IND cleared" | Both were released **2026-08-28**; IMRN closed $1.15 that day with no move. The real catalyst was a distribution agreement disclosed on a 6-K dated Sep 4. |
| 2026-08-10 | TENX | Phase 3 topline | Company release cited "FDA's previous agreement that a single Phase 3 trial with a p-value of 0.01 would be sufficient" | EDGAR full-text search returns that phrase in **exactly one document — the press release itself**. The 10-K says the agreed path was "two confirmatory Phase 3 studies." |

Two more classes worth naming:

- **Intraday quoted as close.** Samsung Electronics on 2026-08-24 was -5.2% intraday and
  closed **-8.70%**. On 2026-09-08 the KOSPI hit 7,172 intraday and closed 6,955
  (-0.6%). Any figure taken from a mid-session article is not a close.
- **Bet unwind.** ALAB rose +9.75% on 2026-09-04 on speculation it would enter the
  S&P 500, then fell -6.94% on 2026-09-08 after the Sep 4 19:15 ET announcement did not
  include it. The cause of day two is the *absence* of news on day one's premise.

## The rules

### 1. "No company-specific cause" is a valid verdict

The pipeline must be able to conclude that no company news explains the move, and the
script must be able to say so. On the evidence above this is the correct answer more
often than not for single-stock moves under roughly 10%.

When no candidate survives rules 2-5, the narration explains the move with what is
actually verifiable: sector index move, peer moves, index or rebalance flows, rates,
oil, or an earnings date approaching. This is more accurate and more useful to a viewer
than a manufactured company story.

Do not drop macro causes. Reclassify them. A move caused by the 10-year yield reaching
4.80% is a real, checkable cause.

### 2. Session-time gate

An article can only explain a move if it was published inside the window that produced
the move.

```
prior session close (16:00 ET, previous trading day)
    <= article publication timestamp <=
current session close (16:00 ET)
```

Implementation notes:

- Normalise every timestamp to US Eastern before comparing. Feed timestamps are
  commonly GMT; a GMT value read as ET shifts an article by 4-5 hours and moves it
  across the close.
- After-hours and pre-market articles belong to the *next* session, not the one that
  just closed. The SNDK case fails on exactly this.
- Keep the raw publication timestamp in the candidate record so the script generator can
  cite it.

### 3. Re-dated article detection

Aggregator feeds re-surface old articles with a fresh `pubDate`.

- Compare the feed's `pubDate` against dates found in the article body and title.
- If the body's most recent concrete event date is more than 3 days before the feed
  date, drop the candidate.
- Treat `stocktwits.com` and similar aggregator republishing as low trust: require a
  second, independent source before using such an item as the sole cause.

### 4. Primary-source check for anything factual

Any candidate that asserts a filing, contract, approval, financing, rating change, or
index event must be confirmed at the source before it can be used as the cause.

- Filings: `https://data.sec.gov/submissions/CIK{cik:010d}.json`, then read the actual
  document under `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/`.
  Check the filing date **and** the 8-K item numbers. Items 1.01 / 2.03 / 3.02 indicate
  a financing; 2.02 is results; 5.02 is an officer change.
- Index events: the index provider's own press release, with its timestamp.
- Company claims: if a company press release attributes something to a regulator or
  partner, check whether that claim appears anywhere else in the company's filing
  history. A claim that exists only in the press release is a claim, not a fact.

If the primary source cannot be reached, the candidate is unverified and cannot be the
stated cause. It may still be mentioned as "reported, not confirmed."

### 5. Materiality check

A contract or deal headline is only a cause if it is large enough to matter.

- Compute the deal value against the most recent reported backlog, annual revenue, or
  market capitalisation, whichever the company itself reports.
- Below **2% of the relevant base**, do not use it as the cause of a move.
- Check whether the deal was already counted. If the company's last results release
  lists the award inside the reported period, it adds nothing new.

The PL case fails on both halves: 1.1% of backlog, and already inside the reported
quarter.

### 6. Degrade loudly, and let the output show it

A fallback that quietly substitutes a wrong value is worse than no fallback. The
translation path is the proof:

```python
except Exception:
    return text  # Fallback: return original English text
```

`run_live_short.py:38-39` is the only exception handler in the news path that logs
nothing. `deep_translator` has been missing for months, so this raised an ImportError on
every run, returned English, and left no trace. Every other handler in the same file logs
(`logger.warning("yfinance news fetch failed: %s", e)` and so on), which is why this one
failure is the one that survived undetected.

But logging alone is not enough, and the code shows why. `analyze_news_direction` *does*
log its failure (`run_live_short.py:200-201`) — and then returns the unfiltered articles
anyway:

```python
except Exception as e:
    logger.warning("LLM news analysis failed: %s", e)
return articles
```

The log records that filtering failed. The video still asserts a cause with full
confidence. A viewer cannot see the log.

So the rule has two halves:

- **Log every fallback in the attribution path.** No bare `except Exception: return
  <default>`. `run_aftermarket.py:64-65` is a second instance.
- **A degraded input must change the output, not just the log.** If translation failed,
  do not embed English in a Korean sentence — skip the news scene and let the technical
  scenes carry the video. If filtering failed, the run is `cause_type: "none"`, not a
  confident company story built from unfiltered headlines.

Skipping the news scene is the better failure mode. A video without a news scene is
merely less complete; a video that reads an unverified English headline as the cause is
wrong, and it is wrong in a way the viewer cannot detect. This is the same principle as
rule 1.

#### The same pattern corrupts stock selection, not just narration

The highest-impact instance of this is not in the news path at all. Both the aftermarket
and weekly pipelines fetch their symbol universe in parallel and silently discard
failures (`run_aftermarket.py:41-69`, `run_weekly_review.py:41-67`):

```python
def _fetch(sym):
    try:
        ...
        if len(hist) < 2:
            return None        # no log
    except Exception:
        return None            # no log

with ThreadPoolExecutor(max_workers=10) as ex:
    results = list(filter(None, ex.map(_fetch, UNIVERSE)))
```

`UNIVERSE` holds 22 symbols. If yfinance rate-limits or drops rows — which it does, see
the data-source table — the failures vanish through `filter(None, ...)` and **"today's
hottest stock" silently becomes the hottest of whatever survived.** The log prints
`Fetching aftermarket data for 22 symbols`; the number that actually returned is recorded
nowhere. Fifteen symbols can fail and the run still looks healthy and still publishes.

Note there are also non-exception exits (`len(hist) < 2`, `week_start <= 0`) that drop
symbols just as silently.

Total failure is handled in both files — `run_aftermarket.py:71-73` guards inside the
function, `run_weekly_review.py:151-154` guards at the call site and exits. That is not
the gap.

The gap is **partial** failure, and it is identical in both. Seven surviving symbols out
of 22 is a non-empty list, so every existing guard passes, and the hottest of those seven
is published as the hottest of 22.

What must change:

- Count at the call site, not at each `return None`. Logging `len(results)` against
  `len(UNIVERSE)` catches all three silent exits at once — the `except`, the
  `len(hist) < 2` check, and the `week_start <= 0` check — and needs one line rather than
  three.
- Put `universe_incomplete` in `degraded`, with the counts, whenever the fetch is partial.
- Apply a coverage floor below which the run is skipped rather than published. The floor
  is a policy choice for the repository owner; as a starting point, proceeding at 20/22
  and skipping at 7/22 is clearly right at both ends, and the line between them is a
  judgement call, not something this document can determine. Recording `universe` on every
  run, degraded or not, lets that line be set from the observed distribution instead of
  from a guess.

A wrong "hottest stock" is not a cosmetic defect. Every downstream stage — indicators,
news, narration, thumbnail — is then correct analysis of the wrong company.

## Direction handling

Replace the direction filter with direction *reporting*.

- Do not require the candidate to match the price direction. Find the cause, then state
  the relationship.
- A positive headline on a down day is a real and common pattern, and explaining it is
  more interesting than hiding it. PL fell 8.2% on the day it announced a contract, for
  reasons that are fully explainable.
- If the only surviving candidate contradicts the price direction, say so plainly rather
  than discarding it and searching for a better-matching story.

## Output contract

The verification step should return, per candidate:

```
{
  "headline": str,
  "url": str,
  "published_at": "ISO 8601, US/Eastern",
  "source": str,
  "in_session_window": bool,          # rule 2
  "redate_suspected": bool,           # rule 3
  "primary_source": str | null,       # rule 4: EDGAR accession, press release URL
  "materiality_pct": float | null,    # rule 5, against the stated base
  "materiality_base": str | null,     # "backlog $814.9M" etc.
  "direction_matches_move": bool,
  "verdict": "cause" | "context" | "rejected",
  "reject_reason": str | null
}
```

And per run:

```
{
  "cause_found": bool,
  "cause_type": "company" | "sector" | "macro" | "index_flow" | "none",
  "supporting": [candidate, ...],
  "sector_context": { "index": "SOX", "change_pct": 3.38 },
  "degraded": [str, ...],   # rule 6, e.g. ["llm_filter", "translation", "universe_incomplete"]
  "universe": { "requested": 22, "returned": 20 },   # rule 6, always present
  "notes": str
}
```

`cause_found: false` with `cause_type: "sector"` is a normal, publishable outcome, not a
pipeline failure.

## Data sources that work

Verified in daily use. Nasdaq's own endpoints need a browser `User-Agent` and
`Accept: application/json`; EDGAR needs a `User-Agent` carrying a contact address.

| Purpose | Endpoint | Notes |
|---|---|---|
| Daily OHLCV, US | `api.nasdaq.com/api/quote/{T}/historical?assetclass=stocks&fromdate=&todate=&limit=` | Authoritative closes. Use `assetclass=etf` for ETFs. |
| Same, alternate shape | `api.nasdaq.com/api/quote/{T}/chart?assetclass=&fromdate=&todate=` | |
| Latest quote | `api.nasdaq.com/api/quote/{T}/info?assetclass=` | **Serves stale prior-session data for thinly traded tickers.** Never use alone to establish a close. |
| Filings index | `data.sec.gov/submissions/CIK{cik:010d}.json` | `filings.recent` gives form, date, items, accession. |
| Filing documents | `www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/` | Read the document, not a summary of it. |
| Employment, CPI, PPI | `bls.gov/news.release/{empsit,cpi,ppi}.nr0.htm` | A browser User-Agent gets blocked; a plain bot User-Agent works. |
| Treasury yields | `home.treasury.gov` daily par yield curve CSV | Beware column offsets: the first numeric column is the 1-month bill, not the 2-year. |
| VIX history | Cboe `VIX_History.csv` | |
| Korean equities | `api.finance.naver.com/siseJson.naver?symbol=&requestType=1&startTime=&endTime=&timeframe=day` | Needs `Referer: https://finance.naver.com`. The last row is the live intraday value while the market is open. |
| Headline discovery | `news.google.com/rss/search?q={query}+when:{N}d` | Discovery only. Never a source of record — see rule 3. |

Known bad: `stooq.com` serves a JavaScript challenge to scripted clients; Yahoo Finance
endpoints return HTTP 429 under repeated use and omit rows for some sessions.

## Applying this to the script

When `cause_type` is `company`, the narration states the cause and, where the number is
material, the size relative to the company.

When `cause_type` is `sector`, `macro`, or `index_flow`, the narration says the move was
not company-specific and names what did move: the sector index and its change, the peer
group, the rate or oil level, or the index flow. Do not pad this into a company story.

When `cause_type` is `none`, the narration says the cause is not established, and the
video leans on the technical picture, which is computed from data rather than inferred
from headlines.

When `degraded` contains `"translation"`, skip the news scene rather than narrating an
English headline inside a Korean sentence.

The two call sites to change are `run_live_short.py:267-271` and `run_aftermarket.py:124-128`.

One caveat when working on the aftermarket path: three latent bugs added in March (an
import of a class that does not exist, a wrong argument name, and a render output path
resolved relatively so the render succeeded but the file could not be found) were only
fixed on 2026-09-09. The aftermarket and weekly pipelines had never completed a run
before that date, so that code has no production history to reason from — expect to
verify its behaviour rather than assume it.

The point is that a viewer who acts on a fabricated cause is worse off than a viewer who
is told the move had no identifiable company cause. Published video cannot be corrected
the way a chat answer can, and this pipeline runs unattended.
