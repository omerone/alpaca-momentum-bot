"""Opening intelligence — premarket-derived signals, computed without ever
trading in premarket.

Research basis (see memory/improvement notes):
- Buying gaps at the open is a documented NEGATIVE edge (-0.25R avg); the gap
  is a *universe selector*, entries need post-open confirmation.
- First-5-min relative volume (today vs same window over 14 days) is the
  best-validated "stock in play" feature (Sharpe 0.48 -> 2.81 in Zarattini
  et al. 2024).
- Premarket volume floor (~200k shares) gates "empty" gaps with no real
  participation.
- A stock that already gapped hard yesterday is exhaustion-prone today.

Free-tier data path: Alpaca historical API returns full SIP data for anything
older than ~15 minutes, so exact values exist by ~09:52 ET. A provisional
IEX-based pass (IEX numerator over IEX denominator, internally consistent as
a ratio) covers 09:36-09:52.
"""

import logging
from datetime import datetime, time as dtime, timedelta

import pandas as pd
import pytz

ET = pytz.timezone("America/New_York")
logger = logging.getLogger(__name__)

RVOL_WINDOW = (dtime(9, 30), dtime(9, 35))     # first five 1-min bars
PM_WINDOW = (dtime(4, 0), dtime(9, 30))
BASELINE_DAYS = 14
MIN_BASELINE_DAYS = 5


def eligibility(intel: dict | None, cfg) -> tuple[bool, str]:
    """Is this symbol tradable today? Returns (ok, short reason)."""
    if not intel:
        return False, "אין נתונים"
    if intel.get("prev_gap_pct") is not None and abs(intel["prev_gap_pct"]) >= cfg.gap_veto_pct:
        return False, f"גאפ אתמול {intel['prev_gap_pct']:+.1f}%"
    if intel.get("pm_vol") is not None and intel["pm_vol"] < cfg.premarket_vol_min:
        return False, f"נפח פרה-מרקט דל ({intel['pm_vol']:,.0f})"
    if intel.get("rvol") is not None and intel["rvol"] < cfg.rvol_min:
        return False, f"RVOL נמוך ({intel['rvol']:.1f}x)"
    return True, "במשחק"


# ---------------------------------------------------------------------------
# Backtest path: everything from a minute-bar DataFrame that includes
# extended hours. Uses only information known by 09:35 of the target day.
# ---------------------------------------------------------------------------

def _window_volume(day_df: pd.DataFrame, lo: dtime, hi: dtime) -> float:
    m = day_df[(day_df.index.time >= lo) & (day_df.index.time < hi)]
    return float(m["volume"].sum())


def backtest_intel(raw: pd.DataFrame, day) -> dict | None:
    """Intel for `day` from a full (extended-hours) minute DataFrame."""
    dates = sorted({d for d in raw.index.date})
    if day not in dates:
        return None
    idx = dates.index(day)
    if idx < 1:
        return None
    prior_days = dates[max(0, idx - BASELINE_DAYS):idx]

    day_df = raw[raw.index.date == day]
    prev_df = raw[raw.index.date == prior_days[-1]]

    def rth_close(df):
        rth = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]
        return float(rth.iloc[-1]["close"]) if len(rth) else None

    def rth_open(df):
        rth = df[df.index.time >= dtime(9, 30)]
        return float(rth.iloc[0]["open"]) if len(rth) else None

    prev_close = rth_close(prev_df)
    today_open = rth_open(day_df)
    if not prev_close or not today_open:
        return None

    gap_pct = (today_open - prev_close) / prev_close * 100

    # yesterday's own gap (for the gap-after-gap veto)
    prev_gap_pct = None
    if idx >= 2:
        prev2_close = rth_close(raw[raw.index.date == dates[idx - 2]])
        prev_open = rth_open(prev_df)
        if prev2_close and prev_open:
            prev_gap_pct = (prev_open - prev2_close) / prev2_close * 100

    pm_vol = _window_volume(day_df, *PM_WINDOW)

    num = _window_volume(day_df, *RVOL_WINDOW)
    base = [_window_volume(raw[raw.index.date == d], *RVOL_WINDOW) for d in prior_days]
    base = [b for b in base if b > 0]
    rvol = (num / (sum(base) / len(base))) if len(base) >= MIN_BASELINE_DAYS else None

    return {
        "gap_pct": round(gap_pct, 2),
        "prev_gap_pct": round(prev_gap_pct, 2) if prev_gap_pct is not None else None,
        "pm_vol": pm_vol,
        "rvol": round(rvol, 2) if rvol is not None else None,
    }


# ---------------------------------------------------------------------------
# Live path: two-stage — provisional IEX ratio at 09:36, exact SIP at 09:52.
# ---------------------------------------------------------------------------

def _fetch_bars(client, symbols, start, end, feed):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    req = StockBarsRequest(
        symbol_or_symbols=list(symbols), timeframe=TimeFrame.Minute,
        start=start, end=end,
        feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
    )
    return client.get_stock_bars(req).data


def compute_baseline(client, symbols, feed: str) -> dict:
    """Per-symbol: avg first-5-min volume over the last 14 trading days,
    prior close, and yesterday's gap. All data is >1 day old — safe for SIP."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    now = datetime.now(ET)
    start = now - timedelta(days=BASELINE_DAYS * 2 + 7)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)

    out = {s: {"avg_open_vol": None, "prev_close": None, "prev_gap_pct": None} for s in symbols}

    # daily bars for prev close + yesterday's gap (SIP-quality, old data)
    try:
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day,
                               start=start, end=end, feed=DataFeed.SIP)
        daily = client.get_stock_bars(req).data
        for sym, bars in daily.items():
            if len(bars) >= 1:
                out[sym]["prev_close"] = float(bars[-1].close)
            if len(bars) >= 2:
                p2c, po = float(bars[-2].close), float(bars[-1].open)
                out[sym]["prev_gap_pct"] = round((po - p2c) / p2c * 100, 2)
    except Exception as e:
        logger.warning("Intel baseline daily bars failed: %s", e)

    # minute bars for the 9:30-9:35 volume baseline, in the requested feed so
    # the live ratio compares like with like
    try:
        bars = _fetch_bars(client, symbols, start, end, feed)
        for sym, blist in bars.items():
            sums: dict = {}
            for b in blist:
                ts = b.timestamp.astimezone(ET)
                if RVOL_WINDOW[0] <= ts.time() < RVOL_WINDOW[1]:
                    sums[ts.date()] = sums.get(ts.date(), 0.0) + float(b.volume)
            vals = [v for v in sums.values() if v > 0][-BASELINE_DAYS:]
            if len(vals) >= MIN_BASELINE_DAYS:
                out[sym]["avg_open_vol"] = sum(vals) / len(vals)
    except Exception as e:
        logger.warning("Intel baseline minute bars failed (%s feed): %s", feed, e)

    return out


def compute_today(client, symbols, baseline: dict, feed: str) -> dict[str, dict]:
    """Today's intel: gap, RVOL vs baseline, premarket volume (SIP pass only)."""
    now = datetime.now(ET)
    day_start = now.replace(hour=4, minute=0, second=0, microsecond=0)
    end = now.replace(hour=9, minute=36, second=0, microsecond=0)
    if feed == "sip":
        end = min(end, now - timedelta(minutes=16))

    intel: dict[str, dict] = {}
    try:
        bars = _fetch_bars(client, symbols, day_start, end, feed)
    except Exception as e:
        logger.warning("Intel today bars failed (%s): %s", feed, e)
        return intel

    for sym in symbols:
        blist = bars.get(sym, [])
        if not blist:
            continue
        open_px = None
        rvol_vol = 0.0
        pm_vol = 0.0
        for b in blist:
            ts = b.timestamp.astimezone(ET)
            t = ts.time()
            if PM_WINDOW[0] <= t < PM_WINDOW[1]:
                pm_vol += float(b.volume)
            if RVOL_WINDOW[0] <= t < RVOL_WINDOW[1]:
                rvol_vol += float(b.volume)
                if open_px is None:
                    open_px = float(b.open)

        base = baseline.get(sym, {})
        prev_close = base.get("prev_close")
        avg_vol = base.get("avg_open_vol")
        intel[sym] = {
            "gap_pct": round((open_px - prev_close) / prev_close * 100, 2)
                       if open_px and prev_close else None,
            "prev_gap_pct": base.get("prev_gap_pct"),
            # IEX premarket coverage is a biased sliver -> only trust SIP
            "pm_vol": pm_vol if feed == "sip" else None,
            "rvol": round(rvol_vol / avg_vol, 2) if avg_vol and rvol_vol else None,
            "quality": feed,
        }
    return intel
