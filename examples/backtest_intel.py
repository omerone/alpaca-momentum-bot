"""Long strategy with vs without opening intelligence — before/after backtest.

Usage:
    python examples/backtest_intel.py META,NVDA,AMZN,TSLA,AMD,AAPL 2026-07-13 2026-08-11

BASE  = the improved long strategy (window, ATR stops, risk sizing)
INTEL = same, but each day a symbol is tradable only if:
        first-5-min RVOL >= rvol_min, premarket volume >= 200k,
        and it did NOT gap >= 5% yesterday (exhaustion veto).
"""

import sys
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import logging
logging.basicConfig(level=logging.WARNING)

import pandas as pd

from config import config
from opening_intel import backtest_intel, eligibility
from analyze_stock import fetch_range
from backtest_shorts import replay_day, daily_atr, prior_closes


def run(symbols: list[str], start: str, end: str):
    raw, data, atr_s, prev_s = {}, {}, {}, {}
    for sym in symbols:
        df = fetch_range(sym, start, end)
        raw[sym] = df                                   # keeps premarket bars
        atr_s[sym] = daily_atr(df, config.atr_period_days)
        prev_s[sym] = prior_closes(df)
        data[sym] = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]

    days = sorted(set().union(*[set(df.index.date) for df in data.values()]))

    results = {}
    skip_log: dict[str, int] = {}
    for mode in ("BASE", "INTEL"):
        all_trades = []
        for day in days:
            day_data = {s: d for s, df in data.items()
                        if not (d := df[df.index.date == day]).empty}
            if not day_data:
                continue

            if mode == "INTEL":
                allowed = {}
                for s in list(day_data):
                    ok, reason = eligibility(backtest_intel(raw[s], day), config)
                    if ok:
                        allowed[s] = day_data[s]
                    else:
                        key = reason.split("(")[0].strip()
                        skip_log[key] = skip_log.get(key, 0) + 1
                day_data = allowed
                if not day_data:
                    continue

            atrs = {s: atr_s[s].get(day) for s in day_data}
            prevs = {s: prev_s[s].get(day) for s in day_data}
            all_trades += replay_day(day_data, atrs, prevs, {"long"})
        results[mode] = all_trades

    print(f"\n=== {','.join(symbols)}  {start} .. {end} ===\n")
    print(f"{'mode':<6} {'trades':>6} {'win%':>5} {'PF':>6} {'total P/L':>12}")
    print("-" * 42)
    for name, trades in results.items():
        pls = [tr[7] for tr in trades]
        wins = sum(p > 0 for p in pls)
        gw = sum(p for p in pls if p > 0)
        gl = -sum(p for p in pls if p < 0)
        pf = gw / gl if gl else float("inf")
        print(f"{name:<6} {len(trades):>6} {wins / len(pls) * 100 if pls else 0:>4.0f}% "
              f"{pf:>6.2f} {sum(pls):>+12.2f}")

    if skip_log:
        print("\nsymbol-days filtered out by intel:")
        for reason, n in sorted(skip_log.items(), key=lambda x: -x[1]):
            print(f"  {n:>4}  {reason}")


if __name__ == "__main__":
    run(sys.argv[1].upper().split(","), sys.argv[2], sys.argv[3])
