"""Analyze a single stock through the bot's lens.

Usage:
    python examples/analyze_stock.py NVDA                              # live snapshot
    python examples/analyze_stock.py NVDA --backtest 2025-12-01 2025-12-19

Live mode: fetches the current quote + today's intraday bars and runs the
exact scanner checklist (momentum / VWAP / volume) to show whether the bot
would buy right now.

Backtest mode: downloads real 1-min SIP bars for the date range (cached in
data/cache/analyze/, separate from the bot's cache) and replays the strategy
day by day on that one symbol.
"""

import sys
import time as systime
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.basicConfig(level=logging.WARNING)

import pandas as pd
import pytz
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from config import config
from scanner import calculate_vwap, calculate_volume_ratio
from strategy import TrailingStopManager

ET = pytz.timezone("America/New_York")
ANALYZE_CACHE = Path("data/cache/analyze")
MIN_BAR_MOMENTUM_PCT = 0.05   # per-minute proxy for the live 3s tick filter
WARMUP_BARS = 21


def get_quote(client: StockHistoricalDataClient, symbol: str) -> float | None:
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    q = client.get_stock_latest_quote(req)[symbol]
    ask, bid = float(q.ask_price), float(q.bid_price)
    if ask > 0 and bid > 0:
        return (ask + bid) / 2
    return ask if ask > 0 else (bid if bid > 0 else None)


# ---------------- live snapshot ----------------

def live_analysis(symbol: str):
    client = StockHistoricalDataClient(config.api_key, config.secret_key)

    print(f"=== {symbol} — live snapshot ===\n")

    price1 = get_quote(client, symbol)
    if price1 is None:
        sys.exit(f"No quote available for {symbol} — check the symbol.")
    print(f"quote #1: ${price1:.2f}  (sampling again in 3s for tick momentum...)")
    systime.sleep(3)
    price = get_quote(client, symbol) or price1
    tick = (price - price1) / price1 * 100
    print(f"quote #2: ${price:.2f}  ->  tick change {tick:+.3f}%\n")

    now_et = datetime.now(ET)
    open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et < open_et:
        open_et -= timedelta(days=1)

    req = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
        start=open_et, end=now_et, feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(req).data.get(symbol, [])
    if not bars:
        print("No intraday bars since market open — market probably closed.")
        return

    vwap = calculate_vwap(bars)
    vs_vwap = (price - vwap) / vwap * 100
    vol_ratio = calculate_volume_ratio(bars, config.volume_lookback_bars)

    day_open = float(bars[0].open)
    day_high = max(float(b.high) for b in bars)
    day_low = min(float(b.low) for b in bars)
    day_change = (price - day_open) / day_open * 100

    rising = 0
    for i in range(len(bars) - 1, 0, -1):
        if float(bars[i].close) > float(bars[i - 1].close):
            rising += 1
        else:
            break

    print(f"day:    open ${day_open:.2f}  high ${day_high:.2f}  low ${day_low:.2f}  "
          f"now ${price:.2f} ({day_change:+.2f}%)")
    print(f"vwap:   ${vwap:.2f}  (price is {vs_vwap:+.2f}% vs VWAP)")
    print(f"volume: last bar {vol_ratio:.1f}x vs {config.volume_lookback_bars}-bar average")
    print(f"streak: {rising} consecutive rising 1-min bars\n")

    checks = [
        (f"price ${price:.2f} in [${config.min_price:.0f}, ${config.max_price:.0f}]",
         config.min_price <= price <= config.max_price),
        (f"tick momentum {tick:+.3f}% >= {config.min_momentum_pct}%",
         tick >= config.min_momentum_pct),
        ("price above VWAP", price > vwap),
        (f"VWAP distance {vs_vwap:+.2f}% >= {config.min_vwap_distance_pct}%",
         vs_vwap >= config.min_vwap_distance_pct),
        (f"volume ratio {vol_ratio:.1f}x >= {config.min_volume_ratio}x",
         vol_ratio >= config.min_volume_ratio),
    ]
    passed = True
    print("scanner checklist:")
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        passed = passed and ok

    if passed:
        score = tick * 10 + vs_vwap * 2 + rising + vol_ratio * 5
        print(f"\n=> The bot WOULD buy {symbol} right now (score ~{score:.1f})")
    else:
        print(f"\n=> The bot would NOT buy {symbol} right now")

    qty = config.position_size_usd / price
    stop = price * (1 - config.initial_stop_loss_pct / 100)
    trail_at = price * (1 + config.min_profit_to_trail_pct / 100)
    print(f"\nif bought now: {qty:.2f} shares (${config.position_size_usd:,.0f}), "
          f"initial stop ${stop:.2f}, trailing kicks in above ${trail_at:.2f}")


# ---------------- historical backtest ----------------

def fetch_range(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Download 1-min SIP bars for [start, end], cached per symbol+range."""
    ANALYZE_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = ANALYZE_CACHE / f"{symbol}_{start}_{end}.parquet"
    if cache_file.exists():
        return pd.read_parquet(cache_file)

    client = StockHistoricalDataClient(config.api_key, config.secret_key)
    start_ts, end_ts = pd.Timestamp(start, tz=ET), pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)
    # free plan forbids SIP data from the last 15 min — stay clear of "now"
    now_cutoff = pd.Timestamp.now(tz=ET) - pd.Timedelta(minutes=30)
    end_ts = min(end_ts, now_cutoff)
    chunks = []
    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(cursor + pd.DateOffset(months=1), end_ts)
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=cursor.to_pydatetime(), end=chunk_end.to_pydatetime(),
            feed=DataFeed.SIP,
        )
        bars = client.get_stock_bars(req).data.get(symbol, [])
        if bars:
            chunks.append(pd.DataFrame([{
                "open": float(b.open), "high": float(b.high), "low": float(b.low),
                "close": float(b.close), "volume": float(b.volume),
                "timestamp": pd.Timestamp(b.timestamp).tz_convert(ET),
            } for b in bars]).set_index("timestamp"))
        cursor = chunk_end

    if not chunks:
        sys.exit(f"No minute data for {symbol} in {start}..{end}")
    df = pd.concat(chunks).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.to_parquet(cache_file)
    return df


def replay_day(df: pd.DataFrame) -> list[tuple]:
    """Replay the strategy on one day of bars for one symbol."""
    stops = TrailingStopManager(config)
    trades = []
    entry_t = None

    for i in range(len(df)):
        t = df.index[i]
        row = df.iloc[i]

        if stops.has_position("X"):
            pos = stops.get_position("X")
            if row.low <= pos.stop_loss:
                exit_price = min(pos.stop_loss, row.open)
                p = stops.close_position("X")
                trades.append((entry_t, t, p.entry_price, exit_price, "stop"))
                continue
            stops.update_price("X", row.close)
            continue

        if i < WARMUP_BARS:
            continue
        prev = df.iloc[i - 1]
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
        stops.open_position("X", float(row.close), 1.0)
        entry_t = t

    if stops.has_position("X"):
        p = stops.close_position("X")
        trades.append((entry_t, df.index[-1], p.entry_price, float(df.iloc[-1].close), "eod"))
    return trades


def backtest(symbol: str, start: str, end: str):
    print(f"=== {symbol} — strategy backtest {start} .. {end} ===\n")
    df = fetch_range(symbol, start, end)
    df = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]
    print(f"{len(df)} regular-hours minute bars, {len(set(df.index.date))} trading days\n")

    total_pl, wins, all_trades = 0.0, 0, 0
    print(f"{'day':<12} {'trades':>6} {'wins':>5} {'P/L $':>10}")
    print("-" * 38)
    for day, day_df in df.groupby(df.index.date):
        trades = replay_day(day_df)
        day_pl = 0.0
        for _, _, p_in, p_out, _ in trades:
            pl_pct = (p_out - p_in) / p_in * 100
            day_pl += config.position_size_usd * pl_pct / 100
            wins += pl_pct > 0
        total_pl += day_pl
        all_trades += len(trades)
        print(f"{str(day):<12} {len(trades):>6} {sum((p_out - p_in) > 0 for _, _, p_in, p_out, _ in trades):>5} {day_pl:>+10.2f}")

    print("-" * 38)
    win_rate = wins / all_trades * 100 if all_trades else 0
    print(f"total: {all_trades} trades, win rate {win_rate:.0f}%, "
          f"P/L ${total_pl:+,.2f} (position ${config.position_size_usd:,.0f})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sym = sys.argv[1].upper()
    if "--backtest" in sys.argv:
        idx = sys.argv.index("--backtest")
        backtest(sym, sys.argv[idx + 1], sys.argv[idx + 2])
    else:
        live_analysis(sym)
