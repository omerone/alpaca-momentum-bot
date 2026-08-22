"""Edge-case & robustness lab for the proposed upgrades (SPY gate / ORB / PEAD).

Usage:
    python examples/backtest_edge.py META,NVDA,AMZN,TSLA,AMD,AAPL 2026-07-13 2026-08-11

Variants tested side by side:
    BASE      current improved longs (tick+VWAP+volume entry, ATR stops)
    SPY_VWAP  BASE + long entries only while SPY trades above its intraday VWAP
    SPY_RET   BASE + entries only while SPY 30-min return > 0  (gate-definition sensitivity)
    ORB       entry = break of first-5-min high, first candle green (paper mechanic),
              our ATR stop + trail
    ORB_T     same, with the paper's tight stop (0.1 x daily ATR), no trail
    ORB_SPY   ORB + SPY VWAP gate

Also prints edge-case counters (gate flip-flops, ORB no-trigger/doji/gap-over days,
earliest possible entries) and a PEAD diagnostic: BASE performance on
earnings-affected days vs normal days (earnings dates via yfinance).

Standalone simulation — never touches live bot state.
"""

import sys
import warnings
from datetime import time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import logging
logging.basicConfig(level=logging.WARNING)
warnings.filterwarnings("ignore")

import pandas as pd

from config import config
from analyze_stock import fetch_range
from backtest_shorts import daily_atr

EQUITY = 100_000.0
WARMUP_BARS = 21
MIN_TICK = 0.05
WINDOW = (dtime(9, 35), dtime(11, 0))
ORB_WINDOW = (dtime(9, 36), dtime(11, 0))
EOD = dtime(15, 55)
RISK = EQUITY * 0.01
MAX_VALUE = config.max_position_value_usd

counters: dict = {}


def count(key):
    counters[key] = counters.get(key, 0) + 1


def spy_context(spy_day: pd.DataFrame) -> pd.DataFrame:
    """Per-minute SPY state: above/below its cumulative VWAP, 30-min return."""
    df = spy_day.copy()
    typical = (df.high + df.low + df.close) / 3
    df["cum_vwap"] = (typical * df.volume).cumsum() / df.volume.cumsum()
    df["above_vwap"] = df.close > df.cum_vwap
    df["ret30"] = df.close.pct_change(30)
    return df


def orb_levels(day_df: pd.DataFrame):
    """First-5-min range. Returns (orh, green, doji, open_above)."""
    first5 = day_df[(day_df.index.time >= dtime(9, 30)) & (day_df.index.time < dtime(9, 35))]
    if len(first5) < 3:
        return None
    orh = float(first5.high.max())
    o, c = float(first5.iloc[0].open), float(first5.iloc[-1].close)
    green = c > o
    doji = abs(c - o) / o < 0.0003
    return {"orh": orh, "green": green, "doji": doji}


def replay_day(day_data, atrs, spy_df, variant) -> list:
    open_pos, trades = {}, []
    entered_today = set()          # ORB: one attempt per symbol per day (paper rule)
    all_times = sorted(set().union(*[set(df.index) for df in day_data.values()]))
    orb = {s: orb_levels(df) for s, df in day_data.items()} if variant.startswith("ORB") else {}
    spy = spy_context(spy_df) if spy_df is not None else None
    prev_gate = None

    def close(sym, t, price, reason):
        p = open_pos.pop(sym)
        trades.append((sym, p["t_in"], t, p["entry"], price, p["qty"],
                       (price - p["entry"]) * p["qty"], reason))

    def gate_ok(t):
        nonlocal prev_gate
        if spy is None or "SPY" not in variant:
            return True
        try:
            row = spy.loc[:t].iloc[-1]
        except (IndexError, KeyError):
            return True
        ok = bool(row["above_vwap"]) if "VWAP" in variant or variant == "ORB_SPY" \
             else bool(row["ret30"] > 0) if not pd.isna(row["ret30"]) else True
        if prev_gate is not None and ok != prev_gate:
            count(f"{variant}:gate_flip")
        prev_gate = ok
        if not ok:
            count(f"{variant}:blocked_by_gate")
        return ok

    for t in all_times:
        for sym in list(open_pos):
            df = day_data[sym]
            if t not in df.index:
                continue
            row, p = df.loc[t], open_pos[sym]
            if t.time() >= EOD:
                close(sym, t, float(row.close), "eod")
                continue
            if row.low <= p["stop"]:
                close(sym, t, min(p["stop"], float(row.open)), "stop")
                continue
            if p["trail_d"] is not None and row.close > p["best"]:
                p["best"] = float(row.close)
                if p["best"] - p["entry"] >= 0.25 * p["stop_d"]:
                    p["stop"] = max(p["stop"], p["best"] - p["trail_d"])

        window = ORB_WINDOW if variant.startswith("ORB") else WINDOW
        if not (window[0] <= t.time() < window[1]) or len(open_pos) >= config.max_positions:
            continue

        candidates = []
        for sym, df in day_data.items():
            if sym in open_pos or t not in df.index:
                continue
            atr = atrs.get(sym)
            if not atr or pd.isna(atr) or atr <= 0:
                continue
            i = df.index.get_loc(t)
            row = df.iloc[i]

            if variant.startswith("ORB"):
                lv = orb.get(sym)
                if not lv or sym in entered_today:
                    continue
                if lv["doji"]:
                    continue
                if not lv["green"]:
                    continue
                if float(row.high) < lv["orh"]:
                    continue
                price = max(lv["orh"], float(row.open))     # stop-order fill
                candidates.append((0, sym, price))
            else:
                if i < WARMUP_BARS:
                    continue
                prev = df.iloc[i - 1]
                tick = (row.close - prev.close) / prev.close * 100
                if tick < MIN_TICK:
                    continue
                day_hist = df.iloc[:i + 1]
                typical = (day_hist.high + day_hist.low + day_hist.close) / 3
                vwap = (typical * day_hist.volume).sum() / day_hist.volume.sum()
                vs_vwap = (row.close - vwap) / vwap * 100
                if row.close <= vwap or vs_vwap < config.min_vwap_distance_pct:
                    continue
                vol_ratio = row.volume / df.iloc[i - 20:i].volume.mean()
                if vol_ratio < config.min_volume_ratio:
                    continue
                score = tick * 10 + vs_vwap * 2 + vol_ratio * 5
                candidates.append((score, sym, float(row.close)))

        for score, sym, price in sorted(candidates, reverse=True):
            if len(open_pos) >= config.max_positions or sym in open_pos:
                continue
            if not gate_ok(t):
                break
            atr = atrs[sym]
            if variant == "ORB_T":
                stop_d, trail_d = 0.1 * atr, None
            else:
                stop_d, trail_d = config.atr_stop_mult * atr, config.atr_trail_mult * atr
            qty = min(RISK / stop_d, MAX_VALUE / price)
            open_pos[sym] = dict(entry=price, qty=qty, stop=price - stop_d,
                                 stop_d=stop_d, trail_d=trail_d, best=price, t_in=t)
            entered_today.add(sym)
            count(f"{variant}:entry@{t.strftime('%H:%M')[:4]}0")

    for sym in list(open_pos):
        df = day_data[sym]
        close(sym, df.index[-1], float(df.iloc[-1].close), "eod")
    return trades


def earnings_days(symbols, start, end) -> dict[str, set]:
    """symbol -> set of dates 'affected by earnings' (report day and day after)."""
    import yfinance as yf
    lo = pd.Timestamp(start).date() - timedelta(days=3)
    hi = pd.Timestamp(end).date() + timedelta(days=1)
    out = {}
    for sym in symbols:
        days = set()
        try:
            ed = yf.Ticker(sym).get_earnings_dates(limit=40)
            for d in ed.index:
                d = d.date()
                if lo <= d <= hi:
                    days.add(d)
                    days.add(d + timedelta(days=1))
        except Exception as e:
            print(f"  ! earnings lookup failed for {sym}: {e}")
        out[sym] = days
    return out


def stats(trades):
    pls = [tr[6] for tr in trades]
    wins = sum(p > 0 for p in pls)
    gw = sum(p for p in pls if p > 0)
    gl = -sum(p for p in pls if p < 0)
    return {
        "n": len(trades),
        "win": wins / len(pls) * 100 if pls else 0,
        "pf": gw / gl if gl else float("inf"),
        "total": sum(pls),
    }


def run(symbols, start, end):
    data, atr_s = {}, {}
    for sym in symbols + ["SPY"]:
        df = fetch_range(sym, start, end)
        if sym != "SPY":
            atr_s[sym] = daily_atr(df, config.atr_period_days)
        rth = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]
        data[sym] = rth
    spy_all = data.pop("SPY")

    days = sorted(set().union(*[set(df.index.date) for df in data.values()]))
    variants = ["BASE", "SPY_VWAP", "SPY_RET", "ORB", "ORB_T", "ORB_SPY"]
    results = {}

    for variant in variants:
        all_trades = []
        for day in days:
            day_data = {s: d for s, df in data.items()
                        if not (d := df[df.index.date == day]).empty}
            if not day_data:
                continue
            spy_day = spy_all[spy_all.index.date == day]
            atrs = {s: atr_s[s].get(day) for s in day_data}
            all_trades += replay_day(day_data, atrs, spy_day if len(spy_day) else None, variant)
        results[variant] = all_trades

    print(f"\n=== {','.join(symbols)}  {start} .. {end} ===\n")
    print(f"{'variant':<9} {'trades':>6} {'win%':>5} {'PF':>6} {'total P/L':>12}")
    print("-" * 45)
    for v in variants:
        s = stats(results[v])
        print(f"{v:<9} {s['n']:>6} {s['win']:>4.0f}% {s['pf']:>6.2f} {s['total']:>+12.2f}")

    # PEAD diagnostic on BASE trades
    edays = earnings_days(symbols, start, end)
    on_e = [tr for tr in results["BASE"] if tr[1].date() in edays.get(tr[0], set())]
    off_e = [tr for tr in results["BASE"] if tr[1].date() not in edays.get(tr[0], set())]
    se, so = stats(on_e), stats(off_e)
    print(f"\nPEAD diagnostic (BASE trades):")
    print(f"  earnings-affected days: {se['n']:>3} trades  win {se['win']:.0f}%  P/L {se['total']:+,.2f}")
    print(f"  normal days:            {so['n']:>3} trades  win {so['win']:.0f}%  P/L {so['total']:+,.2f}")

    print("\nedge-case counters:")
    for k in sorted(counters):
        print(f"  {counters[k]:>4}  {k}")
    counters.clear()


if __name__ == "__main__":
    run(sys.argv[1].upper().split(","), sys.argv[2], sys.argv[3])
