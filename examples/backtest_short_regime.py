"""Regime-routed trading: the SPY/VWAP gate decides the DIRECTION.

    SPY above its intraday VWAP  -> long entries allowed (the live behavior)
    SPY below its intraday VWAP  -> SHORT entries allowed (the mirror)

Earlier research: naked mirror-shorts lose in up-tape and only work in weak
tape. This tests whether gating shorts to weak-tape minutes turns them into a
real second profit engine.

Usage:
    python examples/backtest_short_regime.py META,NVDA,AMZN,TSLA,AMD,AAPL 2026-07-13 2026-08-11

Variants:
    LONG_G   longs gated by SPY>VWAP           (what the live bot does today)
    SHORT_G  shorts gated by SPY<VWAP           (SSR guard, half risk)
    COMBO    both, directions routed by the gate

Standalone simulation — never touches live state.
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
from analyze_stock import fetch_range
from backtest_shorts import daily_atr, prior_closes

EQUITY = 100_000.0
WARMUP_BARS = 21
MIN_TICK = 0.05
WINDOW = (dtime(9, 35), dtime(11, 0))
EOD = dtime(15, 55)
LONG_RISK = EQUITY * 0.01
SHORT_RISK = EQUITY * 0.005
MAX_VALUE = config.max_position_value_usd
SSR_GUARD = -9.0


def spy_gate_series(spy_day: pd.DataFrame) -> pd.Series:
    """Minute-by-minute: True while SPY trades above its cumulative VWAP."""
    typical = (spy_day.high + spy_day.low + spy_day.close) / 3
    vwap = (typical * spy_day.volume).cumsum() / spy_day.volume.cumsum()
    return spy_day.close > vwap


def replay_day(day_data, atrs, prevs, spy_gate, allow) -> list:
    open_pos, trades = {}, []
    all_times = sorted(set().union(*[set(df.index) for df in day_data.values()]))

    def close(sym, t, price, reason):
        p = open_pos.pop(sym)
        pl = (price - p["entry"]) * p["qty"] if p["side"] == "long" else (p["entry"] - price) * p["qty"]
        trades.append((sym, p["side"], p["t_in"], t, p["entry"], price, p["qty"], pl, reason))

    for t in all_times:
        for sym in list(open_pos):
            df = day_data[sym]
            if t not in df.index:
                continue
            row, p = df.loc[t], open_pos[sym]
            if t.time() >= EOD:
                close(sym, t, float(row.close), "eod")
                continue
            if p["side"] == "long":
                if row.low <= p["stop"]:
                    close(sym, t, min(p["stop"], float(row.open)), "stop")
                    continue
                if row.close > p["best"]:
                    p["best"] = float(row.close)
                    if p["best"] - p["entry"] >= 0.25 * p["stop_d"]:
                        p["stop"] = max(p["stop"], p["best"] - p["trail_d"])
            else:
                if row.high >= p["stop"]:
                    close(sym, t, max(p["stop"], float(row.open)), "stop")
                    continue
                if row.close < p["best"]:
                    p["best"] = float(row.close)
                    if p["entry"] - p["best"] >= 0.25 * p["stop_d"]:
                        p["stop"] = min(p["stop"], p["best"] + p["trail_d"])

        if not (WINDOW[0] <= t.time() < WINDOW[1]) or len(open_pos) >= config.max_positions:
            continue

        # the gate routes direction, minute by minute
        try:
            spy_above = bool(spy_gate.loc[:t].iloc[-1]) if spy_gate is not None else True
        except (IndexError, KeyError):
            spy_above = True
        want_long = "long" in allow and spy_above
        want_short = "short" in allow and not spy_above
        if not want_long and not want_short:
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
            day_df = df.iloc[:i + 1]
            typical = (day_df.high + day_df.low + day_df.close) / 3
            vwap = (typical * day_df.volume).sum() / day_df.volume.sum()
            vs_vwap = (row.close - vwap) / vwap * 100
            vol_ratio = row.volume / df.iloc[i - 20:i].volume.mean()
            if vol_ratio < config.min_volume_ratio:
                continue

            if want_long and tick >= MIN_TICK and vs_vwap >= config.min_vwap_distance_pct:
                candidates.append((tick * 10 + vs_vwap * 2 + vol_ratio * 5, sym, float(row.close), "long"))

            if want_short and tick <= -MIN_TICK and vs_vwap <= -config.min_vwap_distance_pct:
                pc = prevs.get(sym)
                day_chg = (row.close - pc) / pc * 100 if pc and not pd.isna(pc) else 0.0
                if day_chg <= SSR_GUARD:
                    continue
                candidates.append((-tick * 10 + -vs_vwap * 2 + vol_ratio * 5, sym, float(row.close), "short"))

        for score, sym, price, side in sorted(candidates, reverse=True):
            if len(open_pos) >= config.max_positions or sym in open_pos:
                continue
            atr = atrs[sym]
            stop_d = config.atr_stop_mult * atr
            trail_d = config.atr_trail_mult * atr
            risk = LONG_RISK if side == "long" else SHORT_RISK
            qty = min(risk / stop_d, MAX_VALUE / price)
            open_pos[sym] = dict(side=side, entry=price, qty=qty, stop_d=stop_d, trail_d=trail_d,
                                 stop=price - stop_d if side == "long" else price + stop_d,
                                 best=price, t_in=t)

    for sym in list(open_pos):
        df = day_data[sym]
        close(sym, df.index[-1], float(df.iloc[-1].close), "eod")
    return trades


def run(symbols, start, end):
    data, atr_s, prev_s = {}, {}, {}
    for sym in symbols + ["SPY"]:
        df = fetch_range(sym, start, end)
        if sym != "SPY":
            atr_s[sym] = daily_atr(df, config.atr_period_days)
            prev_s[sym] = prior_closes(df)
        data[sym] = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]
    spy_all = data.pop("SPY")

    days = sorted(set().union(*[set(df.index.date) for df in data.values()]))
    variants = {"LONG_G": {"long"}, "SHORT_G": {"short"}, "COMBO": {"long", "short"}}
    out = {}
    for name, allow in variants.items():
        all_trades = []
        for day in days:
            day_data = {s: d for s, df in data.items()
                        if not (d := df[df.index.date == day]).empty}
            if not day_data:
                continue
            spy_day = spy_all[spy_all.index.date == day]
            gate = spy_gate_series(spy_day) if len(spy_day) else None
            atrs = {s: atr_s[s].get(day) for s in day_data}
            prevs = {s: prev_s[s].get(day) for s in day_data}
            all_trades += replay_day(day_data, atrs, prevs, gate, allow)
        out[name] = all_trades

    print(f"\n=== {','.join(symbols)}  {start} .. {end} ===")
    print(f"{'variant':<8} {'trades':>6} {'shorts':>6} {'win%':>5} {'PF':>6} {'total P/L':>12}")
    print("-" * 50)
    for name, trades in out.items():
        pls = [tr[7] for tr in trades]
        wins = sum(p > 0 for p in pls)
        gw = sum(p for p in pls if p > 0)
        gl = -sum(p for p in pls if p < 0)
        pf = gw / gl if gl else float("inf")
        n_short = sum(tr[1] == "short" for tr in trades)
        print(f"{name:<8} {len(trades):>6} {n_short:>6} {wins / len(pls) * 100 if pls else 0:>4.0f}% "
              f"{pf:>6.2f} {sum(pls):>+12.2f}")
    return out


if __name__ == "__main__":
    run(sys.argv[1].upper().split(","), sys.argv[2], sys.argv[3])
