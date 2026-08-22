"""Daily research: what actually happened in every trade, and what would have
been better.

Three layers, deliberately separated because they earn trust at different rates:

  A. Autopsy      Per closed trade, replay that day's minute bars and measure
                  what the trade left on the table — how high it went before we
                  sold, how far it fell after, what holding to the close would
                  have paid. Descriptive, so it is honest from trade #1.

  B. Sweep        Replay OUR OWN entries with different stop/trail settings.
                  Same entries, different exits. This turns "the stop felt too
                  tight" into arithmetic. Needs no new market data: the ATR of
                  the day is recoverable from the recorded initial stop.

  C. Conclusions  Rules that read the accumulated autopsies and propose concrete
                  config changes — and stay SILENT until the sample is big
                  enough to mean anything. Six trades cannot teach you anything;
                  a system that pretends otherwise is worse than no system.

Nothing here ever writes to config.py. Every conclusion is a recommendation for
a human to approve. Auto-tuning on a small sample is the fastest known way to
turn a working strategy into a curve-fitted one.
"""

import logging
import sqlite3
from datetime import datetime, time as dtime

import pandas as pd
import pytz

from config import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

MARKET_CLOSE = dtime(16, 0)
# Full-day bars exist only after the close + the free plan's ~15-min SIP
# embargo. Before that, a review of a trade closed today can only be partial.
SESSION_COMPLETE_ET = dtime(16, 16)


def session_complete(day) -> bool:
    """Is the full session for `day` already measurable (closed + embargo)?"""
    now = pd.Timestamp.now(tz=ET)
    if day < now.date():
        return True
    return day == now.date() and now.time() >= SESSION_COMPLETE_ET
# A conclusion needs a sample. These are the gates below which layer C refuses
# to speak: 20 closed trades over at least 10 distinct trading days, so a single
# wild session cannot masquerade as a pattern.
MIN_TRADES_FOR_CONCLUSIONS = 20
MIN_DAYS_FOR_CONCLUSIONS = 10
# Moves smaller than this are noise, not lessons.
MATERIAL_PCT = 0.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_reviews (
    trade_id            INTEGER PRIMARY KEY,
    reviewed_at         TEXT,
    symbol              TEXT,
    trade_day           TEXT,
    actual_pct          REAL,   -- what we made, %
    mfe_pct             REAL,   -- best unrealized point while we held
    mae_pct             REAL,   -- worst unrealized point while we held
    capture_pct         REAL,   -- actual / mfe: how much of the move we kept
    peak_after_exit_pct REAL,   -- how much further it ran after we sold
    drop_after_exit_pct REAL,   -- how far it fell after we sold
    eod_pct             REAL,   -- holding to the closing bell instead
    minutes_to_peak     REAL,
    by_book_pct         REAL,   -- what the bot's OWN rules would have produced
    deviation_pct       REAL,   -- actual minus by-the-book: execution, not market
    verdict             TEXT,
    note                TEXT,
    data_ok             INTEGER
);
"""

# Columns added after the table first shipped, mirroring journal.py's approach.
MIGRATIONS = [("by_book_pct", "REAL"), ("deviation_pct", "REAL")]

# Above this gap between the actual exit and the by-the-book exit, the trade did
# not follow the strategy — that is an execution bug (a false stop trigger, a
# missed exit), not a lesson about the market. It gets reported immediately,
# with no sample-size gate, because one is already one too many.
DEVIATION_PCT = 0.3


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _conn(db_file: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(trade_reviews)")}
    for name, decl in MIGRATIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE trade_reviews ADD COLUMN {name} {decl}")
    return conn


def reviewed_ids(db_file: str) -> set[int]:
    """Only COMPLETE reviews count as done.

    A trade reviewed while its session was still running (or inside the SIP
    embargo) measured a half-day. Leaving it marked "reviewed" would freeze that
    half-measurement forever, so those come back for another pass.
    """
    try:
        with _conn(db_file) as c:
            return {r["trade_id"] for r in
                    c.execute("SELECT trade_id FROM trade_reviews WHERE data_ok = 1")}
    except Exception as e:
        logger.warning("Could not read reviews: %s", e)
        return set()


def load_reviews(db_file: str) -> list[dict]:
    try:
        with _conn(db_file) as c:
            rows = c.execute(
                "SELECT r.*, t.reason, t.pnl_usd, t.qty, t.entry_price, t.exit_price,"
                " t.entry_time, t.exit_time"
                " FROM trade_reviews r LEFT JOIN trades t ON t.id = r.trade_id"
                " ORDER BY r.trade_id DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Could not load reviews: %s", e)
        return []


def _save(db_file: str, review: dict) -> None:
    cols = ", ".join(review)
    marks = ", ".join("?" * len(review))
    with _conn(db_file) as c:
        c.execute(f"INSERT OR REPLACE INTO trade_reviews ({cols}) VALUES ({marks})",
                  tuple(review.values()))


# ---------------------------------------------------------------------------
# market data for a traded day
# ---------------------------------------------------------------------------

def _to_et(value) -> datetime | None:
    """Journal timestamps are naive machine-local strings — anchor them before
    comparing against bar timestamps, which live on the market clock."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(ET)


class _BarSource:
    """Minute bars per symbol, cached across a whole review run.

    minute_data.download_symbol returns whatever is on disk without checking the
    range, so a stale cache would silently hide the day we came to study. Detect
    that and force one refresh per symbol.
    """

    def __init__(self, start: str, end: str):
        import minute_data
        from alpaca.data.historical import StockHistoricalDataClient
        self._md = minute_data
        self._client = StockHistoricalDataClient(config.api_key, config.secret_key)
        self._start, self._end = start, end
        self._frames: dict[str, pd.DataFrame] = {}

    @staticmethod
    def _complete(df: pd.DataFrame, day) -> bool:
        """Does the cache hold this day all the way to the closing bell?

        Checking only the max DATE is not enough: a cache written mid-session
        contains the right day but stops at lunchtime, and every "what happened
        after we sold" number then measures the hole instead of the market.
        """
        if df.empty:
            return False
        d = df[df.index.date == day]
        return not d.empty and d.index[-1].time() >= dtime(15, 55)

    def day(self, symbol: str, day) -> pd.DataFrame:
        df = self._frames.get(symbol)
        if df is None:
            df = self._md.download_symbol(self._client, symbol, self._start, self._end)
            if not self._complete(df, day):
                df = self._md.download_symbol(self._client, symbol, self._start, self._end,
                                              force=True)
            self._frames[symbol] = df
        if df.empty:
            return df
        return df[df.index.date == day]


# ---------------------------------------------------------------------------
# layer A — autopsy
# ---------------------------------------------------------------------------

def _classify(m: dict, reason: str) -> tuple[str, str]:
    """One verdict per trade, in plain Hebrew. Priority matters: the first rule
    that fires is the most actionable lesson of that trade."""
    stopped = "stop" in (reason or "").lower() or "סטופ" in (reason or "")
    peak = m["peak_after_exit_pct"]
    drop = m["drop_after_exit_pct"]
    mae, actual = m["mae_pct"], m["actual_pct"]
    capture = m["capture_pct"]

    if peak is None or drop is None:
        return "partial", "אין עדיין נרות מסוף היום — המסקנה תיקבע אחרי הסגירה"
    # giving back a big unrealized gain is the most expensive habit there is,
    # and it hides behind a flat P/L unless someone measures the peak
    if capture is not None and m["mfe_pct"] >= 1.0 and capture < 30:
        return "gave_it_back", (f"הגיעה ל-{m['mfe_pct']:+.1f}% ונסגרה ב-{actual:+.2f}% — "
                                f"שמרנו רק {capture:.0f}% מהתנועה")
    if stopped and actual < 0 and peak >= MATERIAL_PCT:
        return "stop_too_tight", f"הסטופ תפס אותנו בהפסד ואז המניה עלתה {peak:+.1f}% — סטופ צר מדי"
    if peak >= MATERIAL_PCT:
        return "exited_early", f"אחרי שמכרנו היא המשיכה לעלות עוד {peak:+.1f}% — יצאנו מוקדם"
    if drop <= -MATERIAL_PCT:
        return "good_exit", f"אחרי היציאה היא ירדה {drop:+.1f}% — היציאה הצילה כסף"
    if mae <= -MATERIAL_PCT and actual > 0:
        return "early_entry", f"ירדה {mae:+.1f}% אחרי הכניסה לפני שהתהפכה — נכנסנו קצת מוקדם"
    return "clean", "העסקה התנהלה בלי דרמה — לא נשאר על השולחן ולא נחתכנו"


def review_trade(trade: dict, bars: pd.DataFrame, atr: float | None = None) -> dict | None:
    """Measure one closed trade against what the day actually offered."""
    entry_at, exit_at = _to_et(trade.get("entry_time")), _to_et(trade.get("exit_time"))
    entry, exit_px = float(trade["entry_price"]), float(trade["exit_price"])
    if not exit_at or bars.empty or entry <= 0:
        return None

    held = bars[(bars.index >= entry_at) & (bars.index <= exit_at)] if entry_at else bars[bars.index <= exit_at]
    after = bars[(bars.index > exit_at) & (bars.index.time < MARKET_CLOSE)]
    session = bars[bars.index.time < MARKET_CLOSE]
    if held.empty:
        return None

    actual_pct = (exit_px - entry) / entry * 100
    mfe_pct = (held["high"].max() - entry) / entry * 100
    mae_pct = (held["low"].min() - entry) / entry * 100

    # None, not 0.0 — "no bars after the exit" means we do not know where it
    # went, which is a different statement from "it did not move"
    peak_after = (after["high"].max() - exit_px) / exit_px * 100 if not after.empty else None
    drop_after = (after["low"].min() - exit_px) / exit_px * 100 if not after.empty else None
    eod_pct = (float(session["close"].iloc[-1]) - entry) / entry * 100 if not session.empty else actual_pct

    peak_bar = held["high"].idxmax()
    minutes_to_peak = (peak_bar - entry_at).total_seconds() / 60 if entry_at else None

    metrics = {
        "actual_pct": round(actual_pct, 3),
        "mfe_pct": round(mfe_pct, 3),
        "mae_pct": round(mae_pct, 3),
        "capture_pct": round(actual_pct / mfe_pct * 100, 1) if mfe_pct > 0.01 else None,
        "peak_after_exit_pct": None if peak_after is None else round(peak_after, 3),
        "drop_after_exit_pct": None if drop_after is None else round(drop_after, 3),
        "eod_pct": round(eod_pct, 3),
        "minutes_to_peak": round(minutes_to_peak, 1) if minutes_to_peak is not None else None,
    }
    verdict, note = _classify(metrics, trade.get("reason", ""))

    # Did the bot actually follow its own rules? Replaying the day under the
    # current settings answers that. A large gap means the exit came from
    # somewhere other than the strategy — that is a bug report, not a lesson.
    by_book = _replay(trade, bars, atr, config.atr_stop_mult, config.atr_trail_mult,
                      config.atr_trail_activate) if atr else None
    deviation = round(actual_pct - by_book, 3) if by_book is not None else None
    if deviation is not None and abs(deviation) >= DEVIATION_PCT:
        note += (f" ⚠ היציאה בפועל ({actual_pct:+.2f}%) לא תאמה את חוקי המערכת "
                 f"({by_book:+.2f}%) — הפרש של {deviation:+.2f}%")

    # bars must reach the closing bell, or "what happened after we sold" is a
    # measurement of missing data rather than of the market
    covered = not session.empty and session.index[-1].time() >= dtime(15, 55)
    return {
        "trade_id": trade["id"],
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "symbol": trade["symbol"],
        "trade_day": exit_at.date().isoformat(),
        **metrics,
        "by_book_pct": round(by_book, 3) if by_book is not None else None,
        "deviation_pct": deviation,
        "verdict": verdict,
        "note": note,
        "data_ok": int(covered),
    }


# ---------------------------------------------------------------------------
# layer B — exit-parameter sweep on our own entries
# ---------------------------------------------------------------------------

# (label, stop mult, trail mult, activation, trail cap %) — None means "take the
# live config value", so every row reads as "vs what we actually run".
#
# The capped rows test a specific structural complaint: the trail is a fraction
# of the DAILY ATR, so on a 6%-ATR name like INTC it only rises above the entry
# price after a 2.2% gain — more than most intraday moves ever reach. Capping it
# as a percentage of price is a different hypothesis from "use a smaller
# multiple", which was already tested and REJECTED out-of-sample on 2026-08-12.
VARIANTS = [
    ("סטופ צר",             0.35, 0.25, 0.25, None),
    ("נוכחי",               None, None, None, None),
    ("טריילינג רחב",        None, 0.60, None, None),
    ("סטופ רחב",            0.75, None, None, None),
    ("רחב לגמרי",           0.75, 0.60, 0.25, None),
    ("טריילינג מוגבל 1.0%", None, None, None, 1.0),
    ("טריילינג מוגבל 0.7%", None, None, None, 0.7),
    ("טריילינג מוגבל 0.5%", None, None, None, 0.5),
    ("החזקה עד הסגירה",     None, None, None, None),   # special-cased: no stop
]
HOLD_TO_CLOSE = "החזקה עד הסגירה"


def _atr_from_trade(trade: dict) -> float | None:
    """Recover the day's ATR from the stop we recorded.

    initial_stop = entry - atr_stop_mult * ATR, so ATR falls straight out. This
    avoids re-downloading daily bars for every historical trade, and it uses the
    exact number the bot itself sized with.
    """
    initial = trade.get("initial_stop")
    entry = float(trade["entry_price"])
    if not initial or initial <= 0 or config.atr_stop_mult <= 0:
        return None
    atr = (entry - float(initial)) / config.atr_stop_mult
    return atr if atr > 0 else None


def _replay(trade: dict, bars: pd.DataFrame, atr: float,
            k_stop: float, k_trail: float, k_activate: float,
            trail_cap_pct: float | None = None) -> float | None:
    """Same entry, different exit rules. Returns the P/L % this variant would
    have produced, replaying the day bar by bar."""
    entry_at = _to_et(trade.get("entry_time"))
    entry = float(trade["entry_price"])
    walk = bars[(bars.index >= entry_at) & (bars.index.time < MARKET_CLOSE)] if entry_at \
        else bars[bars.index.time < MARKET_CLOSE]
    if walk.empty:
        return None

    trail_dist = k_trail * atr
    if trail_cap_pct is not None:
        trail_dist = min(trail_dist, trail_cap_pct / 100 * entry)

    stop = entry - k_stop * atr
    arm_at = entry + k_activate * (k_stop * atr)
    high = entry
    for ts, bar in walk.iterrows():
        # the low is checked first: within one minute we cannot know the order,
        # and assuming the adverse leg came first is the honest assumption
        if float(bar["low"]) <= stop:
            return (stop - entry) / entry * 100
        high = max(high, float(bar["high"]))
        if high >= arm_at:
            stop = max(stop, high - trail_dist)
    return (float(walk["close"].iloc[-1]) - entry) / entry * 100


def sweep(trades: list[dict], source: "_BarSource") -> list[dict]:
    """Every variant, over every trade that carries enough information."""
    rows = []
    for label, ks, kt, ka, cap in VARIANTS:
        ks = config.atr_stop_mult if ks is None else ks
        kt = config.atr_trail_mult if kt is None else kt
        ka = config.atr_trail_activate if ka is None else ka

        results, skipped = [], 0
        for t in trades:
            atr = _atr_from_trade(t)
            exit_at = _to_et(t.get("exit_time"))
            if not atr or not exit_at:
                skipped += 1
                continue
            bars = source.day(t["symbol"], exit_at.date())
            if bars.empty:
                skipped += 1
                continue
            if label == HOLD_TO_CLOSE:
                entry_at = _to_et(t.get("entry_time"))
                walk = bars[(bars.index >= entry_at) & (bars.index.time < MARKET_CLOSE)] \
                    if entry_at else bars[bars.index.time < MARKET_CLOSE]
                pct = ((float(walk["close"].iloc[-1]) - float(t["entry_price"]))
                       / float(t["entry_price"]) * 100) if not walk.empty else None
            else:
                pct = _replay(t, bars, atr, ks, kt, ka, cap)
            if pct is None:
                skipped += 1
                continue
            # % -> $ on the size actually traded, so the column is comparable
            results.append({"pct": pct, "usd": pct / 100 * float(t["entry_price"]) * float(t["qty"])})

        if not results:
            continue
        wins = sum(1 for r in results if r["usd"] > 0)
        rows.append({
            "label": label,
            "is_current": label == "נוכחי",
            "trades": len(results),
            "skipped": skipped,
            "total_usd": round(sum(r["usd"] for r in results), 2),
            "avg_pct": round(sum(r["pct"] for r in results) / len(results), 3),
            "win_rate": round(wins / len(results) * 100, 1),
            "params": None if label == HOLD_TO_CLOSE else
                      ({"atr_stop_mult": ks, "atr_trail_mult": kt, "atr_trail_activate": ka}
                       | ({"trail_cap_pct": cap} if cap is not None else {})),
        })
    rows.sort(key=lambda r: r["total_usd"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# layer C — conclusions, but only with enough evidence
# ---------------------------------------------------------------------------

def conclusions(reviews: list[dict], sweep_rows: list[dict]) -> dict:
    """Turn accumulated autopsies into config suggestions — or explain why it is
    still too early to have an opinion."""
    usable = [r for r in reviews if r.get("data_ok")]
    days = {r["trade_day"] for r in usable}
    ready = len(usable) >= MIN_TRADES_FOR_CONCLUSIONS and len(days) >= MIN_DAYS_FOR_CONCLUSIONS

    status = {
        "ready": ready,
        "trades": len(usable),
        "days": len(days),
        "need_trades": max(0, MIN_TRADES_FOR_CONCLUSIONS - len(usable)),
        "need_days": max(0, MIN_DAYS_FOR_CONCLUSIONS - len(days)),
    }

    # Execution integrity is a fact per trade, not a statistical inference — it
    # is reported from the first occurrence, whatever the sample size.
    off_book = [r for r in usable
                if r.get("deviation_pct") is not None and abs(r["deviation_pct"]) >= DEVIATION_PCT]
    alerts = []
    if off_book:
        syms = ", ".join(f"{r['symbol']} ({r['trade_day']})" for r in off_book[:5])
        alerts.append({
            "level": "warn",
            "title": f"{len(off_book)} מתוך {len(usable)} עסקאות לא יצאו לפי חוקי המערכת",
            "detail": (f"היציאה בפועל הייתה רחוקה מהיציאה שהחוקים מחייבים: {syms}. "
                       "זה לא לקח על השוק אלא סימן לבאג ביצוע — סטופ שנורה על ציטוט שגוי, "
                       "יציאה שפוספסה, או החלקה חריגה."),
        })
    status["alerts"] = alerts

    if not ready:
        status["why"] = (
            f"נאספו {len(usable)} עסקאות על פני {len(days)} ימי מסחר. "
            f"כדי להסיק משהו צריך לפחות {MIN_TRADES_FOR_CONCLUSIONS} עסקאות ו-{MIN_DAYS_FOR_CONCLUSIONS} ימים — "
            "אחרת יום אחד חריג נראה כמו חוק. הנתיחות למטה כבר עובדות ונאספות."
        )
        return {"status": status, "items": []}

    n = len(usable)
    items = []

    early = [r for r in usable if r["verdict"] == "exited_early"]
    if len(early) / n >= 0.5:
        avg = sum(r["peak_after_exit_pct"] for r in early) / len(early)
        items.append({
            "title": "הטריילינג סוגר מוקדם מדי",
            "evidence": f"ב-{len(early)} מתוך {n} עסקאות המחיר המשיך לעלות אחרי היציאה, בממוצע {avg:+.1f}%.",
            "change": f"atr_trail_mult: {config.atr_trail_mult} → {round(config.atr_trail_mult * 1.5, 2)}",
            "key": "atr_trail_mult", "value": round(config.atr_trail_mult * 1.5, 2),
        })

    tight = [r for r in usable if r["verdict"] == "stop_too_tight"]
    if len(tight) / n >= 0.3:
        items.append({
            "title": "הסטופ הראשוני צר מדי",
            "evidence": f"ב-{len(tight)} מתוך {n} עסקאות הסטופ נפגע ואז המניה התאוששה מעל מחיר הכניסה.",
            "change": f"atr_stop_mult: {config.atr_stop_mult} → {round(config.atr_stop_mult * 1.4, 2)}",
            "key": "atr_stop_mult", "value": round(config.atr_stop_mult * 1.4, 2),
        })

    # A sweep winner is only a candidate if its NEIGHBOURS also beat the
    # baseline. Twice now a setting cleared "helps in every period" and then
    # turned out to be a spike with losing neighbours (trail cap 1.2% and the
    # 3-position sizing, both 19/08/2026). A real edge is a plateau; a lone
    # peak is a fit to noise.
    def _on_a_plateau(winner) -> bool:
        if not winner.get("params"):
            return False
        keys = [k for k in winner["params"] if winner["params"][k] != getattr(config, k, None)]
        if not keys:
            return False
        base_total = next((r["total_usd"] for r in sweep_rows if r["is_current"]), 0)
        nearby = [r for r in sweep_rows
                  if r is not winner and r.get("params")
                  and any(k in r["params"] for k in keys)]
        if len(nearby) < 2:
            return False                      # nothing to compare against yet
        return all(r["total_usd"] >= base_total for r in nearby)

    best = sweep_rows[0] if sweep_rows else None
    current = next((r for r in sweep_rows if r["is_current"]), None)
    if best and current and not best["is_current"] and best["params"] and _on_a_plateau(best):
        gain = best["total_usd"] - current["total_usd"]
        if current["total_usd"] != 0 and gain / abs(current["total_usd"]) >= 0.2:
            items.append({
                "title": f"סריקת הפרמטרים מעדיפה: {best['label']}",
                "evidence": (f"על אותן {best['trades']} כניסות בדיוק, ההגדרות האלה היו מניבות "
                             f"${best['total_usd']:,.0f} במקום ${current['total_usd']:,.0f} "
                             f"(הפרש ${gain:,.0f})."),
                "change": " · ".join(f"{k}: {getattr(config, k)} → {v}"
                                     for k, v in best["params"].items() if getattr(config, k) != v),
                "params": best["params"],
            })

    if not items:
        items.append({
            "title": "אין מה לשנות",
            "evidence": f"על פני {n} עסקאות ב-{len(days)} ימים לא נמצא דפוס שחוזר על עצמו מספיק כדי להצדיק שינוי.",
            "change": None,
        })
    return {"status": status, "items": items}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def run(db_file: str, trades: list[dict], force: bool = False) -> dict:
    """Review every closed trade that has not been reviewed yet."""
    if not trades:
        return {"reviewed": 0, "skipped": 0, "reason": "אין עסקאות סגורות"}

    done = set() if force else reviewed_ids(db_file)
    pending = [t for t in trades if t["id"] not in done]

    # Research runs ONLY on trades whose session is fully behind us — a trade
    # closed earlier today is deferred to the nightly run instead of producing
    # a half-baked "partial" review on incomplete bars.
    deferred = 0
    eligible = []
    for t in pending:
        d = _to_et(t.get("exit_time"))
        if d and not session_complete(d.date()):
            deferred += 1
        else:
            eligible.append(t)
    pending = eligible
    if deferred:
        logger.info("Post-mortem: deferring %d trade(s) closed today until the session completes", deferred)
    if not pending:
        reason = (f"{deferred} עסקאות של היום ייחקרו אחרי סגירת השוק (23:20)"
                  if deferred else "כל העסקאות כבר נחקרו")
        return {"reviewed": 0, "skipped": 0, "deferred": deferred, "reason": reason}

    days = [_to_et(t.get("exit_time")) for t in pending]
    days = [d.date() for d in days if d]
    if not days:
        return {"reviewed": 0, "skipped": len(pending), "reason": "אין חותמות זמן תקינות"}

    # The free plan refuses SIP data from the last ~15 minutes, and it refuses
    # the WHOLE monthly chunk containing it — so asking for "today + 1" fails
    # the entire download. Clamp the window behind the embargo. Anything still
    # missing shows up as data_ok=0 rather than as a silent hole.
    start = (min(days) - pd.Timedelta(days=3)).isoformat()
    embargo = pd.Timestamp.now(tz=ET) - pd.Timedelta(minutes=16)
    end = min(pd.Timestamp(max(days), tz=ET) + pd.Timedelta(days=1), embargo)
    source = _BarSource(start, end.isoformat())

    reviewed = skipped = 0
    for t in pending:
        exit_at = _to_et(t.get("exit_time"))
        if not exit_at:
            skipped += 1
            continue
        try:
            bars = source.day(t["symbol"], exit_at.date())
            review = review_trade(t, bars, _atr_from_trade(t))
        except Exception as e:
            logger.warning("Review failed for trade %s (%s): %s", t["id"], t["symbol"], e)
            review = None
        if review:
            _save(db_file, review)
            reviewed += 1
        else:
            skipped += 1

    logger.info("Post-mortem: reviewed %d trades (skipped %d)", reviewed, skipped)
    return {"reviewed": reviewed, "skipped": skipped}
