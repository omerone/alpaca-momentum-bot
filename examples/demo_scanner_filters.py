"""Demo: how the scanner's VWAP + volume filters accept/reject a stock."""

import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config
from scanner import calculate_vwap, calculate_volume_ratio

Bar = namedtuple("Bar", ["high", "low", "close", "volume"])


def make_bars(closes: list[float], volumes: list[float]) -> list[Bar]:
    return [Bar(high=c * 1.001, low=c * 0.999, close=c, volume=v)
            for c, v in zip(closes, volumes)]


def check(name: str, bars: list[Bar], tick_change_pct: float):
    price = bars[-1].close
    vwap = calculate_vwap(bars)
    vs_vwap = (price - vwap) / vwap * 100
    vol_ratio = calculate_volume_ratio(bars, config.volume_lookback_bars)

    print(f"\n--- {name} ---")
    print(f"price=${price:.2f}  vwap=${vwap:.2f}  ({vs_vwap:+.2f}%)  "
          f"vol_ratio={vol_ratio:.1f}x  tick={tick_change_pct:+.3f}%")

    checks = [
        (f"tick change {tick_change_pct:+.3f}% >= {config.min_momentum_pct}%",
         tick_change_pct >= config.min_momentum_pct),
        (f"price above VWAP", price > vwap),
        (f"VWAP distance {vs_vwap:+.2f}% >= {config.min_vwap_distance_pct}%",
         vs_vwap >= config.min_vwap_distance_pct),
        (f"volume ratio {vol_ratio:.1f}x >= {config.min_volume_ratio}x",
         vol_ratio >= config.min_volume_ratio),
    ]

    passed = True
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        passed = passed and ok

    if passed:
        score = tick_change_pct * 10 + vs_vwap * 2 + vol_ratio * 5
        print(f"  => BUY CANDIDATE (score ~{score:.1f})")
    else:
        print("  => REJECTED")


# 25 minute-bars of history for each fake stock
n = 25

# 1) Strong momentum: price grinding up, above VWAP, volume spike on last bar
closes = [100 + i * 0.15 for i in range(n)]           # 100.00 -> 103.60
volumes = [10_000] * (n - 1) + [35_000]               # 3.5x volume spike
check("STRONG - rising, above VWAP, volume spike", make_bars(closes, volumes),
      tick_change_pct=0.35)

# 2) Below VWAP: fell all morning, small bounce at the end
closes = [110 - i * 0.35 for i in range(n - 3)] + [102.5, 102.9, 103.2]
volumes = [10_000] * (n - 1) + [30_000]
check("BELOW VWAP - bounce inside a downtrend", make_bars(closes, volumes),
      tick_change_pct=0.30)

# 3) No volume: price ticked up but on thin volume
closes = [100 + i * 0.12 for i in range(n)]
volumes = [10_000] * (n - 1) + [8_000]                # 0.8x - below average
check("NO VOLUME - move not confirmed", make_bars(closes, volumes),
      tick_change_pct=0.25)
