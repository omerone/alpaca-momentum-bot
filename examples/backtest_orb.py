"""Dedicated validation of opening-range-breakout entries with the SPY gate.

Why this and not another parameter sweep: four sweeps over stop width, trail
width, risk % and position count (12-19/08/2026) each produced a "winner" that
turned out to be a lone spike with losing neighbours. That is what a strategy
with no exploitable parameter edge looks like. The bottleneck is which trades
get taken, not how they are managed — so this tests a different ENTRY.

ORB_SPY was flagged on 12/08/2026 as profitable-or-flat in 3 fresh periods
(+4,251 / +3,827 / -94) but never adopted, because it was 1 of 6 variants tried
at once: the multiple-testing risk was untested. This script gives it the
dedicated run, with the plateau requirement applied from the start.

The entry: the stock must break the high of the first N minutes, on a green
opening candle, while SPY trades above its own VWAP. Stop and trail are the
live ATR rules, so only the ENTRY differs from what runs today.

Adoption bar (pre-stated, unchanged):
  1. helps or is neutral in EVERY period, AND
  2. its neighbours in the parameter grid do too — a real edge is a plateau.

Usage:  python examples/backtest_orb.py
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
EOD = dtime(15, 55)
RISK = EQUITY * config.risk_pct_per_trade / 100
MAX_VALUE = config.max_position_value_usd
WINDOW_END = dtime(11, 0)

# (label, kind, opening-range minutes) — the ORB rows straddle 5 min on both
# sides so the winner, if any, has to sit on a plateau.
VARIANTS = [
    ("נוכחי (מומנטום)", "BASE", None),
    ("ORB 3 דק'",       "ORB", 3),
    ("ORB 5 דק'",       "ORB", 5),
    ("ORB 10 דק'",      "ORB", 10),
    ("ORB 15 דק'",      "ORB", 15),
    ("ORB 30 דק'",      "ORB", 30),
]

PERIODS = [
    ("ינו-פבר", "2026-01-02", "2026-02-27"),
    ("מרץ-אפר", "2026-03-02", "2026-04-30"),
    ("מאי-יוני", "2026-05-01", "2026-06-30"),
    ("יולי-אוג", "2026-07-01", "2026-08-14"),
]

SYMBOLS = ["NVDA", "TSLA", "META", "AMD", "AMZN", "AAPL", "MSFT", "INTC", "ORCL", "QCOM"]


def spy_context(spy_day: pd.DataFrame) -> pd.DataFrame:
    df = spy_day.copy()
    typical = (df.high + df.low + df.close) / 3
    df["cum_vwap"] = (typical * df.volume).cumsum() / df.volume.cumsum()
    df["above_vwap"] = df.close > df.cum_vwap
    return df


def opening_range(day_df: pd.DataFrame, minutes: int):
    """High of the first `minutes`, plus the shape of that opening candle."""
    end_h, end_m = divmod(9 * 60 + 30 + minutes, 60)
    first = day_df[(day_df.index.time >= dtime(9, 30)) & (day_df.index.time < dtime(end_h, end_m))]
    if len(first) < max(3, minutes // 2):
        return None
    o, c = float(first.iloc[0].open), float(first.iloc[-1].close)
    return {
        "orh": float(first.high.max()),
        "green": c > o,
        "doji": abs(c - o) / o < 0.0003,
        "start": dtime(end_h, end_m),
    }


def replay_day(day_data, atrs, spy_df, kind, or_min) -> list:
    open_pos, trades, entered = {}, [], set()
    all_times = sorted(set().union(*[set(df.index) for df in day_data.values()]))
    spy = spy_context(spy_df) if spy_df is not None else None
    orb = {s: opening_range(df, or_min) for s, df in day_data.items()} if kind == "ORB" else {}
    win_start = dtime(9, 35) if kind == "BASE" else \
        next((v["start"] for v in orb.values() if v), dtime(9, 35))

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

        if not (win_start <= t.time() < WINDOW_END) or len(open_pos) >= config.max_positions:
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

            if kind == "ORB":
                lv = orb.get(sym)
                if not lv or sym in entered or lv["doji"] or not lv["green"]:
                    continue
                if t.time() < lv["start"] or float(row.high) < lv["orh"]:
                    continue
                candidates.append((0, sym, max(lv["orh"], float(row.open))))
            else:
                if i < WARMUP_BARS:
                    continue
                prev = df.iloc[i - 1]
                tick = (row.close - prev.close) / prev.close * 100
                if tick < MIN_TICK:
                    continue
                hist = df.iloc[:i + 1]
                typical = (hist.high + hist.low + hist.close) / 3
                vwap = (typical * hist.volume).sum() / hist.volume.sum()
                vs_vwap = (row.close - vwap) / vwap * 100
                if row.close <= vwap or vs_vwap < config.min_vwap_distance_pct:
                    continue
                vol_ratio = row.volume / df.iloc[i - 20:i].volume.mean()
                if vol_ratio < config.min_volume_ratio:
                    continue
                candidates.append((tick * 10 + vs_vwap * 2 + vol_ratio * 5, sym, float(row.close)))

        for score, sym, price in sorted(candidates, reverse=True):
            if len(open_pos) >= config.max_positions or sym in open_pos:
                continue
            if not gate_ok(t):
                break
            atr = atrs[sym]
            stop_d = config.atr_stop_mult * atr
            open_pos[sym] = dict(entry=price, qty=min(RISK / stop_d, MAX_VALUE / price),
                                 stop=price - stop_d, stop_d=stop_d,
                                 trail_d=config.atr_trail_mult * atr, best=price, t_in=t)
            entered.add(sym)

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
    return {"n": len(pnl), "win": len(wins) / len(pnl) * 100,
            "pf": (sum(wins) / sum(losses)) if losses else float("inf"),
            "total": sum(pnl)}


def run_period(start, end) -> dict:
    data, atr_s = {}, {}
    for sym in SYMBOLS + ["SPY"]:
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
    for label, kind, or_min in VARIANTS:
        trades = []
        for day in days:
            day_data = {s: d for s, df in data.items()
                        if not (d := df[df.index.date == day]).empty}
            if not day_data:
                continue
            spy_day = spy_all[spy_all.index.date == day] if spy_all is not None else None
            atrs = {s: atr_s[s].get(day) for s in day_data}
            trades += replay_day(day_data, atrs,
                                 spy_day if spy_day is not None and len(spy_day) else None,
                                 kind, or_min)
        out[label] = stats(trades)
    return out


def main():
    print(f"מניות: {', '.join(SYMBOLS)}")
    print("כניסה בלבד משתנה. סטופ, טריילינג, סיזינג ושער SPY — זהים לחי.\n")

    table = {}
    for name, start, end in PERIODS:
        print(f"--- {name} ({start} .. {end}) ---")
        res = run_period(start, end)
        if not res:
            print("  אין נתונים\n")
            continue
        table[name] = res
        base = res[VARIANTS[0][0]]
        print(f"{'גרסה':20}{'עסקאות':>8}{'הצלחה':>8}{'PF':>7}{'סה\"כ':>12}{'מול נוכחי':>12}")
        for label, *_ in VARIANTS:
            s = res[label]
            mark = "" if label == VARIANTS[0][0] else f"{s['total'] - base['total']:>+12,.0f}"
            print(f"{label:20}{s['n']:>8}{s['win']:>7.0f}%{s['pf']:>7.2f}{s['total']:>+12,.0f}{mark}")
        print()

    if len(table) < 2:
        return
    print("=" * 80)
    print("מבחן 1: עוזר בכל תקופה?   מבחן 2: גם השכנים בגריד עוזרים? (רמה, לא קוץ)\n")
    print(f"{'גרסה':20}" + "".join(f"{n:>13}" for n in table) + f"{'גרועות':>9}")
    print("-" * 80)
    verdicts = {}
    for label, *_ in VARIANTS:
        deltas = [table[n][label]["total"] - table[n][VARIANTS[0][0]]["total"] for n in table]
        worse = sum(1 for d in deltas if d < -1)
        verdicts[label] = worse
        row = "".join(f"{d:>+13,.0f}" for d in deltas)
        print(f"{label:20}{row}{('—' if label == VARIANTS[0][0] else worse):>9}")

    orb = [l for l, *_ in VARIANTS if l.startswith("ORB")]
    clean = [l for l in orb if verdicts[l] == 0]
    print()
    if not clean:
        print("פסק דין: אף גרסת ORB לא עוברת את מבחן 1. הכיוון נדחה.")
        return
    for l in clean:
        i = orb.index(l)
        neigh = [orb[j] for j in (i - 1, i + 1) if 0 <= j < len(orb)]
        ok = [n for n in neigh if verdicts[n] == 0]
        print(f"{l}: עובר מבחן 1. שכנים נקיים: {len(ok)}/{len(neigh)} "
              f"({', '.join(f'{n}={verdicts[n]}' for n in neigh)})")
        print(f"  → {'רמה — מועמד אמיתי' if len(ok) == len(neigh) else 'קוץ בודד — נדחה'}")


if __name__ == "__main__":
    main()
