# Parabolic Scale-Out Monitor

Single-page systematic dashboard that reports which scale-out tranche is currently live for a fixed list of equities and ETFs in extended trends. Published as a static GitHub Pages site. Illustrative systematic signals - not investment advice.

## Universe

Seven names, hard-coded in `scripts/build.py`:

- MU, INTC, SOI.PA, 4004.T, MRVL (semiconductor cluster)
- NOK, LIFE (standalone)

A semiconductor cluster-break flag fires on every cluster member when two or more cluster members sit in Tranche 2 or beyond.

## Tranche definitions

Computed from the daily close (`c`) series.

- **Holding** - close above EMA 8, no break in effect.
- **Tranche 1 trigger** - close at least 5% above EMA 8 AND within 3% of the trailing 20-day close-high.
- **Tranche 2 trigger** - confirmed daily close below EMA 8.
- **Tranche 3 trail active** - confirmed daily close below EMA 21.

"Confirmed close" means the latest bar in the upstream feed. Intraday values are never used. Weekend-stamped bars are used as-is and flagged in notes so the reader can verify them.

The Tranche 1 thresholds are surfaced in `data/signals.json` under `assumptions.tranche1` for transparency.

## Architecture

```
parabolic-scale-out/
├── template.html                  Source HTML, under 200 KB. Fetch fallback for local dev.
├── data/
│   └── signals.json               Canonical signal payload, pretty-printed.
├── scripts/
│   └── build.py                   Fetches upstream, computes signals, renders docs/.
├── docs/
│   ├── index.html                 GitHub Pages output. Template with the JSON inlined.
│   └── data/
│       └── signals.json           Copy of the canonical payload at a stable public URL.
└── .github/workflows/build.yml    Daily cron + manual dispatch.
```

The build script runs once per day:

1. Fetches the upstream OHLC blob from `https://phuazz.github.io/Portfolio-Command-Centre/data/history.json`.
2. Computes EMA 8, EMA 21, EMA 50, EMA 200 and MACD (12, 26, 9) on each ticker's close series.
3. Classifies the tranche state, identifies the active trigger level, and measures the distance from close to that level.
4. Aggregates the cluster-break flag and per-ticker freshness flags.
5. Writes `data/signals.json` (pretty), `docs/index.html` (template with inlined JSON), and `docs/data/signals.json` (copy).

Stdlib only - no requirements file. Python 3.10+.

## Local development

```
# Source-only dev. Loads data/signals.json via fetch.
npx serve .

# Full built output, as GitHub Pages serves it.
python scripts/build.py
npx serve docs
```

The template uses a `PRELOADED_DATA` placeholder. During the build the placeholder line is replaced with the compact JSON. The fetch fallback only fires when `PRELOADED_DATA` is `null`, which is the case while editing `template.html` directly.

## Deployment

Hosted on GitHub Pages, served from `/docs` on the default branch. The Actions workflow at `.github/workflows/build.yml` runs daily at 21:30 UTC on weekdays and commits the refreshed `data/` and `docs/` outputs.

Pointing GitHub Pages at `/docs` is a one-time manual step in repo settings after the first push.

## Hardening

The pipeline tolerates several upstream conditions without producing a broken page:

- **Fetch failure** - the build exits non-zero before writing anything, so the previously-published `docs/` remains intact. The Actions log shows the upstream error.
- **Stale upstream** - if any ticker's latest bar is older than five days, that ticker is flagged in `freshness.staleTickers` and a banner appears on the page.
- **Weekend-stamped bars** - if a ticker's latest bar falls on a Saturday or Sunday it is used as-is but flagged in `freshness.weekendStampedTickers` and in the ticker's notes.
- **Short history** - a ticker with fewer than 21 bars produces an error row; 21-49 bars renders with a note that EMA 50 and EMA 200 are unavailable; 50-199 bars renders with a note that EMA 200 is unavailable.
- **Build staleness** - the page itself, at load time, compares its inlined `generatedAt` to the visitor's current time and warns in the banner if the build is more than 36 hours old (catches the case where the cron has been failing for a while).

## Disclaimer

Illustrative systematic signals - not investment advice. The signals on this page describe state, not action.
