"""Reset the bot back to a clean slate.

    ./.venv/bin/python reset.py              # show what exists, change nothing
    ./.venv/bin/python reset.py --local      # forget local state only
    ./.venv/bin/python reset.py --broker     # cancel orders + sell everything
    ./.venv/bin/python reset.py --all        # both

Nothing is touched without typing RESET at the prompt. The account number is
printed first on purpose — it is easy to be pointed at somebody else's keys.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from config import config
from broker import AlpacaBroker
from timeutil import ET, et_to_local, install_log_timezone

install_log_timezone()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("reset")

LOCAL_FILES = [config.stop_state_file, config.trade_log_file, "focus.json"]


def market_open() -> bool:
    now = datetime.now(ET)
    return now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)


def show(broker: AlpacaBroker) -> tuple[list, dict]:
    account = broker.get_account()
    positions = broker.get_open_positions()
    stops = broker.get_open_stops()

    print("\n" + "=" * 62)
    print(f"  חשבון: {account['account_number']}   ({'PAPER — כסף דמה' if account['paper'] else '*** LIVE — כסף אמיתי ***'})")
    print(f"  שווי תיק: ${account['equity']:,.2f}   מזומן: ${account['cash']:,.2f}")
    print("=" * 62)

    if positions:
        total = sum(p["unrealized_pl"] for p in positions)
        print(f"\n  {len(positions)} פוזיציות פתוחות (רווח/הפסד לא ממומש ${total:+,.2f}):")
        for p in positions:
            print(f"    {p['symbol']:6s} {p['qty']:>10.4f} @ ${p['entry_price']:>8.2f}"
                  f"  ->  ${p['current_price']:>8.2f}  ({p['unrealized_pl']:+.2f})")
    else:
        print("\n  אין פוזיציות פתוחות")

    print(f"\n  {len(stops)} הזמנות סטופ פתוחות" if stops else "\n  אין הזמנות פתוחות")
    for sym, s in stops.items():
        print(f"    {sym:6s} stop ${s['stop_price']:.2f} על {s['qty']:g} מניות")

    print("\n  קבצים מקומיים:")
    for f in LOCAL_FILES:
        path = Path(f)
        print(f"    {'✓' if path.exists() else '·'} {f}"
              f"{f'  ({path.stat().st_size:,} bytes)' if path.exists() else '  (לא קיים)'}")
    print()
    return positions, stops


def confirm(what: str) -> bool:
    print(f"\n  ⚠  עומד לבצע: {what}")
    answer = input("  הקלד RESET כדי לאשר (כל דבר אחר יבטל): ").strip()
    if answer != "RESET":
        print("  בוטל — לא נגעתי בכלום.\n")
        return False
    return True


def reset_local() -> None:
    for f in LOCAL_FILES:
        path = Path(f)
        if path.exists():
            path.unlink()
            print(f"    נמחק: {f}")
        else:
            print(f"    כבר לא קיים: {f}")


def reset_broker(broker: AlpacaBroker, positions: list) -> None:
    cancelled = broker.cancel_all_orders()
    print(f"    בוטלו {cancelled} הזמנות")

    if not positions:
        print("    אין פוזיציות לסגור")
        return

    results = broker.close_all_positions()
    ok = sum(1 for r in results if r["ok"])
    print(f"    נשלחו {ok}/{len(results)} פקודות מכירה")
    if not market_open():
        print(f"    ⚠  השוק סגור — הפקודות ימתינו בתור ויתבצעו בפתיחה הבאה ({et_to_local('09:30')} שעון ישראל)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset the trading bot")
    ap.add_argument("--local", action="store_true", help="delete stops.json / trades.db / focus.json")
    ap.add_argument("--broker", action="store_true", help="cancel all orders and liquidate all positions")
    ap.add_argument("--all", action="store_true", help="both of the above")
    args = ap.parse_args()

    do_local = args.local or args.all
    do_broker = args.broker or args.all

    try:
        broker = AlpacaBroker(config)
    except ValueError as e:
        logger.error(str(e))
        return 1

    positions, _ = show(broker)

    if not (do_local or do_broker):
        print("  זו תצוגה בלבד. להרצה בפועל:")
        print("    --local   מחיקת מצב מקומי")
        print("    --broker  ביטול הזמנות ומכירת הכל")
        print("    --all     שניהם\n")
        return 0

    steps = []
    if do_broker:
        steps.append(f"ביטול כל ההזמנות ומכירת {len(positions)} פוזיציות בחשבון {broker.account_number}")
    if do_local:
        steps.append("מחיקת המצב המקומי (סטופים, יומן עסקאות, מיקוד)")

    if not confirm(" + ".join(steps)):
        return 1

    if do_broker:
        print("\n  ברוקר:")
        reset_broker(broker, positions)
    if do_local:
        print("\n  מקומי:")
        reset_local()

    print("\n  הושלם. הפעל מחדש את השרת כדי שהמצב ייטען נקי.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
