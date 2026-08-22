"""Which symbols carry the strategy, and which just donate?

Not a parameter sweep — those are exhausted (stop width, trail width, risk %,
position count, ORB entries: every winner was a lone spike). This asks a
different question: the strategy is applied to 19 names on the focus list, but
is the edge spread across them or concentrated in a few?

Method: run the LIVE strategy on one symbol at a time, per period, so each
symbol's contribution is isolated from competition for position slots.

Honesty guard: picking symbols by their own backtest is textbook overfitting.
The only defensible signal is CONSISTENCY — a name that loses in all four
regimes is a different claim from one that lost once. Nothing here is adopted
on a total; the report is per-period so a single blow-up cannot masquerade as
a verdict.

Usage:  python examples/backtest_symbols.py
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
from backtest_orb import replay_day, stats, PERIODS, SYMBOLS


def run_period(start, end) -> dict:
    data, atr_s = {}, {}
    for sym in SYMBOLS + ["SPY"]:
        try:
            df = fetch_range(sym, start, end)
        except Exception:
            continue
        if df.empty:
            continue
        if sym != "SPY":
            atr_s[sym] = daily_atr(df, config.atr_period_days)
        data[sym] = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]

    spy_all = data.pop("SPY", None)
    days = sorted(set().union(*[set(df.index.date) for df in data.values()])) if data else []

    out = {}
    for sym, df in data.items():
        trades = []
        for day in days:
            d = df[df.index.date == day]
            if d.empty:
                continue
            spy_day = spy_all[spy_all.index.date == day] if spy_all is not None else None
            atr = atr_s[sym].get(day)
            trades += replay_day({sym: d}, {sym: atr},
                                 spy_day if spy_day is not None and len(spy_day) else None,
                                 "BASE", None)
        out[sym] = stats(trades)
    return out


def main():
    print("האסטרטגיה החיה, מניה אחת בכל פעם — כדי לבודד את התרומה של כל אחת.\n")
    table = {}
    for name, start, end in PERIODS:
        res = run_period(start, end)
        if res:
            table[name] = res
            print(f"  {name}: נותחו {len(res)} מניות")
    if len(table) < 2:
        return

    print(f"\n{'מניה':7}" + "".join(f"{n:>12}" for n in table) + f"{'סה\"כ':>12}{'תקופות מפסידות':>17}")
    print("-" * 84)
    rows = []
    for sym in SYMBOLS:
        vals = [table[n].get(sym, {}).get("total", 0.0) for n in table]
        losing = sum(1 for v in vals if v < 0)
        rows.append((sum(vals), losing, sym, vals))
    for total, losing, sym, vals in sorted(rows, reverse=True):
        flag = "  ← מפסידה בכל תקופה" if losing == len(table) else ""
        print(f"{sym:7}" + "".join(f"{v:>+12,.0f}" for v in vals) + f"{total:>+12,.0f}{losing:>10}/{len(table)}{flag}")

    always_bad = [s for t, l, s, v in rows if l == len(table)]
    always_good = [s for t, l, s, v in rows if l == 0]
    print()
    print(f"מפסידות בכל {len(table)} התקופות: {', '.join(always_bad) or 'אין'}")
    print(f"רווחיות בכל {len(table)} התקופות: {', '.join(always_good) or 'אין'}")
    print()
    keep = [s for t, l, s, v in rows if l < len(table)]
    kept = sum(sum(table[n].get(s, {}).get("total", 0.0) for s in keep) for n in table)
    allsum = sum(sum(table[n].get(s, {}).get("total", 0.0) for s in SYMBOLS) for n in table)
    print(f"סך הכל על כל {len(SYMBOLS)} המניות: {allsum:>+12,.0f}")
    print(f"בלי המפסידות-תמיד ({len(keep)} מניות): {kept:>+12,.0f}")
    print("\nאזהרה: הבחירה נעשתה על אותם נתונים שנמדדו. עקביות על פני 4 משטרי שוק")
    print("היא הראיה הכי חזקה שיש כאן, אבל היא עדיין לא מדגם בלתי תלוי.")


if __name__ == "__main__":
    main()
