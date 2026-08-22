"""Score the shorts OBSERVER: what would each logged signal actually have made?

Regime-routed shorts are the one hypothesis in this project that cleared the
strict bar — positive in all 5 backtest periods, from a pre-stated idea rather
than a parameter search (13/08/2026) — and they were shipped deliberately in
observe-only mode. `data/short_observations.jsonl` records every signal the live
bot would have taken. This replays them against what the market actually did.

It is the confirmation step: backtests can be wrong about fills, timing and
which signals fire in real time. Live signals scored against real bars cannot.

Each observation is replayed with the live SHORT rules, mirrored:
  entry at the logged price, stop above it (as logged), trail follows the low
  by atr_trail_mult x ATR once in profit, cover at 15:55 ET.

Usage:  python examples/score_shorts.py
Read-only — touches no live state and places no orders.
"""

import json
import sys
import warnings
from collections import defaultdict
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import logging
logging.basicConfig(level=logging.WARNING)
warnings.filterwarnings("ignore")

import pandas as pd
import pytz

from config import config
from analyze_stock import fetch_range

ET = pytz.timezone("America/New_York")
IL = pytz.timezone("Asia/Jerusalem")
EOD = dtime(15, 55)
OBS_FILE = ROOT / "data" / "short_observations.jsonl"


def load_observations() -> list[dict]:
    if not OBS_FILE.exists():
        return []
    out = []
    for line in OBS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def replay_short(bars: pd.DataFrame, entry_at, entry: float, stop: float, trail_d: float) -> dict:
    """Mirror of the long logic. Returns exit price, time and reason."""
    walk = bars[(bars.index >= entry_at) & (bars.index.time < dtime(16, 0))]
    if walk.empty:
        return {}
    stop_d = stop - entry                       # a short's stop sits ABOVE entry
    arm_at = entry - config.atr_trail_activate * stop_d
    low = entry
    for ts, b in walk.iterrows():
        if ts.time() >= EOD:
            return {"exit": float(b["close"]), "at": ts, "why": "סוף יום"}
        # adverse leg first: within a minute we cannot know the order
        if float(b["high"]) >= stop:
            return {"exit": stop, "at": ts, "why": "סטופ"}
        low = min(low, float(b["low"]))
        if low <= arm_at:
            stop = min(stop, low + trail_d)
    last = walk.iloc[-1]
    return {"exit": float(last["close"]), "at": walk.index[-1], "why": "סוף נתונים"}


def main():
    obs = load_observations()
    if not obs:
        print("אין עדיין תצפיות ב-data/short_observations.jsonl")
        return

    by_day = defaultdict(list)
    for o in obs:
        by_day[o["ts"][:10]].append(o)
    print(f"תצפיות: {len(obs)} על פני {len(by_day)} ימי מסחר "
          f"({', '.join(sorted(by_day))})\n")

    bars_cache: dict[str, pd.DataFrame] = {}
    results = []
    for o in obs:
        sym = o["symbol"]
        ts_il = pd.Timestamp(o["ts"]).tz_localize(IL)
        entry_at = ts_il.tz_convert(ET).floor("min")
        day = entry_at.date().isoformat()
        if sym not in bars_cache:
            try:
                bars_cache[sym] = fetch_range(sym, day, day)
            except Exception as e:
                print(f"  ! {sym}: {e}")
                bars_cache[sym] = pd.DataFrame()
        bars = bars_cache[sym]
        if bars.empty:
            continue
        bars = bars[bars.index.date == entry_at.date()]
        if bars.empty:
            continue

        entry, stop, qty = float(o["price"]), float(o["stop"]), float(o["qty"])
        # the ATR is recoverable from the stop the bot logged
        atr = (stop - entry) / config.atr_stop_mult if config.atr_stop_mult else 0
        r = replay_short(bars, entry_at, entry, stop, config.atr_trail_mult * atr)
        if not r:
            continue
        pnl = (entry - r["exit"]) * qty          # short: profit when price falls
        results.append({**o, "exit": r["exit"], "why": r["why"], "pnl": pnl,
                        "pct": (entry - r["exit"]) / entry * 100,
                        "out_at": r["at"].tz_convert(IL)})

    if not results:
        print("לא נמצאו נרות מתאימים לתצפיות.")
        return

    print(f"{'מניה':7}{'שעה':>7}{'כניסה':>10}{'יציאה':>10}{'תוצאה':>9}{'רווח/הפסד':>12}  סיבה")
    print("-" * 68)
    for r in sorted(results, key=lambda x: x["ts"]):
        print(f"{r['symbol']:7}{pd.Timestamp(r['ts']).strftime('%H:%M'):>7}"
              f"{r['price']:>10.2f}{r['exit']:>10.2f}{r['pct']:>+8.2f}%{r['pnl']:>+12.2f}$  {r['why']}")

    total = sum(r["pnl"] for r in results)
    wins = [r for r in results if r["pnl"] > 0]
    losses = [r for r in results if r["pnl"] < 0]
    gross_w = sum(r["pnl"] for r in wins)
    gross_l = -sum(r["pnl"] for r in losses)
    print("-" * 68)
    print(f"סה\"כ {len(results)} תצפיות · הצלחה {len(wins)/len(results)*100:.0f}% · "
          f"PF {gross_w/gross_l if gross_l else float('inf'):.2f} · תוצאה {total:+,.2f}$")
    print(f"רווח ממוצע {gross_w/len(wins) if wins else 0:+.2f}$ · "
          f"הפסד ממוצע {-gross_l/len(losses) if losses else 0:+.2f}$")

    # Signals fire in bursts: every short lights up in the same minutes, when
    # SPY crosses below its VWAP. 19 signals from one crossing are one bet with
    # 19 tickets, not 19 independent samples — so the sample that matters is
    # the number of REGIME EPISODES, not the number of rows.
    episodes, last = [], None
    for r in sorted(results, key=lambda x: x["ts"]):
        t = pd.Timestamp(r["ts"])
        if last is None or (t - last).total_seconds() > 30 * 60:
            episodes.append([])
        episodes[-1].append(r)
        last = t
    print()
    print(f"אשכולות אמיתיים: {len(episodes)} — כל אשכול הוא כניסה אחת של השוק מתחת ל-VWAP")
    for i, ep in enumerate(episodes, 1):
        span = f"{pd.Timestamp(ep[0]['ts']).strftime('%d/%m %H:%M')}-{pd.Timestamp(ep[-1]['ts']).strftime('%H:%M')}"
        print(f"   #{i} {span}: {len(ep):>2} סיגנלים, {sum(r['pnl'] for r in ep):>+10,.2f}$")
    print(f"\nהמדגם האפקטיבי הוא {len(episodes)}, לא {len(results)}.")

    if len(by_day) < 5 or len(results) < 30:
        print(f"⚠ מדגם קטן מדי להסקה: {len(results)} תצפיות ב-{len(by_day)} ימים.")
        print("  לפני החלטה על הפעלה בפועל: לפחות 30 תצפיות על פני 5 ימי מסחר,")
        print("  והתוצאה צריכה להיות עקבית — לא יום אחד גדול שנושא את הכל.")
    else:
        per_day = {d: sum(r["pnl"] for r in results if r["ts"][:10] == d) for d in sorted(by_day)}
        bad = sum(1 for v in per_day.values() if v < 0)
        print("לפי יום:", " · ".join(f"{d[5:]}: {v:+,.0f}$" for d, v in per_day.items()))
        print(f"ימים מפסידים: {bad}/{len(per_day)} — "
              + ("עקבי, שווה לשקול הפעלה" if bad <= len(per_day) // 3
                 else "לא עקבי, להישאר בתצפית"))


if __name__ == "__main__":
    main()
