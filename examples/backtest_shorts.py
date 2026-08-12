"""Long vs Short vs Both — mirrored-momentum backtest with research-backed guards.

Usage:
    python examples/backtest_shorts.py NVDA,TSLA 2025-12-01 2025-12-31
    python examples/backtest_shorts.py NBIS 2026-07-13 2026-08-11

LONG  = the improved strategy (window, ATR stops, risk sizing)
SHORT = mirror: falling tick + below VWAP + volume spike -> short,
        ATR stop above, trailing down, cover by 15:55.
        Guards from the literature: no short if the stock is already down
        >9% vs prior close (SSR / spring-back zone), half risk vs longs.
BOTH  = shared 5-slot portfolio; a stock that stopped out long may flip
        short later the same day if the short signal fires (and vice versa).

Standalone simulation state — deliberately does NOT touch the live
TrailingStopManager or data/stops.json.
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
from timeutil import to_local
from analyze_stock import fetch_range

EQUITY = 100_000.0
WARMUP_BARS = 21
MIN_BAR_MOMENTUM_PCT = 0.05
WINDOW = (dtime(9, 35), dtime(11, 0))
EOD = dtime(15, 55)
LONG_RISK = EQUITY * 0.01          # $1,000 per long
SHORT_RISK = EQUITY * 0.005        # $500 per short (literature: half size)
MAX_VALUE = config.max_position_value_usd
SSR_GUARD = -9.0                   # no shorts if stock already down 9% vs prior close


def daily_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    daily = df.resample("1D").agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last")).dropna()
    prev_close = daily["close"].shift(1)
    tr = pd.concat([
        daily["high"] - daily["low"],
        (daily["high"] - prev_close).abs(),
        (daily["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=5).mean().shift(1)
    atr.index = atr.index.date
    return atr


def prior_closes(df: pd.DataFrame) -> pd.Series:
    daily = df.resample("1D").agg(close=("close", "last")).dropna()
    prev = daily["close"].shift(1)
    prev.index = prev.index.date
    return prev


def replay_day(day_data: dict, atrs: dict, prevs: dict, allow: set) -> list:
    """One day, shared portfolio. allow ⊆ {'long','short'}. Returns closed trades."""
    open_pos = {}      # symbol -> dict(side, entry, qty, stop, trail_d, best, t_in)
    trades = []
    all_times = sorted(set().union(*[set(df.index) for df in day_data.values()]))

    def close(sym, t, price, reason):
        p = open_pos.pop(sym)
        pl = (price - p["entry"]) * p["qty"] if p["side"] == "long" else (p["entry"] - price) * p["qty"]
        trades.append((sym, p["side"], p["t_in"], t, p["entry"], price, p["qty"], pl, reason))

    for t in all_times:
        # manage open positions
        for sym in list(open_pos):
            df = day_data[sym]
            if t not in df.index:
                continue
            row = df.loc[t]
            p = open_pos[sym]

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
            else:  # short
                if row.high >= p["stop"]:
                    close(sym, t, max(p["stop"], float(row.open)), "stop")
                    continue
                if row.close < p["best"]:
                    p["best"] = float(row.close)
                    if p["entry"] - p["best"] >= 0.25 * p["stop_d"]:
                        p["stop"] = min(p["stop"], p["best"] + p["trail_d"])

        # entries
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
            day_df = df.iloc[:i + 1]
            typical = (day_df.high + day_df.low + day_df.close) / 3
            vwap = (typical * day_df.volume).sum() / day_df.volume.sum()
            vs_vwap = (row.close - vwap) / vwap * 100
            vol_ratio = row.volume / df.iloc[i - 20:i].volume.mean()
            if vol_ratio < config.min_volume_ratio:
                continue

            if ("long" in allow and tick >= MIN_BAR_MOMENTUM_PCT
                    and vs_vwap >= config.min_vwap_distance_pct):
                score = tick * 10 + vs_vwap * 2 + vol_ratio * 5
                candidates.append((score, sym, float(row.close), "long"))

            if ("short" in allow and tick <= -MIN_BAR_MOMENTUM_PCT
                    and vs_vwap <= -config.min_vwap_distance_pct):
                pc = prevs.get(sym)
                day_change = (row.close - pc) / pc * 100 if pc and not pd.isna(pc) else 0.0
                if day_change <= SSR_GUARD:      # already crashed — squeeze zone
                    continue
                score = -tick * 10 + -vs_vwap * 2 + vol_ratio * 5
                candidates.append((score, sym, float(row.close), "short"))

        for score, sym, price, side in sorted(candidates, reverse=True):
            if len(open_pos) >= config.max_positions or sym in open_pos:
                continue
            atr = atrs[sym]
            stop_d = config.atr_stop_mult * atr
            trail_d = config.atr_trail_mult * atr
            risk = LONG_RISK if side == "long" else SHORT_RISK
            qty = min(risk / stop_d, MAX_VALUE / price)
            open_pos[sym] = dict(
                side=side, entry=price, qty=qty, stop_d=stop_d, trail_d=trail_d,
                stop=price - stop_d if side == "long" else price + stop_d,
                best=price, t_in=t,
            )

    for sym in list(open_pos):
        df = day_data[sym]
        close(sym, df.index[-1], float(df.iloc[-1].close), "eod")
    return trades


def simulate(symbols: list[str], start: str, end: str) -> dict:
    """Run LONG / SHORT / BOTH simulations, return structured results (JSON-safe)."""
    data, atr_s, prev_s = {}, {}, {}
    for sym in symbols:
        df = fetch_range(sym, start, end)
        atr_s[sym] = daily_atr(df, config.atr_period_days)
        prev_s[sym] = prior_closes(df)
        data[sym] = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]

    days = sorted(set().union(*[set(df.index.date) for df in data.values()]))
    modes = {"LONG": {"long"}, "SHORT": {"short"}, "BOTH": {"long", "short"}}
    out = {"symbols": symbols, "start": start, "end": end, "modes": {}}

    for name, allow in modes.items():
        all_trades = []
        for day in days:
            day_data = {s: d for s, df in data.items()
                        if not (d := df[df.index.date == day]).empty}
            if not day_data:
                continue
            atrs = {s: atr_s[s].get(day) for s in day_data}
            prevs = {s: prev_s[s].get(day) for s in day_data}
            all_trades += replay_day(day_data, atrs, prevs, allow)

        pls = [tr[7] for tr in all_trades]
        wins = sum(p > 0 for p in pls)
        gw = sum(p for p in pls if p > 0)
        gl = -sum(p for p in pls if p < 0)
        daily: dict = {}
        for tr in all_trades:
            d = str(tr[2].date())
            daily.setdefault(d, {"day": d, "pl": 0.0, "trades": 0})
            daily[d]["pl"] += tr[7]
            daily[d]["trades"] += 1
        daily_list = sorted(daily.values(), key=lambda x: x["day"])
        cum = 0.0
        for d in daily_list:
            cum += d["pl"]
            d["cum"] = round(cum, 2)
            d["pl"] = round(d["pl"], 2)

        out["modes"][name] = {
            "trades": len(all_trades),
            "longs": sum(tr[1] == "long" for tr in all_trades),
            "shorts": sum(tr[1] == "short" for tr in all_trades),
            "win_pct": round(wins / len(pls) * 100, 1) if pls else 0,
            "pf": round(gw / gl, 2) if gl else None,
            "total": round(sum(pls), 2),
            "daily": daily_list,
            "trade_list": [{
                "sym": tr[0], "side": tr[1],
                "t_in": to_local(tr[2]).strftime("%d/%m %H:%M"), "t_out": to_local(tr[3]).strftime("%d/%m %H:%M"),
                "entry": round(tr[4], 2), "exit": round(tr[5], 2),
                "qty": round(tr[6], 2), "pl": round(tr[7], 2), "reason": tr[8],
            } for tr in all_trades],
        }
    return out


def run(symbols: list[str], start: str, end: str):
    sim = simulate(symbols, start, end)
    results = {name: m for name, m in sim["modes"].items()}

    print(f"\n=== {','.join(symbols)}  {start} .. {end} ===\n")
    print(f"{'mode':<7} {'trades':>6} {'longs':>6} {'shorts':>6} {'win%':>5} {'PF':>6} {'total P/L':>12}")
    print("-" * 55)
    for name, m in results.items():
        print(f"{name:<7} {m['trades']:>6} {m['longs']:>6} {m['shorts']:>6} "
              f"{m['win_pct']:>4.0f}% {m['pf'] if m['pf'] is not None else float('inf'):>6.2f} {m['total']:>+12.2f}")

    both = results["BOTH"]["trade_list"]
    s_pl = sum(t["pl"] for t in both if t["side"] == "short")
    l_pl = sum(t["pl"] for t in both if t["side"] == "long")
    print(f"\nBOTH breakdown: longs {l_pl:+,.2f} | shorts {s_pl:+,.2f}")


if __name__ == "__main__":
    run(sys.argv[1].upper().split(","), sys.argv[2], sys.argv[3])
