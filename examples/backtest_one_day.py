"""Demo: replay the bot's strategy on real 1-minute bars for one trading day.

Downloads real SIP minute data via minute_data.py, then simulates:
scan (momentum + VWAP + volume) -> enter -> trailing stop -> exit.

Note: the live bot reacts to 3-second quote ticks; here we approximate with
1-minute bars, so the per-tick momentum threshold is scaled up to 0.05%/min.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.basicConfig(level=logging.WARNING)

from datetime import time as dtime

from config import config
from timeutil import to_local
from strategy import TrailingStopManager
from minute_data import load_all

SYMBOLS = ["NVDA", "TSLA", "AMD", "COIN", "PLTR", "SMCI", "MARA", "SOFI"]
DAY = sys.argv[1] if len(sys.argv) > 1 else "2025-12-15"
MIN_BAR_MOMENTUM_PCT = 0.05   # per-minute proxy for the live 3s tick filter
WARMUP_BARS = 21              # need history for volume average

print(f"Loading 1-min bars for {DAY} ({len(SYMBOLS)} symbols)...")
import pandas as pd
day_date = pd.Timestamp(DAY).date()
data = load_all(SYMBOLS, f"{DAY} 09:30", f"{DAY} 16:00")
# keep only the requested day, regular trading hours
data = {
    sym: d for sym, df in data.items()
    if not (d := df[(df.index.date == day_date)
                    & (df.index.time >= dtime(9, 30))
                    & (df.index.time < dtime(16, 0))]).empty
}
if not data:
    sys.exit(f"No bars for {DAY} in cache — pick a date inside the cached range.")
print(f"Got data for: {', '.join(sorted(data))}\n")

all_times = sorted(set().union(*[set(df.index) for df in data.values()]))

stops = TrailingStopManager(config)
trades = []      # closed trades: (symbol, entry_t, exit_t, entry, exit, reason)
entry_time = {}


def close(symbol, t, exit_price, reason):
    pos = stops.close_position(symbol)
    trades.append((symbol, entry_time.pop(symbol), t, pos.entry_price, exit_price, reason))


for t in all_times:
    # 1) monitor open positions (like monitor_stops in main.py)
    for sym in list(stops.active_symbols()):
        df = data[sym]
        if t not in df.index:
            continue
        row = df.loc[t]
        pos = stops.get_position(sym)
        if row.low <= pos.stop_loss:                      # stop hit inside this bar
            close(sym, t, min(pos.stop_loss, row.open), "stop")
            continue
        stops.update_price(sym, row.close)                # may raise the trail

    # 2) scan for entries (like scan_and_enter in main.py)
    if len(stops.active_symbols()) >= config.max_positions:
        continue

    candidates = []
    for sym, df in data.items():
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
        stops.open_position(sym, price, config.position_size_usd / price)
        entry_time[sym] = t

# close whatever is still open at the last price of the day
for sym in list(stops.active_symbols()):
    close(sym, all_times[-1], float(data[sym].iloc[-1].close), "end of day")

# ---- report ----
print(f"{'symbol':<7} {'entry':>6} {'exit':>6} {'in $':>9} {'out $':>9} {'P/L %':>7} {'P/L $':>9}  reason")
print("-" * 72)
total_pl = 0.0
wins = 0
for sym, t_in, t_out, p_in, p_out, reason in trades:
    pl_pct = (p_out - p_in) / p_in * 100
    pl_usd = config.position_size_usd * pl_pct / 100
    total_pl += pl_usd
    wins += pl_pct > 0
    print(f"{sym:<7} {to_local(t_in).strftime('%H:%M'):>6} {to_local(t_out).strftime('%H:%M'):>6} "
          f"{p_in:>9.2f} {p_out:>9.2f} {pl_pct:>+7.2f} {pl_usd:>+9.2f}  {reason}")

n = len(trades)
print("-" * 72)
print(f"trades={n}  wins={wins}  losses={n - wins}  "
      f"win rate={wins / n * 100 if n else 0:.0f}%  "
      f"total P/L=${total_pl:+,.2f} (position size ${config.position_size_usd:,.0f})")
