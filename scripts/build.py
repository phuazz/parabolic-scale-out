"""Parabolic Scale-Out Monitor - build script.

Fetches daily OHLC from the upstream Portfolio-Command-Centre GitHub Pages
source, computes EMAs and MACD for a fixed list of names, derives a tranche
state for each, and writes data/signals.json. Stdlib only - no requirements.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HISTORY_URL = "https://phuazz.github.io/Portfolio-Command-Centre/data/history.json"

TICKERS: list[str] = ["MU", "INTC", "SOI.PA", "4004.T", "NOK", "MRVL", "LIFE"]
CLUSTER_SEMI: set[str] = {"MU", "INTC", "SOI.PA", "4004.T", "MRVL"}

# Reference tickers - computed and surfaced separately, never classified as
# a tranche row. SLV is the silver benchmark used by the extension chart.
REFERENCE_TICKERS: list[str] = ["SLV"]

# Tranche 1 thresholds. Surfaced in output so the assumption is transparent.
T1_EXTENSION_PCT = 0.05       # close at least 5% above EMA 8
T1_HIGH_LOOKBACK = 20         # trailing N daily closes
T1_HIGH_PROXIMITY_PCT = 0.03  # close within 3% of trailing high

# Silver's January 2026 parabolic peak, expressed as percent above its
# 200-day moving average. Used as a fixed reference line on the chart.
SILVER_JAN_PEAK_PCT = 80

# A ticker is flagged "parabola broken" when it has fallen out of trend
# strongly enough that the routine Tranche 2 treatment understates its
# situation. Definition: in Tranche 2 or beyond AND drawdown from the
# trailing 20-day close-high exceeds this threshold.
PARABOLA_BROKEN_DRAWDOWN = 0.25

# Freshness thresholds.
STALE_BAR_THRESHOLD_DAYS = 5  # warn if a ticker's latest bar is older than this

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DOCS_DIR = PROJECT_ROOT / "docs"
TEMPLATE_PATH = PROJECT_ROOT / "template.html"
SUBSTITUTION_MARKER = "const PRELOADED_DATA = null; /* __SIGNALS_JSON__ */"


def fetch_history(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "parabolic-scale-out/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average seeded by SMA of first `period` values.

    Returns a list aligned to `values` length, with leading Nones for the
    bars before the seed is complete.
    """
    if len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    for v in values[period:]:
        prev = out[-1]
        assert prev is not None  # by construction
        out.append(v * k + prev * (1 - k))
    return out


def macd_latest(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, float | None]:
    """Return the latest MACD line, signal, and histogram. Nones if too short."""
    if len(values) < slow + signal:
        # Need slow EMA series long enough to seed the signal EMA.
        fast_e = ema(values, fast)
        slow_e = ema(values, slow)
        if not fast_e or fast_e[-1] is None or not slow_e or slow_e[-1] is None:
            return {"line": None, "signal": None, "hist": None}
        return {"line": round(fast_e[-1] - slow_e[-1], 6), "signal": None, "hist": None}
    fast_e = ema(values, fast)
    slow_e = ema(values, slow)
    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_e, slow_e)
    ]
    macd_compact = [v for v in macd_line if v is not None]
    signal_e = ema(macd_compact, signal)
    if not signal_e or signal_e[-1] is None:
        return {"line": round(macd_line[-1], 6) if macd_line[-1] is not None else None, "signal": None, "hist": None}
    line_v = macd_line[-1]
    sig_v = signal_e[-1]
    return {
        "line": round(line_v, 6) if line_v is not None else None,
        "signal": round(sig_v, 6),
        "hist": round(line_v - sig_v, 6) if line_v is not None else None,
    }


def classify(close: float, ema8: float, ema21: float, recent_high: float) -> tuple[str, str]:
    """Return (state_code, state_label) for one bar."""
    if close < ema21:
        return "tranche3", "Below EMA 21 - Tranche 3 trail active"
    if close < ema8:
        return "tranche2", "Below EMA 8 - Tranche 2 trigger met"
    extension = close / ema8 - 1.0
    high_proximity = close / recent_high - 1.0  # negative if below the high
    if extension >= T1_EXTENSION_PCT and high_proximity >= -T1_HIGH_PROXIMITY_PCT:
        return "tranche1", "Above EMA 8 - Tranche 1 trigger met (extended near highs)"
    return "holding", "Above EMA 8 - holding"


def compute_for_ticker(ticker: str, raw: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    if ticker not in raw:
        return {"ticker": ticker, "error": "not found in upstream history.json"}
    entry = raw[ticker]
    history = entry.get("history") or []
    if not history:
        return {"ticker": ticker, "error": "no history bars"}
    # Defensive ascending sort by unix-seconds timestamp.
    history = sorted(history, key=lambda b: b.get("d", 0))
    # Defensive close filter - reject missing, None, or non-numeric values.
    closes: list[float] = [
        float(b["c"]) for b in history
        if "c" in b and isinstance(b["c"], (int, float))
    ]
    if len(closes) < 21:
        return {
            "ticker": ticker,
            "error": f"insufficient bars for EMA 21 ({len(closes)})",
            "barCount": len(closes),
        }

    last_close = closes[-1]
    last_bar = history[-1]
    last_bar_dt = datetime.fromtimestamp(last_bar["d"], tz=timezone.utc)
    last_bar_date = last_bar_dt.strftime("%Y-%m-%d")
    last_bar_weekday = last_bar_dt.strftime("%A")
    is_weekend = last_bar_dt.weekday() >= 5  # 5=Sat, 6=Sun
    bar_age_days = (now_utc - last_bar_dt).total_seconds() / 86400.0
    is_stale = bar_age_days > STALE_BAR_THRESHOLD_DAYS

    e8 = ema(closes, 8)
    e21 = ema(closes, 21)
    e50 = ema(closes, 50) if len(closes) >= 50 else [None] * len(closes)
    e200 = ema(closes, 200) if len(closes) >= 200 else [None] * len(closes)
    macd_vals = macd_latest(closes)

    ema8_v = e8[-1]
    ema21_v = e21[-1]
    ema50_v = e50[-1]
    ema200_v = e200[-1]
    if ema8_v is None or ema21_v is None:
        return {"ticker": ticker, "error": "EMA series too short", "barCount": len(closes)}

    lookback = closes[-T1_HIGH_LOOKBACK:] if len(closes) >= T1_HIGH_LOOKBACK else closes
    recent_high = max(lookback)

    state, state_label = classify(last_close, ema8_v, ema21_v, recent_high)

    # Extension above EMA 200, expressed as percent (e.g. 80 means 80%
    # above EMA 200). None when EMA 200 is not available.
    ext_pct_ema200: float | None = None
    if ema200_v is not None and ema200_v > 0:
        ext_pct_ema200 = (last_close / ema200_v - 1.0) * 100.0

    # Parabola-broken flag: distinguishes a routine Tranche 2 from a name
    # that has fallen meaningfully out of its parabolic move.
    drawdown_from_high = (recent_high - last_close) / recent_high if recent_high > 0 else 0.0
    parabola_broken = (
        state in ("tranche2", "tranche3")
        and drawdown_from_high > PARABOLA_BROKEN_DRAWDOWN
    )

    # The "active trigger" is the level whose break (or reclaim) defines the next transition.
    if state in ("holding", "tranche1"):
        trigger_level, trigger_value = "ema8", ema8_v
    elif state == "tranche2":
        trigger_level, trigger_value = "ema21", ema21_v
    else:  # tranche3
        trigger_level, trigger_value = "ema21", ema21_v  # reclaim level

    distance = (last_close - trigger_value) / trigger_value

    notes: list[str] = []
    if ticker == "MU":
        notes.append("Price series may be split-adjusted - verify absolute levels independently.")
    # Order matters: check < 50 first (subset of < 200) so we report the tighter constraint.
    if len(closes) < 50:
        notes.append(f"Limited history ({len(closes)} bars) - EMA 50 and EMA 200 unavailable.")
    elif len(closes) < 200:
        notes.append(f"Limited history ({len(closes)} bars) - EMA 200 unavailable.")
    if is_weekend:
        notes.append(
            f"Latest bar stamped on {last_bar_weekday} - possible non-trading-day artefact from upstream."
        )
    if is_stale:
        notes.append(
            f"Latest bar is {bar_age_days:.1f} days old - upstream data may be stale."
        )
    if parabola_broken:
        notes.append(
            f"Parabola broken - {drawdown_from_high * 100:.1f}% below the trailing 20-day high."
        )

    return {
        "ticker": ticker,
        "lastBarDate": last_bar_date,
        "lastBarUnix": int(last_bar["d"]),
        "lastBarWeekday": last_bar_weekday,
        "barAgeDays": round(bar_age_days, 2),
        "isWeekendStamped": is_weekend,
        "isStale": is_stale,
        "lastClose": round(last_close, 4),
        "ema8": round(ema8_v, 4),
        "ema21": round(ema21_v, 4),
        "ema50": round(ema50_v, 4) if ema50_v is not None else None,
        "ema200": round(ema200_v, 4) if ema200_v is not None else None,
        "extPctEma200": round(ext_pct_ema200, 2) if ext_pct_ema200 is not None else None,
        "drawdownFromRecentHigh": round(drawdown_from_high * 100, 2),
        "parabolaBroken": parabola_broken,
        "macd": macd_vals,
        "recentHigh": round(recent_high, 4),
        "recentHighLookback": min(T1_HIGH_LOOKBACK, len(closes)),
        "state": state,
        "stateLabel": state_label,
        "activeTrigger": {
            "level": trigger_level,
            "value": round(trigger_value, 4),
        },
        "distanceToTrigger": round(distance, 6),
        "cluster": "semiconductor" if ticker in CLUSTER_SEMI else None,
        "barCount": len(closes),
        "notes": notes,
    }


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    print(f"Fetching {HISTORY_URL} ...", file=sys.stderr)
    try:
        raw = fetch_history(HISTORY_URL)
    except Exception as e:
        # On fetch failure exit non-zero without writing any output - the prior
        # docs/ artefacts stay in place rather than being overwritten with a
        # broken or empty payload.
        print(f"FATAL: fetch failed: {e}", file=sys.stderr)
        return 1

    rows: list[dict[str, Any]] = [compute_for_ticker(t, raw, now_utc) for t in TICKERS]

    # Reference tickers (e.g. SLV for the silver-extension chart) - computed
    # with the same machinery, but surfaced separately so they never appear
    # in the tranche table.
    reference: dict[str, dict[str, Any]] = {}
    for t in REFERENCE_TICKERS:
        row = compute_for_ticker(t, raw, now_utc)
        if "error" in row:
            reference[t] = {"ticker": t, "error": row["error"]}
        else:
            reference[t] = {
                "ticker": t,
                "lastBarDate": row["lastBarDate"],
                "lastClose": row["lastClose"],
                "ema200": row["ema200"],
                "extPctEma200": row["extPctEma200"],
            }

    # Cluster flag: semiconductor cluster breaks if 2+ members are in Tranche 2 or beyond.
    semi_broken_members = [
        r["ticker"] for r in rows
        if r.get("cluster") == "semiconductor"
        and r.get("state") in ("tranche2", "tranche3")
    ]
    semi_broken_count = len(semi_broken_members)
    cluster_break = semi_broken_count >= 2
    for r in rows:
        if r.get("cluster") == "semiconductor":
            r["clusterBreak"] = cluster_break
            r["clusterBrokenCount"] = semi_broken_count
            r["clusterBrokenMembers"] = semi_broken_members

    # Aggregate freshness flags across all valid rows.
    valid_ages = [r["barAgeDays"] for r in rows if "barAgeDays" in r]
    max_bar_age_days = max(valid_ages) if valid_ages else None
    weekend_stamped = [r["ticker"] for r in rows if r.get("isWeekendStamped")]
    stale_tickers = [r["ticker"] for r in rows if r.get("isStale")]

    payload: dict[str, Any] = {
        "generatedAt": now_utc.isoformat(timespec="seconds"),
        "source": HISTORY_URL,
        "assumptions": {
            "tranche1": {
                "extensionPct": T1_EXTENSION_PCT,
                "highLookback": T1_HIGH_LOOKBACK,
                "highProximityPct": T1_HIGH_PROXIMITY_PCT,
            },
            "confirmedClose": (
                "Latest bar in upstream history.json. Weekend-stamped bars are used "
                "as-is and flagged in notes; never use intraday values."
            ),
            "priceBasis": "Close (c), not adjusted close (ac).",
        },
        "freshness": {
            "asOfBuild": now_utc.isoformat(timespec="seconds"),
            "staleThresholdDays": STALE_BAR_THRESHOLD_DAYS,
            "maxBarAgeDays": round(max_bar_age_days, 2) if max_bar_age_days is not None else None,
            "staleTickers": stale_tickers,
            "weekendStampedTickers": weekend_stamped,
        },
        "cluster": {
            "semiconductor": {
                "members": sorted(CLUSTER_SEMI),
                "brokenMembers": sorted(semi_broken_members),
                "brokenCount": semi_broken_count,
                "clusterBreak": cluster_break,
                "rule": "Two or more cluster members in Tranche 2 or beyond.",
            },
        },
        "silverJanPeakPct": SILVER_JAN_PEAK_PCT,
        "reference": reference,
        "tickers": rows,
    }

    signals_pretty = json.dumps(payload, indent=2)
    signals_compact = json.dumps(payload, separators=(",", ":"))

    # data/signals.json - canonical, pretty-printed for human inspection
    # and consumed by the fetch fallback during local dev.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data_out = DATA_DIR / "signals.json"
    data_out.write_text(signals_pretty, encoding="utf-8")
    print(f"Wrote {data_out} ({data_out.stat().st_size} bytes, {len(rows)} tickers)", file=sys.stderr)

    # docs/index.html - template with inlined JSON so the GitHub Pages
    # deployment has no fetch dependency.
    if not TEMPLATE_PATH.exists():
        print(f"FATAL: template missing at {TEMPLATE_PATH}", file=sys.stderr)
        return 2
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if SUBSTITUTION_MARKER not in template:
        print(f"FATAL: substitution marker not found in {TEMPLATE_PATH}", file=sys.stderr)
        print(f"       expected literal: {SUBSTITUTION_MARKER!r}", file=sys.stderr)
        return 3
    rendered = template.replace(
        SUBSTITUTION_MARKER,
        f"const PRELOADED_DATA = {signals_compact};",
    )
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    docs_index = DOCS_DIR / "index.html"
    docs_index.write_text(rendered, encoding="utf-8")
    print(f"Wrote {docs_index} ({docs_index.stat().st_size} bytes)", file=sys.stderr)

    # docs/data/signals.json - copy of the canonical JSON so the published
    # page also exposes a stable URL for any external consumer.
    docs_data = DOCS_DIR / "data"
    docs_data.mkdir(parents=True, exist_ok=True)
    docs_signals = docs_data / "signals.json"
    docs_signals.write_text(signals_pretty, encoding="utf-8")
    print(f"Wrote {docs_signals} ({docs_signals.stat().st_size} bytes)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
