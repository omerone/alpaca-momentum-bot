"""Demo: feed a scripted price path through the real TrailingStopManager."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.basicConfig(level=logging.WARNING)

from config import config
from strategy import TrailingStopManager


def run_scenario(name: str, entry: float, path: list[float]):
    print(f"\n=== {name} ===")
    print(f"config: initial_stop={config.initial_stop_loss_pct}% | "
          f"trailing={config.trailing_stop_pct}% | "
          f"trail starts after +{config.min_profit_to_trail_pct}% profit\n")

    mgr = TrailingStopManager(config)
    pos = mgr.open_position("DEMO", entry, quantity=100)
    print(f"{'price':>8} | {'high':>8} | {'stop':>9} | event")
    print("-" * 55)
    print(f"{entry:>8.2f} | {pos.highest_price:>8.2f} | {pos.stop_loss:>9.4f} | ENTRY")

    for price in path:
        old_stop = pos.stop_loss
        action = mgr.update_price("DEMO", price)
        event = ""
        if pos.stop_loss > old_stop:
            event = f"trail raised {old_stop:.4f} -> {pos.stop_loss:.4f}"
        if action == "stop_hit":
            pnl = (price - entry) / entry * 100
            event = f"STOP HIT -> sell (P/L {pnl:+.2f}%)"
        print(f"{price:>8.2f} | {pos.highest_price:>8.2f} | {pos.stop_loss:>9.4f} | {event}")
        if action == "stop_hit":
            mgr.close_position("DEMO")
            break


# Winner: rides up to 103, gives back 0.5% from the top, exits with profit locked in
run_scenario(
    "WINNER - rides the momentum, exits near the top",
    entry=100.0,
    path=[100.10, 100.50, 101.20, 102.00, 103.00, 102.80, 102.40],
)

# Loser: drops right after entry, initial stop caps the loss at ~1%
run_scenario(
    "LOSER - falls immediately, initial stop cuts the loss",
    entry=50.0,
    path=[49.90, 49.70, 49.45],
)
