"""Does capping the trailing stop as a % of price beat scaling it by daily ATR?

The hypothesis, formed on 2026-08-17 after INTC gave back a +2.56% move:

    The trail is `atr_trail_mult * daily ATR`. On a 6%-ATR name that is 2.2% of
    price, so the trailing stop only rises above the entry after a 2.2% gain —
    further than most intraday momentum moves ever travel. The trail therefore
    never engages on exactly the volatile names it was meant to protect.

    Proposed fix:  trail = min(atr_trail_mult * ATR, cap% * price)

This is NOT the same experiment as "use a smaller multiple", which was run on
2026-08-12 and REJECTED: 0.25x looked better in-sample and lost out-of-sample
(+3,903 vs +5,788). A smaller multiple shrinks the trail everywhere; a cap only
binds on high-ATR names and leaves SPY/AAPL untouched. Different mechanism,
so it gets its own test — and the same standard of proof.

Standard of proof (the one the SPY gate had to clear):
    a change is worth adopting only if it helps, or at least does not hurt, in
    EVERY period tested — not if it wins on the total. Totals are dominated by
    whichever regime happened to be favourable.

Usage:
    python examples/backtest_trail_cap.py
    python examples/backtest_trail_cap.py NVDA,TSLA,INTC 2026-03-01 2026-04-30

Standalone simulation — never touches live bot state.
"""

import sys
import warnings
from datetime import time as dtime
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
EOD = dtime(15, 55)
RISK = EQUITY * 0.01
MAX_VALUE = config.max_position_value_usd

# The live strategy, exactly: momentum + VWAP + volume, ATR stop, SPY-VWAP gate.
# Only the trail distance changes between variants.
VARIANTS = [
    ("ATR בלבד (נוכחי)", None),
    ("תקרה 1.2%", 1.2),
    ("תקרה 1.0%", 1.0),
    ("תקרה 0.8%", 0.8),
    ("תקרה 0.7%", 0.7),
    ("תקרה 0.5%", 0.5),
]

# Regime-diverse periods. Per the 2026-08-12 out-of-sample study, BASE was
# strongly positive only in Mar-Apr, negative in Jan-Feb and May-Jun — so a
# change that only wins in one of them has proved nothing.
PERIODS = [
    ("ינו-פבר", "2026-01-02", "2026-02-27"),
    ("מרץ-אפר", "2026-03-02", "2026-04-30"),
    ("מאי-יוני", "2026-05-01", "2026-06-30"),
    ("יולי-אוג", "2026-07-01", "2026-08-14"),
]

DEFAULT_SYMBOLS = ["NVDA", "TSLA", "META", "AMD", "AMZN", "AAPL", "MSFT", "INTC", "ORCL", "QCOM"]


def spy_context(spy_day: pd.DataFrame) -> pd.DataFrame:
    df = spy_day.copy()
    typical = (df.high + df.low + df.close) / 3
    df["cum_vwap"] = (typical * df.volume).cumsum() / df.volume.cumsum()
    df["above_vwap"] = df.close > df.cum_vwap
    return df


def replay_day(day_data, atrs, spy_df, cap_pct) -> list:
    """One trading day under the live rules, with `cap_pct` bounding the trail."""
    open_pos, trades = {}, []
    all_times = sorted(set().union(*[set(df.index) for df in day_data.values()]))
    spy = spy_context(spy_df) if spy_df is not None else None

    def close(sym, t, price, reason):
        p = open_pos.pop(sym)
        trades.append((sym, p["t_in"], t, p["entry"], price, p["qty"],
                       (price - p["entry"]) * p["qty"], reason))

    def gate_ok(t):
        if spy is None:
            return True
        try:
            return bool(spy.loc[:t].iloc[-1]["above_vwap"])
        except (IndexError, KeyError):
            return True

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
            if row.close > p["best"]:
                p["best"] = float(row.close)
                if p["best"] - p["entry"] >= config.atr_trail_activate * p["stop_d"]:
                    p["stop"] = max(p["stop"], p["best"] - p["trail_d"])

        if not (WINDOW[0] <= t.time() < WINDOW[1]) or len(open_pos) >= config.max_positions:
            continue

        candidates = []
        for sym, df in day_data.items():
            if sym in open_pos or t not in df.index:
                continue
            atr = atrs.get(sym)
            if not atr or pd.isna(atr) or atr <= 0:
                continue
            i = df.index.get_loc(t)
            if i < WARMUP_BARS:
                continue
            row, prev = df.iloc[i], df.iloc[i - 1]
            tick = (row.close - prev.close) / prev.close * 100
            if tick < MIN_TICK:
                continue
            hist = df.iloc[:i + 1]
            typical = (hist.high + hist.low + hist.close) / 3
            vwap = (typical * hist.volume).sum() / hist.volume.sum()
            vs_vwap = (row.close - vwap) / vwap * 100
            if row.close <= vwap or vs_vwap < config.min_vwap_distance_pct:
                continue
            if row.volume / df.iloc[i - 20:i].volume.mean() < config.min_volume_ratio:
                continue
            vol_ratio = row.volume / df.iloc[i - 20:i].volume.mean()
            candidates.append((tick * 10 + vs_vwap * 2 + vol_ratio * 5, sym, float(row.close)))

        for score, sym, price in sorted(candidates, reverse=True):
            if len(open_pos) >= config.max_positions or sym in open_pos:
                continue
            if not gate_ok(t):
                break
            atr = atrs[sym]
            stop_d = config.atr_stop_mult * atr
            trail_d = config.atr_trail_mult * atr
            if cap_pct is not None:                       # the whole experiment
                trail_d = min(trail_d, cap_pct / 100 * price)
            qty = min(RISK / stop_d, MAX_VALUE / price)
            open_pos[sym] = dict(entry=price, qty=qty, stop=price - stop_d,
                                 stop_d=stop_d, trail_d=trail_d, best=price, t_in=t)

    for sym in list(open_pos):
        df = day_data[sym]
        close(sym, df.index[-1], float(df.iloc[-1].close), "eod")
    return trades


def stats(trades) -> dict:
    if not trades:
        return {"n": 0, "win": 0.0, "pf": 0.0, "total": 0.0}
    pnl = [t[6] for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p < 0]
    return {
        "n": len(pnl),
        "win": len(wins) / len(pnl) * 100,
        "pf": (sum(wins) / sum(losses)) if losses else float("inf"),
        "total": sum(pnl),
    }


def run_period(symbols, start, end) -> dict:
    data, atr_s = {}, {}
    for sym in symbols + ["SPY"]:
        try:
            df = fetch_range(sym, start, end)
        except Exception as e:
            print(f"  ! {sym}: {e}")
            continue
        if df.empty:
            continue
        if sym != "SPY":
            atr_s[sym] = daily_atr(df, config.atr_period_days)
        data[sym] = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]

    spy_all = data.pop("SPY", None)
    if not data:
        return {}
    days = sorted(set().union(*[set(df.index.date) for df in data.values()]))

    out = {}
    for label, cap in VARIANTS:
        trades = []
        for day in days:
            day_data = {s: d for s, df in data.items()
                        if not (d := df[df.index.date == day]).empty}
            if not day_data:
                continue
            spy_day = spy_all[spy_all.index.date == day] if spy_all is not None else None
            atrs = {s: atr_s[s].get(day) for s in day_data}
            trades += replay_day(day_data, atrs, spy_day if spy_day is not None and len(spy_day) else None, cap)
        out[label] = stats(trades)
    return out


def main():
    if len(sys.argv) > 3:
        symbols = sys.argv[1].split(",")
        periods = [("מותאם", sys.argv[2], sys.argv[3])]
    else:
        symbols, periods = DEFAULT_SYMBOLS, PERIODS

    print(f"מניות: {', '.join(symbols)}")
    print(f"האסטרטגיה החיה (מומנטום + VWAP + נפח + שער SPY). משתנה רק רוחב הטריילינג.\n")

    table = {}
    for name, start, end in periods:
        print(f"--- {name} ({start} .. {end}) ---")
        res = run_period(symbols, start, end)
        if not res:
            print("  אין נתונים\n")
            continue
        table[name] = res
        base = res[VARIANTS[0][0]]
        print(f"{'גרסה':20} {'עסקאות':>7} {'הצלחה':>7} {'PF':>6} {'סה\"כ':>12} {'מול נוכחי':>12}")
        for label, _ in VARIANTS:
            s = res[label]
            delta = s["total"] - base["total"]
            mark = "" if label == VARIANTS[0][0] else f"{delta:>+12,.0f}"
            print(f"{label:20} {s['n']:>7} {s['win']:>6.0f}% {s['pf']:>6.2f} {s['total']:>+12,.0f} {mark}")
        print()

    if len(table) < 2:
        return
    print("=" * 78)
    print("הרוחבי היחיד שקובע: האם השינוי עוזר (או לפחות לא מזיק) בכל תקופה בנפרד?\n")
    print(f"{'גרסה':20}" + "".join(f"{n:>14}" for n in table) + f"{'תקופות גרועות':>16}")
    print("-" * 78)
    for label, _ in VARIANTS:
        deltas = [table[n][label]["total"] - table[n][VARIANTS[0][0]]["total"] for n in table]
        worse = sum(1 for d in deltas if d < -1)
        row = "".join(f"{d:>+14,.0f}" for d in deltas)
        verdict = "—" if label == VARIANTS[0][0] else (f"{worse} מתוך {len(deltas)}")
        print(f"{label:20}{row}{verdict:>16}")
    print("\nכלל ההכרעה: אימוץ רק אם 0 תקופות גרועות. אחרת זו התאמה לרעש.")


if __name__ == "__main__":
    main()
