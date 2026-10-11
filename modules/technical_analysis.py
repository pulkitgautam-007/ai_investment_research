"""
Technical Analysis Module (Context/Signals architecture)
========================================================
Part of the AI Investment Research System.

PHILOSOPHY (settled by design — read before the code):
  This module DETECTS and DESCRIBES. It does NOT judge importance.
  "This is statistically unusual" is measurable and belongs here.
  "This matters" is interpretation and belongs to report synthesis,
  which can weigh this alongside the fundamental and macro picture.
  So: it describes DIRECTION and CHARACTER (measurable). It does not
  EVALUATE consequence. "Downtrend" yes; "bearish" no. No verdict.

TWO REGISTERS (sorted by the nature of the statement, not by indicator):
  CONTEXT  - always present, exactly one sentence, trend-POSITION only.
             Judged by "is it true, current, concise?" - not by significance.
  SIGNALS  - conditional, threshold-gated, RELATIVE TO THE STOCK'S OWN
             HISTORY. Silent when nothing clears the bar. An empty signals
             list is a valid, meaningful result ("nothing unusual").

CONCEPT vs PARAMETER:
  The trend signal reports a "recent trend REGIME CHANGE" (the phenomenon),
  detected via a qualifying crossover within a recent-observation window
  (the implementation). "Recent" currently = 5 trading days, but that number
  is a tunable parameter, not part of the concept.

Public entry point:
    analyze_technical(ticker, verbose=False) -> dict

Starting thresholds (HYPOTHESES - to be tuned against real AAPL / INFY.NS):
  Volume     : today in top 5% of the year AND 2 of last 3 days in top 10%
  Volatility : recent 21-day vol >= 1.5x the full-year vol
  Trend      : qualifying crossover within last 5 days, first in ~3 months
  Lookback   : one year of daily data, single window
"""

import yfinance as yf
import pandas as pd
import numpy as np

# Version of this module's logic/output. Bump it (1 -> 2) whenever the logic
# or the shape of the output changes (comment-only edits don't count).
# main.py saves it with every report so old and new results can be told apart.
MODULE_VERSION = "1"


# --- THRESHOLDS (collected here so calibration is a one-place edit) ---
VOLUME_TODAY_PCTILE = 0.95         # today must be in the top 5% of the year
VOLUME_SUSTAIN_PCTILE = 0.90       # "elevated" bar for the sustain check
VOLUME_SUSTAIN_DAYS_NEEDED = 2     # how many of...
VOLUME_SUSTAIN_WINDOW = 3          # ...the last N days must be elevated

VOLATILITY_RECENT_WINDOW = 21      # ~1 trading month
VOLATILITY_RATIO_TRIGGER = 1.5     # recent vol >= 1.5x baseline vol
VOLATILITY_RATIO_EX_SHOCK = 1.3    # ...AND still >= 1.3x after removing the single
                                   # biggest recent day (so one shock can't carry it)


TREND_BREAK_RECENT_DAYS = 5        # price/200 break: fast phenomenon, short "recent"
TREND_CROSS_RECENT_DAYS = 25       # 50/200 structural change: slow phenomenon, longer "recent"
TREND_BREAK_QUIET_DAYS = 63        # price break must be first in this prior window (~3mo)
TREND_CROSS_QUIET_DAYS = 63        # structural change must be first in this prior window


# ---------------------------------------------------------------------------
# STEP 1 - FETCH PRICE DATA
# ---------------------------------------------------------------------------
def _fetch_price_history(ticker, verbose=False):
    """Pull ~1y of daily bars. Returns a DataFrame, or None on failure/empty."""
    try:
        if verbose:
            print(f"  [1/3] Fetching 1y of daily price data for {ticker}...")
        data = yf.download(ticker, period="1y", interval="1d",
                           auto_adjust=True, progress=False)
        if data is None or data.empty:
            if verbose:
                print(f"  [1/3] No data returned for {ticker}.")
            return None
        # Flatten MultiIndex columns (single-ticker quirk in newer yfinance).
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        return data
    except Exception as e:
        if verbose:
            print(f"  [1/3] Price fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# STEP 2 - CONTEXT (always present; price position + plain-English character)
# ---------------------------------------------------------------------------
# Gentle character bands (looser than SIGNAL thresholds on purpose - these
# describe a mild tendency, they do NOT flag something noteworthy).
CONTEXT_VOL_SUBDUED = 0.8      # recent vol < 0.8x yearly  -> "calmer than usual"
CONTEXT_VOL_ELEVATED = 1.25    # recent vol > 1.25x yearly -> "more than usual"
CONTEXT_VOLUME_LOW = 0.7       # recent volume < 0.7x median -> "lighter than usual"
CONTEXT_VOLUME_HIGH = 1.4      # recent volume > 1.4x median -> "heavier than usual"
CONTEXT_VOLUME_RECENT_DAYS = 10  # "recent" volume = last ~2 weeks


def _price_position_sentence(latest, ma50, ma200):
    """The original one-sentence 'where the price sits' description."""
    if ma50 is not None and ma200 is not None:
        above50 = latest > ma50
        above200 = latest > ma200
        if above50 and above200:
            return "The stock is trading above both its 50-day and 200-day moving averages"
        elif not above50 and not above200:
            return "The stock is trading below both its 50-day and 200-day moving averages"
        elif above200 and not above50:
            return ("The stock is above its 200-day average but below its 50-day, "
                    "sitting between its long- and short-term trend lines")
        else:
            return ("The stock is above its 50-day average but below its 200-day, "
                    "sitting between its short- and long-term trend lines")
    elif ma50 is not None:
        side = "above" if latest > ma50 else "below"
        return (f"The stock is trading {side} its 50-day average; there isn't yet "
                "a full year of data for a 200-day average")
    else:
        return "There isn't enough price history to place the stock against its moving averages"


def _volatility_character(data):
    """Return 'subdued' / 'normal' / 'elevated' / None (if not enough data)."""
    returns = data["Close"].pct_change().dropna()
    if len(returns) < VOLATILITY_RECENT_WINDOW + 30:
        return None
    baseline = float(returns.std())
    if baseline == 0:
        return None
    recent = float(returns.iloc[-VOLATILITY_RECENT_WINDOW:].std())
    ratio = recent / baseline
    if ratio < CONTEXT_VOL_SUBDUED:
        return "subdued"
    if ratio > CONTEXT_VOL_ELEVATED:
        return "elevated"
    return "normal"


def _volume_character(data):
    """Return 'low' / 'normal' / 'high' / None (if not enough data)."""
    volume = data["Volume"].dropna()
    if len(volume) < 60:
        return None
    median_vol = float(volume.median())
    if median_vol == 0:
        return None
    recent_avg = float(volume.iloc[-CONTEXT_VOLUME_RECENT_DAYS:].mean())
    ratio = recent_avg / median_vol
    if ratio < CONTEXT_VOLUME_LOW:
        return "low"
    if ratio > CONTEXT_VOLUME_HIGH:
        return "high"
    return "normal"


def _build_context(data):
    """
    Describe the stock's current state as present-tense FACTS: where the price
    sits, plus its volatility and volume CHARACTER in plain words. Character
    words only - never numbers-for-their-own-sake, never 'why a signal didn't
    fire'. Falls back to price-position only when data is too thin.
    """
    close = data["Close"]
    latest = float(close.iloc[-1])
    ma50 = close.rolling(window=50).mean().iloc[-1]
    ma200 = close.rolling(window=200).mean().iloc[-1]
    ma50 = float(ma50) if not pd.isna(ma50) else None
    ma200 = float(ma200) if not pd.isna(ma200) else None

    position = _price_position_sentence(latest, ma50, ma200)
    vol_char = _volatility_character(data)
    volm_char = _volume_character(data)

    # Build the plain-English character clause from whatever we could compute.
    vol_phrase = {
        "subdued": "calmer-than-usual price movement",
        "normal": "typical day-to-day price movement",
        "elevated": "more price movement than usual",
    }.get(vol_char)
    volm_phrase = {
        "low": "lighter-than-usual trading activity",
        "normal": "normal trading activity",
        "high": "heavier-than-usual trading activity",
    }.get(volm_char)

    parts = [p for p in (vol_phrase, volm_phrase) if p is not None]
    if parts:
        sentence = position + ", with " + " and ".join(parts) + "."
    else:
        # Thin data - fall back to price position only.
        sentence = position + "."

    return {"sentence": sentence,
            "latest_price": round(latest, 2),
            "ma_50": round(ma50, 2) if ma50 is not None else None,
            "ma_200": round(ma200, 2) if ma200 is not None else None,
            "volatility_character": vol_char,
            "volume_character": volm_char}


# ---------------------------------------------------------------------------
# STEP 3 - SIGNALS (each returns a dict if it fires, else None)
# ---------------------------------------------------------------------------
def _volume_signal(data):
    """
    Fire only if BOTH:
      (a) today's volume is in the top 5% of the year, AND
      (b) at least 2 of the last 3 days are in the top 10% (sustained).
    """
    volume = data["Volume"].dropna()
    if len(volume) < 60:
        return None

    # rank(pct=True) gives each day its percentile within the year (0.0-1.0).
    pct_rank = volume.rank(pct=True)
    today_pct = float(pct_rank.iloc[-1])
    if today_pct < VOLUME_TODAY_PCTILE:
        return None  # today isn't extreme enough

    last3 = pct_rank.iloc[-VOLUME_SUSTAIN_WINDOW:]
    elevated_days = int((last3 >= VOLUME_SUSTAIN_PCTILE).sum())
    if elevated_days < VOLUME_SUSTAIN_DAYS_NEEDED:
        return None  # a lone spike, not sustained participation

    median_vol = float(volume.median())
    today_vol = float(volume.iloc[-1])
    x_normal = today_vol / median_vol if median_vol else None

    desc = (f"Trading volume is in the top {round((1 - VOLUME_TODAY_PCTILE) * 100)}% "
            "of the past year and has stayed elevated across recent sessions")
    desc += f" (about {x_normal:.1f}x the stock's median daily volume)." if x_normal else "."

    return {"type": "volume", "description": desc,
            "today_percentile": round(today_pct * 100, 1),
            "elevated_days_of_3": elevated_days}


def _volatility_signal(data):
    """
    Fire only if recent (21-day) annualized volatility indicates a genuine
    higher-volatility REGIME, not a single-day shock.

    Two-part test (both must hold):
      (1) recent 21-day vol >= 1.5x the full-year baseline vol, AND
      (2) with the single largest-magnitude day in that window removed,
          recent vol is STILL >= 1.3x baseline.
    A regime persists when you drop one day; a lone shock collapses.
    """
    returns = data["Close"].pct_change().dropna()
    if len(returns) < VOLATILITY_RECENT_WINDOW + 30:
        return None

    baseline = float(returns.std() * np.sqrt(252) * 100)
    if baseline == 0:
        return None

    recent_window = returns.iloc[-VOLATILITY_RECENT_WINDOW:]
    recent = float(recent_window.std() * np.sqrt(252) * 100)
    ratio = recent / baseline

    # Part 1: is recent volatility elevated at all?
    if ratio < VOLATILITY_RATIO_TRIGGER:
        return None

    # Part 2: strip the single biggest-magnitude day and re-measure.
    # If elevation depended on one shock, this collapses below the bar.
    biggest_idx = recent_window.abs().idxmax()
    recent_ex_shock = recent_window.drop(biggest_idx)
    recent_ex = float(recent_ex_shock.std() * np.sqrt(252) * 100)
    ratio_ex = recent_ex / baseline

    if ratio_ex < VOLATILITY_RATIO_EX_SHOCK:
        return None  # a single-day shock was carrying it - not a regime

    desc = (f"Recent volatility (~{recent:.0f}% annualized) is running about "
            f"{ratio:.1f}x the stock's ~{baseline:.0f}% yearly norm, and stays "
            f"elevated ({ratio_ex:.1f}x) even setting aside its single largest "
            "day - a genuinely jumpier regime, not a one-day shock.")

    return {"type": "volatility", "description": desc,
            "recent_vol_pct": round(recent, 1),
            "baseline_vol_pct": round(baseline, 1),
            "ratio": round(ratio, 2),
            "ratio_ex_shock": round(ratio_ex, 2)}

def _trend_change_signal(data):
    """
    Report RECENT TREND CHANGES as descriptions of measurable structural
    change - NOT as evaluations of significance. Two phenomena, two clocks:

      PRICE/200 BREAK  (fast): price recently crossed its 200-day average.
                       Recency window ~5 days.
      50/200 CHANGE    (slow): the 50-day average recently crossed the 200-day.
                       A crossover is inherently late (it's an average of 50
                       days), so its natural "recent" clock is longer (~25 days).

    Each event is checked against its OWN recency window, and each must be the
    first such event in its OWN preceding quiet window (materiality gate that
    keeps choppy sideways markets silent).
    """
    close = data["Close"]
    ma50 = close.rolling(window=50).mean()
    ma200 = close.rolling(window=200).mean()

    # Need enough history for a 200-day line plus the longest window we inspect.
    longest_span = max(TREND_BREAK_RECENT_DAYS + TREND_BREAK_QUIET_DAYS,
                       TREND_CROSS_RECENT_DAYS + TREND_CROSS_QUIET_DAYS)
    if len(ma200.dropna()) < longest_span + 1:
        return None

    # State series: a change in state marks a crossover/break day.
    fifty_above = (ma50 > ma200).astype(int)
    cross_days = fifty_above.diff().fillna(0)        # +1 golden, -1 death
    price_above_200 = (close > ma200).astype(int)
    break_days = price_above_200.diff().fillna(0)    # +1 broke above, -1 below

    events = []

    # --- PRICE/200 BREAK: fast clock ---
    brk_recent = break_days.iloc[-TREND_BREAK_RECENT_DAYS:]
    brk_quiet = break_days.iloc[
        -(TREND_BREAK_RECENT_DAYS + TREND_BREAK_QUIET_DAYS):-TREND_BREAK_RECENT_DAYS]
    if int((brk_quiet != 0).sum()) == 0:
        if (brk_recent == 1).any():
            events.append("price has recently risen back above its 200-day average")
        elif (brk_recent == -1).any():
            events.append("price has recently fallen below its 200-day average")

    # --- 50/200 STRUCTURAL CHANGE: slow clock ---
    cr_recent = cross_days.iloc[-TREND_CROSS_RECENT_DAYS:]
    cr_quiet = cross_days.iloc[
        -(TREND_CROSS_RECENT_DAYS + TREND_CROSS_QUIET_DAYS):-TREND_CROSS_RECENT_DAYS]
    if int((cr_quiet != 0).sum()) == 0:
        if (cr_recent == 1).any():
            events.append("the 50-day average has moved above the 200-day, a change "
                          "in the stock's longer-term trend structure")
        elif (cr_recent == -1).any():
            events.append("the 50-day average has moved below the 200-day, a change "
                          "in the stock's longer-term trend structure")

    if not events:
        return None

    desc = "Recent trend change: " + "; ".join(events) + "."
    return {"type": "trend_change", "description": desc, "events": events}

# ---------------------------------------------------------------------------
# PUBLIC ENTRY POINT
# ---------------------------------------------------------------------------
def analyze_technical(ticker, verbose=False):
    """
    Run technical analysis for `ticker`. Returns a dict for Report Generation.
    Ends at description - no verdict. Graceful fallback on fetch failure.
    """
    if verbose:
        print(f"Running technical analysis for {ticker}...")

    data = _fetch_price_history(ticker, verbose=verbose)
    if data is None:
        return {"ticker": ticker, "available": False,
                "context": {"sentence": "Price data could not be retrieved for this ticker.",
                            "latest_price": None, "ma_50": None, "ma_200": None},
                "signals": []}

    if verbose:
        print("  [2/3] Building context (trend position)...")
    context = _build_context(data)

    if verbose:
        print("  [3/3] Checking for signals (unusual behavior)...")
    signals = []
    for detector in (_volume_signal, _volatility_signal, _trend_change_signal):
        result = detector(data)
        if result is not None:
            signals.append(result)

    if verbose:
        print(f"  Done. {len(signals)} signal(s) fired." if signals
              else "  Done. No signals - nothing statistically unusual.")

    return {"ticker": ticker, "available": True,
            "context": context, "signals": signals}


# ---------------------------------------------------------------------------
# MODULE'S OWN TEST - runs only when this file is executed directly.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    for symbol in ["AAPL", "INFY.NS"]:
        print("\n" + "=" * 60)
        print(f"TESTING: {symbol}")
        print("=" * 60)
        result = analyze_technical(symbol, verbose=True)
        print(json.dumps(result, indent=2, default=str))