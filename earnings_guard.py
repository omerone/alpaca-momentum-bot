"""Earnings-day guard: no NEW entries in a stock on its earnings-reaction day.

Why: our edge is calibrated to normal-regime behavior. On the reaction day the
stock moves 3-5x its usual range, ATR stops sit inside the noise, and the
signal reads "momentum" where the tape is actually a repricing fight. Own-data
diagnostic: ~18-20 earnings-day trades across backtests netted ~-$1,100.
This is a RISK rule (like flat_eod), not an alpha claim.

Reaction day logic:
- report before the open (BMO)  -> reaction day = report date
- report after the close (AMC)  -> reaction day = next calendar day
- unknown hour                  -> both days blocked (conservative)

Dates come from yfinance, cached to disk once per day. Fail-open: if the
lookup fails for a symbol, it is NOT blocked — a data hiccup must not stop
the bot (same policy as the SPY gate).
"""

import json
import logging
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_FILE = Path("data/earnings_cache.json")
LOOKAHEAD_LIMIT = 12          # yfinance rows per symbol
# yfinance exposes no timeout. On 24/08/2026 the ORCL lookup hung for 765
# seconds and stalled everything behind it, so each symbol gets a hard cap.
LOOKUP_TIMEOUT_S = 15


def _with_timeout(fn, seconds: float):
    """Run fn in a worker thread and give up if it outlives `seconds`.

    The worker is a daemon: if the call is truly wedged it keeps running in the
    background and dies with the process, but the caller is freed either way.
    """
    box: dict = {}
    def _run():
        try:
            box["value"] = fn()
        except Exception as exc:
            box["error"] = exc
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"lookup exceeded {seconds}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _reaction_days(ts) -> set[str]:
    """Map one report timestamp to the day(s) whose trading it disrupts."""
    d = ts.date()
    t = ts.time()
    if t <= dtime(9, 0):                      # before the open
        days = {d}
    elif t >= dtime(15, 30):                  # after (or at) the close
        days = {d + timedelta(days=1)}
    else:                                     # midnight placeholder / unknown
        days = {d, d + timedelta(days=1)}
    return {x.isoformat() for x in days}


def fetch_blocked_days(symbols: list[str]) -> dict[str, list[str]]:
    """symbol -> ISO dates on which new entries should be blocked."""
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    out: dict[str, list[str]] = {}
    horizon_lo = date.today() - timedelta(days=7)
    horizon_hi = date.today() + timedelta(days=21)

    for sym in symbols:
        try:
            ed = _with_timeout(
                lambda: yf.Ticker(sym).get_earnings_dates(limit=LOOKAHEAD_LIMIT),
                LOOKUP_TIMEOUT_S,
            )
            days: set[str] = set()
            if ed is not None and not ed.empty:
                for ts in ed.index:
                    if horizon_lo <= ts.date() <= horizon_hi:
                        days |= _reaction_days(ts)
            out[sym] = sorted(days)
        except Exception as e:
            logger.warning("Earnings lookup failed for %s (fail-open): %s", sym, e)
            out[sym] = []
    return out


def load_blocked_days(symbols: list[str]) -> dict[str, list[str]]:
    """Disk-cached once per day; refetches when the day or symbol set changes."""
    today = date.today().isoformat()
    try:
        cached = json.loads(CACHE_FILE.read_text())
        if cached.get("day") == today and set(symbols) <= set(cached.get("data", {})):
            return cached["data"]
    except Exception:
        pass

    data = fetch_blocked_days(symbols)
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"day": today, "data": data}))
    except Exception:
        pass

    blocked_now = [s for s, days in data.items() if today in days]
    if blocked_now:
        logger.info("Earnings-day guard: blocking new entries today in %s", ", ".join(blocked_now))
    return data


def is_blocked_today(symbol: str, blocked: dict[str, list[str]]) -> bool:
    return date.today().isoformat() in blocked.get(symbol, [])
