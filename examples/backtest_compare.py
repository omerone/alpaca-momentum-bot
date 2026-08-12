"""Before/after backtest: baseline strategy vs research-backed improvements.

Usage:
    python examples/backtest_compare.py NVDA,TSLA 2025-12-01 2025-12-31
    python examples/backtest_compare.py NBIS 2026-07-13 2026-08-11

BASELINE (the bot as it was):
    entries any time of day, fixed 1% stop / 0.5% trail, $25k per position

IMPROVED (research-backed):
    entries only 09:35-11:00 ET, stop = 0.5 x daily ATR(14),
    trail = 0.35 x ATR, size = risk 1% of equity to the stop
    (capped at $25k value), flat by 15:55
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
from strategy import TrailingStopManager
from analyze_stock import fetch_range

EQUITY = 100_000.0
WARMUP_BARS = 21
MIN_BAR_MOMENTUM_PCT = 0.05

BASELINE = dict(name="BASELINE", window=None, atr_stops=False, eod=dtime(16, 0))
IMPROVED = dict(name="IMPROVED", window=(dtime(9, 35), dtime(11, 0)),
                atr_stops=True, eod=dtime(15, 55))


def daily_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR per day computed from prior days only (no lookahead)."""
    daily = df.resample("1D").agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last")).dropna()
    prev_close = daily["close"].shift(1)
    tr = pd.concat([
        daily["high"] - daily["low"],
        (daily["high"] - prev_close).abs(),
        (daily["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=5).mean().shift(1)
    atr.index = atr.index.date          # key by plain date for lookup
    return atr


def replay_day(day_data: dict[str, pd.DataFrame], atrs: dict[str, float], mode: dict) -> list:
    """Replay one trading day across symbols. Returns closed trades."""
    stops = TrailingStopManager(config)
    entry_info: dict[str, tuple] = {}   # symbol -> (entry_time, qty)
    trades = []

    all_times = sorted(set().union(*[set(df.index) for df in day_data.values()]))

    def close(sym, t, price, reason):
        pos = stops.close_position(sym)
        t_in, qty = entry_info.pop(sym)
        trades.append((sym, t_in, t, pos.entry_price, price, qty, reason))

    for t in all_times:
        # 1) manage open positions
        for sym in list(stops.active_symbols()):
            df = day_data[sym]
            if t not in df.index:
                continue
            row = df.loc[t]
            if t.time() >= mode["eod"]:
                close(sym, t, float(row.close), "eod")
                continue
            pos = stops.get_position(sym)
            if row.low <= pos.stop_loss:
                close(sym, t, min(pos.stop_loss, float(row.open)), "stop")
                continue
            stops.update_price(sym, float(row.close))

        # 2) entries
        if mode["window"] and not (mode["window"][0] <= t.time() < mode["window"][1]):
            continue
        if t.time() >= mode["eod"]:
            continue
        if len(stops.active_symbols()) >= config.max_positions:
            continue

        candidates = []
        for sym, df in day_data.items():
            if stops.has_position(sym) or t not in df.index:
                continue
            i = df.index.get_loc(t)
            if i < WARMUP_BARS:
                continue
            row, prev = df.iloc[i], df.iloc[i - 1]
            tick = (row.close - prev.close) / prev.close * 100
            if tick < MIN_BAR_MOMENTUM_PCT:
                continue
            day_df = df.iloc[:i + 1]
            typical = (day_df.high + day_df.low + day_df.close) / 3
            vwap = (typical * day_df.volume).sum() / day_df.volume.sum()
            vs_vwap = (row.close - vwap) / vwap * 100
            if row.close <= vwap or vs_vwap < config.min_vwap_distance_pct:
                continue
            vol_ratio = row.volume / df.iloc[i - 20:i].volume.mean()
            if vol_ratio < config.min_volume_ratio:
                continue
            score = tick * 10 + vs_vwap * 2 + vol_ratio * 5
            candidates.append((score, sym, float(row.close)))

        for score, sym, price in sorted(candidates, reverse=True):
            if len(stops.active_symbols()) >= config.max_positions:
                break
            if mode["atr_stops"]:
                atr = atrs.get(sym)
                if not atr or pd.isna(atr) or atr <= 0:
                    continue
                stop_d = config.atr_stop_mult * atr
                trail_d = config.atr_trail_mult * atr
                risk_usd = EQUITY * config.risk_pct_per_trade / 100
                value = min((risk_usd / stop_d) * price, config.max_position_value_usd)
                qty = value / price
                stops.open_position(sym, price, qty, stop_d, trail_d)
            else:
                qty = config.position_size_usd / price
                stops.open_position(sym, price, qty)
            entry_info[sym] = (t, qty)

    # close leftovers at last price
    for sym in list(stops.active_symbols()):
        df = day_data[sym]
        close(sym, df.index[-1], float(df.iloc[-1].close), "eod")
    return trades


def run(symbols: list[str], start: str, end: str):
    data, atr_series = {}, {}
    for sym in symbols:
        df = fetch_range(sym, start, end)
        atr_series[sym] = daily_atr(df, config.atr_period_days)
        df = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]
        data[sym] = df

    days = sorted(set().union(*[set(df.index.date) for df in data.values()]))

    results = {}
    for mode in (BASELINE, IMPROVED):
        daily_pl, all_trades = {}, []
        for day in days:
            day_data = {s: d for s, df in data.items()
                        if not (d := df[df.index.date == day]).empty}
            if not day_data:
                continue
            atrs = {s: atr_series[s].get(day) for s in day_data}
            trades = replay_day(day_data, atrs, mode)
            pl = sum(q * (po - pi) for _, _, _, pi, po, q, _ in trades)
            daily_pl[day] = (len(trades), pl)
            all_trades += trades
        results[mode["name"]] = (daily_pl, all_trades)

    # ---- side-by-side report ----
    print(f"\n{'':12} | {'BASELINE':>20} | {'IMPROVED':>20}")
    print(f"{'day':<12} | {'trades':>7} {'P/L $':>12} | {'trades':>7} {'P/L $':>12}")
    print("-" * 60)
    for day in days:
        b = results["BASELINE"][0].get(day, (0, 0.0))
        i = results["IMPROVED"][0].get(day, (0, 0.0))
        print(f"{str(day):<12} | {b[0]:>7} {b[1]:>+12.2f} | {i[0]:>7} {i[1]:>+12.2f}")
    print("-" * 60)

    for name in ("BASELINE", "IMPROVED"):
        trades = results[name][1]
        pls = [q * (po - pi) for _, _, _, pi, po, q, _ in trades]
        wins = sum(p > 0 for p in pls)
        gross_win = sum(p for p in pls if p > 0)
        gross_loss = -sum(p for p in pls if p < 0)
        pf = gross_win / gross_loss if gross_loss else float("inf")
        print(f"{name:<9} trades={len(trades):<4} win rate={wins / len(trades) * 100 if trades else 0:>3.0f}%  "
              f"profit factor={pf:.2f}  total P/L=${sum(pls):+,.2f}")


if __name__ == "__main__":
    run(sys.argv[1].upper().split(","), sys.argv[2], sys.argv[3])
